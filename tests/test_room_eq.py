"""Room correction in the arbiter's DSP configs (fake Camilla, no audio)."""
import json
import pytest
from hifimate import room_eq
from hifimate.formats import GATE
from test_formats import fixture

ROOM = {'enabled': True, 'source': 'test', 'channels': {
    'L': [{'freq': 72.0, 'gain': -3.1, 'q': 5.0}, {'freq': 130.0, 'gain': -4.9, 'q': 5.0}],
    'R': [{'freq': 120.0, 'gain': -9.6, 'q': 4.1}]}}


def controller(tmp_path, room=ROOM):
    c, state, calls = fixture(tmp_path)
    c.room_path = str(tmp_path / 'room-correction.json')
    if room is not None:
        room_eq.save(c.room_path, room)
    return c, state, calls


def room_steps(config):
    return [s for s in config['pipeline'] if any(n.startswith(room_eq.ROOM) for n in s.get('names', []))]


def test_room_filters_join_every_config_before_the_gate(tmp_path):
    c, state, _ = controller(tmp_path)
    state['config'] = {'devices': {}, 'filters': {'Headroom': {'type': 'Gain', 'parameters': {'gain': -6.0}}},
                       'pipeline': [{'type': 'Filter', 'channels': [0, 1], 'names': ['Headroom']}]}
    c.select('a')
    for revision, rate in enumerate((44100, 768000, 44100), 1):
        state['source'] = rate
        assert c.change('a', revision, 42, 7, 99, 'lane') and c.ready
        config = state['config']
        assert config['pipeline'][0]['names'] == ['Headroom'] and config['pipeline'][-1]['names'] == [GATE]
        steps = room_steps(config)
        assert [s['channels'] for s in steps] == [[0], [1]]
        assert steps[0]['names'] == ['_HiFiMateRoom_L1', '_HiFiMateRoom_L2'] and steps[1]['names'] == ['_HiFiMateRoom_R1']
        assert config['filters']['_HiFiMateRoom_R1'] == {'type': 'Biquad', 'description': None, 'parameters': {'type': 'Peaking', 'freq': 120.0, 'gain': -9.6, 'q': 4.1}}
        assert sum(n.startswith(room_eq.ROOM) for n in config['filters']) == 3   # replaced, never stacked
    assert c.status()['room'] == {'configured': True, 'enabled': True, 'source': 'test', 'created': None, 'filters': {'L': 2, 'R': 1}}


def test_disabled_or_missing_room_adds_nothing(tmp_path):
    for i, room in enumerate(({**ROOM, 'enabled': False}, None)):
        (tmp_path / str(i)).mkdir()
        c, state, _ = controller(tmp_path / str(i), room)
        c.select('a'); assert c.change('a', 1, 42, 7, 99, 'lane')
        assert not room_steps(state['config']) and not any(n.startswith(room_eq.ROOM) for n in state['config']['filters'])


def test_broken_file_plays_without_correction_and_reports(tmp_path):
    c, state, _ = controller(tmp_path, None)
    (tmp_path / 'room-correction.json').write_text('{"enabled": true, "channels": {"L": [{"freq": 100, "gain": 6, "q": 2}]}}')
    c.select('a'); assert c.change('a', 1, 42, 7, 99, 'lane') and c.ready
    assert not room_steps(state['config'])
    assert 'gain' in c.status()['room_error']


def test_switch_updates_the_running_dsp_without_touching_the_gate(tmp_path):
    c, state, calls = controller(tmp_path, {**ROOM, 'enabled': False})
    assert c.apply_room() is False            # no chain up: nothing to do
    c.select('a'); assert c.change('a', 1, 42, 7, 99, 'lane') and c.ready   # playing: gate open
    assert state['fader_mute'] is False
    room_eq.save(c.room_path, ROOM)
    stops = sum(x == ('Stop', None) for x in calls)
    assert c.apply_room() is True
    assert len(room_steps(state['config'])) == 2 and state['config']['pipeline'][-1]['names'] == [GATE]
    assert sum(x == ('Stop', None) for x in calls) == stops          # the DAC stays open
    assert state['fader_mute'] is False and state['fader_volume'] == 0.0
    room_eq.save(c.room_path, {**ROOM, 'enabled': False})
    assert c.apply_room() is True and not room_steps(state['config'])


def test_validation_is_cut_only_and_bounded():
    room_eq.validate(ROOM)
    for bad in ({**ROOM, 'enabled': 'yes'}, {'enabled': True, 'channels': {'C': []}},
                {'enabled': True, 'channels': {'L': [{'freq': 100, 'gain': 3, 'q': 2}]}},
                {'enabled': True, 'channels': {'L': [{'freq': 100, 'gain': -3}]}},
                {'enabled': True, 'channels': {'L': [{'freq': 5, 'gain': -3, 'q': 2}]}},
                {'enabled': True, 'channels': {'L': [{'freq': 100, 'gain': -3, 'q': 2}] * 17}}):
        with pytest.raises(ValueError): room_eq.validate(bad)
    # A step that mixes a user filter with a room filter keeps the user's.
    config = {'filters': {'Mine': {}, '_HiFiMateRoom_L1': {}}, 'pipeline': [{'type': 'Filter', 'channels': [0], 'names': ['Mine', '_HiFiMateRoom_L1']}]}
    assert room_eq.strip(config) == {'filters': {'Mine': {}}, 'pipeline': [{'type': 'Filter', 'channels': [0], 'names': ['Mine']}]}


def test_ctrl_switch_needs_login_and_a_fit_then_tells_the_arbiter(tmp_path):
    from test_ctrl import FakeCollector, login, request, serving, wait_for
    from hifimate.ctrl import App
    events = []
    app = App(tmp_path, FakeCollector(), lambda *a, **k: '', lambda path, event, timeout=3: events.append(event) or {'ok': True},
              lambda prefix: None)
    with serving(app) as address:
        assert request(address, 'room', {'enabled': False})[0] == 403
        cookie = {'Cookie': login(address, app)}
        assert request(address, 'room', {'enabled': 'no'}, cookie)[0] == 400
        assert request(address, 'room', {'enabled': True}, cookie)[0] == 409       # nothing fitted yet
        path = tmp_path / 'config/room-correction.json'
        room_eq.save(path, ROOM)
        assert request(address, 'room', {'enabled': False}, cookie)[0] == 202
        wait_for(lambda: events)
    assert events == [{'action': 'room'}]
    assert room_eq.load(path)['enabled'] is False and room_eq.load(path)['channels'] == ROOM['channels']
    assert path.stat().st_mode & 0o777 == 0o600
