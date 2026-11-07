"""Control HTTP and collection use only fake tools, never the live host."""
from contextlib import contextmanager,closing
import http.client
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import pytest
from hifimate.ctrl import App,ControlError,Server,Snapshots
from hifimate.ctrl_auth import Auth
from hifimate.ctrl_state import Collector,UNITS,MA_SERVICES,compose,output_rows,journal_rows

ROOT=Path(__file__).parents[1]
PASSWORD='test-password'


def wait_for(predicate):
    deadline=time.monotonic()+3
    while not predicate():
        assert time.monotonic()<deadline
        time.sleep(.01)


class FakeCollector:
    def __init__(self):
        self.lock=threading.RLock(); self.at=0; self.slow={}; self.events=[]; self.calls=0
        self.lanes=[dict(source='airplay',has_data=False,can_switch=False)]; self.pair_active=False
    def snapshot(self): self.calls+=1; return {'lanes':self.lanes,'events':list(self.events)}
    def add_event(self,message,**kwargs): self.events.append({'message':message,**kwargs})
    def format_path(self): return '/fake/format.sock'
    def pairing(self): return {'active':self.pair_active,'until':0}
    def logs(self,unit='',errors=False,lines=80): return [{'message':'fake journal'}]


@pytest.fixture
def app(tmp_path):
    calls=[]
    def runner(argv,timeout):
        assert timeout>0; calls.append(argv)
        if argv[0]=='systemd-run': instance.collector.pair_active=True
        return 'hw:CARD=USB,DEV=0 | usb | Test DAC | 44100,192000\n'
    def request(path,event,timeout=3): calls.append(event); return {'ok':True}
    instance=App(tmp_path,FakeCollector(),runner,request,lambda prefix:calls.append('pair-stop'))
    instance.calls=calls
    return instance


@contextmanager
def serving(app):
    with Server(('127.0.0.1',0),app,ROOT/'gui') as server:
        worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01})
        worker.start()
        try: yield server.server_address
        finally: server.shutdown(); worker.join()


def request(address,path='state',data=None,headers=None):
    with closing(http.client.HTTPConnection(*address,timeout=4)) as conn:
        body=None if data is None else json.dumps(data)
        default={'Host':'hifi.test','Origin':'http://hifi.test','Content-Type':'application/json'}
        conn.request('GET' if data is None else 'POST','/ctrl/api/'+path,body,{**default,**(headers or {})})
        response=conn.getresponse(); body=response.read()
        return response.status,dict(response.getheaders()),json.loads(body)


def linux_password(app,password=PASSWORD):
    """The Linux password the (fake) PAM accepts for this app."""
    app.auth.verify=lambda given: given==password
    (app.prefix/'config').mkdir(parents=True,exist_ok=True)     # as in an installed prefix


def login(address,app):
    linux_password(app)
    status,headers,_=request(address,'login',{'password':PASSWORD})
    assert status==200
    return headers['Set-Cookie'].split(';')[0]


def test_no_linux_password_readable_but_all_actions_forbidden(app):
    # PAM works but the user has no password yet (a locked account): nothing logs in.
    with serving(app) as address:
        assert request(address)[2]['auth']=={'configured':True,'logged_in':False}
        assert request(address,'login',{'password':PASSWORD})[0]==401
        for name in ('service','switch-input','switch-output','pair','pair-stop'):
            status,_,data=request(address,name,{})
            assert status==403 and '登录' in data['error']
    assert not app.calls


def test_without_pam_nothing_can_log_in(app):
    app.auth.check_available=lambda: False
    linux_password(app)
    with serving(app) as address:
        assert request(address)[2]['auth']=={'configured':False,'logged_in':False}
        status,_,data=request(address,'login',{'password':PASSWORD})
        assert status==403 and 'PAM' in data['error']
        assert request(address,'pair',{})[0]==403
    assert not app.calls


