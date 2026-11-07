"""LAN-readable HiFiMate Control; every mutation requires a local password session."""
import argparse
import getpass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import threading
import time
from urllib.parse import parse_qs,urlsplit
from .ctrl_auth import Auth,SESSION_SECONDS
from . import room_eq
from .ctrl_state import Collector,UNITS,RESTART_ONLY,MA_SERVICES,compose,run,ipc


class ControlError(Exception):
    def __init__(self,status,message): self.status=status; self.message=message


class Snapshots:
    """One demand-driven poller; slow/disconnected viewers never block it."""
    def __init__(self,collector):
        self.collector=collector; self.lock=threading.Lock(); self.clients=set(); self.stop=None

    def subscribe(self):
        with self.lock:
            if len(self.clients)>=8: raise ControlError(503,'状态订阅已满')
            client=queue.Queue(maxsize=1); self.clients.add(client)
            if self.stop is None:
                self.stop=threading.Event()
                threading.Thread(target=self.poll,args=(self.stop,),daemon=True).start()
            return client

    def unsubscribe(self,client):
        with self.lock:
            self.clients.discard(client)
            if not self.clients and self.stop is not None:
                self.stop.set(); self.stop=None

    def poll(self,stop):
        while not stop.is_set():
            try: value=self.collector.snapshot()
            except Exception: value={'error':'状态采集暂不可用','time':time.time()}
            with self.lock:
                if stop.is_set(): break
                for client in self.clients:
                    try: client.get_nowait()
                    except queue.Empty: pass
                    client.put_nowait(value)
            stop.wait(1)

    def close(self):
        with self.lock:
            if self.stop: self.stop.set()


def stop_pair(prefix):
    """Signal only a verified pairing process; SIGINT lets asyncio run finally."""
    from .bluetooth_pair import window_file,window_active
    path=window_file()
    if not window_active(path): raise ValueError('当前没有配对窗口')
    pid=int(json.loads(path.read_text())['pid']); proc=Path('/proc')/str(pid)
    fd=os.pidfd_open(pid) if hasattr(os,'pidfd_open') else None
    try:
        args=(proc/'cmdline').read_bytes().split(b'\0')
        expected=(str(Path(prefix)).encode(),b'hifimate.bluetooth_alias',b'pair')
        if proc.stat().st_uid!=os.geteuid() or not all(item in args for item in expected):
            raise ValueError('配对进程不属于本安装，拒绝发送信号')
        if fd is not None: signal.pidfd_send_signal(fd,signal.SIGINT)
        else: os.kill(pid,signal.SIGINT)
    finally:
        if fd is not None: os.close(fd)


