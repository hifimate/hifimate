"""MA deployment contract; no Docker or MA process is launched."""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def test_custom_loader_keeps_ma_unprivileged_and_shares_provider_directory():
    compose = yaml.safe_load((ROOT / 'music-assistant/compose.yml').read_text())
    server = compose['services']['server']
    assert server['volumes'][1]['target'] == '/run.sh'
    assert server['volumes'][1]['read_only']
    assert server['network_mode'] == 'host'
    assert {v['target'] for v in server['volumes']} == {'/data', '/run.sh', '/share/music_assistant/custom_providers', '/media', '/etc/localtime'}
    assert next(v for v in server['volumes'] if v['target'] == '/etc/localtime')['read_only']
    assert next(v for v in server['volumes'] if v['target'] == '/media')['read_only']
    assert 'MUSIC_ASSISTANT_IMAGE' in server['image']
    subscriber = compose['services']['provider-subscriber']
    assert subscriber['environment']['SUBSCRIBER_TARGET_DIR'] == '/share/music_assistant/custom_providers'
    assert subscriber['user'] == '${MUSIC_ASSISTANT_UID:?set by the installer in .env}:${MUSIC_ASSISTANT_GID:?set by the installer in .env}'
    assert 'cap_add' not in subscriber
    assert any(v['target'] == '/share/music_assistant/custom_providers' for v in subscriber['volumes'])
    assert next(v for v in subscriber['volumes'] if v['target'] == '/etc/localtime')['read_only']
    ncm = compose['services']['ncm-api']
    assert ncm['restart'] == 'unless-stopped'
    assert ncm['user'] == '${MUSIC_ASSISTANT_UID:?set by the installer in .env}:${MUSIC_ASSISTANT_GID:?set by the installer in .env}'
    assert ncm['environment']['TZ'] == '${MUSIC_ASSISTANT_TZ:-}'
    assert next(v for v in ncm['volumes'] if v['target'] == '/etc/localtime')['read_only']
    # migu-api: built from a pinned commit, unprivileged, loopback only
    migu = compose['services']['migu-api']
    assert '#' in migu['build']['context'] and len(migu['build']['context'].split('#')[1].rstrip('}')) == 40
    assert migu['user'] == '${MUSIC_ASSISTANT_UID:?set by the installer in .env}:${MUSIC_ASSISTANT_GID:?set by the installer in .env}'
    assert migu['read_only'] and migu['cap_drop'] == ['ALL']
    assert migu['ports'] == ['${MUSIC_ASSISTANT_MIGU_API_BIND:-127.0.0.1}:${MUSIC_ASSISTANT_MIGU_API_PORT:-3004}:6200']
    assert migu['environment']['TZ'] == '${MUSIC_ASSISTANT_TZ:-}'
    assert next(v for v in migu['volumes'] if v['target'] == '/etc/localtime')['read_only']


def test_private_mpd_and_ordinary_dlna_keep_separate_queues_and_lanes():
    ma = (ROOT / 'config/host-stack/music-mpd.conf').read_text()
    dlna = (ROOT / 'config/host-stack/mpd.conf').read_text()
    assert 'bind_to_address "127.0.0.1"' in ma and 'port "6601"' in ma
    assert 'target "music_assistant_dlna_in"' in ma and 'audio_output_format' not in ma
    assert 'port "6600"' in dlna and 'target "dlna_in"' in dlna
    # Both MPD lanes keep a controller-adjustable PipeWire mixer, but no lane
    # may restore another's volume through WirePlumber's shared role key.
    assert 'mixer_type' not in ma and 'mixer_type' not in dlna
    rules = (ROOT / 'config/host-stack/wireplumber/wireplumber.conf.d/51-hifimate.conf').read_text()
    stream_rules = rules[rules.index('stream.rules'):]
    for match in ('node.name = "mpd.HiFi DLNA"', 'node.name = "mpd.HiFi Music Assistant"',
                  'application.process.binary = "shairport-sync"', 'application.name = "CamillaDSP"'):
        assert match in stream_rules
    assert 'state.restore-props = false' in stream_rules
    bluez = rules[rules.index('monitor.bluez.rules'):rules.index('stream.rules')]
    assert 'state.restore-props = false' in bluez
    assert 'name "HiFi DLNA"' in dlna
    assert 'name "HiFi Music Assistant"' in ma
    manifest = json.loads((ROOT / 'config/host-stack-manifest.json').read_text())
    assert {'hifimate-music-mpd.service', 'hifimate-mpd.service', 'hifimate-dlna.service'} <= set(manifest['units'])
    assert 'upmpdcli' in manifest['executables']


def test_mpd_units_signal_readiness_and_dlna_stop_is_bounded():
    for name in ('hifimate-mpd.service', 'hifimate-music-mpd.service'):
        unit = (ROOT / 'systemd' / name).read_text()
        assert 'Type=notify' in unit and 'Type=simple' not in unit
    dlna = (ROOT / 'systemd/hifimate-dlna.service').read_text()
    assert 'TimeoutStopSec=10' in dlna
    # upmpdcli drops an early SIGTERM: ExecStop resends it until the process exits.
    stop = next(line for line in dlna.splitlines() if line.startswith('ExecStop='))
    assert stop.count('kill -TERM "$$MAINPID"') == 1 and 'kill -0 "$$MAINPID"' in stop


def test_receiver_adapters_never_fail_on_hifi_or_mpd_dependency_results():
    """'dependency' start failures are not retried by Restart=; use soft deps."""
    def unit(name):
        lines = (ROOT / 'systemd' / name).read_text().splitlines()
        return {key: value.split() for key, _, value in (line.partition('=') for line in lines) if _}
    dlna, airplay = unit('hifimate-dlna.service'), unit('hifimate-airplay.service')
    for adapter in (dlna, airplay):
        assert not {'hifimate.service', 'hifimate-mpd.service'} & set(adapter.get('Requires', []))
        assert 'hifimate.service' in adapter['Wants'] and 'hifimate.service' in adapter['After']
    assert 'hifimate-mpd.service' in dlna['Wants'] and 'hifimate-mpd.service' in dlna['After']
    # Shairport holds hifimate.service's metadata FIFO: follow explicit stop/restart.
    assert airplay['PartOf'] == ['hifimate.service']
    assert 'RuntimeDirectoryPreserve=restart' in (ROOT / 'systemd/hifimate.service').read_text()
