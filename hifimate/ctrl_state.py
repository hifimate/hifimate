"""Bounded read-only snapshots for HiFiMate Control (standard library only)."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import struct
import subprocess
import threading
import time
from urllib.parse import urlsplit
from .lane_state import lane_states

GROUPS=(('音频核心',('hifimate','hifimate-camilladsp','pipewire','wireplumber')),
        ('接收端',('hifimate-mpd','hifimate-dlna','hifimate-music-mpd','hifimate-airplay','hifimate-airplay-net','hifimate-bluetooth')),
        ('界面与 Music Assistant',('hifimate-ctrl','hifimate-spectrum','music-assistant')))
UNITS=tuple(unit for _,units in GROUPS for unit in units if unit!='music-assistant')
RESTART_ONLY={'hifimate-ctrl','pipewire','wireplumber'}
MA_SERVICES=('server','web','provider-subscriber','ncm-api','migu-api')


def run(argv,timeout=2):
    result=subprocess.run(argv,check=False,capture_output=True,text=True,timeout=timeout)
    if len(result.stdout)>4*1024*1024: raise ValueError('组件响应过大')
    # A missing unit makes bulk show nonzero, but other unit records are useful.
    if result.returncode and not (argv[:3]==['systemctl','--user','show'] and result.stdout):
        raise subprocess.CalledProcessError(result.returncode,argv)
    return result.stdout


def compose(prefix):
    directory=Path(prefix)/'music-assistant'
    if not (directory/'.hifimate-managed').is_file() or not (directory/'.env').is_file():
        raise ValueError('Music Assistant 尚未安装')
    return ['docker','compose','--project-directory',str(directory),'--env-file',str(directory/'.env'),
            '-f',str(directory/'compose.yml')]


def ipc(path,event,timeout=3):
    with socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET) as client:
        client.settimeout(timeout); client.connect(str(path))
        client.sendall(json.dumps(event).encode())
        data,_,flags,_=client.recvmsg(65536)
        if flags & socket.MSG_TRUNC: raise ValueError('仲裁响应过大')
        result=json.loads(data)
        if not isinstance(result,dict): raise ValueError('无效仲裁响应')
        return result


def dsp_status(url):
    """Small read-only RFC6455 client; no optional websocket package is needed."""
    target=urlsplit(url)
    if target.scheme!='ws' or target.hostname not in ('127.0.0.1','localhost','::1'):
        raise ValueError('DSP 地址须为本机 ws')
    deadline=time.monotonic()+1.5
    with socket.create_connection((target.hostname,target.port or 80),timeout=1) as client:
        def read(size):
            data=b''
            while len(data)<size:
                client.settimeout(max(.01,deadline-time.monotonic()))
                block=client.recv(size-len(data))
                if not block: raise OSError('DSP 连接已关闭')
                data+=block
            return data
        key=base64.b64encode(os.urandom(16)).decode()
        client.sendall((f'GET {target.path or "/"} HTTP/1.1\r\nHost: {target.netloc}\r\n'
                        f'Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n'
                        'Sec-WebSocket-Version: 13\r\n\r\n').encode())
        header=b''
        while not header.endswith(b'\r\n\r\n'):
            if len(header)>8192: raise ValueError('DSP 握手过大')
            header+=read(1)
        accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        headers=dict(line.split(b':',1) for line in header.split(b'\r\n')[1:] if b':' in line)
        headers={key.strip().lower():value.strip() for key,value in headers.items()}
        if not header.startswith(b'HTTP/1.1 101 ') or headers.get(b'sec-websocket-accept')!=accept:
            raise ValueError('DSP 握手失败')
        def send(payload,opcode=1):
            mask=os.urandom(4)
            client.sendall(bytes([0x80|opcode,0x80|len(payload)])+mask+
                           bytes(value^mask[i%4] for i,value in enumerate(payload)))
        result={}
        for command,label in (('GetState','state'),('GetVolume','volume'),('GetMute','mute'),
                              ('GetProcessingLoad','load'),('GetClippedSamples','clipped')):
            send(json.dumps(command).encode())
            for _ in range(8):
                first,second=read(2); size=second&127
                if size==126: size=struct.unpack('!H',read(2))[0]
                if size==127: size=struct.unpack('!Q',read(8))[0]
                if size>65536 or not first&128 or second&128: raise ValueError('无效 DSP 帧')
                payload=read(size); opcode=first&15
                if opcode==9: send(payload,10); continue
                if opcode!=1: raise ValueError('无效 DSP 消息')
                message=json.loads(payload).get(command,{})
                if message.get('result')!='Ok': raise ValueError('DSP 状态查询失败')
                result[label]=message.get('value'); break
            else: raise ValueError('DSP 消息过多')
        send(b'',8)
        return result


def output_rows(text):
    rows=[]
    for line in text.splitlines():
        fields=[part.strip() for part in line.split('|')]
        if len(fields)!=4 or not re.fullmatch(r'\d+(,\d+)*',fields[3]): continue
        rows.append(dict(device=fields[0],kind=fields[1],name=fields[2],
                         rates=[int(rate) for rate in fields[3].split(',')]))
    return rows


POLICIES={1:'FIFO',2:'RR'}


def realtime_label(pid,proc=Path('/proc')):
    """'RR 5' for a realtime process; 'FIFO 5 · 1 线程' when only some threads are.

    PipeWire and WirePlumber run just their data loop realtime, so the main
    thread's policy alone would report them as ordinary."""
    threads=[]
    try: tasks=sorted((proc/str(pid)/'task').iterdir(),key=lambda p:int(p.name))
    except (OSError,ValueError): return None
    for task in tasks:
        try:
            fields=(task/'stat').read_text().rsplit(')',1)[1].split()
            # stat fields 40/41 (rt_priority, policy); fields[0] is field 3.
            threads.append((int(task.name),POLICIES.get(int(fields[38])),int(fields[37])))
        except (OSError,ValueError,IndexError): continue
    realtime=[t for t in threads if t[1]]
    if not realtime: return None
    top=max(realtime,key=lambda t:t[2])
    if any(t[0]==pid for t in realtime) and len(realtime)>=len(threads)/2:
        return f'{top[1]} {top[2]}'
    return f'{top[1]} {top[2]} · {len(realtime)} 线程'