def test_auth_password_cookie_logout_and_origin(app):
    with serving(app) as address:
        linux_password(app)
        assert request(address,'login',{'password':'wrong'})[0]==401
        status,headers,_=request(address,'login',{'password':PASSWORD})
        assert status==200
        cookie=headers['Set-Cookie']
        for flag in ('HttpOnly','SameSite=Strict','Path=/ctrl/','Max-Age=2592000'): assert flag in cookie
        auth={'Cookie':cookie.split(';')[0]}
        assert request(address,headers=auth)[2]['auth']['logged_in']
        for origin in ('http://evil.test','','https://hifi.test','http://hifi.test/path'):
            assert request(address,'pair',{},headers={**auth,'Origin':origin})[0]==403
        assert request(address,'pair',{},headers={**auth,'Content-Type':'text/plain'})[0]==415
        assert request(address,'logout',{},headers=auth)[0]==200
        assert request(address,'pair',{},headers=auth)[0]==403
    assert not app.calls


def test_login_rate_limit_and_restart(tmp_path):
    now=[0]; checked=[]
    auth=Auth(verify=lambda given: checked.append(given) or given==PASSWORD,clock=lambda:now[0])
    for _ in range(5): assert auth.login('bad','peer')[0]==401
    assert auth.login(PASSWORD,'peer')[0]==429 and len(checked)==5      # refused before PAM is asked
    now[0]=61; status,token,_=auth.login(PASSWORD,'peer'); assert status==200 and auth.valid(token)
    # sessions live in memory only: a restart (what `hifimate password` does) logs everyone out
    assert not Auth(verify=lambda given: True,clock=lambda:now[0]).valid(token)
    for bad in ('', 'x'*1025, 'a\0b', None, 42): assert auth.login(bad,'p2')[0] in (401,429)
    assert all(isinstance(c,str) and c and '\0' not in c for c in checked)


@pytest.mark.parametrize('unit,operation,status',[('bad','restart',400),('hifimate-ctrl','stop',403),
                         ('pipewire','stop',403),('wireplumber','stop',403),('hifimate.service;id','restart',400)])
def test_service_allowlist(app,unit,operation,status):
    with serving(app) as address:
        cookie=login(address,app)
        assert request(address,'service',{'unit':unit,'operation':operation},{'Cookie':cookie})[0]==status
    assert not app.calls


def test_service_group_and_no_individual_containers(app):
    directory=app.prefix/'music-assistant'; directory.mkdir(); (directory/'.hifimate-managed').touch(); (directory/'.env').touch()
    with serving(app) as address:
        cookie=login(address,app)
        assert request(address,'service',{'unit':'server','operation':'restart'},{'Cookie':cookie})[0]==400
        assert request(address,'service',{'unit':'music-assistant','operation':'restart'},{'Cookie':cookie})[0]==202
        wait_for(lambda:app.calls)
    assert app.calls[0]==compose(app.prefix)+['restart',*MA_SERVICES]
    assert not app.action_lock.locked()


def test_switch_input_requires_data_and_async_result(app):
    with pytest.raises(ControlError): app.action('switch-input',{'source':'airplay'})
    assert not app.calls
    app.collector.lanes[0].update(has_data=True,can_switch=True)
    assert app.action('switch-input',{'source':'airplay'})['accepted']
    wait_for(lambda:not app.action_lock.locked())
    assert app.calls==[{'action':'switch','source':'airplay'}]
    assert '完成' in app.collector.events[-1]['message']


def test_output_validated_no_argv_injection_and_pair_detached(app):
    with pytest.raises(ControlError): app.action('switch-output',{'device':'hw:CARD=USB,DEV=0;id'})
    app.calls.clear()
    app.action('pair',{}); wait_for(lambda:not app.action_lock.locked())
    command=app.calls[0]
    assert command[:3]==['systemd-run','--user','--collect']
    assert command[-3:]==['pair','--seconds','120']
    app.action('pair-stop',{}); wait_for(lambda:not app.action_lock.locked())
    assert app.calls[-1]=='pair-stop'


