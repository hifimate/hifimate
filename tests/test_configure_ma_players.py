import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('configure_ma_players', ROOT / 'scripts/configure_ma_players.py')
ma = importlib.util.module_from_spec(spec); spec.loader.exec_module(ma)


def settings():
    # Shape of a real MA 2.10.7 settings.json after discovery (trimmed).
    return {'players': {
        'uuid:0000dlna': {'provider': 'dlna', 'name': None, 'default_name': 'HiFi DLNA-UPnP/AV', 'enabled': True,
                          'values': {'protocol_parent_id': 'upfake0001'}},
        'apfake000001': {'provider': 'airplay', 'name': None, 'default_name': 'HiFi AirPlay', 'enabled': True,
                           'values': {'protocol_parent_id': 'upfake0001'}},
        'upfake0001': {'provider': 'universal_player', 'default_name': 'HiFi AirPlay', 'enabled': True,
                       'values': {'linked_protocol_ids': ['uuid:0000dlna', 'apfake000001'],
                                  'volume_control': 'uuid:0000dlna'}},
        'uuid:tv': {'provider': 'dlna', 'default_name': 'BRAVIA', 'enabled': True, 'values': {}},
        'mpd_127.0.0.1_6601': {'provider': 'mpd', 'name': 'HiFi MA', 'enabled': True, 'values': {}},
    }, 'player_queues': {'mpd_127.0.0.1_6601': {'values': {'volume_normalization': 'disabled'}, 'queue_id': 'mpd_127.0.0.1_6601'}}}


def test_apply_disables_hifi_dlna_in_ma_and_normalization_once():
    data = settings()
    changes = ma.apply(data, 'HiFi')
    players = data['players']
    assert players['uuid:0000dlna']['enabled'] is False
    assert players['uuid:tv']['enabled'] is True  # other DLNA renderers untouched
    assert 'volume_control' not in players['upfake0001']['values']
    assert players['upfake0001']['values']['linked_protocol_ids'] == ['uuid:0000dlna', 'apfake000001']  # MA keeps it listed
    assert data['player_queues']['upfake0001']['values']['volume_normalization'] == 'disabled'
    assert len([c for c in changes if 'mpd' not in c.lower()]) == 3
    assert ma.apply(data, 'HiFi') == []  # idempotent


def test_apply_respects_explicit_user_normalization_and_other_names():
    data = settings()
    data['player_queues']['upfake0001'] = {'values': {'volume_normalization': 'dynamic'}, 'queue_id': 'upfake0001'}
    ma.apply(data, 'HiFi')
    assert data['player_queues']['upfake0001']['values']['volume_normalization'] == 'dynamic'
    other = settings(); assert ma.hifimate_players(other, 'Living Room') == (None, [])


def test_cli_apply_wait_and_found(tmp_path):
    assert not ma.wait(tmp_path, 'HiFi', 0, sleep=lambda _: None)
    (tmp_path / 'settings.json').write_text('{"truncated')
    assert not ma.wait(tmp_path, 'HiFi', 0, sleep=lambda _: None)  # mid-write is retried, not fatal
    (tmp_path / 'settings.json').write_text(json.dumps(settings()))
    (tmp_path / 'settings.json').chmod(0o600)
    assert ma.wait(tmp_path, 'HiFi', 0, sleep=lambda _: None)
    data = ma.load(tmp_path); ma.apply(data, 'HiFi'); ma.save(tmp_path, data)
    assert ma.load(tmp_path)['players']['uuid:0000dlna']['enabled'] is False
    assert (tmp_path / 'settings.json').stat().st_mode & 0o777 == 0o600


def test_apply_sets_up_hifimates_mpd_player_like_a_user_would():
    data = {'providers': {}, 'players': {}}
    state = {}
    changes = ma.apply(data, '客厅', [44100, 48000, 96000, 192000, 768000], state)
    provider = data['providers']['mpd']
    assert provider['domain'] == 'mpd' and provider['values']['manual_discovery_ip_addresses'] == ['127.0.0.1:6601']
    player = data['players']['mpd_127.0.0.1_6601']
    assert player['name'] == '客厅 MA'
    values = player['values']
    assert values['output_codec'] == 'wav' and values['flow_mode_sample_rate'] == 'bit_perfect'
    assert values['volume_control'] == 'native'
    # what the DAC and MA share, at 16 and 24 bit (768 kHz is not an MA option)
    assert values['sample_rates'] == ['44100||16', '44100||24', '48000||16', '48000||24',
                                      '96000||16', '96000||24', '192000||16', '192000||24']
    assert data['player_queues']['mpd_127.0.0.1_6601']['values']['volume_normalization'] == 'disabled'
    assert state == {'mpd_added': True} and changes
    assert ma.apply(data, '客厅', [44100], state) == []          # idempotent, rates not narrowed later


def test_apply_keeps_what_the_user_chose():
    data = {'providers': {'mpd--x': {'domain': 'mpd', 'values': {'manual_discovery_ip_addresses': ['10.0.0.9:6600']}}},
            'players': {'mpd_127.0.0.1_6601': {'name': 'Den', 'values': {'output_codec': 'flac', 'sample_rates': ['48000||24']}}}}
    ma.apply(data, 'HiFi', [44100, 48000], {})
    assert 'mpd' not in data['providers']                         # the user's own instance is reused
    assert data['providers']['mpd--x']['values']['manual_discovery_ip_addresses'] == ['10.0.0.9:6600', '127.0.0.1:6601']
    player = data['players']['mpd_127.0.0.1_6601']
    assert player['name'] == 'Den' and player['values']['output_codec'] == 'flac'
    assert player['values']['sample_rates'] == ['48000||24'] and player['values']['flow_mode_sample_rate'] == 'bit_perfect'


def test_a_removed_mpd_provider_is_not_added_back():
    data = {'providers': {}, 'players': {}}
    assert not any('MPD Players' in c for c in ma.apply(data, 'HiFi', [], {'mpd_added': True}))
    assert 'mpd' not in data['providers']
    # no DAC rates known (unplugged): MA's full list
    fresh = {'providers': {}, 'players': {}}
    ma.apply(fresh, 'HiFi', [], {})
    assert len(fresh['players']['mpd_127.0.0.1_6601']['values']['sample_rates']) == 16


def test_cli_remembers_what_it_added(tmp_path):
    import subprocess, sys
    (tmp_path / 'settings.json').write_text('{"providers": {}, "players": {}}')
    run = lambda: subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/configure_ma_players.py'), 'apply',
                                  str(tmp_path), '--name', 'HiFi', '--rates', '44100,96000'], capture_output=True, text=True)
    assert run().returncode == 0
    assert json.loads((tmp_path / '.hifimate-ma-players.json').read_text()) == {'mpd_added': True}
    data = json.loads((tmp_path / 'settings.json').read_text())
    del data['providers']['mpd']                                   # the user deletes it in MA
    (tmp_path / 'settings.json').write_text(json.dumps(data))
    assert run().returncode == 0
    assert 'mpd' not in json.loads((tmp_path / 'settings.json').read_text())['providers']
