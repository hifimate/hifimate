import json
import pytest
from hifimate.formats import AudioFormat, LinuxFormatController

def fixture(tmp_path):
    card=tmp_path/'card1'; card.mkdir(); (card/'id').write_text('DAC')
    (card/'stream0').write_text('Playback:\n Interface 1\n Format: S32_LE\n Channels: 2\n Rates: 44100, 768000\n')
    hw=card/'pcm0p/sub0'; hw.mkdir(parents=True)
    (card/'pcm0p/info').write_text('device: 0\nstream: PLAYBACK\nid: USB Audio\n')
    state={'rate':44100,'source':44100,'config':{'devices':{}},'wrong':False,'active':True,'fader_mute':False,'fader_volume':0.0,'mute':False}; calls=[]
    def run(args):
        calls.append(args)
        if args==['pw-top','-b','-n','2']:
            return f"R 43 256 {48000 if state['wrong'] else state['rate']} 0 0 0 lane\n"
        if args==['pw-dump']:
            return json.dumps([{'id':42,'info':{'props':{'object.serial':99},'params':{'Format':[{'rate':state['source'],'format':'S24LE','channels':2}]}}}, {'id':43,'info':{'props':{'node.name':'lane'},'params':{'Format':[{'rate':48000 if state['wrong'] else state['rate']}]}}}])
        if args[-1]=='sink-inputs': return json.dumps([{'index':7,'sink':1,'properties':{'object.serial':'99'},'sample_specification':f"s24le 2ch {state['source']}Hz"}])
        if args[-1]=='sinks': return json.dumps([{'index':1,'name':'lane'}])
        if 'clock.force-rate' in args:
            state['global_force']=int(args[-1])
            if state['global_force']: state['rate']=state['global_force']
        return ''
    def normal(text):
        # Like Camilla: optional fields left out come back as null.
        config=json.loads(text)
        for item in (config.get('filters') or {}).values(): item.setdefault('description',None)
        for step in config.get('pipeline') or []: step.setdefault('description',None); step.setdefault('bypassed',None)
        return config
    def rpc(cmd,val=None):
        calls.append((cmd,val))
        if cmd=='ValidateConfigJson': return json.dumps(normal(val))
        if cmd=='GetConfigJson': return json.dumps(state['config'] if state['active'] else None)
        if cmd=='GetPreviousConfig': return state['config']
        if cmd=='SetConfigJson':
            state['config']=normal(val); state['active']=True
            # The DSP capture stream (node.force-rate) moves its own group.
            if not state.get('global_force'):
                state['rate']=state['config']['devices'].get('capture_samplerate',state['rate'])
                (hw/'hw_params').write_text(f"rate: {state['rate']} (x)\nformat: S32_LE\nchannels: 2\n")
        if cmd=='Stop': state['active']=False
        if cmd=='GetState': return state.get('processing','Running') if state['active'] else 'Inactive'
        if cmd=='SetFaderMute': state['fader_mute']=val[1]
        if cmd=='SetFaderVolume': state['fader_volume']=val[1]
        if cmd=='GetFaderMute': return [val,state['fader_mute']]
        if cmd=='GetFaderVolume': return [val,state['fader_volume']]
        if cmd=='GetMute': return state['mute']
    return LinuxFormatController(dict(alsa_device='auto',proc_asound=str(tmp_path),lanes=['lane'],timeout=0,stop_timeout=0),run,rpc),state,calls

def test_follow_441_768_441_and_no_mix(tmp_path):
    c,s,calls=fixture(tmp_path); c.select('a')
    for i,rate in enumerate((44100,768000,44100),1):
        s['source']=rate; assert c.change('a',i,42,7,99,'lane'); assert c.ready
    assert not any('move-sink-input' in x for x in calls)
    assert s['config']['devices']['capture']['device']=='lane.monitor'
    assert s['config']['devices']['playback']['format']=='S32_LE'

def test_stale_and_nonowner(tmp_path):
    c,s,calls=fixture(tmp_path); c.select('a'); assert c.change('a',1,42,7,99,'lane')
    assert not c.select('a'); assert c.revision==1
    assert not c.change('other',2,42,7,99,'lane')
    assert not c.change('a',1,42,7,99,'lane')
    with pytest.raises(RuntimeError): c.change('a',2,42,7,100,'lane')
    assert not c.ready

def test_wrong_specified_node_muted_and_restore(tmp_path):
    c,s,calls=fixture(tmp_path); c.select('a'); c.change('a',1,42,7,99,'lane')
    s['wrong']=True; s['source']=768000
    with pytest.raises(RuntimeError): c.change('a',2,42,7,99,'lane')
    assert not c.ready and c.effective is None and c.current is None
    assert c.owner=='a' and s['fader_mute']

def test_precision_and_range():
    for bits in (16,24,32): assert AudioFormat(768000,bits).bits==bits
    with pytest.raises(ValueError): AudioFormat(768001)

