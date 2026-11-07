"""Real Linux coordinator/controller, with only OS/DSP/receiver transports faked."""
import itertools
import json
import queue
import threading
from types import SimpleNamespace
import pytest
from test_formats import fixture
from hifimate.receiver_events import ReceiverEvents, LANES
from hifimate.remote_pause import Sessions
from hifimate.format_daemon import worker_loop, raise_if_fatal
from hifimate.formats import GATE, SafetyError
from hifimate.mpd import MPDEvents

DEVICE='/org/bluez/hci7/dev_AA_BB_CC_DD_EE_FF'
NATIVE='bluez_input.AA_BB_CC_DD_EE_FF.0'


def stack(tmp_path,pause_status='confirmed',clock=None,idle_timeout=300):
    c,state,calls=fixture(tmp_path); original=c.run
    sources=list(LANES); lanes=[LANES[s] if s!='bluetooth' else NATIVE for s in sources]
    state['absent']=set(); state['corked']=set(); state['rates']={s:44100 for s in sources}
    state['user_volume']=-12
    def run(args):
        calls.append(args)
        if args==['pw-top','-b','-n','2']:
            return '\n'.join(f"R {100+i} 256 {48000 if state['wrong'] else state['rate']} 0 0 0 {lane}" for i,lane in enumerate(lanes))
        if args==['pw-dump']:
            return json.dumps([{'id':42+i,'info':{'props':{'object.serial':99+i,'node.name':lane,
                'media.class':'Audio/Source' if s=='bluetooth' else 'Stream/Output/Audio'},
                'params':{'Format':[{'rate':state['rates'][s],'format':'S24LE','channels':2}]}}}
                for i,(s,lane) in enumerate(zip(sources,lanes)) if s not in state['absent']])
        if args[-1]=='sink-inputs':
            return json.dumps([{'index':7+i,'sink':1+i,'properties':{'object.serial':str(99+i)},'corked':s in state['corked'],
                'sample_specification':f"s24le 2ch {state['rates'][s]}Hz"} for i,s in enumerate(sources) if s!='bluetooth' and s not in state['absent']])
        if args[-1]=='sinks': return json.dumps([{'index':1+i,'name':lane} for i,lane in enumerate(lanes)])
        if args[-1]=='sources': return json.dumps([{'name':NATIVE,'properties':{'object.serial':102},'sample_specification':f"s24le 2ch {state['rates']['bluetooth']}Hz"}])
        return original(args)
    c.run=run; c.config['lanes']=list(LANES.values())
    registry=Sessions(); paused=[]
    def pause(previous):
        assert c.ready and c.owner.endswith('/'+b.source) and c.candidate is None
        if (previous['source'],previous.get('session'))!=(b.source,b.session):
            assert state['fader_mute']  # Handoff: committed new output is still held at zero.
        paused.append(previous)
        return {'source':previous['source'],'session':previous['session'],'requested':pause_status!='unsupported','status':pause_status}
    b=ReceiverEvents(c,pause=pause,current=registry.authorized,idle_timeout=idle_timeout,
                     **({'clock':clock} if clock else {}))
    def event(source,action='play',session=None):
        session=session or source+'-session'
        e=dict(source=source,event=action,event_id=source+action+session,session=session,
               device=DEVICE if source=='bluetooth' else 'shairport-metadata' if source=='airplay' else None,
               remote={'player':DEVICE+'/player0'} if source=='bluetooth' else {})
        registry.observe(e)
        return b.request(source,action,e['event_id'],e['device'],session,e['remote'])
    return c,state,calls,b,event,paused,registry

@pytest.mark.parametrize('old,new',list(itertools.permutations(LANES,2)))
def test_all_twelve_handoffs_commit_then_pause_old_no_autorestore(tmp_path,old,new):
    c,s,calls,b,event,paused,_=stack(tmp_path)
    event(old); assert c.owner is None and not c.ready
    b.update(); first=c.owner; assert c.ready
    event(new); assert c.owner==first and c.candidate and c.ready
    b.update(); assert c.ready and c.owner!=first and c.candidate is None
    assert [p['source'] for p in paused]==[old]
    assert b.pause_results[-1]['status']=='confirmed' and c.fade_status()=='ramping'
    assert s['config']['pipeline'][-1]['names']==[GATE]
    assert not s['fader_mute']
    owner=c.owner; rev=c.revision; count=calls.count(('SetFaderMute',[4,False]))
    for _ in range(3): b.update()
    assert c.owner==owner and c.revision==rev and calls.count(('SetFaderMute',[4,False]))==count
    assert not event(old,session=old+'-session')  # old session cannot take back
    event(new,'stop'); assert c.owner is None and not c.ready and c.effective is None
    b.update(); assert c.owner is None  # all old fake streams are still sending
    event(old,session='fresh-resume'); b.update(); assert c.ready and c.owner!=first


