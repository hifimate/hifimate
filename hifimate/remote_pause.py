"""Best-effort, bounded protocol pauses. Routing success is never pause evidence."""
import asyncio
import http.client
import ipaddress
import re
import subprocess
import threading
from .mpd import MPD, PORTS

class Sessions:
    """Updated by receiver threads BEFORE queueing, fencing delayed work/pauses."""
    def __init__(self): self.lock=threading.Lock(); self.live={}; self.latest=None; self.seen=set()
    @staticmethod
    def key(event): return event['source'],event.get('device')
    def observe(self,event):
        if not event.get('session'): return
        with self.lock:
            key=self.key(event)
            old=self.live.get(key)
            if event['event']=='play':
                identity=(*key,event['session'])
                if identity in self.seen: return
                if len(self.seen)>=65536: raise RuntimeError('receiver session budget exhausted')
                self.seen.add(identity)
                self.live[key]=(event['session'],True)
                self.latest=identity
            elif old and old[0]==event['session']: self.live[key]=(old[0],False)
    def current(self,event):
        with self.lock: return self.live.get(self.key(event))==(event.get('session'),True)
    def reissue(self,event):
        with self.lock:
            key=self.key(event); old=self.live.get(key)
            if not old or old[0]!=event.get('session'): raise ValueError('接收会话已变化，请刷新')
            self.live[key]=(old[0],True); self.latest=(*key,old[0])
    def authorized(self,event):
        with self.lock:
            key=self.key(event)
            return self.latest==(*key,event.get('session')) and self.live.get(key)==(event.get('session'),True)


def dacp_endpoint(metadata,run=subprocess.run):
    """Resolve the advertised DACP service AND match the Shairport peer IP."""
    daid=metadata.get('daid',''); token=metadata.get('acre','')
    if not re.fullmatch(r'[0-9a-fA-F]{1,16}',daid) or not re.fullmatch(r'[0-9]{1,20}',token):
        return None
    try: peer=ipaddress.ip_address(metadata.get('clip',''))
    except ValueError: return None
    # Parsable output still translates service types through Avahi's local
    # database unless disabled (e.g. _dacp._tcp -> "iTunes Remote Control").
    output=run(['avahi-browse','--resolve','--terminate','--parsable','--no-db-lookup','_dacp._tcp'],
               check=True,capture_output=True,text=True,timeout=2).stdout
    matches=set()
    for line in output.splitlines():
        fields=line.split(';')
        if len(fields)<10 or fields[0]!='=': continue
        name=fields[3].lower()
        if name!='itunes_ctrl_'+daid.lower() or fields[4]!='_dacp._tcp': continue
        try: address=ipaddress.ip_address(fields[7]); port=int(fields[8])
        except ValueError: continue
        if address==peer and 1<=port<=65535:
            # IPv6 link-local requires the resolved interface scope.
            host=fields[7]
            if address.version==6 and address.is_link_local and '%' not in host: host+='%'+fields[1]
            matches.add((host,port))
    return next(iter(matches)) if len(matches)==1 else None


def dacp_request(endpoint,token,path):
    connection=http.client.HTTPConnection(*endpoint,timeout=1)
    try:
        connection.request('GET','/ctrl-int/1/'+path,headers={'Active-Remote':token})
        response=connection.getresponse(); data=response.read(65537)
        if len(data)>65536: raise RuntimeError('oversized DACP response')
        if response.status not in (200,204): raise RuntimeError(f'DACP HTTP {response.status}')
        return data
    finally: connection.close()


def dacp_state(data):
    # Parse bounded DMAP containers, do not search arbitrary binary for "caps".
    def fields(payload,depth=0):
        if depth>4: raise ValueError('nested DACP response')
        while payload:
            if len(payload)<8: raise ValueError('truncated DACP response')
            tag=payload[:4]; length=int.from_bytes(payload[4:8],'big')
            if length>len(payload)-8: raise ValueError('truncated DACP field')
            value=payload[8:8+length]; payload=payload[8+length:]
            if tag==b'caps' and length==1: return value[0]
            if tag==b'cmst':
                found=fields(value,depth+1)
                if found is not None: return found
        return None
    return fields(data)


