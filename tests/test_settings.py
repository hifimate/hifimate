"""The settings page: playback over hifimate.conf, provider subscriptions, the API."""
import json
from pathlib import Path

import pytest

from hifimate import settings
from test_ctrl import FakeCollector, ROOT, login, request, serving  # noqa: E402

OPTIONS = {'log_level': 'info', 'interval_minutes': 360, 'prune_removed': False, 'github_proxy': '',
           'sources': ['andychao2024/music-assistant-providers']}
STATE = {'sources': {'github:andychao2024/music-assistant-providers': {
    'ref': 'v3.6.2', 'providers': ['lxmusic', 'netease_metadata'], 'synced_at': '2026-10-25T16:48:42+00:00'}}}


def prefix_with(tmp_path):
    (tmp_path / 'config').mkdir(parents=True)
    (tmp_path / 'config/hifimate.conf').write_text(json.dumps({'alsa_device': 'auto', 'rates': [], 'fade_ms': 400}))
    (tmp_path / 'config/identity.json').write_text('{"schema": 1, "name": "客厅"}')
    sub = tmp_path / 'music-assistant/provider-subscriber'
    sub.mkdir(parents=True)
    (sub / 'options.json').write_text(json.dumps(OPTIONS)); (sub / 'options.json').chmod(0o600)
    (sub / 'state.json').write_text(json.dumps(STATE))
    (tmp_path / 'music-assistant/.hifimate-managed').write_text('hifimate-music-assistant-compose-v1\n')
    (tmp_path / 'music-assistant/.env').write_text(f'MUSIC_LIBRARY_DIR=/srv/hifimate/music\nMUSIC_ASSISTANT_PROVIDER_SUBSCRIBER_CONFIG_DIR={sub}\n')
    return tmp_path


def test_playback_settings_are_checked_and_laid_over_hifimate_conf(tmp_path):
    prefix = prefix_with(tmp_path)
    assert settings.effective(prefix / 'config/hifimate.conf')['fade_ms'] == 400
    saved = settings.save_playback(prefix / 'config', {'fade_ms': 250.4, 'idle_timeout_s': 0, 'rates': [96000, 44100]})
    assert saved == {'fade_ms': 250, 'idle_timeout_s': 0, 'rates': [44100, 96000]}
    merged = settings.effective(prefix / 'config/hifimate.conf')
    assert merged['fade_ms'] == 250 and merged['idle_timeout_s'] == 0 and merged['alsa_device'] == 'auto'
    for bad in ({'fade_ms': 5}, {'idle_timeout_s': 10}, {'idle_timeout_s': 1.5}, {'rates': [12345]}, {'rates': [44100, 44100]},
                {'alsa_device': 'hw:X'}, 'nope'):
        with pytest.raises(ValueError):
            settings.save_playback(prefix / 'config', bad)
    # a broken file is ignored: the defaults apply, the arbiter still starts
    (prefix / 'config/settings.json').write_text('{broken')
    assert settings.effective(prefix / 'config/hifimate.conf')['fade_ms'] == 400


def test_the_arbiter_reads_the_overlay(tmp_path, monkeypatch):
    prefix = prefix_with(tmp_path)
    settings.save_playback(prefix / 'config', {'fade_ms': 900})
    from hifimate import daemon, format_daemon
    seen = {}
    monkeypatch.setattr(format_daemon, 'serve', lambda s: seen.update(s) or 0)
    assert daemon.run(prefix / 'config/hifimate.conf') == 0 and seen['fade_ms'] == 900