@pytest.mark.parametrize('other', ['airplay', 'bluetooth', 'dlna'])
def test_ma_flow_restart_edges_and_late_stop_do_not_revoke_new_owner(tmp_path, other):
    c,s,_,b,event,_,registry=stack(tmp_path)
    stream=tmp_path/'card1/stream0'
    stream.write_text(stream.read_text().replace('44100, 768000', '44100, 192000, 768000'))
    sent=[]
    def emit(e):
        registry.observe(e)
        sent.append(e)
        b.request(e['source'],e['event'],e['event_id'],session=e['session'],remote=e.get('remote'))
    mpd=MPDEvents('music_assistant_dlna',emit)
    mpd.update({'state':'stop'})
    mpd.update({'state':'play'}); b.update(); first=sent[-1]['session']
    assert c.ready and b.source=='music_assistant_dlna'
    # A visible stream restart creates a new MPD session and follows its PCM.
    mpd.update({'state':'stop'}); assert c.owner is None
    s['rates']['music_assistant_dlna']=192000
    mpd.update({'state':'play'}); b.update()
    assert sent[-1]['session']!=first and c.ready and c.current[0].rate==192000
    event(other); b.update(); owner=c.owner
    mpd.update({'state':'pause'}); b.update()
    assert c.owner==owner and c.ready
    event(other,'stop'); b.update()
    assert c.owner is None  # no fallback to the still-existing MA stream


def test_ma_new_play_after_takeover_remains_a_new_request_not_filtered_as_old_pcm(tmp_path):
    # MPD state does not identify who issued play. Characterize this limitation:
    # an upstream automatic restart AFTER takeover is indistinguishable from
    # deliberate resume. Do not hide it with a speculative debounce policy.
    c,_,_,b,event,_,registry=stack(tmp_path)
    def emit(e):
        registry.observe(e)
        b.request(e['source'],e['event'],e['event_id'],session=e['session'],remote=e.get('remote'))
    mpd=MPDEvents('music_assistant_dlna',emit)
    mpd.update({'state':'stop'}); mpd.update({'state':'play'}); b.update()
    event('airplay'); b.update(); assert b.source=='airplay'
    mpd.update({'state':'stop'}); b.update(); assert b.source=='airplay'
    mpd.update({'state':'play'}); b.update()
    assert b.source=='music_assistant_dlna' and c.ready


def test_prepare_rejection_keeps_owner_uncommitted_and_does_not_pause_old(tmp_path):
    c,s,calls,b,event,paused,_=stack(tmp_path)
    event('dlna'); b.update(); owner=c.owner
    s['rates']['airplay']=48000  # Source negotiated, DAC cannot support it.
    event('airplay'); b.update()
    assert c.owner==owner and c.candidate.endswith('/airplay') and not c.ready
    assert c.current is None and c.effective is None and s['fader_mute'] and not paused
    count=len(calls)
    for _ in range(4): b.update()
    assert not any(x==('SetFaderMute',[4,False]) for x in calls[count:])
    s['rates']['airplay']=768000; b.update()
    assert c.ready and c.owner.endswith('/airplay') and len(paused)==1


def test_validate_config_failure_occurs_before_any_clock_or_config_write(tmp_path):
    c,s,calls,b,event,paused,_=stack(tmp_path); event('dlna'); b.update()
    rpc=c.rpc
    def fail(command,value=None):
        if command=='ValidateConfigJson': raise RuntimeError('invalid user pipeline')
        return rpc(command,value)
    c.rpc=fail; calls.clear(); event('airplay'); b.update()
    assert not c.ready and not paused and c.owner.endswith('/dlna')
    assert not any(isinstance(x,tuple) and x[0]=='SetConfigJson' or isinstance(x,list) and 'clock.force-rate' in x for x in calls)


