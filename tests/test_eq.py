"""The EQ page's backend: the file, versions, imports, curves and the HTTP API."""
import json
import os
from pathlib import Path

import pytest

np = pytest.importorskip('numpy')
from hifimate import eq, user_filters  # noqa: E402
from test_ctrl import FakeCollector, ROOT, login, request, serving  # noqa: E402

MINE = '''filters:
  bass: {type: Biquad, parameters: {type: Peaking, freq: 52, gain: 2.96, q: 1.7}}
  lift: {type: Gain, parameters: {gain: 2.0}}
pipeline:
  - {type: Filter, channels: [0, 1], names: [bass]}
  - {type: Filter, channels: [1], names: [lift]}
'''


class FlatModel:
    """A stand-in for the offline CamillaDSP: each channel's gain is the sum of its
    Gain filters (not bypassed); everything else is flat. Records what it ran."""
    def __init__(self):
        self.runs = []

    def response(self, config, rate, size):
        self.runs.append(config)
        h = np.zeros((2, 2, size // 2 + 1), dtype=complex)
        for c in (0, 1):
            db = sum(config['filters'][n]['parameters']['gain'] for step in config['pipeline'] if not step.get('bypassed')
                     for n in step['names'] if c in step['channels'] and config['filters'][n]['type'] == 'Gain')
            h[c, c] = 10 ** (db / 20)
        return h


def prefix_with(tmp_path, text=MINE):
    (tmp_path / 'config/host-stack').mkdir(parents=True)
    (tmp_path / 'config/host-stack/camilladsp.yml').write_text(
        'filters:\n  Headroom: {type: Gain, parameters: {gain: -6.0}}\npipeline: []\n')
    if text is not None:
        (tmp_path / 'config/dsp-filters.yml').write_text(text)
    return tmp_path


def test_read_save_and_versions(tmp_path):
    prefix = prefix_with(tmp_path)
    state = eq.read(prefix)
    assert state['error'] is None and list(state['config']['filters']) == ['bass', 'lift'] and state['versions'] == []
    config = state['config']
    config['filters']['bass']['parameters']['gain'] = 1.5
    clock = iter([1_800_000_000 + i for i in range(30)])
    tag = eq.save(prefix, config, state['etag'], clock=lambda: next(clock))
    text = (prefix / 'config/dsp-filters.yml').read_text()
    assert text.startswith('# Personal DSP filters') and '"gain": 1.5' in text
    assert eq.parse_text(text) == user_filters.validate(config)                    # the written file is valid
    assert user_filters.load(prefix / 'config/dsp-filters.yml') == user_filters.validate(config)
    assert len(eq.read(prefix)['versions']) == 1                                   # the old file was kept
    with pytest.raises(eq.Conflict):                                               # someone edited it meanwhile
        eq.save(prefix, config, state['etag'])
    for n in range(12):
        config['filters']['bass']['parameters']['gain'] = n
        tag = eq.save(prefix, config, tag, clock=lambda: next(clock))
    versions = eq.read(prefix)['versions']
    assert len(versions) == eq.VERSIONS and versions[0]['created'] >= versions[-1]['created']
    assert eq.version(prefix, versions[0]['id'])['filters']['bass']['parameters']['gain'] == 10
    for bad in ('../x', '', None, '20260101-000000-9999'):
        with pytest.raises(ValueError):
            eq.version(prefix, bad)


def test_a_broken_file_is_shown_not_lost(tmp_path):
    prefix = prefix_with(tmp_path, 'filters: {x: {type: Volume, parameters: {}}}\n')
    state = eq.read(prefix)
    assert 'Volume' in state['error'] or 'type' in state['error']
    assert state['config'] == {'filters': {}, 'pipeline': []} and 'Volume' in state['text']
    with pytest.raises(ValueError):
        eq.save(prefix, {'filters': {'v': {'type': 'Volume', 'parameters': {}}}, 'pipeline': []}, state['etag'])
    # a first file (none yet) is created
    empty = prefix_with(tmp_path / 'new', None)
    tag = eq.read(empty)['etag']
    eq.save(empty, eq.parse_text(MINE), tag)
    assert (empty / 'config/dsp-filters.yml').is_file() and eq.read(empty)['versions'] == []


def test_text_mode_errors_are_readable():
    with pytest.raises(ValueError, match='YAML'):
        eq.parse_text('filters: [unclosed\n')
    with pytest.raises(ValueError):
        eq.parse_text('mixers: {}\n')
    assert eq.parse_text('') == {'filters': {}, 'pipeline': []}


def test_import_rew_autoeq_and_equalizer_apo():
    text = '''Preamp: -6.2 dB
Filter 1: ON PK Fc 105 Hz Gain -2.3 dB Q 0.70
Filter 2: ON LSC Fc 105 Hz Gain 6.0 dB Q 0.70
Filter 3: ON HSC Fc 10 kHz Gain -3.0 dB Q 0.70
Filter 4: OFF PK Fc 3000 Hz Gain 2 dB Q 2
Filter 5: ON LS 6dB Fc 80 Hz Gain 3 dB
Filter 6: ON HP Fc 20 Hz Q 0.707
Filter 7: ON NO Fc 60 Hz
Channel: R
Filter 8: ON PK Fc 250 Hz Gain -4 dB BW Oct 1.0
Filter 9: ON XX Fc 250 Hz
nonsense'''
    config, skipped = eq.import_text(text)
    p = {name: f['parameters'] for name, f in config['filters'].items()}
    assert p['eq_preamp_1'] == {'gain': -6.2}
    assert p['eq_2'] == {'type': 'Peaking', 'freq': 105.0, 'gain': -2.3, 'q': 0.7}
    assert p['eq_3']['type'] == 'Lowshelf' and p['eq_4'] == {'type': 'Highshelf', 'freq': 10000.0, 'gain': -3.0, 'q': 0.7}
    assert p['eq_6'] == {'type': 'Lowshelf', 'freq': 80.0, 'gain': 3.0, 'slope': 6.0}
    assert p['eq_7'] == {'type': 'Highpass', 'freq': 20.0, 'q': 0.707} and p['eq_8']['q'] == 0.7071
    assert abs(p['eq_9']['q'] - 1.4142) < 1e-3                                       # 1 octave
    steps = {s['names'][0]: s for s in config['pipeline']}
    assert steps['eq_5'].get('bypassed') is True and steps['eq_9']['channels'] == [1] and steps['eq_2']['channels'] == [0, 1]
    assert skipped == 2
    with pytest.raises(ValueError, match='没有认出'):
        eq.import_text('hello\nworld')


def test_curves_show_eq_room_total_and_clipping(tmp_path):
    model = FlatModel()
    user = eq.parse_text(MINE)
    c = eq.curves(model, user, None, head=-6.0)
    assert len(c['freq']) == eq.POINTS and c['room'] is None and c['total'] == c['eq']
    assert all(abs(v) < 1e-6 for v in c['eq']['L']) and all(abs(v - 2.0) < 1e-6 for v in c['eq']['R'])
    assert c['max_boost'] == 2.0 and c['clips'] is False and c['headroom'] == -6.0
    # what the DSP would run: the personal filters, renamed as the arbiter does
    assert all(name.startswith(user_filters.USER) for name in model.runs[-1]['filters'])
    loud = eq.parse_text(MINE.replace('gain: 2.0', 'gain: 7.5'))
    assert eq.curves(model, loud, None, head=-6.0)['clips'] is True
    room = {'enabled': True, 'channels': {'L': [{'freq': 40, 'gain': -3, 'q': 4}], 'R': []}}
    with_room = eq.curves(model, user, room, head=-6.0)
    assert with_room['room'] is not None and len(model.runs[-1]['pipeline']) > len(user['pipeline'])
    assert eq.headroom(prefix_with(tmp_path)) == -6.0 and eq.headroom(tmp_path / 'none') == 0.0


@pytest.fixture
def app(tmp_path):
    from hifimate.ctrl import App
    calls = []
    prefix = prefix_with(tmp_path)
    def request(path, payload, timeout=None):
        calls.append(payload)
        return {'ok': True}
    instance = App(prefix, FakeCollector(), lambda *a, **k: '', request, lambda prefix: None, response_model=FlatModel())
    instance.calls = calls
    return instance


def test_eq_api_reads_without_login_and_edits_with_it(app):
    with serving(app) as address:
        status, _, state = request(address, 'eq')
        assert status == 200 and state['curves']['max_boost'] == 2.0 and list(state['config']['filters']) == ['bass', 'lift']
        assert request(address, 'eq/preview', {'config': state['config']})[0] == 403       # editing needs login
        cookie = {'Cookie': login(address, app)}
        config = state['config']
        config['filters']['lift']['parameters']['gain'] = 3.0
        status, _, preview = request(address, 'eq/preview', {'config': config}, cookie)
        assert status == 200 and preview['curves']['max_boost'] == 3.0
        status, _, bad = request(address, 'eq/preview', {'config': {'filters': {'x': {'type': 'Volume', 'parameters': {}}}, 'pipeline': []}}, cookie)
        assert status == 400 and bad['error']
        status, _, applied = request(address, 'eq/apply', {'config': config, 'etag': state['etag']}, cookie)
        assert status == 200 and applied['live'] and len(applied['versions']) == 1
        assert app.calls[-1] == {'action': 'filters'}
        assert request(address, 'eq/apply', {'config': config, 'etag': state['etag']}, cookie)[0] == 409   # stale etag
        status, _, parsed = request(address, 'eq/parse', {'text': MINE}, cookie)
        assert status == 200 and parsed['config']['pipeline'][1]['channels'] == [1]
        status, _, imported = request(address, 'eq/import', {'text': 'Filter 1: ON PK Fc 100 Hz Gain 1 dB Q 1'}, cookie)
        assert status == 200 and imported['config']['filters']['eq_1']['parameters']['freq'] == 100.0
        status, _, version = request(address, 'eq/version', {'id': applied['versions'][0]['id']}, cookie)
        assert status == 200 and version['config']['filters']['lift']['parameters']['gain'] == 2.0
        status, _, by = request(address, 'eq/bypass', {'on': True}, cookie)
        assert status == 200 and by['bypass'] is True and app.calls[-1] == {'action': 'filters', 'bypass': True}
        assert request(address, 'eq/bypass', {'on': 'yes'}, cookie)[0] == 400
        assert request(address, 'eq/nothing', {}, cookie)[0] == 404
        # a large pasted export is accepted (the general limit is 8 KB)
        big = '\n'.join(f'Filter {i}: ON PK Fc {100 + i} Hz Gain 0.1 dB Q 1' for i in range(1, 60))
        assert len(big) > 2000
        assert request(address, 'eq/import', {'text': big}, cookie)[0] == 200


def test_eq_page_is_served(app):
    import http.client
    from contextlib import closing
    with serving(app) as address:
        for path, kind in (('/ctrl/eq/', 'text/html'), ('/ctrl/eq/eq.js', 'text/javascript'), ('/ctrl/eq/eq.css', 'text/css')):
            with closing(http.client.HTTPConnection(*address)) as conn:
                conn.request('GET', path); response = conn.getresponse()
                assert response.status == 200 and response.getheader('Content-Type').startswith(kind); response.read()


def test_eq_node_helpers():
    import subprocess
    result = subprocess.run(['node', str(ROOT / 'tests/eq_browser.mjs')], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