def test_native_bluez_f32p_direct_capture(tmp_path):
    c,state,calls=fixture(tmp_path)
    original=c.run
    name='bluez_input.AA_BB_CC_DD_EE_FF.0'
    def run(args):
        if args==['pw-top','-b','-n','2']:
            return f"R 43 256 {48000 if state['wrong'] else state['rate']} 0 0 0 {name}\n"
        if args==['pw-dump']:
            return json.dumps([{'id':42,'info':{'props':{'object.serial':99,'node.name':name,'media.class':'Audio/Source'},'params':{'Format':[{'rate':state['source'],'format':'F32P','channels':2}]}}}])
        if args[-1]=='sources': return json.dumps([{'name':name,'properties':{'object.serial':'99'},'sample_specification':f"float32le 2ch {state['source']}Hz"}])
        return original(args)
    c.run=run; c.select('bluez')
    assert c.change('bluez',1,42,None,99,name)
    assert state['config']['devices']['capture']['device']==name
    assert c.current[0].container=='F32P'
    state['source']=768000
    assert c.change('bluez',2,42,None,99,name)
    assert state['config']['devices']['samplerate']==768000

def test_internal_gate_never_writes_main_mute(tmp_path):
    c,state,calls=fixture(tmp_path)
    for desired in (False,True):
        state['mute']=desired
        c.select(str(desired)); c.change(str(desired),1,42,7,99,'lane')
        assert state['mute'] is desired and c.ready
        c.idle(); assert state['mute'] is desired and state['fader_mute']
    assert not any(isinstance(x,tuple) and x[0] in ('SetMute','SetVolume') for x in calls)


def test_monitor_static_port_rate_does_not_define_graph_clock(tmp_path):
    c, state, calls = fixture(tmp_path)
    original = c.run
    def run(args):
        if args == ['pw-dump']:
            nodes = json.loads(original(args))
            nodes[1]['info']['params']['Format'][0]['rate'] = 48000
            return json.dumps(nodes)
        return original(args)
    c.run = run
    c.select('a')
    assert c.change('a', 1, 42, 7, 99, 'lane')
    state['source'] = 768000
    c.refresh()
    assert c.ready and c.current[0].rate == 768000
    count = sum(isinstance(x, tuple) and x[0] == 'SetConfigJson' for x in calls)
    c.refresh()
    assert sum(isinstance(x, tuple) and x[0] == 'SetConfigJson' for x in calls) == count
    assert c.effective['graph_rate'] == c.effective['dac_rate'] == 768000


def test_unsupported_rate_and_source_changed_before_unmute(tmp_path):
    c, state, calls = fixture(tmp_path)
    c.select('a')
    state['source'] = 48000
    with pytest.raises(RuntimeError, match='unsupported DAC rate'):
        c.change('a', 1, 42, 7, 99, 'lane')
    assert not c.ready
    state['source'] = 44100
    original = c.observe
    count = 0
    def observe(*args):
        nonlocal count
        count += 1
        if count == 2:
            state['source'] = 768000
        return original(*args)
    c.observe = observe
    with pytest.raises(RuntimeError, match='source changed'):
        c.change('a', 2, 42, 7, 99, 'lane')
    assert not c.ready and c.effective is None
    assert ('SetMute', False) not in calls


def test_receiver_refresh_follows_same_owner_without_new_intent(tmp_path, monkeypatch):
    from hifimate.receiver_events import ReceiverEvents, LANES
    monkeypatch.setitem(LANES, 'dlna', 'lane')
    c, state, calls = fixture(tmp_path)
    bridge = ReceiverEvents(c)
    bridge.request('dlna', 'play', 'request-1',session='one')
    bridge.update()
    owner = c.owner
    assert c.ready and c.current[0].rate == 44100
    state['source'] = 768000
    bridge.update()
    assert c.owner == owner and c.ready and c.current[0].rate == 768000
    assert not bridge.request('airplay', 'stop', 'old-stop')
    assert c.owner == owner
    bridge.request('dlna', 'stop', 'stop',session='one')
    assert c.owner is None and not c.ready
    bridge.request('dlna', 'play', 'request-2',session='two')
    bridge.update()
    assert c.owner != owner and c.ready


@pytest.mark.parametrize('field,value', [
    ('capture_samplerate', 48000), ('resampler', {'type':'Synchronous'}),
    ('enable_rate_adjust', True),
])
def test_same_rate_refresh_repairs_changed_processing(tmp_path,field,value):
    c,state,calls=fixture(tmp_path)
    c.select('a'); c.change('a',1,42,7,99,'lane')
    state['config']['devices'][field]=value
    c.refresh()
    assert c.ready and c.revision==2
    assert not state['config']['devices']['resampler']
    assert not state['config']['devices']['enable_rate_adjust']
    assert state['config']['devices']['capture_samplerate']==44100


def test_same_rate_refresh_repairs_wrong_capture_and_channels(tmp_path):
    c,state,calls=fixture(tmp_path)
    c.select('a'); c.change('a',1,42,7,99,'lane')
    state['config']['devices']['capture']['device']='wrong.monitor'
    state['config']['devices']['playback']['channels']=1
    c.refresh()
    assert c.ready and c.revision==2
    assert state['config']['devices']['capture']['device']=='lane.monitor'
    assert state['config']['devices']['playback']['channels']==2