@pytest.mark.parametrize('status',['unsupported','error','accepted'])
def test_pause_result_does_not_undo_successful_routing(tmp_path,status):
    c,s,calls,b,event,paused,_=stack(tmp_path,status)
    event('airplay'); b.update(); event('dlna'); b.update()
    assert c.ready and c.owner.endswith('/dlna') and b.pause_results[-1]['status']==status


def test_same_source_format_change_user_mute_and_volume_survive_fade(tmp_path):
    c,s,calls,b,event,paused,_=stack(tmp_path)
    now=[0]; c.clock=lambda:now[0]
    event('dlna'); b.update(); owner=c.owner
    s['mute']=True; s['user_volume']=-39
    b.update(); assert c.ready and c.effective['muted']  # normal mute is not a failure
    s['rates']['dlna']=768000; b.update()
    assert c.ready and c.owner==owner and c.current[0].rate==768000 and not paused
    assert c.fade_status()=='ramping'; now[0]=1; assert c.fade_status()=='released'
    event('dlna','stop'); assert c.fade_status()=='muted'
    now[0]=100; b.tick(); assert s['fader_mute'] and not c.ready
    assert s['mute'] and s['user_volume']==-39
    assert not any(isinstance(x,tuple) and x[0] in ('SetMute','SetVolume') for x in calls)


def test_user_changes_during_remote_pause_and_no_gate_after_newer_intent(tmp_path):
    c,s,calls,b,event,paused,_=stack(tmp_path)
    event('dlna'); b.update()
    def remote(previous):
        s['mute']=True; s['user_volume']=-31
        event('bluetooth')  # newer request arrives while the old pause is in flight
        return {'status':'accepted'}
    b.pause=remote; event('airplay'); b.update()
    assert not c.ready and c.effective is None and s['fader_mute']
    # No old fade timer can restore sound after a cancellation.
    assert s['mute'] and s['user_volume']==-31


def test_gate_readback_and_same_rate_wrong_dac_do_not_fake_ready(tmp_path):
    c,s,calls,b,event,paused,_=stack(tmp_path)
    event('dlna'); b.update(); s['wrong']=True; b.update()
    assert not c.ready and c.effective is None and c.current is None
    assert s['fader_mute'] and b.error


def test_timeout_path_uncaught_exception_is_fatal_and_nonzero():
    stop=threading.Event(); fatal=[]; muted=[]
    controller=SimpleNamespace(lock=threading.RLock(),error=None,mute=lambda:muted.append(True))
    class Empty:
        def get(self,**_): raise queue.Empty
    def tick(): raise RuntimeError('timeout exploded')
    worker_loop(Empty(),stop,fatal,controller,SimpleNamespace(tick=tick),threading.Event(),Sessions())
    assert stop.is_set() and muted==[True] and 'timeout exploded' in fatal[0]
    with pytest.raises(RuntimeError,match='timeout exploded'): raise_if_fatal(fatal)


def test_timeout_silence_failure_retains_both_errors():
    stop=threading.Event(); fatal=[]
    def mute(): raise SafetyError('DSP Stop unreachable')
    controller=SimpleNamespace(lock=threading.RLock(),error=None,mute=mute)
    class Empty:
        def get(self,**_): raise queue.Empty
    worker_loop(Empty(),stop,fatal,controller,SimpleNamespace(tick=mute),threading.Event(),Sessions())
    assert stop.is_set() and len(fatal)==2
    with pytest.raises(RuntimeError,match='silence failed'): raise_if_fatal(fatal)


def test_real_pending_timeout_silence_failure_exits_worker(tmp_path):
    c,s,calls,b,event,paused,registry=stack(tmp_path)
    event('dlna'); b.update(); event('airplay'); s['absent'].add('airplay')
    b.pending=(c.candidate,-1)
    rpc=c.rpc
    def broken(command,value=None):
        if command=='Stop': raise RuntimeError('lost DSP')
        return rpc(command,value)
    c.rpc=broken; stop=threading.Event(); fatal=[]
    class Empty:
        def get(self,**_): raise queue.Empty
    worker_loop(Empty(),stop,fatal,c,b,threading.Event(),registry)
    assert stop.is_set() and fatal and not c.ready and c.effective is None


