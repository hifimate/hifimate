"""Explicit receiver requests plus event-driven, single-stream lane binding.

Hooks announce attempts, not successful playback. Only a unique uncorked stream
can finish preparation. No status/format observation creates a new request.
"""
import json
import time
import uuid
from .formats import SafetyError

LANES = {'dlna':'dlna_in','music_assistant_dlna':'music_assistant_dlna_in',
         'airplay':'airplay_in','bluetooth':'bluetooth_in'}

class ReceiverEvents:
    def __init__(self, controller, timeout=5, clock=time.monotonic, pause=None, current=None,
                 idle_timeout=300, idle_poll=5):
        if not (idle_timeout==0 or 30<=idle_timeout<=86400): raise ValueError('idle_timeout_s must be 0 or 30..86400')
        self.controller=controller; self.timeout=timeout; self.clock=clock
        # Close the DAC after the owner's lane has been silent (Camilla Paused)
        # for idle_timeout seconds; 0 keeps the output open until a stop edge.
        self.idle_timeout=idle_timeout; self.idle_poll=idle_poll
        self.quiet_since=None; self.next_poll=0; self.kept_open=None
        self.epoch=uuid.uuid4().hex; self.sequence=0; self.pending=None; self.source=None
        self.seen=set(); self.error=None; self.device=None; self.session=None
        self.failed=None; self.active=None; self.intent=None; self.retired=set()
        self.pause=pause or (lambda event: {'source':event['source'],'status':'unsupported','requested':False,'detail':'no pause adapter'})
        self.is_current=current or (lambda event: True)
        controller.valid_intent=lambda: self.intent is None or self.is_current(self.intent)
        self.pause_results=[]
        self.requests={}
    def request(self, source, action, event_id, device=None, session=None, remote=None, manual=False):
        if source not in LANES or action not in ('play','stop'): raise ValueError('invalid receiver event')
        if type(event_id) is not str or not 1<=len(event_id)<=128: raise ValueError('event id required')
        if source=='bluetooth' and (type(device) is not str or not device.startswith('/org/bluez/') or type(session) is not str or not session):
            raise ValueError('Bluetooth device/session required')
        if source in ('dlna','music_assistant_dlna') and not session:
            # Legacy upmpdcli hooks are wakeups only. They precede MPD play and
            # cannot distinguish a delayed stop from a new playback session.
            return False
        key=(source,device,session)
        if action=='play':
            self.requests[source]=dict(source=source,device=device,session=session,remote=dict(remote or {}))
        if not manual and action=='play' and (key in self.retired or self.intent and key==(self.source,self.device,self.session)):
            return False
        if event_id in self.seen: return False
        if len(self.seen)>=65536: raise RuntimeError('receiver event budget exhausted; restart required')
        self.seen.add(event_id)
        if action=='stop':
            if source!=self.source: return False
            if (device,session)!=(self.device,self.session): return False
            self._end(key); return True
        if self.intent: self.retired.add((self.source,self.device,self.session))
        self.sequence+=1
        identity=f'{self.epoch}/{self.sequence}/{source}'
        self.controller.select(identity); self.source=source; self.device=device; self.session=session
        self.intent=dict(source=source,device=device,session=session,remote=remote or {})
        self.failed=None; self.quiet_since=None
        self.pending=(identity,self.clock()+self.timeout)
        return True
    def switch(self,source,sessions):
        """Explicit local intent; caller holds controller.lock. PCM is evidence only."""
        from .lane_state import lane_states
        if source not in LANES: raise ValueError('无效输入')
        if (self.controller.owner or '').endswith('/'+source): raise ValueError('已经是当前输入')
        previous=self.requests.get(source)
        if not previous: raise ValueError('该输入尚无播放请求')
        c=self.controller
        states=lane_states(json.loads(c.run(['pactl','--format=json','list','sink-inputs'])),
                           json.loads(c.run(['pactl','--format=json','list','sinks'])),
                           json.loads(c.run(['pw-dump'])))
        if states[source]['count']!=1: raise ValueError('该输入没有唯一的活动数据流')
        # Re-authorize this recorded session once, without weakening automatic
        # retired-session fencing. A newer receiver edge still cancels this intent.
        sessions.reissue(previous)
        self.retired.discard((source,previous['device'],previous['session']))
        return self.request(source,'play','manual-'+uuid.uuid4().hex,
                            previous['device'],previous['session'],previous['remote'],manual=True)
    def _end(self,key):
        self.controller.idle(); self.retired.add(key)
        self.source=None; self.pending=None; self.intent=None; self.active=None; self.quiet_since=None
    def _activate(self,binding,node):
        c=self.controller
        evidence=json.dumps([binding,node.get('info',{}).get('params',{}).get('Format',[])],sort_keys=True)
        if evidence==self.failed: return False
        try:
            if c.candidate:
                c.change(c.candidate,c.candidate_revision+1,*binding,hold=True)
                # The old remote is touched only after the new chain is verified
                # and its owner committed. It cannot revoke the new owner.
                previous=self.active; self.active=dict(self.intent)
                if previous and (previous['source'],previous.get('device'))!=(self.source,self.device):
                    result=self.pause(previous)
                    self.pause_results.append(result); self.pause_results=self.pause_results[-32:]
                    print(json.dumps({'event':'remote_pause',**result}),flush=True)
                if not self.is_current(self.intent): raise RuntimeError('receiver session superseded during handoff')
                c.release()
            elif c.binding==binding and c.ready: c.refresh()
            else: c.change(c.owner,c.revision+1,*binding)
            if not c.ready: raise RuntimeError(c.error or 'format not ready')
        except Exception:
            # Stop/SetConfig generates its own PW events. The same failed
            # source evidence must not cause an endless reconfiguration loop.
            self.failed=evidence
            self.pending=None
            raise
        self.failed=None; self.pending=None; self.error=None
        return True
    def update(self):
        if not self.source: return
        c=self.controller; lane=LANES[self.source]
        try:
            if self.source=='bluetooth':
                nodes=json.loads(c.run(['pw-dump']))
                address=self.device.rsplit('/dev_',1)[-1].replace('_',':').lower()
                native=[]
                for n in nodes:
                    props=n.get('info',{}).get('props',{})
                    name=props.get('node.name','')
                    addr=str(props.get('api.bluez5.address',props.get('device.string',''))).lower()
                    if name.startswith('bluez_input.') and (addr==address or address.replace(':','_') in name.lower()):
                        kind=props.get('media.class')
                        if kind=='Stream/Output/Audio':
                            # bluez5.media-source-role = playback (51-hifimate.conf):
                            # a playback stream routed into bluetooth_in. It binds
                            # through that lane below, with its address checked.
                            continue
                        if kind!='Audio/Source': raise RuntimeError('BlueZ native node is not Audio/Source')
                        native.append(n)
                if len(native)>1: raise RuntimeError('multiple BlueZ native sources')
                if native:
                    n=native[0]; props=n['info']['props']
                    binding=(n['id'],None,props['object.serial'],props['node.name'])
                    self._activate(binding,n)
                    return
            inputs=json.loads(c.run(['pactl','--format=json','list','sink-inputs']))
            sinks=json.loads(c.run(['pactl','--format=json','list','sinks']))
            sink=next((s for s in sinks if s.get('name')==lane),None)
            active=[i for i in inputs if sink and i.get('sink')==sink['index'] and not i.get('corked',False)]
            if len(active)>1: raise RuntimeError('multiple active streams in authorized lane')
            if not active:
                bound=c.binding if c.ready and not self.pending else None
                if bound and any(sink and i.get('sink')==sink['index'] and i.get('index')==bound[1]
                                 and str(i.get('properties',{}).get('object.serial'))==str(bound[2])
                                 for i in inputs):
                    # The owner's own stream is only corked (e.g. AirPlay flushes
                    # and resyncs after the handoff's clock change). Corked means
                    # no PCM, i.e. silence: keep the verified chain. Rebuilding it
                    # would stall the graph again and loop; a long silence is
                    # closed by the idle timeout, a vanished stream still fails.
                    return
                if self.pending and self.clock()<self.pending[1]:
                    # Keep old output only while its original proof still holds.
                    if c.ready: c.verify_current()
                    return
                raise RuntimeError('authorized stream absent or request timed out')
            item=active[0]
            serial=item.get('properties',{}).get('object.serial')
            nodes=json.loads(c.run(['pw-dump']))
            matches=[n for n in nodes if str(n.get('info',{}).get('props',{}).get('object.serial'))==str(serial)]
            if serial is None or len(matches)!=1: raise RuntimeError('source serial not uniquely resolved')
            if self.source=='bluetooth':
                # The lane's only stream must come from the requesting phone:
                # check the Pulse stream's address, else its PipeWire node's.
                address=self.device.rsplit('/dev_',1)[-1].replace('_',':').lower()
                props=item.get('properties',{}); node=matches[0].get('info',{}).get('props',{})
                reported={str(x.get(k,'')).lower() for x in (props,node) for k in ('api.bluez5.address','device.string')}
                named=address.replace(':','_') in str(node.get('node.name','')).lower()
                if address not in reported and not named:
                    raise RuntimeError('Bluetooth stream in the lane is not from the requesting device')
            binding=(matches[0]['id'],item['index'],serial,lane)
            self._activate(binding,matches[0])
        except Exception as exc:
            if self.pending and self.clock()>=self.pending[1]: self.pending=None
            self.error=str(exc); c.error=str(exc)
            if isinstance(exc,SafetyError): raise
            c.mute()
    def tick(self):
        if self.pending and self.clock()>=self.pending[1]: self.update()
        self._idle_check()
    def _idle_check(self):
        c=self.controller; now=self.clock()
        if not self.idle_timeout or not self.source or self.pending or not c.ready:
            self.quiet_since=None; return
        if now<self.next_poll: return
        self.next_poll=now+self.idle_poll
        key=(self.source,self.device,self.session)
        if key==self.kept_open: return
        try: state=c.processing_state()
        except Exception: self.quiet_since=None; return  # chain checks own RPC failures
        if state!='Paused':
            self.quiet_since=None; return
        if self.quiet_since is None: self.quiet_since=now
        if now-self.quiet_since<self.idle_timeout: return
        # Pause the sender first, so resuming later is an observable play edge
        # rather than PCM reappearing on a closed output.
        result=self.pause(dict(self.intent))
        self.pause_results.append(result); self.pause_results=self.pause_results[-32:]
        print(json.dumps({'event':'idle_timeout','seconds':self.idle_timeout,**result}),flush=True)
        if result.get('status') in ('confirmed','accepted','stale'): self._end(key)
        else: self.kept_open=key  # resume would be unobservable; keep this session open