def test_sse_snapshot_and_shared_demand_poller(app):
    with serving(app) as address:
        with closing(http.client.HTTPConnection(*address,timeout=4)) as conn:
            conn.request('GET','/ctrl/api/events')
            response=conn.getresponse(); assert response.status==200
            assert response.getheader('X-Accel-Buffering')=='no'
            line=response.readline(); assert line.startswith(b'data: ')
            assert json.loads(line[6:])['lanes'][0]['source']=='airplay'
            response.close()
        wait_for(lambda:not app.snapshots.clients)
    shared=Snapshots(app.collector)
    first=shared.subscribe(); second=shared.subscribe()
    assert first.get(timeout=3)['lanes']
    assert second.get(timeout=3)['lanes']
    shared.unsubscribe(first); assert shared.stop is not None
    shared.unsubscribe(second); assert shared.stop is None


def test_logs_bounded_and_allowlisted(tmp_path):
    calls=[]
    def runner(argv,timeout):
        calls.append(argv)
        return '\n'.join(json.dumps({'MESSAGE':'line','PRIORITY':'3','__REALTIME_TIMESTAMP':str(i)}) for i in range(300))
    c=Collector(tmp_path,runner)
    rows=c.logs('hifimate',True,10000)
    assert len(rows)==200 and calls[-1][calls[-1].index('-n')+1]=='200'
    assert calls[-1][-2:]==['-p','0..3']
    with pytest.raises(ValueError): c.logs('--user;echo')
    assert output_rows('bad | builtin | HDMI | ambiguous\nhw:1 | usb | DAC | 44100,192000')[0]['rates']==[44100,192000]
    assert journal_rows('not json\n'+json.dumps({'MESSAGE':[1,2]}))==[]


def test_cached_collector_down_component_and_native_bluetooth(tmp_path):
    (tmp_path/'config').mkdir(); (tmp_path/'config/hifimate.conf').write_text(json.dumps({'alsa_device':'hw:USB'}))
    calls=[]; now=[100]
    def runner(argv,timeout):
        calls.append(argv)
        if argv[0]=='systemctl':
            return '\n\n'.join(f'Id={unit}.service\nActiveState=active\nSubState=running\nMainPID=0\nNRestarts=2\nCPUUsageNSec=0\nActiveEnterTimestampMonotonic=1000000' for unit in UNITS)
        if argv[-1]=='sinks': return '[{"name":"airplay_in","index":1}]'
        if argv[-1]=='sink-inputs': return '[{"sink":1,"corked":false,"sample_specification":"s16le 2ch 44100Hz"}]'
        assert argv[0]!='pw-dump'  # ~50 ms CPU per call; pactl sources suffice
        if argv[-1]=='sources': return '[{"state":"RUNNING","sample_specification":"s24le 2ch 48000Hz","properties":{"media.class":"Audio/Source","node.name":"bluez_input.aa"}},{"state":"SUSPENDED","sample_specification":"s32le 2ch 48000Hz","properties":{"media.class":"Audio/Source","node.name":"alsa_input.pci"}}]'
        if argv[-1]=='--list': return 'hw:USB | usb | DAC | 44100,192000'
        if argv[0]=='journalctl': return ''
        raise AssertionError(argv)
    def failed(url): raise OSError('DSP unavailable')
    c=Collector(tmp_path,runner,lambda *a:{'ok':True,'ready':False,'switchable':['airplay']},failed,clock=lambda:now[0])
    state=c.snapshot(); count=len(calls)
    assert state['errors']['dsp']=='OSError' and state['dsp']=={}
    assert state['outputs'][0]['selected']
    assert next(row for row in state['lanes'] if row['source']=='airplay')['can_switch']
    assert next(row for row in state['lanes'] if row['source']=='bluetooth')['rate']==48000
    assert state['services'][0]['restarts']==2
    assert c.snapshot() is state and len(calls)==count
    now[0]+=2; c.snapshot(); assert len(calls)>count
    # Unit rows are cached for 3 s while snapshots run every second: the
    # Music Assistant row must still appear exactly once.
    for _ in range(4):
        now[0]+=1
        assert [row['id'] for row in c.snapshot()['services']].count('music-assistant')==1


def test_static_assets_no_traversal(app):
    with serving(app) as address:
        for path in ('/ctrl/','/ctrl/ctrl.js','/ctrl/ctrl.css'):
            with closing(http.client.HTTPConnection(*address)) as conn:
                conn.request('GET',path); response=conn.getresponse()
                assert response.status==200 and response.read()
        with closing(http.client.HTTPConnection(*address)) as conn:
            conn.request('GET','/ctrl/../config/ma-token'); response=conn.getresponse()
            assert response.status==404; response.read()