def test_waiting_for_candidate_does_not_leave_invalid_old_output_ready(tmp_path):
    c,s,calls,b,event,paused,_=stack(tmp_path)
    event('dlna'); b.update(); owner=c.owner
    s['absent'].add('airplay'); event('airplay'); b.update()
    assert c.ready and c.owner==owner and b.pending
    s['rates']['dlna']=768000; b.update()
    assert not c.ready and c.effective is None and s['fader_mute'] and not paused
    assert b.pending  # candidate can still finish; no old auto-resume


def test_gate_failure_attempts_stop_and_is_never_swallowed(tmp_path):
    c,s,calls=fixture(tmp_path); rpc=c.rpc
    def fail(command,value=None):
        if command=='SetFaderMute': raise RuntimeError('mute rejected')
        return rpc(command,value)
    c.rpc=fail
    with pytest.raises(SafetyError,match='mute rejected'): c.mute()
    assert ('Stop',None) in calls and not c.ready and c.effective is None


def test_native_config_normalization_does_not_restart_fade(tmp_path):
    c,s,calls=fixture(tmp_path); c.select('a'); c.change('a',1,42,7,99,'lane')
    s['config']['filters'][GATE]['description']=None
    s['config']['pipeline'][-1].update(description=None,bypassed=None)
    before=calls.count(('SetFaderMute',[4,False])); c.refresh()
    assert c.ready and c.revision==1 and calls.count(('SetFaderMute',[4,False]))==before


def test_stop_ack_without_thread_exit_is_fatal_not_a_new_fade(tmp_path):
    c,s,calls=fixture(tmp_path); c.select('a'); c.change('a',1,42,7,99,'lane'); rpc=c.rpc
    def deferred_stop(command,value=None):
        if command=='Stop': return  # ACK does not imply old threads have exited.
        return rpc(command,value)
    c.rpc=deferred_stop; before=calls.count(('SetFaderMute',[4,False]))
    s['source']=768000
    with pytest.raises(SafetyError,match='did not become inactive'):
        c.change('a',2,42,7,99,'lane')
    assert not c.ready and c.effective is None
    assert calls.count(('SetFaderMute',[4,False]))==before


def test_idle_resume_keeps_previous_user_dsp_pipeline(tmp_path):
    c,s,calls=fixture(tmp_path)
    s['config']['filters']={'UserEQ':{'type':'Gain','parameters':{'gain':-9.0}}}
    s['config']['pipeline']=[{'type':'Filter','channels':[0,1],'names':['UserEQ']}]
    c.select('a'); c.change('a',1,42,7,99,'lane'); c.idle()
    assert c.rpc('GetConfigJson')=='null'
    c.select('b'); c.change('b',1,42,7,99,'lane')
    assert s['config']['filters']['UserEQ']['parameters']['gain']==-9.0
    assert s['config']['pipeline'][0]['names']==['UserEQ']


@pytest.mark.parametrize('source',list(LANES))
def test_idle_timeout_pauses_sender_closes_output_and_resume_is_a_new_request(tmp_path,source):
    now=[1000.0]; clock=lambda: now[0]
    c,s,calls,b,event,paused,_=stack(tmp_path,clock=clock)
    event(source); b.update(); assert c.ready and s['active']
    s['processing']='Paused'  # sender stalled or sending digital silence
    for _ in range(5):
        now[0]+=60; b.tick()
        assert c.ready and s['active']  # still under 300 s of silence
    now[0]+=61; b.tick()
    assert not c.ready and not s['active'] and c.owner is None  # DSP stopped, ALSA closed
    assert paused[-1]['source']==source and b.pause_results[-1]['status']=='confirmed'
    assert b.source is None and ['pw-metadata','-n','settings','0','clock.force-rate','0'] in calls
    # The same session cannot reopen the output; only a new play edge can.
    s['processing']='Running'
    assert not event(source); b.update(); assert not c.ready
    event(source,session='resumed'); b.update(); assert c.ready and s['active']


def test_any_audio_restarts_the_idle_clock(tmp_path):
    now=[0.0]
    c,s,calls,b,event,paused,_=stack(tmp_path,clock=lambda: now[0])
    event('dlna'); b.update()
    for state in ('Paused','Paused','Running','Paused','Paused','Paused','Paused','Paused'):
        s['processing']=state; now[0]+=60; b.tick()
    assert c.ready and not paused  # only 300 s since the last audible chunk
    now[0]+=60; b.tick(); assert not c.ready