async def bluez_pause(remote,device,current=lambda: True):
    from dbus_next.aio import MessageBus
    from dbus_next import BusType, Message, MessageType
    player=remote.get('player','')
    if not re.fullmatch(re.escape(device)+r'/player[0-9]+',player):
        return {'status':'unsupported','requested':False,'detail':'no matching BlueZ MediaPlayer1'}
    bus=None; requested=False; accepted=False
    try:
        async with asyncio.timeout(2):
            bus=await MessageBus(bus_type=BusType.SYSTEM).connect()
            async def call(interface,member,signature='',body=None):
                answer=await bus.call(Message(destination='org.bluez',path=player,interface=interface,
                                              member=member,signature=signature,body=body or []))
                if answer.message_type==MessageType.ERROR: raise RuntimeError(str(answer.body))
                return answer.body
            async def property(name):
                return (await call('org.freedesktop.DBus.Properties','Get','ss',['org.bluez.MediaPlayer1',name]))[0].value
            if await property('Device')!=device: return {'status':'stale','requested':False,'detail':'BlueZ player device changed'}
            if await property('Status') in ('paused','stopped'): return {'status':'confirmed','requested':False,'detail':'BlueZ already inactive'}
            if not current(): return {'status':'stale','requested':False,'detail':'BlueZ session superseded'}
            requested=True
            await call('org.bluez.MediaPlayer1','Pause'); accepted=True
            for _ in range(5):
                if await property('Status') in ('paused','stopped'): return {'status':'confirmed','requested':True,'detail':'BlueZ Status'}
                await asyncio.sleep(.05)
            return {'status':'accepted','requested':True,'detail':'BlueZ Pause returned; paused status not observed'}
    except Exception as exc:
        return {'status':'accepted' if accepted else 'error','requested':requested,
                'detail':('BlueZ Pause accepted; status unavailable: ' if accepted else 'BlueZ: ')+str(exc)}
    finally:
        if bus:
            bus.disconnect()
            try: await asyncio.wait_for(bus.wait_for_disconnect(),.2)
            except Exception: pass


class RemotePause:
    def __init__(self,sessions,mpd=MPD,resolve=dacp_endpoint,http=dacp_request,bluez=bluez_pause):
        self.sessions=sessions; self.mpd=mpd; self.resolve=resolve; self.http=http; self.bluez=bluez
    def __call__(self,event):
        result={'source':event['source'],'session':event.get('session'),'requested':False}
        try:
            if not self.sessions.current(event): return {**result,'status':'stale','detail':'receiver session ended or superseded'}
            source=event['source']; remote=event.get('remote') or {}
            if source in PORTS:
                with self.mpd(PORTS[source]) as client:
                    status=client.command('status')
                    if status.get('state') in ('pause','stop'): return {**result,'status':'confirmed','detail':'MPD already inactive'}
                    # Song changes within a continuing session are legitimate. The
                    # live receiver session (not a track ID) fences pause authority.
                    if not self.sessions.current(event): return {**result,'status':'stale','detail':'MPD session superseded'}
                    result['requested']=True; client.command('pause 1')
                    try: state=client.command('status').get('state')
                    except Exception as exc: return {**result,'status':'accepted','detail':'MPD pause accepted; status unavailable: '+str(exc)}
                    return {**result,'status':'confirmed' if state in ('pause','stop') else 'accepted','detail':'MPD state: '+str(state)}
            if source=='bluetooth':
                if not remote.get('player'): return {**result,'status':'unsupported','detail':'no BlueZ MediaPlayer1'}
                return {**result,**asyncio.run(self.bluez(remote,event['device'],lambda: self.sessions.current(event)))}
            if source=='airplay':
                endpoint=self.resolve(remote)
                if not endpoint: return {**result,'status':'unsupported','detail':'DACP credentials or matching advertised peer unavailable'}
                if not self.sessions.current(event): return {**result,'status':'stale','detail':'AirPlay session superseded'}
                result['requested']=True; self.http(endpoint,remote['acre'],'pause')
                try:
                    state=dacp_state(self.http(endpoint,remote['acre'],'playstatusupdate?revision-number=0'))
                    return {**result,'status':'confirmed' if state in (2,3) else 'accepted','detail':'DACP play state: '+str(state)}
                except Exception as exc:
                    return {**result,'status':'accepted','detail':'DACP pause accepted; status unavailable: '+str(exc)}
            return {**result,'status':'unsupported','detail':'no pause adapter'}
        except Exception as exc: return {**result,'status':'error','detail':str(exc)}
