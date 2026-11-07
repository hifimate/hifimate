"""Guided room correction: session, verdicts, level, fit, apply, history. No audio."""
import json
import numpy as np
import pytest
from hifimate import room_eq
from hifimate import roomfit as rf
from hifimate.ctrl import App
from test_ctrl import FakeCollector, login, request, serving
from test_roomfit import minimum_phase, pcm_wav, raw, take_samples

F = np.fft.rfftfreq(1 << 16, 1 / rf.RATE)


def room(extra_db=0.0):
    """A room with a +9 dB mode at 125 Hz (and an optional global offset)."""
    return minimum_phase(rf.peaking_db(np.maximum(F, 1), 125, 9, 4) + extra_db)


def recording(channel, level, h, gain=0.2, noise=2e-5, clip=False):
    x = take_samples(channel, level)[:, 0 if channel == 'L' else 1]
    n = len(x) + len(h) - 1
    size = 1 << int(np.ceil(np.log2(n)))
    heard = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(h, size), size)[:len(x)]
    rec = np.concatenate([np.zeros(14400), gain * heard / 10 ** (level / 20) * 10 ** (-30 / 20), np.zeros(40000)])
    rec = rec + np.random.default_rng(len(rec)).normal(0, noise, len(rec))
    if clip:
        rec = np.clip(rec * 300, -1, 1)
    return pcm_wav(rec, rf.RATE)


@pytest.fixture
def app(tmp_path):
    events = []
    instance = App(tmp_path, FakeCollector(), lambda *a, **k: '', lambda path, event, timeout=3: events.append(event) or {'ok': True},
                   lambda prefix: None, roomfit_play=lambda url: '1', roomfit_remove=lambda song: None)
    instance.events = events
    return instance


def post(address, name, data, cookie):
    status, _, body = request(address, f'roomfit/{name}', data, cookie)
    assert status == 200, body
    return body


def measure(address, cookie, sid, position, channel, level, wav):
    status, _, started = request(address, 'roomfit/start', {'channel': channel, 'level_db': level, 'session': sid,
                                 'position': position, 'device': {'echoCancellation': 'false'}}, cookie)
    assert status == 200, started
    status, _, body = raw(address, 'POST', f'/ctrl/api/roomfit/upload/{started["id"]}', wav, {**cookie, 'Content-Type': 'audio/wav'})
    assert status == 200, body
    return json.loads(body)


def run_session(address, cookie, positions=('center', 'left', 'right'), h=None):
    h = room() if h is None else h
    sid = post(address, 'session/new', {'mic': None}, cookie)['session']['id']
    level = measure(address, cookie, sid, 'level', 'L', rf.LEVEL_START_DB, recording('L', rf.LEVEL_START_DB, h))
    chosen = level['level']['level_db']
    assert level['session']['state'] == 'measure' and rf.LEVELS[0] <= chosen <= rf.LEVELS[1]
    for key in positions:
        for channel in ('L', 'R'):
            result = measure(address, cookie, sid, key, channel, chosen, recording(channel, chosen, h))
            assert result['verdict']['grade'] in ('good', 'warn'), result['verdict']
        post(address, 'session/accept', {'session': sid, 'position': key}, cookie)
    return sid


