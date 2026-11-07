from hifimate import *

def test_all_twelve_preemptions():
    for a in Source:
      for b in Source:
       if a==b: continue
       be=MemoryBackend(); c=Controller(be)
       assert c.play(Intent(a,'a',1)); assert c.play(Intent(b,'b',1)); assert c.owner.source is b; assert a in be.pauses

def test_duplicates_old_and_stop_no_restore():
    b=MemoryBackend(); c=Controller(b); i=Intent(Source.DLNA,'s',2)
    assert c.play(i); assert not c.play(i); assert not c.play(Intent(Source.DLNA,'s',1))
    assert c.stop(Source.DLNA,'s',2); assert c.owner is None and c.idle
    assert not c.write(Source.DLNA,[1.0])

def test_prepare_or_grant_failure_does_not_own():
    b=MemoryBackend(); b.fail_prepare.add(Source.AIRPLAY); c=Controller(b)
    assert not c.play(Intent(Source.AIRPLAY,'x',1)); assert c.owner is None
    b=MemoryBackend(); b.fail_grant.add(Source.BLUETOOTH); c=Controller(b)
    assert not c.play(Intent(Source.BLUETOOTH,'x',1)); assert c.owner is None

def test_fade_spectrum_and_normal_zero_samples():
    b=MemoryBackend(); c=Controller(b); assert c.play(Intent(Source.BLUETOOTH,'x',1))
    assert c.write(Source.BLUETOOTH,[1,0,0,0]); first=b.output[-1][1]; assert 0<first[0]<1
    c.write(Source.BLUETOOTH,[0,0,0,0]); assert c.spectrum.source is Source.BLUETOOTH

def test_wrong_stop_cannot_revoke_new_owner():
    b=MemoryBackend(); c=Controller(b); assert c.play(Intent(Source.DLNA,'a',1)); assert c.play(Intent(Source.AIRPLAY,'b',1)); assert not c.stop(Source.DLNA,'a',1); assert c.owner.source is Source.AIRPLAY

def test_failed_handover_keeps_old_output_and_does_not_pause_it():
    b=MemoryBackend(); c=Controller(b)
    assert c.play(Intent(Source.DLNA, 'old', 1))
    b.fail_prepare.add(Source.AIRPLAY)
    assert not c.play(Intent(Source.AIRPLAY, 'new', 1))
    assert c.owner.source is Source.DLNA
    assert b.pauses == []

def test_failed_reconnect_does_not_revoke_same_lane_owner():
    b=MemoryBackend(); c=Controller(b)
    assert c.play(Intent(Source.DLNA, 'old', 1))
    b.fail_grant.add(Source.DLNA)
    assert not c.play(Intent(Source.DLNA, 'old', 2))
    assert c.owner.generation == 1
    assert b.revokes == []

def test_same_lane_reconnect_does_not_revoke_new_graph():
    b=MemoryBackend(); c=Controller(b)
    assert c.play(Intent(Source.BLUETOOTH, 'device', 1))
    assert c.play(Intent(Source.BLUETOOTH, 'device', 2))
    assert c.owner.generation == 2
    assert b.pauses == []

def test_string_source_and_invalid_pcm_are_handled():
    b=MemoryBackend(); c=Controller(b)
    assert c.play(Intent('airplay', 'phone', 1))
    try:
        c.write('airplay', [float('nan')])
    except ValueError:
        pass
    else:
        raise AssertionError('NaN PCM must be rejected')

def test_old_pcm_and_normal_silence_do_not_change_ownership():
    b=MemoryBackend(); c=Controller(b)
    assert c.play(Intent(Source.DLNA, 'old', 1))
    assert c.play(Intent(Source.AIRPLAY, 'new', 1))
    assert not c.write(Source.DLNA, [1.0, 1.0])
    assert c.owner.source is Source.AIRPLAY
    assert c.write(Source.AIRPLAY, [0.0, 0.0])
    assert c.owner.source is Source.AIRPLAY

def test_stopped_source_needs_new_generation_to_reclaim():
    b=MemoryBackend(); c=Controller(b)
    assert c.play(Intent(Source.DLNA, 'old', 1))
    assert c.stop(Source.DLNA, 'old', 1)
    assert not c.play(Intent(Source.DLNA, 'old', 1))
    assert c.play(Intent(Source.DLNA, 'old', 2))