class App:
    def __init__(self,prefix,collector=None,runner=run,request=ipc,stop_pairing=stop_pair,
                 roomfit_play=None,roomfit_remove=None,port=5009,auth=None,response_model=None):
        self.prefix=Path(prefix); self.collector=collector or Collector(prefix,runner,request)
        self.run=runner; self.request=request; self.stop_pairing=stop_pairing
        self.auth=auth or Auth(); self.snapshots=Snapshots(self.collector)
        self.action_lock=threading.Lock()
        from . import roomfit
        self.roomfit=roomfit.Store(self.prefix/'state/roomfit'); self.port=port
        self.sessions=roomfit.Sessions(self.prefix/'state/roomfit'); self.profiles=roomfit.Profiles(self.prefix/'state/roomfit')
        self.roomfit_play=roomfit_play or roomfit.play; self.roomfit_remove=roomfit_remove or roomfit.remove
        self.takes={}; self.take_lock=threading.Lock()
        from . import lyrics
        self.lyrics=lyrics.Lyrics(lyrics.base_url(self.prefix),migu=lyrics.Migu(lyrics.migu_url(self.prefix)))
        # The EQ page's curves: the installed CamillaDSP run offline (dsp_model.py).
        from .dsp_model import ResponseModel
        runtime=Path(os.environ.get('XDG_RUNTIME_DIR') or '/tmp')/'hifimate-eq'
        self.response_model=response_model or ResponseModel(self.prefix/'bin/hifimate-camilladsp',runtime)
        self.eq_lock=threading.Lock()

    # --- the settings page -------------------------------------------------------
    def settings_state(self):
        from . import settings
        conf=json.loads((self.prefix/'config/hifimate.conf').read_text())
        user=settings.load_user(self.prefix/'config')
        snapshot=self.collector.snapshot() or {}
        dac=next((row for row in snapshot.get('outputs') or [] if row.get('selected')),None)
        def env(key):
            try:
                for line in (self.prefix/'music-assistant/.env').read_text().splitlines():
                    if line.startswith(key+'='): return line.split('=',1)[1]
            except OSError: pass
            return None
        try: name=json.loads((self.prefix/'config/identity.json').read_text()).get('name')
        except (OSError,ValueError): name=None
        try: record=json.loads(Path('/etc/hifimate/install.json').read_text())
        except (OSError,ValueError): record={}
        return {
            'playback':{'defaults':{k:conf.get(k) for k in settings.PLAYBACK_KEYS},'user':user,
                        'effective':{**{k:conf.get(k) for k in settings.PLAYBACK_KEYS},**user},
                        'dac':{'device':dac.get('device'),'rates':[r for r in dac.get('rates') or [] if r in settings.RATES]} if dac else None,
                        'all_rates':list(settings.RATES)},
            'subscriptions':settings.subscriptions(self.prefix),
            'device':{'name':name,'prefix':str(self.prefix),'music':env('MUSIC_LIBRARY_DIR'),'ma_data':env('MUSIC_ASSISTANT_DATA_DIR'),
                      'share':record.get('share'),'system':bool(record)},
        }

    def settings_post(self,name,data):
        from . import settings
        try:
            if name=='playback':
                saved=settings.save_playback(self.prefix/'config',data.get('playback'))
                self.collector.add_event('播放设置已保存')
                # The arbiter reads them at start: restart it (asynchronously, observable).
                return {'saved':saved,**self.action('service',{'unit':'hifimate','operation':'restart'})}
            if name=='subscriptions':
                sources=settings.save_subscriptions(self.prefix,data.get('sources'),data.get('prune_removed'))
                self.collector.add_event('音乐源订阅已保存：'+(', '.join(sources) or '无'))
                return {'sources':sources,**self.action('ma-sync',{})}
        except (OSError,ValueError,TypeError,json.JSONDecodeError) as error: raise ControlError(400,str(error)) from None
        raise ControlError(404,'接口不存在')

    def ma_sync(self):
        """Restart the subscriber, wait for its pass over the sources, then restart
        Music Assistant so it loads what was added or updated."""
        from datetime import datetime,timezone
        started=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self.run(compose(self.prefix)+['restart','provider-subscriber'],timeout=60)
        deadline=time.monotonic()+180; lines=[]
        while time.monotonic()<deadline:
            text=self.run(compose(self.prefix)+['logs','--no-color','--since',started,'provider-subscriber'],timeout=10)
            lines=text.splitlines()
            if any('Next check in' in line for line in lines): break
            time.sleep(2)
        else: raise RuntimeError('订阅器 3 分钟内没有完成同步')
        for line in lines:
            if '[ERROR]' in line or '[WARN' in line:
                self.collector.add_event('订阅同步：'+line.split('|',1)[-1].strip()[:200],priority=3 if '[ERROR]' in line else 4)
        self.run(compose(self.prefix)+['restart','server'],timeout=120)

    # --- the EQ page (config/dsp-filters.yml) -------------------------------------
    def eq_room(self):
        room=room_eq.load(self.prefix/'config/room-correction.json') if (self.prefix/'config/room-correction.json').exists() else None
        return room if room and room.get('enabled') else None

    def eq_curves(self,config):
        from . import eq
        with self.eq_lock:
            try: return eq.curves(self.response_model,config,self.eq_room(),eq.headroom(self.prefix))
            except RuntimeError as error: raise ControlError(503,f'无法计算曲线：{error}') from None

    def eq_state(self):
        """The saved EQ, its curves and versions (readable without login, like the rest)."""
        from . import eq
        state=eq.read(self.prefix)
        try: state['curves']=self.eq_curves(state['config'])
        except ControlError as error: state['curves']=None; state['curves_error']=error.message
        controller=(self.collector.snapshot() or {}).get('controller') or {}
        state['bypass']=bool(controller.get('filters_bypassed'))
        state['room']=controller.get('room')
        return state

    def eq_post(self,name,data):
        from . import eq
        try:
            if name=='preview':
                return {'curves':self.eq_curves(data.get('config'))}
            if name=='parse':
                text=data.get('text')
                if not isinstance(text,str): raise ValueError('需要文本')
                return {'config':eq.parse_text(text)}
            if name=='import':
                text=data.get('text')
                if not isinstance(text,str): raise ValueError('需要文本')
                config,skipped=eq.import_text(text,data.get('prefix') if isinstance(data.get('prefix'),str) and re.fullmatch(r'[a-z]{1,8}',data.get('prefix') or '') else 'eq')
                return {'config':config,'skipped':skipped}
            if name=='version':
                return {'config':eq.version(self.prefix,data.get('id'))}
            if name=='apply':
                with self.eq_lock:
                    tag=eq.save(self.prefix,data.get('config'),data.get('etag'))
                reply=self.request(self.collector.format_path(),{'action':'filters'},timeout=10)
                if reply.get('filters_error'): raise ValueError(reply['filters_error'])
                self.collector.add_event('EQ 已保存'+('并在播放中生效' if reply.get('ok') else '，下次播放时生效'))
                return {'etag':tag,'live':bool(reply.get('ok')),'versions':eq.versions(self.prefix)}
            if name=='bypass':
                on=data.get('on')
                if not isinstance(on,bool): raise ValueError('on 须为 true 或 false')
                reply=self.request(self.collector.format_path(),{'action':'filters','bypass':on},timeout=10)
                if reply.get('error'): raise ValueError(reply['error'])
                return {'bypass':on,'live':bool(reply.get('ok'))}
        except eq.Conflict as error: raise ControlError(409,str(error)) from None
        except (ValueError,TypeError,AttributeError) as error: raise ControlError(400,str(error)) from None
        raise ControlError(404,'接口不存在')

    def lyrics_now(self,query=None):
        """Lyrics of the track playing now, or of the one named in query (the MA page's own
        browser player, which HiFi does not see); key says which, so a page can tell a stale answer."""
        if query and query.get('title'):
            text=lambda k:(query.get(k) or '')[:200] or None
            try: duration=float(query.get('duration') or 0) or None
            except ValueError: duration=None
            origin=query.get('origin') if query.get('origin') in ('migu','bilibili') else None
            origin_id=query.get('origin_id') if origin and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',query.get('origin_id') or '') else None
            now={'title':text('title'),'artist':text('artist'),'album':text('album'),
                 'duration':duration if duration and 0<duration<36000 else None,
                 'origin':origin if origin_id or origin=='bilibili' else None,'origin_id':origin_id}
        else: now=self.collector.snapshot().get('now')
        if not now: return {'key':None,'id':None,'lines':[],'reason':None}
        key='|'.join(str(now.get(k) or '') for k in ('title','artist','album'))
        return {'key':key,**self.lyrics.for_track(now)}

    def transport(self,data):
        """Play/pause, next, previous, seek on the active source (quick; answered synchronously)."""
        try: result=self.collector.nowplaying.command(data.get('command'),data.get('position'))
        except ValueError as error: raise ControlError(409,str(error)) from None
        except (OSError,RuntimeError,subprocess.SubprocessError) as error:
            raise ControlError(503,'播放器没有响应：'+type(error).__name__) from None
        with self.collector.lock: self.collector.at=-1     # show the new state at once
        return result

    def roomfit_start(self,data):
        """Generate the take and start it on the DLNA MPD (it fetches the WAV from us)."""
        from . import roomfit
        channel=data.get('channel'); level=data.get('level_db')
        if channel not in ('L','R') or not isinstance(level,(int,float)) or not roomfit.LEVELS[0]<=level<=roomfit.LEVELS[1]:
            raise ControlError(400,'声道或电平无效')
        device=data.get('device') if isinstance(data.get('device'),dict) else {}
        device={k:str(v)[:200] for k,v in list(device.items())[:16]}
        mic=data.get('mic') or None
        if mic is not None and mic not in {row['id'] for row in roomfit.mics()}: raise ControlError(400,'未知的麦克风')
        link={}
        if data.get('session') is not None:
            try:
                session=self.sessions.load(data['session'])
                if data.get('position')!='level': roomfit.Sessions.position(session,data.get('position'))
            except ValueError as error: raise ControlError(400,str(error)) from None
            if session['state'] not in ('level','measure','computed'): raise ControlError(409,'这次测量已经结束')
            link={'session':session['id'],'position':data.get('position')}
            mic=session['mic']
        # The sweep plays through the current room correction; the fit undoes it.
        try: room=room_eq.load(self.prefix/'config/room-correction.json')
        except (OSError,ValueError): room=None
        link['room_eq']=room['channels'] if room and room['enabled'] else None
        with self.take_lock:
            if any(take.get('song') and time.monotonic()<take['until'] for take in self.takes.values()):
                raise ControlError(409,'上一段扫频还在播放')
            take=self.roomfit.new(channel,float(level),{'device':device,'mic':mic,**link})
            total=roomfit.LEAD_SECONDS+roomfit.SWEEP_SECONDS+roomfit.TAIL_SECONDS
            # Busy until MPD has finished it; released early by done() and the upload.
            self.takes={take:{'wav':roomfit.take_wav(channel,float(level)),'until':time.monotonic()+total+2}}
        url=f'http://127.0.0.1:{self.port}/ctrl/api/roomfit/sweep/{take}.wav'
        song=self.roomfit_play(url)
        self.takes[take]['song']=song
        def done():
            try: self.roomfit_remove(song)
            except Exception: pass   # MPD down or queue changed: nothing left to clean
            finally:
                with self.take_lock: self.takes.pop(take,None)
        timer=threading.Timer(total+1,done); timer.daemon=True; timer.start()
        self.collector.add_event(f'房间测量：扫频 {channel} 声道 · {level:g} dBFS')
        return {'ok':True,'id':take,'lead_s':roomfit.LEAD_SECONDS,'sweep_s':roomfit.SWEEP_SECONDS,'total_s':total}

    def roomfit_upload(self,take,data):
        from . import roomfit
        meta=self.roomfit.meta(take)
        if meta.get('analysis'): raise ControlError(409,'这段测量已经上传过')
        # The page uploads after the sweep ended: the next channel may start
        # right away (左 + 右 used to hit the busy window and get 409).
        with self.take_lock: self.takes.pop(take,None)
        samples,rate=roomfit.read_recording(data)
        self.roomfit.write(take,'recording.wav',data)
        result=roomfit.analyze(samples,rate,meta.get('mic'))
        self.roomfit.write(take,'analysis.json',json.dumps(result).encode())
        summary={k:result[k] for k in ('peak_dbfs','clipped','signal_dbfs','noise_dbfs','snr_db','sweep_start_s')}
        self.roomfit.update(take,analysis=summary,recorded_rate=rate)
        extra=self.session_take(meta,take,result) if meta.get('session') else {}
        return {'ok':True,'id':take,'channel':meta['channel'],**result,**extra}

    # --- guided measurement (roomfit.Sessions / Profiles) ---
    def session_take(self,meta,take,result):
        """File a finished take under its session: level step or a position, with its verdict."""
        from . import roomfit
        with self.take_lock:
            session=self.sessions.load(meta['session'])
            verdict=roomfit.judge(result,meta.get('device'))
            if meta['position']=='level':
                session['level']={**roomfit.recommend_level(result,meta['level_db']),'take':take,'measured_snr_db':result['snr_db']}
                session['level_db']=session['level']['level_db']; session['state']='measure'
                self.sessions.save(session)
                return {'verdict':verdict,'level':session['level'],'session':session}
            position=roomfit.Sessions.position(session,meta['position'])
            center=roomfit.Sessions.position(session,'center')
            reference=None
            if position['key']!='center' and center['status']=='accepted' and meta['channel'] in center['takes']:
                reference=json.loads((self.roomfit.path(center['takes'][meta['channel']])/'analysis.json').read_text())
            verdict=roomfit.judge(result,meta.get('device'),reference)
            position['takes'][meta['channel']]=take; position['verdicts'][meta['channel']]=verdict
            position['status']='measured' if set(position['takes'])>={'L','R'} else 'partial'
            self.sessions.save(session)
            return {'verdict':verdict,'session':session}

    def room_live(self):
        """Ask the arbiter to put room-correction.json into the running DSP."""
        try:
            reply=self.request(self.collector.format_path(),{'action':'room'},timeout=10)
            return bool(reply.get('ok'))
        except (OSError,ValueError,RuntimeError): return False

    def roomfit_post(self,name,data):
        from . import roomfit
        sessions=self.sessions
        with self.take_lock:
            if name=='session/new':
                mic=data.get('mic') or None
                if mic is not None and mic not in {row['id'] for row in roomfit.mics()}: raise ControlError(400,'未知的麦克风')
                for old in [sessions.latest()]:
                    if old: old['state']='abandoned'; sessions.save(old)
                return {'ok':True,'session':sessions.new(mic)}
            session=sessions.load(data.get('session')) if not name.startswith('profiles/') else None
            if name=='session/abandon':
                session['state']='abandoned'; sessions.save(session); return {'ok':True}
            if name=='session/accept':
                position=roomfit.Sessions.position(session,data.get('position'))
                if not set(position['takes'])>={'L','R'}: raise ControlError(409,'这个位置还没测完左右声道')
                if any(v['grade']=='bad' for v in position['verdicts'].values()): raise ControlError(409,'这个位置的录音不可用，请重录')
                position['status']='accepted'; sessions.save(session); return {'ok':True,'session':session}
            if name=='session/compute':
                accepted=[p for p in session['positions'] if p['status']=='accepted']
                if len(accepted)<roomfit.MIN_POSITIONS or not any(p['key']=='center' for p in accepted):
                    raise ControlError(409,f'至少需要 {roomfit.MIN_POSITIONS} 个已接受的位置（含中心）')
                computed={'positions':[p['key'] for p in accepted],'channels':{}}
                for channel in ('L','R'):
                    curves,weights=[],[]
                    for p in accepted:
                        take=p['takes'][channel]; meta=self.roomfit.meta(take)
                        analysis=json.loads((self.roomfit.path(take)/'analysis.json').read_text())
                        computed['frequency']=analysis['frequency']
                        curves.append(roomfit.undo_eq(analysis['frequency'],analysis['magnitude_db'],channel,meta.get('room_eq')))
                        weights.append(roomfit.CENTER_WEIGHT if p['key']=='center' else 1.0)
                    f=roomfit.np().asarray(computed['frequency']); mean=roomfit.average(curves,weights)
                    filters=roomfit.fit_peq(f,mean)
                    after=mean+sum((roomfit.peaking_db(f,x['freq'],x['gain'],x['q']) for x in filters),roomfit.np().zeros(len(f)))
                    spread=[round(roomfit.band_rms(f,c,mean),1) for c in curves]
                    computed['channels'][channel]={'filters':filters,'before':[round(float(v),2) for v in mean],
                                                   'after':[round(float(v),2) for v in after],'spread_db':spread}
                computed['target']=[round(float(v),2) for v in roomfit.house_target(roomfit.np().asarray(computed['frequency']))]
                session['computed']=computed; session['state']='computed'; sessions.save(session)
                return {'ok':True,'session':session}
            if name=='session/apply':
                computed=session.get('computed')
                if session['state']!='computed' or not computed: raise ControlError(409,'请先完成计算')
                channels={ch:computed['channels'][ch]['filters'] for ch in ('L','R')}
                profile=self.profiles.add({'session':session['id'],'mic':session['mic'],'level_db':session['level_db'],
                                           'positions':computed['positions'],'channels':channels})
                self.activate_profile(profile)
                session['state']='applied'; session['profile']=profile['id']; sessions.save(session)
                return {'ok':True,'profile':profile,'live':self.room_live()}
            if name=='profiles/activate':
                profile=self.profiles.load(data.get('id')); self.activate_profile(profile)
                return {'ok':True,'profile':profile,'live':self.room_live()}
            if name=='profiles/rename':
                return {'ok':True,'profile':self.profiles.rename(data.get('id'),data.get('name'))}
            if name=='profiles/delete':
                profile=self.profiles.load(data.get('id'))
                self.profiles.delete(profile['id'])
                path=self.prefix/'config/room-correction.json'
                try: room=room_eq.load(path)
                except (OSError,ValueError): room=None
                live=None
                if room and room.get('profile')==profile['id']:
                    # The version in use is gone: correction off, nothing left to restore it from.
                    room_eq.save(path,{**room,'enabled':False,'profile':None}); live=self.room_live()
                self.collector.add_event(f"房间校正：删除方案 {time.strftime('%m-%d %H:%M',time.localtime(profile['created']))}")
                return {'ok':True,'live':live}
        raise ControlError(404,'接口不存在')

    def activate_profile(self,profile):
        from .roomfit import POSITIONS
        labels={key:label for key,label,_ in POSITIONS}
        positions='、'.join(labels.get(p,p) for p in profile['positions'])
        room_eq.save(self.prefix/'config/room-correction.json',{'enabled':True,'profile':profile['id'],'created':profile['created'],
            'source':f"roomfit {profile['session']}：{positions}",'channels':profile['channels']})
        self.collector.add_event(f"房间校正：应用方案 {time.strftime('%m-%d %H:%M',time.localtime(profile['created']))}")

    def profiles_state(self):
        try: room=room_eq.load(self.prefix/'config/room-correction.json')
        except (OSError,ValueError): room=None
        active=room.get('profile') if room else None
        return {'profiles':[{**p,'active':p['id']==active} for p in self.profiles.list()],
                'enabled':bool(room and room['enabled']),'active':active}

    def action(self,name,data):
        if name=='switch-input':
            from .receiver_events import LANES
            source=data.get('source')
            if source not in LANES: raise ControlError(400,'无效输入')
            lane=next((row for row in self.collector.snapshot()['lanes'] if row['source']==source),{})
            if not lane.get('can_switch'): raise ControlError(409,'该输入没有可切换的数据或已是当前输入')
            def execute():
                reply=self.request(self.collector.format_path(),{'action':'switch','source':source},timeout=30)
                if not reply.get('ok'): raise ValueError(reply.get('error','切换失败'))
            label='切换输入：'+source
        elif name=='switch-output':
            device=data.get('device')
            # Fresh inventory, then hifimate-output independently validates transactionally.
            from .ctrl_state import output_rows
            rows=output_rows(self.run([str(self.prefix/'bin/hifimate-output'),'--list'],timeout=3))
            if not isinstance(device,str) or not any(row['device']==device for row in rows):
                raise ControlError(400,'输出不存在或能力不明确')
            execute=lambda:self.run([str(self.prefix/'bin/hifimate-output'),'--device',device,'--apply'],timeout=90)
            label='切换输出：'+device
        elif name=='service':
            unit=data.get('unit'); operation=data.get('operation')
            if unit not in (*UNITS,'music-assistant') or operation not in ('restart','stop'):
                raise ControlError(400,'不允许的服务或操作')
            if operation=='stop' and unit in RESTART_ONLY: raise ControlError(403,'该服务仅允许重启')
            if unit=='music-assistant':
                args=compose(self.prefix)+[operation,*MA_SERVICES]
            else: args=['systemctl','--user',operation,unit+'.service']
            execute=lambda:self.run(args,timeout=65)
            label=('重启' if operation=='restart' else '停止')+'服务：'+unit
        elif name=='pair':
            if self.collector.pairing()['active']: raise ControlError(409,'配对窗口已打开')
            # A transient USER service survives ctrl restarts; no root privilege.
            args=['systemd-run','--user','--collect','--unit=hifimate-ctrl-pair','--service-type=exec',
                  '--property=RuntimeMaxSec=150','--property=TimeoutStopSec=20',
                  '--property=KillSignal=SIGINT','--',str(self.prefix/'bin/hifimate-bluetooth'),'pair','--seconds','120']
            def execute():
                self.run(args,timeout=5)
                deadline=time.monotonic()+12
                while time.monotonic()<deadline:
                    if self.collector.pairing()['active']: return
                    time.sleep(.1)
                raise RuntimeError('配对窗口未打开')
            label='开启蓝牙配对 120 s'
        elif name=='room':
            enabled=data.get('enabled')
            if not isinstance(enabled,bool): raise ControlError(400,'enabled 须为 true 或 false')
            path=self.prefix/'config/room-correction.json'
            try: room=room_eq.load(path)
            except (OSError,ValueError): raise ControlError(409,'房间校正文件无效') from None
            if not room: raise ControlError(409,'尚未生成房间校正')
            def execute():
                room_eq.save(path,{**room,'enabled':enabled})
                # The arbiter updates the running DSP (or the next chain picks it up).
                reply=self.request(self.collector.format_path(),{'action':'room'},timeout=10)
                if not reply.get('ok',False) and reply.get('error'): raise RuntimeError(reply['error'])
            label='房间校正：'+('开' if enabled else '关')
        elif name=='pair-stop':
            execute=lambda:self.stop_pairing(self.prefix); label='停止蓝牙配对'
        elif name=='ma-sync':
            execute=self.ma_sync; label='同步音乐源订阅并重启 Music Assistant'
        else: raise ControlError(404,'未知操作')
        if not self.action_lock.acquire(blocking=False): raise ControlError(409,'已有操作正在执行，请稍后重试')
        self.collector.add_event(label+' · 已接受')
        def work():
            try:
                execute(); self.collector.add_event(label+' · 完成')
            except Exception as error:
                # Avoid disclosing stderr, credentials, or local configuration.
                self.collector.add_event(label+' · 失败（'+type(error).__name__+'），请查看服务日志',priority=3)
            finally:
                with self.collector.lock:
                    self.collector.at=-1; self.collector.slow.clear()
                self.action_lock.release()
        # Respond before a self/MA restart disconnects HTTP. The job is observable
        # via the event stream; reconnecting clients must never replay a POST.
        timer=threading.Timer(.3,work); timer.daemon=True; timer.start()
        return {'ok':True,'accepted':True,'message':label+' · 已接受，请等待状态更新'}


