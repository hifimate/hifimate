"""Serialized source-fenced Linux format control; never routes sources into a mix."""
from dataclasses import dataclass
from contextlib import closing
import copy
import json
from pathlib import Path
import re
import subprocess
import threading
import time

FORMATS = {'s16le':16,'s24le':24,'s24_32le':24,'s24-32le':24,'s32le':32,'float32le':32,
           'S16LE':16,'S24LE':24,'S24_32LE':24,'S32LE':32,'F32LE':32,
           'F32P':32,'S16':16,'S24':24,'S24_32':24,'S32':32,'F32':32}
@dataclass(frozen=True)
class AudioFormat:
    rate: int
    bits: int = 24
    channels: int = 2
    container: str = 'S24LE'
    def __post_init__(self):
        if type(self.rate) is not int or not 44100 <= self.rate <= 768000: raise ValueError('unsupported rate')
        if self.bits not in (16,24,32) or self.channels != 2: raise ValueError('unsupported precision/channels')

from .output_devices import device_capabilities
from . import room_eq, user_filters


# Aux4 is reserved for the internal handoff gate. Main volume/mute remain user-owned.
GATE = '_HiFiMateHandoff'
FADER = 4

class SafetyError(RuntimeError):
    """Output could not be reliably silenced; restart the service."""