def source_nodes(text):
    """Pulse sources as minimal pw-dump nodes for lane_states.

    Only native BlueZ sources are read from them; pw-dump costs ~50 ms CPU
    and a large JSON parse per second, this ~10 ms."""
    return [{'info':{'state':'running' if row.get('state')=='RUNNING' else str(row.get('state','')).lower(),
                     'props':row.get('properties',{})},'sample_specification':row.get('sample_specification','')}
            for row in json.loads(text)]


def journal_rows(text):
    rows=[]
    for line in text.splitlines():
        try:
            item=json.loads(line)
            message=item.get('MESSAGE','')
            if not isinstance(message,str): continue
            rows.append({'id':str(item.get('__CURSOR',item.get('__REALTIME_TIMESTAMP',''))),
                         'time':int(item.get('__REALTIME_TIMESTAMP',0))/1000000,
                         # systemd's own lines about a unit come from init.scope
                         # and carry the unit in USER_UNIT.
                         'unit':item.get('USER_UNIT',item.get('_SYSTEMD_USER_UNIT','')),
                         'priority':int(item.get('PRIORITY',6)), 'message':message[:4096]})
        except (ValueError,TypeError,AttributeError): continue
    return rows


class Collector:
    def __init__(self,prefix,runner=run,request=ipc,dsp=dsp_status,clock=time.monotonic,cgroups=Path('/sys/fs/cgroup')):
        self.cgroups=Path(cgroups); self.proc=Path('/proc'); self.prefix=Path(prefix); self.run=runner; self.request=request; self.dsp=dsp; self.clock=clock
        self.lock=threading.RLock(); self.cached=None; self.at=0; self.slow={}; self.cpu={}
        self.events=deque(maxlen=120); self.seen=deque(maxlen=400); self.sequence=0
        self.last_services={}; self.last_owner=None; self.host_cpu=None
        from .nowplaying import NowPlaying
        from .ma_api import load_token
        self.nowplaying=NowPlaying(self.run,self.request,self.format_path,ma_token=lambda:load_token(self.prefix))

    def settings(self):
        try: return json.loads((self.prefix/'config/hifimate.conf').read_text())
        except (OSError,ValueError): return {}

    def format_path(self):
        runtime=os.environ.get('XDG_RUNTIME_DIR',f'/run/user/{os.geteuid()}')
        return self.settings().get('socket',runtime+'/hifimate/format.sock').replace('%(uid)',str(os.geteuid()))

    def add_event(self,message,kind='action',unit='hifimate-ctrl',priority=6,stamp=None):
        with self.lock:
            self.sequence+=1
            self.events.append({'id':'ctrl-'+str(self.sequence),'time':stamp or time.time(),
                                'unit':unit,'priority':priority,'message':str(message)[:4096],'kind':kind})

    def logs(self,unit='',errors=False,lines=80):
        if unit and unit not in (*UNITS,'music-assistant'): raise ValueError('不允许的日志服务')
        lines=max(1,min(200,int(lines)))
        if unit=='music-assistant':
            if errors: raise ValueError('Compose 日志没有 journal 优先级')
            # Compose logs are not in the user journal. Never expose container env.
            text=self.run(compose(self.prefix)+['logs','--no-color','--tail',str(min(lines,50)),*MA_SERVICES],timeout=3)
            return [{'time':0,'unit':unit,'priority':6,'message':line[:4096]} for line in text.splitlines()[-lines:]]
        # This user's own entries only (its services and its systemd manager),
        # whatever journal files it may be able to read.
        args=['journalctl','--user','--no-pager','-o','json','-n',str(lines),f'_UID={os.getuid()}']
        for name in ((unit,) if unit else UNITS): args+=['-u',name+'.service']
        if not unit or unit=='hifimate-bluetooth': args+=['-u','hifimate-ctrl-pair.service']
        if errors: args+=['-p','0..3']
        return journal_rows(self.run(args,timeout=2))[-lines:]

    def remember(self,key,seconds,operation):
        now=self.clock(); old=self.slow.get(key)
        if old and now-old[0]<seconds: return old[1]
        value=operation(); self.slow[key]=(now,value); return value

    def services(self):
        props='Id,ActiveState,SubState,ActiveEnterTimestampMonotonic,NRestarts,CPUUsageNSec,MainPID'
        text=self.run(['systemctl','--user','show','--no-pager','--property='+props,
                       *(name+'.service' for name in UNITS)],timeout=2)
        records={}
        for block in text.strip().split('\n\n'):
            row=dict(line.split('=',1) for line in block.splitlines() if '=' in line)
            records[row.get('Id','').removesuffix('.service')]=row
        now=self.clock(); rows=[]
        for group,names in GROUPS:
            for name in names:
                if name=='music-assistant': continue
                row=records.get(name,{}); active=row.get('ActiveState','unavailable')
                pid=int(row.get('MainPID',0) or 0); cpu=None
                total=row.get('CPUUsageNSec','')
                if total.isdigit():
                    sample=(now,int(total),pid); old=self.cpu.get(name); self.cpu[name]=sample
                    if old and old[2]==pid and now>old[0] and sample[1]>=old[1]:
                        cpu=round((sample[1]-old[1])/(now-old[0])/1e7,2)
                scheduler=realtime_label(pid,self.proc) if pid else None
                since=int(row.get('ActiveEnterTimestampMonotonic',0) or 0)/1e6
                rows.append(dict(id=name,group=group,state=active,detail=row.get('SubState',''),
                                 uptime=max(0,now-since) if since and active=='active' else None,
                                 restarts=int(row.get('NRestarts',0) or 0),cpu=cpu,rt=scheduler,pid=pid,
                                 can_stop=name not in RESTART_ONLY))
        return rows

    def ma(self):
        base=compose(self.prefix)
        text=self.run(base+['ps','--all','--format','json'],timeout=2)
        try: items=json.loads(text)
        except ValueError: items=[json.loads(line) for line in text.splitlines() if line.strip()]
        if isinstance(items,dict): items=[items]
        items=[i for i in items if i.get('Service') in MA_SERVICES]
        ids=[i['ID'] for i in items if i.get('ID')]
        details=json.loads(self.run(['docker','inspect',*ids],timeout=2)) if ids else []
        active=len(details)==len(MA_SERVICES) and all(i.get('State',{}).get('Running') and
                 i['State'].get('Health',{}).get('Status','healthy')=='healthy' for i in details)
        uptimes=[]
        from datetime import datetime
        for i in details:
            try: uptimes.append(time.time()-datetime.fromisoformat(i['State']['StartedAt'].replace('Z','+00:00')).timestamp())
            except (ValueError,KeyError): pass
        # cgroup v2 counters instead of `docker stats` (which blocks ~1.5 s).
        usage=0
        try:
            for i in details:
                text=(self.cgroups/f'system.slice/docker-{i["Id"]}.scope/cpu.stat').read_text()
                usage+=int(text.split('usage_usec',1)[1].split()[0])
            sample=(self.clock(),usage); old=self.cpu.get('music-assistant'); self.cpu['music-assistant']=sample
            cpu=round((usage-old[1])/(sample[0]-old[0])/1e4,2) if old and sample[0]>old[0] and usage>=old[1] else None
        except (OSError,ValueError,KeyError,IndexError): cpu=None
        return dict(id='music-assistant',group=GROUPS[-1][0],state='active' if active else 'degraded',
                    detail=f'{len(details)} / {len(MA_SERVICES)} 容器',uptime=min(uptimes) if uptimes else None,
                    restarts=sum(i.get('RestartCount',0) for i in details),cpu=cpu,rt=None,can_stop=True)

    def pairing(self):
        from .bluetooth_pair import window_file,window_active
        path=window_file()
        if not window_active(path): return {'active':False,'until':0}
        # The marker includes five seconds of cleanup grace, not pairing time.
        try: return {'active':True,'until':float(json.loads(path.read_text())['until'])-5}
        except (OSError,ValueError,KeyError): return {'active':False,'until':0}

    def metrics(self):
        result={'host':socket.gethostname(),'kernel':platform.release(),'uptime':None,'cpu':None,'memory':None,'commit':None}
        try:
            result['uptime']=float(Path('/proc/uptime').read_text().split()[0])
            values=[int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:9]]
            total=sum(values); idle=values[3]+values[4]
            if self.host_cpu and total>self.host_cpu[0]:
                result['cpu']=round(100*(1-(idle-self.host_cpu[1])/(total-self.host_cpu[0])),1)
            self.host_cpu=(total,idle)
            mem=dict((line.split(':')[0],int(line.split()[1])) for line in Path('/proc/meminfo').read_text().splitlines())
            result['memory']={'used':(mem['MemTotal']-mem['MemAvailable'])*1024,'total':mem['MemTotal']*1024}
        except (OSError,ValueError,KeyError): pass
        try: result['commit']=(self.prefix/'share/hifimate/commit').read_text().strip()[:64]
        except OSError: pass
        return result

    def snapshot(self,force=False):
        with self.lock:
            now=self.clock()
            if not force and self.cached is not None and now-self.at<1: return self.cached
            errors={}; settings=self.settings()
            def safe(name,operation,fallback):
                try: return operation()
                except (OSError,ValueError,RuntimeError,subprocess.SubprocessError,KeyError,TypeError) as error:
                    # Do not publish command stderr: it may contain private config.
                    errors[name]=type(error).__name__; return fallback
            jobs={
                'controller':lambda:self.request(self.format_path(),{'action':'status'}),
                # Unit state changes rarely; an action clears this cache.
                'services':lambda:self.remember('services',3,self.services),
                'inputs':lambda:json.loads(self.run(['pactl','--format=json','list','sink-inputs'],timeout=1)),
                'sinks':lambda:json.loads(self.run(['pactl','--format=json','list','sinks'],timeout=1)),
                'nodes':lambda:source_nodes(self.run(['pactl','--format=json','list','sources'],timeout=1)),
                'dsp':lambda:self.dsp(settings.get('camilla_url','ws://127.0.0.1:1234'))}
            unavailable=[dict(id=name,group=group,state='unavailable',detail='状态查询失败',uptime=None,
                              restarts=None,cpu=None,rt=None,pid=None,can_stop=name not in RESTART_ONLY)
                         for group,names in GROUPS for name in names if name!='music-assistant']
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures={name:pool.submit(safe,name,operation,unavailable if name=='services' else [] if name in ('inputs','sinks','nodes') else {})
                         for name,operation in jobs.items()}
                data={name:future.result() for name,future in futures.items()}
            # Slow inventories (hifimate-output ~0.2 s, compose ~0.1 s) change rarely and
            # are cached; an action clears the cache. No hardware-open/probe operation.
            outputs=safe('outputs',lambda:self.remember('outputs',60,lambda:output_rows(self.run(
                [str(self.prefix/'bin/hifimate-output'),'--list'],timeout=2))),[])
            ma=safe('music-assistant',lambda:self.remember('ma',15,self.ma),None)
            # A copy: the unit list is cached across snapshots, MA is appended per snapshot.
            data['services']=list(data['services'])
            if ma: data['services'].append(ma)
            else: data['services'].append(dict(id='music-assistant',group=GROUPS[-1][0],state='unavailable',
                                               detail='未安装或不可用',uptime=None,restarts=0,cpu=None,rt=None,can_stop=True))
            logs=safe('journal',lambda:self.remember('journal',3,lambda:self.logs(lines=60)),[])
            for row in logs:
                if row['id'] in self.seen: continue
                self.seen.append(row['id'])
                # The timeline keeps arbitration events and warnings; the 日志
                # tab is the full journal.
                if row['unit']=='hifimate.service' and row['message'].startswith('{"event"'):
                    self.events.append({**row,'kind':'arbiter'})
                elif row['priority']<=4: self.events.append({**row,'kind':'journal'})
            for row in data['services']:
                previous=self.last_services.get(row['id'])
                if previous and previous!=row['state']: self.add_event(row['id']+' → '+row['state'],'service',row['id'])
                self.last_services[row['id']]=row['state']
            controller=data['controller']; owner=controller.get('owner')
            if owner!=self.last_owner:
                rate=(controller.get('format') or {}).get('rate')
                self.add_event('当前输入：'+(owner.rsplit('/',1)[-1] if owner else '空闲')+(f' · {rate/1000:g} kHz' if rate else ''),'owner','hifimate')
                self.last_owner=owner
            lanes=lane_states(data['inputs'],data['sinks'],data['nodes'])
            for key,row in lanes.items():
                row['available']=not any(name in errors for name in ('inputs','sinks','nodes'))
                row['active']=bool(controller.get('ready') and owner and owner.endswith('/'+key))
                row['can_switch']=bool(row['available'] and row['count']==1 and key in controller.get('switchable',[]) and not (owner or '').endswith('/'+key))
            selected=settings.get('alsa_device','auto')
            for row in outputs: row['selected']=row['device']==selected
            # Auto is resolved by the same read-only selector used by hifimate-output.
            if selected=='auto':
                from .output_devices import choose
                selected=safe('selected_output',lambda:choose(Path(settings.get('proc_asound','/proc/asound')),None,False),None)
                for row in outputs: row['selected']=row['device']==selected
            playing=owner.rsplit('/',1)[-1] if controller.get('ready') and owner else None
            now=safe('nowplaying',lambda:self.nowplaying.read(playing),None)
            self.cached={'time':time.time(),'controller':controller,'lanes':list(lanes.values()),'outputs':outputs,'now':now,
                         'dsp':data['dsp'],'services':data['services'],'pairing':self.pairing(),
                         'events':sorted(self.events,key=lambda row:row['time'])[-80:],'errors':errors,'system':self.metrics()}
            self.at=self.clock(); return self.cached
