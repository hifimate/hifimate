import importlib.util
import json
import os
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('bluetooth_speaker', ROOT / 'scripts/bluetooth_speaker.py')
bt = importlib.util.module_from_spec(spec); spec.loader.exec_module(bt)
STOCK = '[General]\n\n# Default device class.\n#Class = 0x000100\n\n#DiscoverableTimeout = 0\n\n[Policy]\n#AutoEnable=true\n'


def system(tmp_path, text=STOCK):
    root = tmp_path / 'root'; (root / 'etc/bluetooth').mkdir(parents=True)
    (root / 'etc/bluetooth/main.conf').write_text(text); (root / 'etc/bluetooth/main.conf').chmod(0o644)
    calls = []
    def run(args, **_):
        calls.append(args)
        from types import SimpleNamespace
        return SimpleNamespace(stdout='{ path=/usr/libexec/bluetooth/bluetoothd ; argv[]=/usr/libexec/bluetooth/bluetoothd ; ignore_errors=no ; pid=0 }')
    return root, calls, run


def test_install_adds_marked_loudspeaker_class_and_remove_restores(tmp_path):
    root, calls, run = system(tmp_path)
    assert bt.apply('install', 1000, tmp_path / 'prefix', root, run, os.getuid())
    conf = (root / 'etc/bluetooth/main.conf').read_text()
    assert bt.active_class(conf.splitlines()) == '0x000414'
    assert conf.splitlines()[1:4] == bt.LINES + [bt.REPAIRING] and '[Policy]' in conf
    assert bt.active(conf.splitlines(), 'JustWorksRepairing') == 'confirm'
    record = json.loads((root / 'var/lib/hifimate-bluetooth-speaker/1000.json').read_text())
    assert record['prefix'] == str(tmp_path / 'prefix')
    unit = (root / bt.DROPIN).read_text()
    assert 'ExecStart=\nExecStart=/usr/libexec/bluetooth/bluetoothd --noplugin=hostname\n' in unit
    assert calls[-2:] == [['systemctl', 'daemon-reload'], ['systemctl', 'restart', 'bluetooth.service']]
    assert not bt.apply('install', 1000, tmp_path / 'prefix', root, run, os.getuid())  # idempotent
    assert bt.apply('remove', 1000, tmp_path / 'prefix', root, run, os.getuid())
    assert (root / 'etc/bluetooth/main.conf').read_text() == STOCK
    assert not (root / 'var/lib/hifimate-bluetooth-speaker/1000.json').exists() and not (root / bt.DROPIN).exists()
    assert not bt.apply('remove', 1000, tmp_path / 'prefix', root, run, os.getuid())


def test_admin_class_and_edited_lines_are_preserved(tmp_path):
    admin = STOCK.replace('#Class = 0x000100', 'Class = 0x200420')
    root, calls, run = system(tmp_path, admin)
    with pytest.raises(RuntimeError, match='administrator'):
        bt.apply('install', 1000, tmp_path / 'prefix', root, run, os.getuid())
    assert (root / 'etc/bluetooth/main.conf').read_text() == admin and not (root / bt.DROPIN).exists()
    assert not (root / 'var/lib/hifimate-bluetooth-speaker').exists() or not any((root / 'var/lib/hifimate-bluetooth-speaker').iterdir())
    root, calls, run = system(tmp_path / 'b')
    bt.apply('install', 1000, tmp_path / 'prefix', root, run, os.getuid())
    conf = root / 'etc/bluetooth/main.conf'
    conf.write_text(conf.read_text().replace('Class = 0x000414', 'Class = 0x000418'))
    with pytest.raises(RuntimeError, match='preserving'):
        bt.apply('remove', 1000, tmp_path / 'prefix', root, run, os.getuid())
    assert 'Class = 0x000418' in conf.read_text()


def test_requires_sudo_with_matching_uid():
    result = subprocess.run(['python3', '-I', str(ROOT / 'scripts/bluetooth_speaker.py'), 'install', '1000', '/tmp/x'],
                            capture_output=True, text=True)
    assert result.returncode != 0 and 'requires sudo' in (result.stderr + result.stdout)


def test_installer_wires_explicit_speaker_setup_and_removal():
    install = (ROOT / 'scripts/install.sh').read_text(); uninstall = (ROOT / 'scripts/uninstall.sh').read_text()
    assert '--setup-bluetooth-speaker) SETUP_BT_SPEAKER=1' in install
    assert '$SUDO python3 -B -I "$ROOT/scripts/bluetooth_speaker.py" install "$(id -u)" "$PREFIX"' in install
    assert 'config/bluetooth-speaker.request' in install
    assert 'sudo python3 -B -I "$PREFIX/bin/hifimate-bluetooth-speaker" remove "$(id -u)" "$PREFIX"' in uninstall
    rules = (ROOT / 'config/host-stack/wireplumber/wireplumber.conf.d/51-hifimate.conf').read_text()
    assert 'monitor.bluez.seat-monitoring = disabled' in rules and 'bluez5.roles = [ a2dp_sink ]' in rules


def test_unmanaged_dropin_and_existing_plugin_options_are_preserved(tmp_path):
    root, calls, run = system(tmp_path)
    (root / bt.DROPIN).parent.mkdir(parents=True); (root / bt.DROPIN).write_text('[Service]\nExecStart=\nExecStart=/x\n')
    with pytest.raises(RuntimeError, match='unmanaged'):
        bt.apply('install', 1000, tmp_path / 'prefix', root, run, os.getuid())
    assert (root / 'etc/bluetooth/main.conf').read_text() == STOCK
    from types import SimpleNamespace
    assert bt.dropin(lambda *a, **k: SimpleNamespace(stdout='argv[]=/usr/sbin/bluetoothd -E ; x')).count('--noplugin=hostname') == 1
    with pytest.raises(RuntimeError, match='administrator'):
        bt.dropin(lambda *a, **k: SimpleNamespace(stdout='argv[]=/usr/sbin/bluetoothd --noplugin=sap ; x'))


def test_admin_repairing_policy_is_kept(tmp_path):
    root, calls, run = system(tmp_path, STOCK.replace('[General]\n', '[General]\nJustWorksRepairing = never\n'))
    bt.apply('install', 1000, tmp_path / 'prefix', root, run, os.getuid())
    conf = (root / 'etc/bluetooth/main.conf').read_text()
    assert bt.active(conf.splitlines(), 'JustWorksRepairing') == 'never' and bt.REPAIRING not in conf
    bt.apply('remove', 1000, tmp_path / 'prefix', root, run, os.getuid())
    assert 'Managed by HiFiMate' not in (root / 'etc/bluetooth/main.conf').read_text()
