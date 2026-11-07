import json
from pathlib import Path

import pytest

from conftest import record_file
from hifimate.identity import validate_name
from test_install_scripts import INSTALL, UNINSTALL, run


@pytest.mark.parametrize('name', ['', ' bad', 'bad ', 'x\ny', 'x\ry', 'x\ty', 'x\x00y',
                                  'x"; upnpiface=eth0', 'x#y', 'x=y', '$(touch /tmp/owned)',
                                  'x\\y', 'x\u202ey', '音' * 17])
def test_name_validation(name):
    with pytest.raises(ValueError):
        validate_name(name)


def test_name_chinese_space_install_upgrade_and_uninstall(tmp_path):
    prefix = tmp_path / 'prefix'
    result = run(INSTALL, prefix, '--apply', '--name', '客厅 音响')
    assert result.returncode == 0, result.stderr
    config = prefix / 'config/host-stack'
    assert 'name = "客厅 音响 AirPlay"' in (config / 'shairport-sync.conf').read_text()
    assert 'friendlyname = 客厅 音响 DLNA' in (config / 'upmpdcli.conf').read_text()
    assert not (config / 'music-upmpdcli.conf').exists()
    identity = prefix / 'config/identity.json'
    assert json.loads(identity.read_text())['name'] == '客厅 音响'
    assert 'config/identity.json' in (prefix / '.hifimate-install-manifest').read_text()
    assert run(INSTALL, prefix, '--apply').returncode == 0
    assert json.loads(identity.read_text())['name'] == '客厅 音响'
    assert run(INSTALL, prefix, '--apply', '--name', '书房').returncode == 0
    assert json.loads(identity.read_text())['name'] == '书房'
    assert run(UNINSTALL, prefix, '--apply').returncode == 0
    assert not prefix.exists()


@pytest.mark.parametrize('name', ['', 'bad\nupnpiface=eth0', '"; evil', '$(evil)'])
def test_invalid_name_before_any_install_writes(tmp_path, name):
    prefix = tmp_path / 'prefix'
    result = run(INSTALL, prefix, '--apply', '--name', name, '--install-deps')
    assert result.returncode != 0
    assert not prefix.exists()
    assert 'unexpected host command' not in result.stderr


def test_name_file_preflight_rejects_admin_changes_and_symlink(tmp_path):
    prefix = tmp_path / 'prefix'
    assert run(INSTALL, prefix, '--apply').returncode == 0
    path = prefix / 'config/identity.json'
    original = path.read_bytes()
    path.write_text('{}')
    assert run(INSTALL, prefix, '--apply', '--name', 'Next').returncode != 0
    path.write_bytes(original)
    external = tmp_path / 'administrator'
    path.rename(external)
    path.symlink_to(external)
    assert run(INSTALL, prefix, '--apply', '--name', 'Next').returncode != 0
    assert external.read_bytes() == original


def test_new_files_owned_and_uninstall_does_not_contact_bluez(tmp_path):
    prefix = tmp_path / 'prefix'
    assert run(INSTALL, prefix, '--apply').returncode == 0
    marker = (prefix / '.hifimate-install-manifest').read_text()
    for path in ('bin/hifimate-bluetooth', 'lib/hifimate/identity.py',
                 'lib/hifimate/bluetooth_alias.py', 'music-assistant/compose.yml'):
        assert path in marker
    assert '/usr/bin/true' not in (prefix / 'systemd/hifimate-bluetooth.service').read_text()
    assert run(UNINSTALL, prefix, '--apply').returncode == 0


def test_dash_name_passed_as_data_and_new_wrappers_disable_bytecode(tmp_path):
    import subprocess
    prefix = tmp_path / 'prefix'
    result = run(INSTALL, prefix, '--apply', '--name', '-音响')
    assert result.returncode == 0, result.stderr
    assert json.loads((prefix / 'config/identity.json').read_text())['name'] == '-音响'
    for name in ('hifimate-bluetooth',):
        result = subprocess.run([str(prefix / 'bin' / name), '--help'], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert not list(prefix.rglob('*.pyc'))
    assert run(UNINSTALL, prefix, '--apply').returncode == 0