def test_guided_session_end_to_end(app):
    with serving(app) as address:
        cookie = {'Cookie': login(address, app)}
        assert request(address, 'roomfit/session/new', {'mic': None})[0] == 403
        sid = run_session(address, cookie, ('center', 'left'))
        status, _, refused = request(address, 'roomfit/session/compute', {'session': sid}, cookie)
        assert status == 409 and '3' in refused['error']                       # two positions are not enough
        h = room()
        level = json.loads((app.prefix / f'state/roomfit/sessions/{sid}.json').read_text())['level_db']
        for channel in ('L', 'R'):
            measure(address, cookie, sid, 'right', channel, level, recording(channel, level, h))
        post(address, 'session/accept', {'session': sid, 'position': 'right'}, cookie)
        computed = post(address, 'session/compute', {'session': sid}, cookie)['session']['computed']
        for channel in ('L', 'R'):
            main = min(computed['channels'][channel]['filters'], key=lambda x: x['gain'])
            assert main['freq'] == pytest.approx(125, rel=0.08) and main['gain'] < -5
        assert computed['positions'] == ['center', 'left', 'right']
        applied = post(address, 'session/apply', {'session': sid}, cookie)
        assert applied['live'] and app.events[-1] == {'action': 'room'}
        active = room_eq.load(app.prefix / 'config/room-correction.json')
        assert active['enabled'] and active['profile'] == applied['profile']['id']
        assert request(address, 'roomfit/session')[2]['session'] is None              # applied: closed
        listed = request(address, 'roomfit/profiles')[2]
        assert [p['active'] for p in listed['profiles']] == [True] and listed['enabled']


def test_bad_take_cannot_be_accepted_and_redo_replaces_it(app):
    with serving(app) as address:
        cookie = {'Cookie': login(address, app)}
        h = room()
        sid = post(address, 'session/new', {'mic': None}, cookie)['session']['id']
        measure(address, cookie, sid, 'level', 'L', rf.LEVEL_START_DB, recording('L', rf.LEVEL_START_DB, h))
        measure(address, cookie, sid, 'center', 'L', -20, recording('L', -20, h))
        bad = measure(address, cookie, sid, 'center', 'R', -20, recording('R', -20, h, clip=True))
        assert bad['verdict']['grade'] == 'bad' and any('削波' in r for r in bad['verdict']['reasons'])
        status, _, body = request(address, 'roomfit/session/accept', {'session': sid, 'position': 'center'}, cookie)
        assert status == 409 and '重录' in body['error']
        measure(address, cookie, sid, 'center', 'R', -20, recording('R', -20, h))       # 重录
        post(address, 'session/accept', {'session': sid, 'position': 'center'}, cookie)


def test_a_session_measured_through_active_correction_fits_the_bare_room(app):
    with serving(app) as address:
        cookie = {'Cookie': login(address, app)}
        first = post(address, 'session/compute', {'session': run_session(address, cookie)}, cookie)['session']['computed']
        filters = first['channels']
        room_eq.save(app.prefix / 'config/room-correction.json', {'enabled': True, 'channels': {ch: filters[ch]['filters'] for ch in 'LR'}})
        # The room as heard through that correction: the fit must undo it, not cut twice.
        corrected = room(sum(rf.peaking_db(np.maximum(F, 1), x['freq'], x['gain'], x['q']) for x in filters['L']['filters']))
        second = post(address, 'session/compute', {'session': run_session(address, cookie, h=corrected)}, cookie)['session']['computed']
        a, b = (min(c['channels']['L']['filters'], key=lambda x: x['gain']) for c in (first, second))
        assert b['freq'] == pytest.approx(a['freq'], rel=0.05) and b['gain'] == pytest.approx(a['gain'], abs=1.5)


def test_three_profiles_are_kept_and_any_can_be_restored(app):
    store = rf.Profiles(app.prefix / 'state/roomfit')
    made = []
    for i in range(4):
        made.append(store.add({'session': f's-{i}', 'mic': None, 'level_db': -20, 'positions': ['center'],
                               'channels': {'L': [{'freq': 100.0 + i, 'gain': -3.0, 'q': 2.0}], 'R': []}}))
        import time; time.sleep(1.01)       # ids carry the second
    assert [p['id'] for p in store.list()] == [p['id'] for p in made[:0:-1]]
    with serving(app) as address:
        cookie = {'Cookie': login(address, app)}
        post(address, 'profiles/activate', {'id': made[1]['id']}, cookie)
        assert room_eq.load(app.prefix / 'config/room-correction.json')['channels']['L'][0]['freq'] == 101.0
        assert request(address, 'roomfit/profiles/activate', {'id': made[0]['id']}, cookie)[0] == 400   # pruned


