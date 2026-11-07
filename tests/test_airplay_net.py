import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest
from hifimate import airplay_net as net

ROOT = Path(__file__).parents[1]
TEMPLATE = 'general = { name = "HiFi AirPlay"; output_backend = "pa"; };\npa = { application_name = "HiFi AirPlay"; };\n'


def routes(v4, v6=()):
    def run(args, **_):
        data = v4 if '-4' in args else v6
        return SimpleNamespace(stdout=json.dumps(list(data)))
    return run


def test_default_interface_prefers_lowest_metric_ipv4_then_ipv6():
    assert net.default_interface(routes([{'dev': 'wlp3s0', 'metric': 600}, {'dev': 'enp2s0', 'metric': 100}])) == 'enp2s0'
    assert net.default_interface(routes([], [{'dev': 'eth0', 'metric': 1024}])) == 'eth0'
    assert net.default_interface(routes([], [])) is None
    assert net.default_interface(routes([{'dev': 'bad name;rm', 'metric': 1}])) is None


def test_render_config_pins_interface_and_respects_an_explicit_one():
    text = net.render_config(TEMPLATE, 'wlp3s0')
    assert text.startswith('general = { interface = "wlp3s0"; name = "HiFi AirPlay"')
    assert net.configured_interface(text) == 'wlp3s0'
    assert net.render_config(text, 'enp2s0') == text  # user/explicit setting wins
    assert net.render_config(TEMPLATE, None) == TEMPLATE  # offline: automatic choice
    with pytest.raises(ValueError):
        net.render_config(TEMPLATE, 'x"; y')
    with pytest.raises(ValueError):
        net.render_config('pa = {};\n', 'eth0')


def test_render_writes_private_runtime_copy(tmp_path):
    source = tmp_path / 'shairport-sync.conf'; source.write_text(TEMPLATE)
    target = tmp_path / 'run/hifimate-airplay/shairport-sync.conf'
    assert net.render(source, target, routes([{'dev': 'wlp3s0', 'metric': 600}])) == 'wlp3s0'
    assert net.configured_interface(target.read_text()) == 'wlp3s0'
    assert target.stat().st_mode & 0o777 == 0o600 and source.read_text() == TEMPLATE


def test_watch_restarts_only_when_the_default_route_interface_changes(tmp_path):
    target = tmp_path / 'shairport-sync.conf'
    target.write_text(net.render_config(TEMPLATE, 'wlp3s0'))
    current = ['wlp3s0']; restarts = []
    def run(args, **_):
        if args[0] == 'systemctl':
            restarts.append(args); return SimpleNamespace(returncode=0)
        return routes([{'dev': current[0], 'metric': 100}])(args)
    read, write = os.pipe()
    stream = os.fdopen(read)
    monitor = SimpleNamespace(stdout=stream, terminate=lambda: None)
    clock = [0.0]
    net.watch(target, 'hifimate-airplay.service', run=run, popen=lambda *a, **k: monitor,
              clock=lambda: clock[0], settle=0, once=True)
    assert restarts == []  # same interface: no restart
    current[0] = 'enp2s0'; os.write(write, b'default via 10.0.0.1 dev enp2s0\n')
    net.watch(target, 'hifimate-airplay.service', run=run, popen=lambda *a, **k: monitor,
              clock=lambda: clock[0], settle=0, once=True)
    assert restarts == [['systemctl', '--user', 'try-restart', 'hifimate-airplay.service']]
    target.unlink(); restarts.clear()
    net.watch(target, 'hifimate-airplay.service', run=run, popen=lambda *a, **k: monitor,
              clock=lambda: clock[0], settle=0, once=True)
    assert restarts == []  # Shairport not running: nothing to follow
    os.close(write); stream.close()


def test_airplay_units_render_per_start_and_follow_routes():
    unit = (ROOT / 'systemd/hifimate-airplay.service').read_text()
    assert 'ExecStartPre=@PREFIX@/bin/hifimate-airplay-net render @PREFIX@/config/host-stack/shairport-sync.conf %t/hifimate-airplay/shairport-sync.conf' in unit
    assert '@SHAIRPORT_SYNC@ -c %t/hifimate-airplay/shairport-sync.conf' in unit
    assert 'RuntimeDirectory=hifimate-airplay' in unit and 'hifimate-airplay-net.service' in unit
    watcher = (ROOT / 'systemd/hifimate-airplay-net.service').read_text()
    assert 'watch %t/hifimate-airplay/shairport-sync.conf' in watcher and 'PartOf' not in watcher
    manifest = json.loads((ROOT / 'config/host-stack-manifest.json').read_text())
    assert 'hifimate-airplay-net.service' in manifest['units'] and manifest['executables']['iproute2'] == 'ip'
    install = (ROOT / 'scripts/install.sh').read_text()
    assert install.count('hifimate-airplay-net.service') >= 3