def test_idle_timeout_keeps_output_when_resume_would_be_unobservable(tmp_path):
    now=[0.0]
    c,s,calls,b,event,paused,_=stack(tmp_path,'unsupported',clock=lambda: now[0])
    event('airplay'); b.update(); s['processing']='Paused'
    now[0]+=1; b.tick(); now[0]+=301; b.tick()
    # Without a remote pause, a later resume would not produce a play edge.
    assert c.ready and s['active'] and b.pause_results[-1]['status']=='unsupported'
    count=len(paused)
    for _ in range(3): now[0]+=400; b.tick()
    assert len(paused)==count  # not retried for the same session
    event('airplay','stop'); assert not c.ready


def test_idle_timeout_can_be_disabled_and_is_bounded(tmp_path):
    now=[0.0]
    c,s,calls,b,event,paused,_=stack(tmp_path,clock=lambda: now[0],idle_timeout=0)
    event('dlna'); b.update(); s['processing']='Paused'
    for _ in range(5): now[0]+=3600; b.tick()
    assert c.ready and not paused
    for bad in (-1,10,90000):
        with pytest.raises(ValueError): ReceiverEvents(c,idle_timeout=bad)


@pytest.mark.parametrize('source',['airplay','dlna','music_assistant_dlna'])
def test_owner_stream_briefly_corked_keeps_the_verified_chain(tmp_path,source):
    # AirPlay resyncs (flush + cork) after the handoff's clock change. Tearing
    # the chain down for that re-stalls the graph and loops; a corked stream is
    # silence, so keep the committed output and resume when it uncorks.
    c,s,calls,b,event,paused,_=stack(tmp_path)
    event('dlna' if source!='dlna' else 'airplay'); b.update()
    event(source); b.update(); assert c.ready and c.owner.endswith('/'+source)
    owner=c.owner; configs=sum(1 for x in calls if isinstance(x,tuple) and x[0]=='SetConfigJson')
    s['corked'].add(source)
    for _ in range(3): b.update()
    assert c.ready and c.owner==owner and c.error is None and s['active']
    s['corked'].discard(source); b.update()
    assert c.ready and c.owner==owner
    assert sum(1 for x in calls if isinstance(x,tuple) and x[0]=='SetConfigJson')==configs  # no rebuild
    # A stream that really disappears is still a failure.
    s['absent'].add(source); b.update()
    assert not c.ready and 'absent' in c.error


def test_bluetooth_playback_role_stream_binds_through_the_bluetooth_lane(tmp_path):
    # WirePlumber's bluez5.media-source-role = playback (51-hifimate.conf) turns the
    # phone's A2DP node into a Stream/Output/Audio routed into bluetooth_in. It
    # must bind through that lane, not fail as a "non-Audio/Source native node".
    c,s,calls,b,event,paused,_=stack(tmp_path)
    original=c.run
    def run(args):
        if args==['pw-dump']:
            nodes=json.loads(original(args))
            for n in nodes:
                props=n['info']['props']
                if props.get('node.name')==NATIVE:
                    props['media.class']='Stream/Output/Audio'; props['api.bluez5.address']='aa:bb:cc:dd:ee:ff'
            return json.dumps(nodes)
        if args[-1]=='sink-inputs':
            inputs=json.loads(original(args))
            if 'bluetooth' not in s['absent']:
                inputs.append({'index':30,'sink':40,'properties':{'object.serial':'102','api.bluez5.address':'AA:BB:CC:DD:EE:FF'},
                               'corked':False,'sample_specification':f"s24le 2ch {s['rates']['bluetooth']}Hz"})
            return json.dumps(inputs)
        if args[-1]=='sinks': return json.dumps(json.loads(original(args))+[{'index':40,'name':'bluetooth_in'}])
        if args==['pw-top','-b','-n','2']:
            return original(args)+f"\nR 120 256 {s['rate']} 0 0 0 bluetooth_in"
        return original(args)
    c.run=run; c.config['lanes']=list(LANES.values())
    event('dlna'); b.update(); assert c.ready
    event('bluetooth'); b.update()
    assert c.ready and c.owner.endswith('/bluetooth') and c.error is None, c.error
    assert c.binding[3]=='bluetooth_in' and c.binding[1]==30
    assert s['config']['devices']['capture']['device']=='bluetooth_in.monitor'
    assert paused[-1]['source']=='dlna'