def test_ctrl_node_helpers():
    result=subprocess.run(['node',str(ROOT/'tests/ctrl_browser.mjs')],text=True,capture_output=True)
    assert result.returncode==0,result.stderr


def test_secure_cookie_http_rate_limit_and_unknown_action(app):
    with serving(app) as address:
        linux_password(app)
        headers={'Origin':'https://hifi.test','X-Forwarded-Proto':'https'}
        status,response,_=request(address,'login',{'password':PASSWORD},headers)
        assert status==200 and '; Secure' in response['Set-Cookie']
        cookie=response['Set-Cookie'].split(';')[0]
        assert request(address,'unknown',{}, {'Cookie':cookie})[0]==404
        for _ in range(4): assert request(address,'login',{'password':'bad'})[0]==401
        assert request(address,'login',{'password':'bad'})[0]==429
    assert not app.calls


def test_mutation_failure_is_reported_and_lock_released(app):
    def fail(argv,timeout): raise subprocess.TimeoutExpired(argv,timeout)
    app.run=fail
    assert app.action('service',{'unit':'hifimate','operation':'restart'})['accepted']
    with pytest.raises(ControlError) as error: app.action('pair',{})
    assert error.value.status==409
    wait_for(lambda:not app.action_lock.locked())
    assert app.collector.events[-1]['priority']==3 and '失败' in app.collector.events[-1]['message']


def test_global_login_budget_and_expiry(tmp_path):
    now=[0]; auth=Auth(verify=lambda given: given==PASSWORD,clock=lambda:now[0])
    for n in range(20): assert auth.login('bad',str(n))[0]==401
    assert auth.login(PASSWORD,'new-peer')[0]==429
    now[0]=61; status,token,_=auth.login(PASSWORD,'peer'); assert status==200
    now[0]+=30*86400+1; assert not auth.valid(token)


def test_all_components_down_still_produces_complete_state(tmp_path):
    (tmp_path/'config').mkdir(); (tmp_path/'config/hifimate.conf').write_text('{"alsa_device":"hw:missing"}')
    def fail(*args,**kwargs): raise OSError('down')
    c=Collector(tmp_path,fail,fail,fail)
    snapshot=c.snapshot()
    assert len(snapshot['services'])==len(UNITS)+1
    assert all(row['state']=='unavailable' for row in snapshot['services'])
    assert all(not row['available'] and not row['can_switch'] for row in snapshot['lanes'])
    assert {'controller','services','dsp','inputs','sinks','nodes'}<=snapshot['errors'].keys()