def test_level_and_verdict_rules():
    assert rf.recommend_level({'snr_db': 30.0, 'peak_dbfs': -40.0}, -24) == {'level_db': -16.0, 'expected_snr_db': 38.0, 'enough': True}
    near_clip = rf.recommend_level({'snr_db': 20.0, 'peak_dbfs': -10.0}, -24)
    assert near_clip['level_db'] == -20.0 and not near_clip['enough']          # headroom wins, warns
    assert rf.recommend_level({'snr_db': 10.0, 'peak_dbfs': -60.0}, -24)['level_db'] == rf.LEVELS[1]
    good = {'snr_db': 40.0, 'clipped': False, 'frequency': [100.0], 'magnitude_db': [0.0]}
    assert rf.judge(good) == {'grade': 'good', 'reasons': []}
    assert rf.judge({**good, 'snr_db': 28.0})['grade'] == 'warn'
    assert rf.judge(good, {'noiseSuppression': 'true'})['grade'] == 'bad'
    far = rf.judge(good, None, {'magnitude_db': [12.0]})
    assert far['grade'] == 'warn' and '差异' in far['reasons'][0]


def test_delete_a_profile_and_the_one_in_use_turns_correction_off(app):
    store = rf.Profiles(app.prefix / 'state/roomfit')
    import time
    old = store.add({'session': 's-a', 'mic': None, 'level_db': -20, 'positions': ['center'], 'channels': {'L': [], 'R': []}})
    time.sleep(1.01)
    used = store.add({'session': 's-b', 'mic': None, 'level_db': -20, 'positions': ['center'],
                      'channels': {'L': [{'freq': 120.0, 'gain': -5.0, 'q': 3.0}], 'R': []}})
    with serving(app) as address:
        cookie = {'Cookie': login(address, app)}
        assert request(address, 'roomfit/profiles/delete', {'id': old['id']})[0] == 403
        post(address, 'profiles/activate', {'id': used['id']}, cookie)
        assert post(address, 'profiles/delete', {'id': old['id']}, cookie)['live'] is None     # not in use: correction stays
        assert room_eq.load(app.prefix / 'config/room-correction.json')['enabled']
        assert post(address, 'profiles/delete', {'id': used['id']}, cookie)['live'] is True
        room = room_eq.load(app.prefix / 'config/room-correction.json')
        assert not room['enabled'] and room['profile'] is None and app.events[-1] == {'action': 'room'}
        assert request(address, 'roomfit/profiles')[2]['profiles'] == []
        assert request(address, 'roomfit/profiles/delete', {'id': used['id']}, cookie)[0] == 400


def test_rename_a_profile(app):
    store = rf.Profiles(app.prefix / 'state/roomfit')
    made = store.add({'session': 's-a', 'mic': None, 'level_db': -20, 'positions': ['center'], 'channels': {'L': [], 'R': []}})
    with serving(app) as address:
        cookie = {'Cookie': login(address, app)}
        assert request(address, 'roomfit/profiles/rename', {'id': made['id'], 'name': 'x'})[0] == 403
        renamed = post(address, 'profiles/rename', {'id': made['id'], 'name': '  沙发   中间 '}, cookie)['profile']
        assert renamed['name'] == '沙发 中间'
        assert request(address, 'roomfit/profiles')[2]['profiles'][0]['name'] == '沙发 中间'
        assert request(address, 'roomfit/profiles/rename', {'id': made['id'], 'name': '长' * 25}, cookie)[0] == 400
        assert request(address, 'roomfit/profiles/rename', {'id': made['id'], 'name': 3}, cookie)[0] == 400
        assert 'name' not in post(address, 'profiles/rename', {'id': made['id'], 'name': ' '}, cookie)['profile']   # cleared