def test_bluetooth_lane_stream_from_another_device_is_rejected(tmp_path):
    c,s,calls,b,event,paused,_=stack(tmp_path)
    original=c.run
    def run(args):
        if args==['pw-dump']:
            nodes=json.loads(original(args))
            for n in nodes:
                props=n['info']['props']
                if props.get('node.name')==NATIVE:
                    props['media.class']='Stream/Output/Audio'; props['node.name']='bluez_input.11_22_33_44_55_66.1'
            return json.dumps(nodes)
        if args[-1]=='sink-inputs':
            return json.dumps(json.loads(original(args))+[{'index':30,'sink':40,'properties':{'object.serial':'102'},
                'corked':False,'sample_specification':f"s24le 2ch {s['rates']['bluetooth']}Hz"}])
        if args[-1]=='sinks': return json.dumps(json.loads(original(args))+[{'index':40,'name':'bluetooth_in'}])
        return original(args)
    c.run=run
    event('bluetooth'); b.update()
    assert not c.ready and 'not from the requesting device' in c.error


@pytest.mark.parametrize('old,new',list(itertools.permutations(LANES,2)))
def test_manual_switch_reauthorizes_retired_data_then_uses_normal_handoff(tmp_path,old,new):
    c,s,_,b,event,paused,registry=stack(tmp_path)
    original=c.run
    def running(args):
        value=original(args)
        if args==['pw-dump']:
            nodes=json.loads(value)
            for node in nodes: node['info']['state']='running'
            return json.dumps(nodes)
        return value
    c.run=running
    event(old); b.update(); first=c.owner
    event(new); b.update(); assert c.owner!=first
    # Ordinary observations/replays are still fenced. Only explicit switch can
    # grant the recorded session another intent, and only while it has data.
    assert not event(old)
    with c.lock:
        assert b.switch(old,registry)
        assert c.candidate and c.owner.endswith('/'+new)
        b.update()
    assert c.ready and c.owner.endswith('/'+old) and c.owner!=first
    assert [item['source'] for item in paused]==[old,new]
    assert registry.authorized(b.intent)
    assert b.pause_results[-1]['status']=='confirmed'


def test_manual_switch_rejects_unknown_no_request_corked_absent_and_current(tmp_path):
    c,s,_,b,event,_,registry=stack(tmp_path)
    event('dlna'); b.update(); owner=c.owner
    for source in ('unknown','airplay','dlna'):
        with pytest.raises(ValueError): b.switch(source,registry)
        assert c.owner==owner and not c.candidate
    event('airplay'); b.update(); owner=c.owner
    for status in ('corked','absent'):
        s[status].add('dlna')
        with pytest.raises(ValueError,match='活动数据流'): b.switch('dlna',registry)
        assert c.owner==owner and not c.candidate
        s[status].clear()
    registry.observe({'source':'dlna','event':'play','session':'newer-unprocessed'})
    with pytest.raises(ValueError,match='会话已变化'): b.switch('dlna',registry)
    assert c.owner==owner


def test_queued_stop_before_manual_reissue_cannot_revoke_it_but_new_stop_can(tmp_path):
    c,s,_,b,event,_,registry=stack(tmp_path)
    event('dlna'); b.update(); event('airplay'); b.update()
    old=dict(source='dlna',event='stop',event_id='queued-stop',session='dlna-session')
    registry.observe(old)  # observed already, but blocked behind controller.lock
    with c.lock:
        b.switch('dlna',registry); b.update()
    owner=c.owner
    stop=threading.Event(); fatal=[]
    class Feed:
        sent=False
        def get(self,**kwargs):
            if not self.sent: self.sent=True; return old
            stop.set(); raise queue.Empty
    worker_loop(Feed(),stop,fatal,c,b,threading.Event(),registry)
    assert not fatal and c.owner==owner and c.ready
    # A genuinely later stop is observed after the reissue and remains binding.
    event('dlna','stop'); assert c.owner is None and not c.ready