def test_ma_single_row_covers_every_container_and_cpu(tmp_path):
    directory=tmp_path/'music-assistant'; directory.mkdir(); (directory/'.hifimate-managed').touch(); (directory/'.env').touch()
    calls=[]; n=len(MA_SERVICES)
    def runner(argv,timeout):
        calls.append(argv)
        if 'ps' in argv: return json.dumps([{'ID':str(i),'Service':name} for i,name in enumerate(MA_SERVICES)])
        if argv[:2]==['docker','inspect']:
            return json.dumps([{'Id':f'c{i}','State':{'Running':True,'Health':{'Status':'healthy'},'StartedAt':'2026-01-01T00:00:00Z'},'RestartCount':i} for i in range(n)])
        raise AssertionError(argv)  # never the slow `docker stats`
    cgroups=tmp_path/'cgroup'; now=[100.0]
    def usage(total):
        for i in range(n):
            scope=cgroups/f'system.slice/docker-c{i}.scope'; scope.mkdir(parents=True,exist_ok=True)
            (scope/'cpu.stat').write_text(f'usage_usec {total*(i+1)//10}\nuser_usec 0\n')
    collector=Collector(tmp_path,runner,clock=lambda:now[0],cgroups=cgroups)
    usage(0); row=collector.ma()
    assert row['state']=='active' and row['detail']==f'{n} / {n} 容器'
    assert row['restarts']==sum(range(n)) and row['cpu'] is None
    # 33 ms of CPU over 1 s across the four containers = 3.3 %.
    usage(33000); now[0]=101.0
    assert collector.ma()['cpu']==round(sum(33000*(i+1)//10 for i in range(n))/1e4,2)
    assert calls[0]==compose(tmp_path)+['ps','--all','--format','json']


def test_no_optional_python_dependencies_and_nginx_routes():
    import sys
    result=subprocess.run([sys.executable,'-B','-S','-c','import hifimate.ctrl'],cwd=ROOT,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    nginx=(ROOT/'music-assistant/nginx.conf').read_text()
    assert nginx.count('return 301 "/ctrl/#spectrum";')==2
    assert 'location = /ctrl { return 301 /ctrl/; }' in nginx
    assert 'proxy_pass http://127.0.0.1:5009;' in nginx
    assert 'proxy_buffering off;' in nginx and 'proxy_read_timeout 3600s;' in nginx
    assert 'spectrum.html' not in nginx


def test_stdlib_dsp_client_only_sends_read_commands(monkeypatch):
    import base64,hashlib
    from hifimate.ctrl_state import dsp_status
    class Socket:
        def __init__(self): self.buffer=b''; self.commands=[]
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def settimeout(self,value): assert 0<value<=1.5
        def recv(self,size):
            data=self.buffer[:size]; self.buffer=self.buffer[size:]; return data
        def sendall(self,data):
            if data[:1]==b'\x88': return
            if data.startswith(b'GET'):
                key=data.split(b'Sec-WebSocket-Key: ')[1].split(b'\r\n')[0]
                accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                self.buffer=b'HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n'
            else:
                assert data[1]&128
                mask=data[2:6]; payload=bytes(c^mask[i%4] for i,c in enumerate(data[6:]))
                command=json.loads(payload); self.commands.append(command)
                values={'GetState':'Running','GetVolume':-12,'GetMute':False,'GetProcessingLoad':1.3,'GetClippedSamples':0}
                reply=json.dumps({command:{'result':'Ok','value':values[command]}}).encode()
                self.buffer+=bytes([129,len(reply)])+reply
    sock=Socket()
    monkeypatch.setattr('hifimate.ctrl_state.socket.create_connection',lambda *a,**k:sock)
    result=dsp_status('ws://127.0.0.1:1234')
    assert result=={'state':'Running','volume':-12,'mute':False,'load':1.3,'clipped':0}
    assert len(sock.commands)==5 and all(command.startswith('Get') for command in sock.commands)
    with pytest.raises(ValueError): dsp_status('ws://external.invalid:1234')


def test_realtime_label_reads_every_thread(tmp_path):
    from hifimate.ctrl_state import realtime_label
    def task(pid,tid,policy,prio,comm='data-loop.0'):
        path=tmp_path/str(pid)/'task'/str(tid); path.mkdir(parents=True)
        # Fields 3..41; comm may contain spaces and ')'.
        rest=['S']+['0']*36+[str(prio),str(policy)]
        (path/'stat').write_text(f'{tid} ({comm}) '+' '.join(rest)+' 0 0\n')
    task(10,10,2,5,'mpd'); task(10,11,2,5); task(10,12,0,0)
    assert realtime_label(10,tmp_path)=='RR 5'
    # PipeWire: only the data loop is realtime (SCHED_FIFO via module-rt).
    task(20,20,0,0,'pipewire'); task(20,21,1,5); task(20,22,0,0,'module-rt')
    assert realtime_label(20,tmp_path)=='FIFO 5 · 1 线程'
    task(30,30,0,0,'python3 (x)'); task(30,31,0,0)
    assert realtime_label(30,tmp_path) is None
    assert realtime_label(99,tmp_path) is None


def test_logs_are_only_this_users_own(tmp_path):
    # a user with read access to other journals must still show only its own entries
    seen=[]
    def runner(argv,timeout):
        seen.append(argv); return ''
    Collector(tmp_path,runner).logs()
    argv=seen[-1]
    assert argv[:2]==['journalctl','--user'] and f'_UID={os.getuid()}' in argv