def test_cancelled_transaction_cannot_start_or_unmute(tmp_path):
    c,state,calls=fixture(tmp_path)
    c.select('a'); c.cancelled.set()
    with pytest.raises(RuntimeError,match='stopping'):
        c.change('a',1,42,7,99,'lane')
    assert not c.ready and ('SetMute',False) not in calls
    assert not any(isinstance(x,tuple) and x[0]=='SetConfigJson' for x in calls)


def test_receiver_pending_timeout_stops_periodic_probe(tmp_path,monkeypatch):
    from hifimate.receiver_events import ReceiverEvents,LANES
    monkeypatch.setitem(LANES,'dlna','lane')
    c,state,calls=fixture(tmp_path)
    original=c.run
    def run(args):
        if args[-1]=='sink-inputs': return '[]'
        return original(args)
    c.run=run; now=[0]
    bridge=ReceiverEvents(c,timeout=1,clock=lambda:now[0])
    bridge.request('dlna','play','request',session='one')
    bridge.update(); assert bridge.pending
    now[0]=2; bridge.tick()
    assert bridge.pending is None and not c.ready
    count=len(calls); bridge.tick(); bridge.tick()
    assert len(calls)==count


def test_rpc_closes_plain_websocket_without_context_manager(monkeypatch):
    import sys
    from types import SimpleNamespace
    closed=[];sent=[]
    class WebSocket:
        def send(self,value): sent.append(json.loads(value))
        def recv(self): return '{"GetState":{"result":"Ok","value":"Inactive"}}'
        def close(self): closed.append(True)
    monkeypatch.setitem(sys.modules,'websocket',SimpleNamespace(create_connection=lambda *a,**k:WebSocket()))
    c=LinuxFormatController({'camilla_url':'ws://unused'})
    assert c.rpc('GetState')=='Inactive'
    assert sent==['GetState'] and closed==[True]


def test_s24_in_s32_pulse_spelling(tmp_path):
    c,state,calls=fixture(tmp_path);original=c.run
    def run(args):
        value=original(args)
        if args==['pw-dump']:return value.replace('S24LE','S24_32LE')
        if args[-1]=='sink-inputs':return value.replace('s24le','s24-32le')
        return value
    c.run=run;c.select('a')
    assert c.change('a',1,42,7,99,'lane')
    assert c.current[0].bits==24


def test_failed_evidence_does_not_reconfigure_from_own_events(tmp_path,monkeypatch):
    from hifimate.receiver_events import ReceiverEvents,LANES
    monkeypatch.setitem(LANES,'dlna','lane')
    c,state,calls=fixture(tmp_path)
    bridge=ReceiverEvents(c)
    state['source']=48000  # Not advertised by this fake DAC.
    bridge.request('dlna','play','request',session='one');bridge.update()
    assert not c.ready and bridge.error
    revision=c.revision
    for _ in range(5): bridge.update()
    assert c.revision==revision
    state['source']=768000
    bridge.update()
    assert c.ready and c.current[0].rate==768000 and bridge.error is None


def test_camilla_silence_pause_is_configured_and_still_a_verified_open_chain(tmp_path):
    c,s,calls=fixture(tmp_path); c.select('a'); assert c.change('a',1,42,7,99,'lane')
    d=s['config']['devices']
    assert d['silence_threshold']==-90 and d['silence_timeout']==10
    s['processing']='Paused'  # Camilla's silence detector; ALSA PCM still open
    c.verify_current(); assert c.ready and c.processing_state()=='Paused'
    # A user's own silence settings in the DSP config are kept.
    s['config']['devices'].update(silence_threshold=-70,silence_timeout=3)
    s['processing']='Running'; s['source']=768000; assert c.change('a',2,42,7,99,'lane')
    assert s['config']['devices']['silence_threshold']==-70 and s['config']['devices']['silence_timeout']==3


@pytest.mark.parametrize('options',[{'silence_threshold_db':-20},{'silence_timeout_s':-1},{'silence_timeout_s':120}])
def test_silence_settings_are_bounded(options):
    with pytest.raises(ValueError):
        LinuxFormatController(dict(alsa_device='auto',**options),lambda a:'',lambda c,v=None:None)


def test_rate_is_forced_per_dsp_group_never_globally(tmp_path):
    # A global clock.force-rate also forces idle lanes: a phone's A2DP node
    # starting in a 96 kHz group is not recoverable when moved to 48 kHz.
    c,s,calls=fixture(tmp_path); c.select('a')
    for i,rate in enumerate((44100,768000,44100),1):
        s['source']=rate; assert c.change('a',i,42,7,99,'lane'); assert c.ready and s['rate']==rate
    assert not [x for x in calls if isinstance(x,list) and 'clock.force-rate' in x]  # never written
    from pathlib import Path
    unit=(Path(__file__).parents[1]/'systemd/hifimate-camilladsp.service').read_text()
    assert 'Environment=PULSE_PROP=node.force-rate=0' in unit