class Server(ThreadingHTTPServer):
    daemon_threads=True
    allow_reuse_address=True
    def __init__(self,address,app,assets):
        self.app=app; self.assets=Path(assets); self.slots=threading.BoundedSemaphore(24)
        super().__init__(address,Handler)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False): request.close(); return
        try: super().process_request(request,address)
        except BaseException: self.slots.release(); raise
    def process_request_thread(self,request,address):
        try: super().process_request_thread(request,address)
        finally: self.slots.release()
    def server_close(self):
        self.app.snapshots.close(); super().server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def setup(self):
        self.request.settimeout(5); super().setup()
    def log_message(self,*_): pass
    def token(self):
        try:
            cookie=SimpleCookie(self.headers.get('Cookie',''))
            return cookie['hifimate_ctrl'].value if 'hifimate_ctrl' in cookie else ''
        except Exception: return ''
    def reply(self,status,data,kind='application/json; charset=utf-8',cookie=None):
        body=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status); self.send_header('Content-Type',kind)
        self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; frame-ancestors 'self'")
        if cookie: self.send_header('Set-Cookie',cookie)
        self.end_headers()
        if self.command!='HEAD': self.wfile.write(body)
    def fail(self,status,message): self.reply(status,{'ok':False,'error':message})
    def do_GET(self):
        try:
            path=urlsplit(self.path).path; app=self.server.app
            if path=='/ctrl/api/events': return self.events()
            if path=='/ctrl/api/state':
                return self.reply(200,{**app.collector.snapshot(),'auth':{'configured':app.auth.available(),'logged_in':app.auth.valid(self.token())}})
            if path=='/ctrl/api/lyrics':
                query={k:v[-1] for k,v in parse_qs(urlsplit(self.path).query).items()}
                return self.reply(200,app.lyrics_now(query))
            if path=='/ctrl/api/logs':
                query=parse_qs(urlsplit(self.path).query)
                return self.reply(200,{'logs':app.collector.logs(query.get('unit',[''])[-1],query.get('errors',['0'])[-1]=='1',query.get('lines',['80'])[-1])})
            if path=='/ctrl/api/eq':
                return self.reply(200,app.eq_state())
            if path=='/ctrl/api/settings':
                return self.reply(200,app.settings_state())
            if path=='/ctrl/api/roomfit/mics':
                from .roomfit import mics
                return self.reply(200,{'mics':mics()})
            if path=='/ctrl/api/roomfit/session':
                return self.reply(200,{'session':app.sessions.latest()})
            if path=='/ctrl/api/roomfit/profiles':
                return self.reply(200,app.profiles_state())
            if path=='/ctrl/api/roomfit/takes':
                return self.reply(200,{'takes':app.roomfit.list()})
            if path.startswith('/ctrl/api/roomfit/sweep/'): return self.sweep(path)
            if path=='/ctrl/api/roomfit/ca':
                from .tls import ca_name
                return self.reply(200,{'name':ca_name(app.prefix/'config/tls')})
            if path=='/ctrl/roomfit/hifimate-ca.crt':
                # Plain HTTP on purpose: the phone needs it before it trusts HTTPS.
                body=(app.prefix/'config/tls/ca.crt').read_bytes()
                return self.reply(200,body,'application/x-x509-ca-cert')
            files={'/ctrl/':'ctrl.html','/ctrl/ctrl.css':'ctrl.css','/ctrl/ctrl.js':'ctrl.js',
                   '/ctrl/nowplaying.js':'nowplaying.js','/ctrl/nowplaying.css':'nowplaying.css',
                   '/ctrl/roomfit/':'roomfit.html','/ctrl/roomfit/roomfit.css':'roomfit.css','/ctrl/roomfit/roomfit.js':'roomfit.js',
                   '/ctrl/roomfit/roomfit-worklet.js':'roomfit-worklet.js',
                   '/ctrl/eq/':'eq.html','/ctrl/eq/eq.css':'eq.css','/ctrl/eq/eq.js':'eq.js',
                   '/ctrl/settings/':'settings.html','/ctrl/settings/settings.js':'settings.js'}
            if path not in files: return self.fail(404,'页面不存在')
            name=files[path]; kind='text/html; charset=utf-8' if name.endswith('.html') else 'text/css' if name.endswith('.css') else 'text/javascript'
            self.reply(200,(self.server.assets/name).read_bytes(),kind)
        except (BrokenPipeError,ConnectionResetError): self.close_connection=True
        except (OSError,ValueError,KeyError,RuntimeError,subprocess.SubprocessError) as error:
            try: self.fail(503 if isinstance(error,OSError) else 400,'状态或日志暂不可用')
            except OSError: self.close_connection=True
    def sweep(self,path):
        """The take's WAV for the local MPD only: direct loopback, never through nginx."""
        if self.client_address[0] not in ('127.0.0.1','::1') or self.headers.get('X-Real-IP') or self.headers.get('X-Forwarded-For'):
            return self.fail(403,'仅供本机播放器读取')
        take=path.removeprefix('/ctrl/api/roomfit/sweep/').removesuffix('.wav')
        entry=self.server.app.takes.get(take)
        if not entry: return self.fail(404,'扫频不存在或已结束')
        self.reply(200,entry['wav'],'audio/wav')
    def events(self):
        origin=self.headers.get('Origin')
        if origin and not self.same_origin(): return self.fail(403,'来源不匹配')
        app=self.server.app
        try: client=app.snapshots.subscribe()
        except ControlError as error: return self.fail(error.status,error.message)
        try:
            self.send_response(200); self.send_header('Content-Type','text/event-stream')
            self.send_header('Cache-Control','no-store'); self.send_header('Connection','close')
            self.send_header('X-Accel-Buffering','no'); self.end_headers(); self.close_connection=True
            while True:
                try:
                    data=client.get(timeout=1.5)
                    data={**data,'auth':{'configured':app.auth.available(),'logged_in':app.auth.valid(self.token())}}
                    payload=b'data: '+json.dumps(data,ensure_ascii=False,allow_nan=False).encode()+b'\n\n'
                except queue.Empty: payload=b': keepalive\n\n'
                self.wfile.write(payload); self.wfile.flush()
                import select
                if select.select([self.connection],[],[],0)[0] and not self.connection.recv(1,socket.MSG_PEEK): break
        except (OSError,ValueError): pass
        finally: app.snapshots.unsubscribe(client)
    def same_origin(self):
        origin=urlsplit(self.headers.get('Origin',''))
        # nginx overwrites X-Forwarded-Proto; direct connections are HTTP.
        scheme=self.headers.get('X-Forwarded-Proto','http')
        return (origin.scheme==scheme and origin.scheme in ('http','https') and
                origin.netloc==self.headers.get('Host') and not origin.path and not origin.query and not origin.fragment)
    def do_POST(self):
        try:
            if not self.same_origin(): raise ControlError(403,'请求来源不匹配')
            path=urlsplit(self.path).path; app=self.server.app
            if path.startswith('/ctrl/api/roomfit/upload/'): return self.upload(path)
            if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json':
                raise ControlError(415,'请求须为 JSON')
            if self.headers.get('Transfer-Encoding'): raise ControlError(400,'不支持分块请求')
            size=int(self.headers.get('Content-Length','0'))
            # An EQ (a filter list, a pasted export) can be larger than an action.
            limit=256*1024 if path.startswith('/ctrl/api/eq/') else 8192
            if not 0<size<=limit: raise ControlError(413,'请求大小无效')
            body=self.rfile.read(size)
            if len(body)!=size: raise ValueError('truncated body')
            data=json.loads(body)
            if not isinstance(data,dict): raise ValueError('object required')
            path=urlsplit(self.path).path; app=self.server.app
            if path=='/ctrl/api/login':
                # X-Real-IP is set by the local trusted nginx, never taken from XFF.
                peer=self.headers.get('X-Real-IP',self.client_address[0])
                status,token,error=app.auth.login(data.get('password'),peer)
                if error: return self.fail(status,error)
                secure='; Secure' if self.headers.get('X-Forwarded-Proto')=='https' else ''
                return self.reply(200,{'ok':True},cookie=f'hifimate_ctrl={token}; Path=/ctrl/; Max-Age={SESSION_SECONDS}; HttpOnly; SameSite=Strict'+secure)
            if not app.auth.available(): raise ControlError(403,'这台主机不能用 PAM 验证密码')
            if not app.auth.valid(self.token()): raise ControlError(403,'请先登录')
            if path=='/ctrl/api/logout':
                app.auth.logout(self.token())
                return self.reply(200,{'ok':True},cookie='hifimate_ctrl=; Path=/ctrl/; Max-Age=0; HttpOnly; SameSite=Strict')
            if path=='/ctrl/api/transport': return self.reply(200,app.transport(data))
            if path=='/ctrl/api/roomfit/start': return self.reply(200,app.roomfit_start(data))
            if path.startswith('/ctrl/api/eq/'): return self.reply(200,app.eq_post(path.removeprefix('/ctrl/api/eq/'),data))
            if path.startswith('/ctrl/api/settings/'): return self.reply(200,app.settings_post(path.removeprefix('/ctrl/api/settings/'),data))
            if path.startswith('/ctrl/api/roomfit/'):
                try: return self.reply(200,app.roomfit_post(path.removeprefix('/ctrl/api/roomfit/'),data))
                except ValueError as error: raise ControlError(400,str(error)) from None
            if not path.startswith('/ctrl/api/'): raise ControlError(404,'接口不存在')
            self.reply(202,app.action(path.removeprefix('/ctrl/api/'),data))
        except ControlError as error: self.close_connection=True; self.fail(error.status,error.message)
        except (ValueError,TypeError,OSError,RuntimeError,subprocess.SubprocessError): self.close_connection=True; self.fail(400,'请求无效或组件不可用')
    def upload(self,path):
        from . import roomfit
        app=self.server.app
        if self.headers.get('Transfer-Encoding'): raise ControlError(400,'不支持分块请求')
        size=int(self.headers.get('Content-Length','0'))
        if not 0<size<=roomfit.MAX_UPLOAD: raise ControlError(413,'录音大小无效')
        # Read the body before any refusal: closing on a client still sending
        # makes the browser report a network error instead of the reason.
        self.request.settimeout(60)   # a few MB from a phone over Wi-Fi
        body=self.rfile.read(size)
        if len(body)!=size: raise ValueError('truncated body')
        if not app.auth.available() or not app.auth.valid(self.token()): raise ControlError(403,'请先登录')
        if self.headers.get('Content-Type','').split(';')[0].strip().lower() not in ('audio/wav','audio/x-wav'):
            raise ControlError(415,'录音须为 WAV')
        take=path.removeprefix('/ctrl/api/roomfit/upload/')
        try: result=app.roomfit_upload(take,body)
        except ValueError as error: raise ControlError(400,str(error)) from None
        self.reply(200,result)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',nargs='?',choices=('serve','set-ma-token','close-pair','reload-filters'),default='serve')
    parser.add_argument('--prefix',type=Path,required=True)
    parser.add_argument('--stdin',action='store_true',help='set-ma-token: read the token from stdin')
    parser.add_argument('--port',type=int,default=5009)
    args=parser.parse_args()
    if args.action=='close-pair':
        from .bluetooth_pair import window_active
        if window_active():
            stop_pair(args.prefix)
            deadline=time.monotonic()+20
            while window_active() and time.monotonic()<deadline: time.sleep(.1)
            if window_active(): parser.error('配对窗口未能关闭，保留安装以便重试')
        return
    if args.action=='reload-filters':
        # Personal filters (config/dsp-filters.yml): check, then into the running DSP.
        from . import user_filters
        from .ctrl_state import Collector,ipc
        path=args.prefix/'config/dsp-filters.yml'
        try: user=user_filters.load(path)
        except (OSError,ValueError) as error: parser.error(str(error))
        if user is None: parser.error(f'{path} 不存在（重新安装会创建模板）')
        try: reply=ipc(Collector(args.prefix).format_path(),{'action':'filters'},timeout=10)
        except (OSError,ValueError,RuntimeError) as error: parser.error(f'仲裁器没有响应：{error}')
        if reply.get('error') or reply.get('filters_error'): parser.error(reply.get('error') or reply['filters_error'])
        count=len(user['filters'])
        print(f'个人滤波器 {count} 个：'+('已在播放中生效。' if reply.get('ok') else '格式正确，下次播放时生效。')); return
    if args.action=='set-ma-token':
        # A long-lived token of a non-admin MA user (role "user"); checked
        # against MA before it is stored, never echoed.
        import sys
        from . import ma_api
        token=sys.stdin.readline(4098).strip() if args.stdin else getpass.getpass('MA 长期令牌：')
        try:
            ma_api.call('player_queues/all',{},token,timeout=5)
            ma_api.save_token(args.prefix,token)
        except (OSError,ValueError,ma_api.MAError) as error: parser.error(f'令牌不可用：{error}')
        print('MA 令牌已保存；/ctrl 会显示 MA 的曲目并控制它的队列。'); return
    app=App(args.prefix,port=args.port)
    with Server(('127.0.0.1',args.port),app,args.prefix/'gui') as server:
        server.serve_forever(poll_interval=.5)


if __name__=='__main__': main()
