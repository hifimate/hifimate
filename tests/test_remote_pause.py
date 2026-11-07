import asyncio
from types import SimpleNamespace
import pytest
from hifimate.remote_pause import Sessions,RemotePause,dacp_endpoint,dacp_state,dacp_request,bluez_pause

DEVICE='/org/bluez/hci7/dev_AA_BB_CC_DD_EE_FF'

def live(source='dlna',**extra):
    registry=Sessions()
    event=dict(source=source,event='play',event_id='start',session='session',device=DEVICE if source=='bluetooth' else None,**extra)
    registry.observe(event)
    return registry,event

@pytest.mark.parametrize('source,port',[('dlna',6600),('music_assistant_dlna',6601)])
def test_mpd_pause_and_actual_state_confirmation(source,port):
    registry,event=live(source); calls=[]
    class Client:
        def __init__(self,p): assert p==port
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def command(self,command):
            calls.append(command)
            return {'state':'pause' if 'pause 1' in calls else 'play'}
    result=RemotePause(registry,mpd=Client)(event)
    assert calls==['status','pause 1','status'] and result['status']=='confirmed' and result['requested']

@pytest.mark.parametrize('failure,expected',[('pause 1','error'),('readback','accepted')])
def test_mpd_ack_error_vs_accepted_without_readback(failure,expected):
    registry,event=live(); calls=[]
    class Client:
        def __init__(self,_): pass
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def command(self,command):
            calls.append(command)
            if command==failure or failure=='readback' and len(calls)==3: raise RuntimeError('MPD unavailable')
            return {'state':'play'}
    result=RemotePause(registry,mpd=Client)(event)
    assert result['status']==expected and result['requested']


def test_session_fencing_and_stop_never_reauthorize_old_play():
    registry,event=live()
    registry.observe({**event,'event':'stop'})
    registry.observe(event)  # late duplicate after stop
    assert not registry.current(event) and not registry.authorized(event)
    result=RemotePause(registry,mpd=lambda _:pytest.fail('stale pause must not connect'))(event)
    assert result['status']=='stale' and not result['requested']
    resumed={**event,'session':'fresh'}; registry.observe(resumed); registry.observe(event)
    assert registry.authorized(resumed) and not registry.current(event)
    other={**event,'source':'airplay','session':'air'}; registry.observe(other)
    assert registry.current(resumed) and not registry.authorized(resumed)  # still pausable


def test_dacp_only_uses_matching_id_and_peer_address():
    meta={'daid':'AB12','acre':'1234567','clip':'192.0.2.11'}
    records='\n'.join(['=;eth0;IPv4;iTunes_Ctrl_AB12;_dacp._tcp;local;x.local;192.0.2.12;3689;',
                        '=;eth0;IPv4;iTunes_Ctrl_AB12;_dacp._tcp;local;x.local;192.0.2.11;4567;'])
    calls=[]
    def run(args,**kwargs):
        calls.append((args,kwargs))
        # --parsable alone retains Avahi's service-type database translation.
        output=records if '--no-db-lookup' in args else records.replace('_dacp._tcp','iTunes Remote Control')
        return SimpleNamespace(stdout=output)
    assert dacp_endpoint(meta,run)==('192.0.2.11',4567)
    assert calls[0][1]['timeout']==2
    assert dacp_endpoint({**meta,'acre':'bad\r\nHeader'},run) is None
    assert len(calls)==1
    assert dacp_endpoint({**meta,'daid':'FF'},run) is None
    assert dacp_endpoint({**meta,'clip':'example.org'},run) is None


def test_dacp_ipv6_link_local_has_resolved_interface_scope():
    output='=;wlan3;IPv6;iTunes_Ctrl_AB12;_dacp._tcp;local;x.local;fe80::1234;4567;'
    assert dacp_endpoint({'daid':'AB12','acre':'12','clip':'fe80::1234'},lambda *a,**k:SimpleNamespace(stdout=output))==('fe80::1234%wlan3',4567)