class LinuxFormatController:
    def __init__(self, config, run=None, rpc=None):
        self.config=config; self.run=run or self._run; self.rpc=rpc or self._rpc
        self.owner=None; self.candidate=None; self.revision=0; self.candidate_revision=0
        self.ready=False; self.current=None; self.error=None
        self.binding=None; self.retired=set(); self.lock=threading.RLock()
        self.effective=None; self.fade='muted'; self.fade_until=0; self.silenced=False
        self.clock=time.monotonic; self.cancelled=threading.Event(); self.valid_intent=lambda: True
        # Room correction (room_eq.py): config/room-correction.json next to
        # the managed host-stack directory unless configured explicitly.
        base=config.get('base_config')
        self.room_path=config.get('room_correction') or (str(Path(base).parents[1]/'room-correction.json') if base else None)
        self.room_error=None
        # Personal filters (user_filters.py): config/dsp-filters.yml, likewise.
        self.user_path=config.get('dsp_filters') or (str(Path(base).parents[1]/'dsp-filters.yml') if base else None)
        self.user_error=None
        # The EQ page's A/B: every personal filter bypassed, in memory only
        # (the file is unchanged; a restart or turning it off ends it).
        self.user_bypass=False
        self.fade_ms=float(config.get('fade_ms',400))
        if not 20 <= self.fade_ms <= 2000: raise ValueError('fade_ms must be 20..2000')
        # Camilla's own silence detector pauses processing (the ALSA PCM stays
        # open); ReceiverEvents closes the output after a longer idle timeout.
        self.silence_threshold=float(config.get('silence_threshold_db',-90))
        self.silence_timeout=float(config.get('silence_timeout_s',10))
        if not -200 <= self.silence_threshold <= -40 or not 0 <= self.silence_timeout <= 60:
            raise ValueError('silence_threshold_db must be -200..-40 and silence_timeout_s 0..60')
    @staticmethod
    def _run(args):
        return subprocess.run(args,check=True,capture_output=True,text=True,timeout=5).stdout
    def _rpc(self,command,value=None):
        import websocket
        with closing(websocket.create_connection(self.config['camilla_url'],timeout=3)) as ws:
            ws.send(json.dumps(command if value is None else {command:value}))
            result=json.loads(ws.recv())[command]
            if result.get('result')!='Ok': raise RuntimeError('Camilla rejected '+command)
            return result.get('value')
    def mute(self):
        self.ready=False; self.effective=None; self.fade='muted'; self.fade_until=0
        self.current=None; self.binding=None
        if self.silenced: return
        errors=[]
        # Stop also works before a gate exists (first install / old DSP config).
        # Try both operations even if one RPC fails. Never disguise safety failure.
        for command,value in (('SetFaderMute',[FADER,True]),('Stop',None)):
            try: self.rpc(command,value)
            except Exception as exc: errors.append(f'{command}: {exc}')
        try:
            deadline=time.monotonic()+self.config.get('stop_timeout',3)
            while True:
                # Stop's RPC reply only acknowledges queueing. Camilla clears
                # active_config after joining capture AND playback threads.
                state=self.rpc('GetState'); raw=self.rpc('GetConfigJson')
                active=json.loads(raw) if isinstance(raw,str) else raw
                if state=='Inactive' and active is None: break
                if time.monotonic()>=deadline: raise RuntimeError('DSP Stop did not become inactive')
                time.sleep(.05)
        except Exception as exc: errors.append('stop readback: '+str(exc))
        if errors: raise SafetyError('; '.join(errors))
        self.silenced=True

    def select(self,identity):
        with self.lock:
            if type(identity) is not str or not identity or len(identity)>128: raise ValueError('invalid identity')
            if identity in (self.owner,self.candidate): return False
            if identity in self.retired: raise ValueError('retired owner')
            if self.candidate: self.retired.add(self.candidate)
            self.candidate=identity; self.candidate_revision=0
            return True

    def _gate(self):
        return {'type':'Volume','parameters':{'fader':'Aux4','ramp_time':self.fade_ms,'limit':0.0}}

    @staticmethod
    def _gate_step():
        return {'type':'Filter','channels':[0,1],'names':[GATE]}

    def _gate_valid(self,config):
        pipeline=config.get('pipeline') or []
        gate=config.get('filters',{}).get(GATE,{})
        return (gate.get('type')=='Volume' and gate.get('parameters')==self._gate()['parameters']
                and pipeline and self._is_gate_step(pipeline[-1]))

    @staticmethod
    def _is_gate_step(step):
        # Camilla serializes optional description/bypassed fields as null.
        return (step.get('type')=='Filter' and step.get('channels')==[0,1]
                and step.get('names')==[GATE] and not step.get('bypassed'))

    def release(self):
        """Release only a verified committed chain; DSP interpolates each sample.

        Muted Volume starts at exact zero, then ramps from its -100 dB floor.
        No Python timer writes the user's volume, or survives a subsequent Stop.
        """
        self._check_cancelled()
        if self.candidate or not self.ready or not self.current: raise RuntimeError('unverified fade release')
        self.verify_current()
        self._check_cancelled()
        if self.rpc('GetFaderMute',FADER)!=[FADER,True]: raise RuntimeError('handoff gate not muted')
        self.rpc('SetFaderVolume',[FADER,0.0])
        if self.rpc('GetFaderVolume',FADER)!=[FADER,0.0]: raise RuntimeError('handoff gain readback failed')
        user_muted=bool(self.rpc('GetMute'))
        self.rpc('SetFaderMute',[FADER,False])
        if self.rpc('GetFaderMute',FADER)!=[FADER,False]: raise RuntimeError('handoff release readback failed')
        self.effective.update(muted=user_muted,user_muted=user_muted)
        self.fade='ramping'; self.fade_until=self.clock()+self.fade_ms/1000

    def verify_current(self):
        """Recheck the chain without granting a request or changing its format."""
        if not self.ready or not self.current or not self.binding: raise RuntimeError('output is not ready')
        fmt,lane,_,device=self.current
        actual,hw,_=device_capabilities(self.config['alsa_device'],Path(self.config.get('proc_asound','/proc/asound')))
        stream,pulse,serial,_=self.binding
        if (actual!=device or not self._verified(fmt,lane,device,hw)
                or self.observe(stream,serial,pulse,lane)!=fmt):
            raise RuntimeError('current output chain changed')

    def fade_status(self):
        if self.fade=='ramping' and self.clock()>=self.fade_until:
            return 'released'  # elapsed configured ramp, not a measured PCM assertion
        return self.fade

    def status(self):
        with self.lock:
            return dict(room=room_eq.summary(self._room()) if self.room_path else None,room_error=self.room_error,
                        filters=user_filters.summary(self._user()) if self.user_path else None,filters_error=self.user_error,filters_bypassed=self.user_bypass,
                        ready=self.ready,owner=self.owner,candidate=self.candidate,
                        revision=self.revision,error=self.error,fade=self.fade_status(),
                        format=vars(self.current[0]) if self.current else None,
                        effective=copy.deepcopy(self.effective))

    def observe(self,stream,serial,pulse_input,lane):
        nodes=json.loads(self.run(['pw-dump']))
        node=next((n for n in nodes if n.get('id')==stream),None)
        if not node or str(node.get('info',{}).get('props',{}).get('object.serial'))!=str(serial): raise RuntimeError('stale source node')
        formats=node['info'].get('params',{}).get('Format',[])
        if len(formats)!=1: raise RuntimeError('source format unavailable')
        f=formats[0]; container=f['format']; fmt=AudioFormat(f['rate'],FORMATS[container],f['channels'],container)
        if pulse_input is None:
            props=node['info'].get('props',{})
            if props.get('media.class')!='Audio/Source' or props.get('node.name')!=lane or not lane.startswith('bluez_input.'):
                raise RuntimeError('not a native BlueZ audio source')
            sources=json.loads(self.run(['pactl','--format=json','list','sources']))
            matches=[i for i in sources if i.get('name')==lane and str(i.get('properties',{}).get('object.serial'))==str(serial)]
            if len(matches)!=1: raise RuntimeError('native source Pulse identity mismatch')
            spec=matches[0].get('sample_specification','').split()
            if len(spec)!=3 or FORMATS.get(spec[0])!=fmt.bits or spec[1]!='2ch' or spec[2]!=f'{fmt.rate}Hz':
                raise RuntimeError('native source format disagreement')
            return fmt
        inputs=json.loads(self.run(['pactl','--format=json','list','sink-inputs']))
        sinks=json.loads(self.run(['pactl','--format=json','list','sinks']))
        sink=next((s for s in sinks if s.get('name')==lane),None)
        inp=next((i for i in inputs if i.get('index')==pulse_input),None)
        if not inp or not sink or inp.get('sink')!=sink['index'] or str(inp.get('properties',{}).get('object.serial'))!=str(serial): raise RuntimeError('source lane/serial mismatch')
        if inp.get('corked',False): raise RuntimeError('source stream corked')
        spec=inp.get('sample_specification','').split()
        if len(spec)!=3 or spec[0] not in FORMATS or FORMATS[spec[0]]!=fmt.bits or spec[1]!='2ch' or spec[2]!=f'{fmt.rate}Hz': raise RuntimeError('Pulse/PW format disagreement')
        return fmt
    def processing_state(self):
        return self.rpc('GetState')
    def graph_rate(self, lane):
        # Format.rate is an adapter's port format, not the running graph clock.
        # PipeWire's profiler reports the effective driver rate per running node.
        text = self.run(['pw-top', '-b', '-n', '2'])
        rate = None
        for line in text.splitlines():
            fields = line.split()
            if len(fields) >= 5 and fields[0] == 'R' and fields[-1] == lane:
                rate = int(fields[3])
        return rate

    def _check_cancelled(self):
        if self.cancelled.is_set(): raise RuntimeError('format controller is stopping')
        if not self.valid_intent(): raise RuntimeError('receiver intent superseded')

    def _verified(self,fmt,lane,device,hw):
        raw=self.rpc('GetConfigJson')
        actual=json.loads(raw) if isinstance(raw,str) else raw
        d=(actual or {}).get('devices',{})
        capture=d.get('capture') or {}; playback=d.get('playback') or {}
        params=hw.read_text()
        return (self.graph_rate(lane)==fmt.rate
                and d.get('samplerate')==fmt.rate and d.get('capture_samplerate')==fmt.rate
                and capture.get('type')=='Pulse' and capture.get('channels')==2
                and capture.get('device')==(lane if lane.startswith('bluez_input.') else lane+'.monitor')
                and playback.get('type')=='Alsa' and playback.get('channels')==2
                and playback.get('device')==device and playback.get('format')=='S32_LE'
                and not d.get('resampler') and not d.get('enable_rate_adjust')
                # Paused is Camilla's silence detector on an open, configured chain.
                and self._gate_valid(actual) and self.rpc('GetState') in ('Running','Paused')
                and re.search(r'^rate:\s*'+str(fmt.rate)+r'\s',params,re.M)
                and re.search(r'^format:\s*S32_LE\s*$',params,re.M)
                and re.search(r'^channels:\s*2\s*$',params,re.M))

    def _configuration(self,fmt,lane,base,device):
        config=copy.deepcopy(base); d=config['devices']
        d.update(samplerate=fmt.rate,capture_samplerate=fmt.rate,enable_rate_adjust=False,resampler=None)
        # Keep a useful processing period at 768 kHz rather than 3000 wakes/s.
        d['chunksize']=next((n for n in (256,512,1024,2048,4096,8192,16384) if n/fmt.rate>=.005),16384)
        d['capture']={'type':'Pulse','device':(lane if lane.startswith('bluez_input.') else lane+'.monitor'),'channels':2}
        d['playback']={'type':'Alsa','device':device,'channels':2,'format':'S32_LE'}
        if not d.get('silence_timeout'):  # keep a user's own silence settings
            d.update(silence_threshold=self.silence_threshold,silence_timeout=self.silence_timeout)
        if config.get('filters') and any(x.get('type')=='Conv' for x in config['filters'].values()):
            profiles=self.config.get('fir_profiles',{})
            if str(fmt.rate) not in profiles: raise RuntimeError('FIR profile missing for rate')
            config['filters']=copy.deepcopy(profiles[str(fmt.rate)])
        filters=config['filters']=config.get('filters') or {}
        for name,item in filters.items():
            if name!=GATE and item.get('parameters',{}).get('fader')=='Aux4':
                raise RuntimeError('Aux4 is reserved for the HiFiMate handoff')
        filters[GATE]=self._gate()
        pipeline=config.get('pipeline') or []
        # A prior managed gate must occur only as the final, unbypassed stage.
        without=[]
        for step in pipeline:
            if GATE in step.get('names',[]):
                if not self._is_gate_step(step): raise RuntimeError('modified internal handoff stage')
            else: without.append(step)
        config['pipeline']=without+[self._gate_step()]
        config=self._personal(config)
        self._check_cancelled()
        self.rpc('ValidateConfigJson',json.dumps(config))
        return config

    def _room(self):
        """The room correction to apply; an unreadable file applies none and is reported."""
        if not self.room_path: return None
        try:
            room=room_eq.load(self.room_path); self.room_error=None; return room
        except (OSError,ValueError) as exc:
            self.room_error=str(exc); return None

    def _user(self):
        """The personal filters to apply; an unreadable file applies none and is reported."""
        if not self.user_path: return None
        try:
            user=user_filters.load(self.user_path); self.user_error=None
        except (OSError,ValueError) as exc:
            self.user_error=str(exc); return None
        if user and self.user_bypass:
            user={**user,'pipeline':[{**step,'bypassed':True} for step in user['pipeline']]}
        return user

    def _personal(self,config):
        """Preset steps, then personal filters, then room correction, then the gate."""
        config=user_filters.apply(room_eq.strip(config),self._user(),before=GATE)
        return room_eq.apply(config,self._room(),before=GATE)

    def apply_room(self):
        """Put the current personal filters and room correction into the running DSP.

        Devices are unchanged, so Camilla swaps the pipeline without reopening
        the DAC; the handoff gate stays last with its fader untouched. When no
        chain is up, the next configuration picks the file up anyway.
        """
        with self.lock:
            if not (self.ready and self.owner and self.current) or self.candidate: return False
            raw=self.rpc('GetConfigJson')
            active=json.loads(raw) if isinstance(raw,str) else raw
            if not active or not self._gate_valid(active): raise RuntimeError('DSP config not owned by HiFiMate')
            config=self._personal(active)
            if not self._gate_valid(config): raise RuntimeError('DSP filters would move the handoff gate')
            # Camilla reads back every optional field it was not given (description,
            # bypassed, ...) as null, so compare with its own normalized form.
            raw=self.rpc('ValidateConfigJson',json.dumps(config)); expected=json.loads(raw) if isinstance(raw,str) else raw
            if not isinstance(expected,dict): raise RuntimeError('DSP did not return the validated config')
            self.rpc('SetConfigJson',json.dumps(config))
            raw=self.rpc('GetConfigJson'); actual=json.loads(raw) if isinstance(raw,str) else raw
            if not isinstance(actual,dict) or room_eq.strip(actual)!=room_eq.strip(expected) or sorted(actual.get('filters',{}))!=sorted(expected['filters']):
                raise RuntimeError('DSP filter readback mismatch')
            if self.rpc('GetFaderMute',FADER)!=[FADER,False] or self.rpc('GetFaderVolume',FADER)!=[FADER,0.0]:
                raise RuntimeError('handoff gate changed while applying room correction')
            return True

    def _apply(self,fmt,config):
        self._check_cancelled()
        self.mute()
        self.rpc('SetFaderVolume',[FADER,0.0])
        if self.rpc('GetFaderMute',FADER)!=[FADER,True]: raise RuntimeError('handoff mute readback failed')
        # The rate is forced per graph group by the DSP's own capture stream
        # (PULSE_PROP=node.force-rate=0 in hifimate-camilladsp.service): only the
        # selected lane + DSP group follows fmt.rate. A global clock.force-rate
        # would also force idle lanes, e.g. start a phone's A2DP node in a
        # 96 kHz group, which PipeWire 1.4 cannot recover when moved to 48 kHz.
        self._check_cancelled()
        self.silenced=False  # Even a lost SetConfig reply may have started DSP.
        self.rpc('SetConfigJson',json.dumps(config))

    def change(self,identity,revision,stream,pulse_input,serial=None,lane=None,hold=False):
        with self.lock:
            target=self.candidate or self.owner
            prior_revision=self.candidate_revision if self.candidate else self.revision
            if identity!=target or type(revision) is not int or revision<=prior_revision: return False
            if serial is None or (lane not in self.config['lanes'] and not (pulse_input is None and isinstance(lane,str) and lane.startswith('bluez_input.'))): raise ValueError('explicit source serial and configured lane required')
            if self.candidate: self.candidate_revision=revision
            else: self.revision=revision
            try:
                self._check_cancelled()
                fmt=self.observe(stream,serial,pulse_input,lane)
                device,hw,rates=device_capabilities(self.config['alsa_device'],Path(self.config.get('proc_asound','/proc/asound')))
                allowed=self.config.get('rates') or rates
                if fmt.rate not in rates or fmt.rate not in allowed: raise RuntimeError('unsupported DAC rate')
                raw=self.rpc('GetConfigJson')
                base=json.loads(raw) if isinstance(raw,str) else raw
                if not base:
                    # Stop clears active config: continue from Camilla's previous
                    # one across idle and daemon restarts. Personal filters and room
                    # correction are re-applied from their files either way.
                    previous=self.rpc('GetPreviousConfig')
                    if isinstance(previous,str):
                        import yaml
                        base=yaml.safe_load(previous)
                    else: base=previous
                if not base:
                    import yaml
                    base=yaml.safe_load(Path(self.config['base_config']).read_text())
                # Read-only prepare and ValidateConfig happen before disrupting old output.
                config=self._configuration(fmt,lane,base,device)
                self._apply(fmt,config)
                deadline=time.monotonic()+self.config.get('timeout',3)
                while True:
                    self._check_cancelled()
                    if self._verified(fmt,lane,device,hw): break
                    if time.monotonic()>=deadline: raise RuntimeError('specified graph/DSP/DAC readback failed')
                    time.sleep(.05)
                if self.observe(stream,serial,pulse_input,lane)!=fmt: raise RuntimeError('source changed during format transaction')
                self._check_cancelled()
                if self.owner and self.owner!=identity: self.retired.add(self.owner)
                self.owner=identity; self.candidate=None; self.revision=revision
                self.current=(fmt,lane,config,device); self.binding=(stream,pulse_input,serial,lane)
                self.ready=True; self.error=None
                self.effective={'graph_rate':fmt.rate,'dsp_rate':fmt.rate,'dac_rate':fmt.rate,'dac_container':'S32_LE','source_container':fmt.container,'source_container_bits':fmt.bits,'resampled':False,'muted':True,'user_muted':bool(self.rpc('GetMute'))}
                if not hold: self.release()
                return True
            except Exception as exc:
                self.error=str(exc); self.current=None; self.binding=None
                self.mute()
                raise
    def refresh(self):
        with self.lock:
            if self.candidate or not self.owner or not self.binding: return
            stream,pulse,serial,lane=self.binding
            try:
                fmt=self.observe(stream,serial,pulse,lane)
                if self.current and fmt==self.current[0] and self.ready:
                    device,hw,_=device_capabilities(self.config['alsa_device'],Path(self.config.get('proc_asound','/proc/asound')))
                    if (self._verified(fmt,lane,device,hw)
                            and self.rpc('GetFaderMute',FADER)==[FADER,False]
                            and self.rpc('GetFaderVolume',FADER)==[FADER,0.0]):
                        user_muted=bool(self.rpc('GetMute'))
                        self.effective.update(muted=user_muted,user_muted=user_muted)
                        return
                self.change(self.owner,self.revision+1,stream,pulse,serial,lane)
            except Exception as exc:
                self.error=str(exc); self.current=None; self.binding=None; self.mute(); raise
    def idle(self):
        with self.lock:
            self.mute(); self.run(['pw-metadata','-n','settings','0','clock.force-rate','0'])
            if self.owner: self.retired.add(self.owner)
            if self.candidate: self.retired.add(self.candidate)
            self.owner=None; self.candidate=None; self.binding=None; self.current=None