def test_subscriptions_keep_the_rest_of_the_options(tmp_path):
    prefix = prefix_with(tmp_path)
    subs = settings.subscriptions(prefix)
    assert subs['available'] and subs['sources'][0]['ref'] == 'v3.6.2' and subs['sources'][0]['providers'] == ['lxmusic', 'netease_metadata']
    settings.save_subscriptions(prefix, ['andychao2024/music-assistant-providers', ' neqq3/ma_ncloud_music ',
                                         'https://gitee.com/andychao2020/music-assistant-providers', 'neqq3/ma_ncloud_music'], True)
    path = prefix / 'music-assistant/provider-subscriber/options.json'
    options = json.loads(path.read_text())
    assert options['sources'] == ['andychao2024/music-assistant-providers', 'neqq3/ma_ncloud_music',
                                  'https://gitee.com/andychao2020/music-assistant-providers']
    assert options['prune_removed'] is True and options['interval_minutes'] == 360 and options['github_proxy'] == ''
    assert path.stat().st_mode & 0o777 == 0o600
    assert settings.source_key('https://gitee.com/a/b.git') == 'gitee:a/b' and settings.source_key('o/r@v1') == 'github:o/r'
    for bad in (['repo-only'], ['https://example.com/a/b'], ['a/b; rm -rf /'], 'a/b', [1]):
        with pytest.raises(ValueError):
            settings.save_subscriptions(prefix, bad)
    with pytest.raises(ValueError):
        settings.save_subscriptions(prefix, [f'o/r{i}' for i in range(settings.MAX_SOURCES + 1)])


@pytest.fixture
def app(tmp_path):
    from hifimate.ctrl import App
    prefix = prefix_with(tmp_path)
    runs = []
    def runner(argv, timeout=2):
        runs.append(argv)
        return '[INFO] Loaded config\n[ERROR] github:bad/repo: not found\n[INFO] Next check in 21600 seconds.\n' if 'logs' in argv else ''
    instance = App(prefix, FakeCollector(), runner, lambda *a, **k: {'ok': True}, lambda p: None)
    instance.runs = runs
    return instance


def test_settings_api(app):
    with serving(app) as address:
        status, _, state = request(address, 'settings')
        assert status == 200 and state['playback']['effective']['fade_ms'] == 400
        assert state['subscriptions']['sources'][0]['source'] == 'andychao2024/music-assistant-providers'
        assert state['device']['name'] == '客厅' and state['device']['music'] == '/srv/hifimate/music'
        assert request(address, 'settings/playback', {'playback': {'fade_ms': 300}})[0] == 403
        cookie = {'Cookie': login(address, app)}
        status, _, reply = request(address, 'settings/playback', {'playback': {'fade_ms': 300, 'idle_timeout_s': 900}}, cookie)
        assert status == 200 and reply['saved'] == {'fade_ms': 300, 'idle_timeout_s': 900} and reply['accepted']
        assert request(address, 'settings/playback', {'playback': {'fade_ms': 1}}, cookie)[0] == 400
        assert request(address, 'settings/subscriptions', {'sources': ['bad']}, cookie)[0] == 400


def test_saving_subscriptions_syncs_then_restarts_music_assistant(app):
    import time
    with serving(app) as address:
        cookie = {'Cookie': login(address, app)}
        status, _, reply = request(address, 'settings/subscriptions', {'sources': ['neqq3/ma_ncloud_music']}, cookie)
        assert status == 200 and reply['sources'] == ['neqq3/ma_ncloud_music'] and reply['accepted']
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(argv[-2:] == ['restart', 'server'] for argv in app.runs):
            time.sleep(0.05)
    order = [argv[-2:] if 'restart' in argv else ['logs'] for argv in app.runs if 'restart' in argv or 'logs' in argv]
    assert order[0] == ['restart', 'provider-subscriber'] and order[-1] == ['restart', 'server'] and ['logs'] in order
    messages = [e['message'] for e in app.collector.events]
    assert any('bad/repo' in m for m in messages) and any('完成' in m for m in messages)


def test_settings_page_and_node_helpers(app):
    import http.client, subprocess
    from contextlib import closing
    with serving(app) as address:
        with closing(http.client.HTTPConnection(*address)) as conn:
            conn.request('GET', '/ctrl/settings/'); response = conn.getresponse()
            assert response.status == 200 and b'settings.js' in response.read()
    result = subprocess.run(['node', str(ROOT / 'tests/settings_browser.mjs')], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