def test_dacp_http_sends_pause_and_active_remote_not_playpause(monkeypatch):
    calls=[]
    class Connection:
        def __init__(self,*a,**k): calls.append((a,k))
        def request(self,*a,**k): calls.append((a,k))
        def getresponse(self): return SimpleNamespace(status=204,read=lambda size:b'')
        def close(self): calls.append('closed')
    monkeypatch.setattr('http.client.HTTPConnection',Connection)
    dacp_request(('192.0.2.1',12345),'987','pause')
    assert calls[1]==(('GET','/ctrl-int/1/pause'),{'headers':{'Active-Remote':'987'}})
    assert calls[-1]=='closed'


def dmap(state):
    payload=b'caps'+(1).to_bytes(4,'big')+bytes([state])
    return b'cmst'+len(payload).to_bytes(4,'big')+payload

@pytest.mark.parametrize('state,expected',[(3,'confirmed'),(2,'confirmed'),(4,'accepted')])
def test_dacp_pause_readback(state,expected):
    registry,event=live('airplay',remote={'daid':'12','acre':'345','clip':'192.0.2.1'}); paths=[]
    def request(endpoint,token,path):
        assert endpoint==('192.0.2.1',5555) and token=='345'
        paths.append(path); return dmap(state)
    result=RemotePause(registry,resolve=lambda _:('192.0.2.1',5555),http=request)(event)
    assert paths==['pause','playstatusupdate?revision-number=0'] and result['status']==expected
    assert result['requested']


def test_dacp_missing_credentials_and_status_timeout_are_honest():
    registry,event=live('airplay',remote={})
    result=RemotePause(registry,resolve=lambda _:None)(event)
    assert result['status']=='unsupported' and not result['requested']
    event['remote']={'acre':'123'}
    def request(endpoint,token,path):
        if path!='pause': raise TimeoutError('no status response')
    result=RemotePause(registry,resolve=lambda _:('192.0.2.1',12345),http=request)(event)
    assert result['status']=='accepted' and result['requested']
    with pytest.raises(ValueError): dacp_state(b'caps\x00\x00\xff\xffx')


def test_bluez_uses_actual_player_on_dynamic_adapter_and_confirms(monkeypatch):
    import sys
    MessageType=SimpleNamespace(METHOD_RETURN='return',ERROR='error')
    calls=[]; paused=[False]
    class Bus:
        async def connect(self): return self
        async def call(self,msg):
            calls.append(msg)
            if msg.member=='Pause': paused[0]=True; body=[]
            else: body=[SimpleNamespace(value=DEVICE if msg.body[-1]=='Device' else 'paused' if paused[0] else 'playing')]
            return SimpleNamespace(message_type=MessageType.METHOD_RETURN,body=body)
        def disconnect(self): calls.append('disconnect')
        async def wait_for_disconnect(self): pass
    monkeypatch.setitem(sys.modules,'dbus_next',SimpleNamespace(MessageType=MessageType,
        BusType=SimpleNamespace(SYSTEM='system'),Message=lambda **kwargs:SimpleNamespace(**kwargs)))
    monkeypatch.setitem(sys.modules,'dbus_next.aio',SimpleNamespace(MessageBus=lambda **_:Bus()))
    result=asyncio.run(bluez_pause({'player':DEVICE+'/player2'},DEVICE))
    assert result['status']=='confirmed' and result['requested']
    assert len([msg for msg in calls if msg!='disconnect' and msg.member=='Pause' and msg.path==DEVICE+'/player2'])==1
    assert calls[-1]=='disconnect'


def test_bluez_missing_player_unsupported_and_error_not_confirmed():
    registry,event=live('bluetooth')
    result=RemotePause(registry)(event)
    assert result['status']=='unsupported' and not result['requested']
    async def fail(*_): raise RuntimeError('org.bluez.Error.NotSupported')
    event['remote']={'player':DEVICE+'/player0'}
    result=RemotePause(registry,bluez=fail)(event)
    assert result['status']=='error' and 'NotSupported' in result['detail']
