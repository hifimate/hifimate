"""Personal DSP filters (config/dsp-filters.yml) in the arbiter's configs (fake Camilla)."""
import pytest
import yaml
from hifimate import room_eq, user_filters
from hifimate.formats import GATE
from test_formats import fixture
from test_room_eq import ROOM
from test_install_scripts import INSTALL, UNINSTALL, run

MINE = """\
filters:
  speaker_peak: {type: Biquad, parameters: {type: Peaking, freq: 52, gain: 2.96, q: 1.7}}
  air_shelf: {type: Biquad, parameters: {type: Highshelf, freq: 8000, gain: 2.0, q: 0.7}}
pipeline:
  - {type: Filter, channels: [0, 1], names: [speaker_peak, air_shelf]}
"""
HEADROOM = {'devices': {}, 'filters': {'Headroom': {'type': 'Gain', 'parameters': {'gain': -6.0}}},
            'pipeline': [{'type': 'Filter', 'channels': [0, 1], 'names': ['Headroom']}]}


def controller(tmp_path, text=MINE, room=ROOM):
    c, state, calls = fixture(tmp_path)
    c.user_path = str(tmp_path / 'dsp-filters.yml'); c.room_path = str(tmp_path / 'room-correction.json')
    if text is not None: (tmp_path / 'dsp-filters.yml').write_text(text)
    if room is not None: room_eq.save(c.room_path, room)
    state['config'] = HEADROOM
    return c, state, calls


def order(config):
    return [step['names'][0] for step in config['pipeline']]


def test_every_chain_gets_headroom_then_mine_then_room_then_the_gate(tmp_path):
    c, state, _ = controller(tmp_path)
    c.select('a')
    for revision, rate in enumerate((44100, 768000, 44100), 1):
        state['source'] = rate
        assert c.change('a', revision, 42, 7, 99, 'lane') and c.ready
        config = state['config']
        assert order(config) == ['Headroom', '_HiFiMateUser_speaker_peak', '_HiFiMateRoom_L1', '_HiFiMateRoom_R1', GATE]
        assert config['filters']['_HiFiMateUser_air_shelf']['parameters']['freq'] == 8000
        assert sum(n.startswith(user_filters.USER) for n in config['filters']) == 2      # replaced, never stacked
    assert c.status()['filters'] == {'configured': True, 'filters': 2} and c.status()['filters_error'] is None


def test_turning_room_correction_off_keeps_my_filters(tmp_path):
    c, state, _ = controller(tmp_path)
    c.select('a'); assert c.change('a', 1, 42, 7, 99, 'lane')
    room_eq.save(c.room_path, {**ROOM, 'enabled': False})
    assert c.apply_room() is True
    assert order(state['config']) == ['Headroom', '_HiFiMateUser_speaker_peak', GATE]
    room_eq.save(c.room_path, ROOM)
    assert c.apply_room() is True
    assert order(state['config']) == ['Headroom', '_HiFiMateUser_speaker_peak', '_HiFiMateRoom_L1', '_HiFiMateRoom_R1', GATE]


def test_editing_the_file_replaces_my_filters_live_in_place(tmp_path):
    c, state, calls = controller(tmp_path)
    c.select('a'); assert c.change('a', 1, 42, 7, 99, 'lane')
    stops = sum(x == ('Stop', None) for x in calls)
    (tmp_path / 'dsp-filters.yml').write_text(MINE.replace('speaker_peak', 'speaker').replace('gain: 2.0', 'gain: 1.0'))
    assert c.apply_room() is True
    config = state['config']
    assert order(config) == ['Headroom', '_HiFiMateUser_speaker', '_HiFiMateRoom_L1', '_HiFiMateRoom_R1', GATE]
    assert '_HiFiMateUser_speaker_peak' not in config['filters'] and config['filters']['_HiFiMateUser_air_shelf']['parameters']['gain'] == 1.0
    assert sum(x == ('Stop', None) for x in calls) == stops                           # the DAC stays open
    (tmp_path / 'dsp-filters.yml').write_text(user_filters.TEMPLATE)                  # emptied: none left
    assert c.apply_room() is True and order(state['config']) == ['Headroom', '_HiFiMateRoom_L1', '_HiFiMateRoom_R1', GATE]


def test_a_broken_file_plays_without_my_filters_and_reports(tmp_path):
    c, state, _ = controller(tmp_path, text='filters: {x: {type: Volume, parameters: {}}}\npipeline: []\n', room=None)
    c.select('a'); assert c.change('a', 1, 42, 7, 99, 'lane') and c.ready
    assert order(state['config']) == ['Headroom', GATE]
    assert 'type' in c.status()['filters_error']


@pytest.mark.parametrize('bad', [
    'devices: {}\n',                                                                      # only filters/pipeline
    'filters: {"bad name": {type: Gain, parameters: {}}}\npipeline: [{type: Filter, channels: [0], names: ["bad name"]}]\n',
    'filters: {v: {type: Volume, parameters: {fader: Aux4}}}\npipeline: [{type: Filter, channels: [0], names: [v]}]\n',
    'filters: {g: {type: Gain, parameters: {}}}\npipeline: [{type: Mixer, name: m}]\n',
    'filters: {g: {type: Gain, parameters: {}}}\npipeline: [{type: Filter, channels: [2], names: [g]}]\n',
    'filters: {g: {type: Gain, parameters: {}}}\npipeline: [{type: Filter, channels: [0], names: [missing]}]\n',
    'filters: {g: {type: Gain, parameters: {}}}\npipeline: []\n',                       # defined, never used
    'filters: [\n',                                                                       # not YAML
])
def test_validation(tmp_path, bad):
    (tmp_path / 'f.yml').write_text(bad)
    with pytest.raises(ValueError, match='dsp-filters'):
        user_filters.load(tmp_path / 'f.yml')


def test_template_is_valid_and_empty(tmp_path):
    assert user_filters.validate(yaml.safe_load(user_filters.TEMPLATE)) == {'filters': {}, 'pipeline': []}
    assert user_filters.load(tmp_path / 'none.yml') is None
    assert user_filters.validate(yaml.safe_load(MINE))['pipeline'][0]['names'] == ['speaker_peak', 'air_shelf']


def test_install_creates_the_template_once_and_keeps_my_edits(tmp_path):
    prefix = tmp_path / 'hifimate'
    assert run(INSTALL, prefix, '--apply').returncode == 0
    path = prefix / 'config/dsp-filters.yml'
    assert path.read_text() == user_filters.TEMPLATE
    path.write_text(MINE)
    result = run(INSTALL, prefix, '--apply')
    assert result.returncode == 0, result.stderr
    assert path.read_text() == MINE and not (prefix / 'config/dsp-filters.yml.new').exists()
    assert ' config/dsp-filters.yml' not in (prefix / '.hifimate-install-manifest').read_text()   # user state, not owned


def test_uninstall_removes_the_untouched_template_but_keeps_my_filters(tmp_path):
    for edited in (False, True):
        prefix = tmp_path / f'hifi-{edited}'
        assert run(INSTALL, prefix, '--apply').returncode == 0
        if edited: (prefix / 'config/dsp-filters.yml').write_text(MINE)
        result = run(UNINSTALL, prefix, '--apply')
        assert result.returncode == 0, result.stderr
        if edited: assert (prefix / 'config/dsp-filters.yml').read_text() == MINE
        else: assert not prefix.exists()
