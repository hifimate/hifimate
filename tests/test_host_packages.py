"""Distribution packages: the right manager, names and the upmpdcli repository hint."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import host_stack as hs  # noqa: E402

MANIFEST = json.loads((ROOT / 'config/host-stack-manifest.json').read_text())


def which_only(*names):
    tools = {hs.MANAGERS[n] for n in names}
    return lambda command: f'/usr/bin/{command}' if command in tools else None


def test_os_release_picks_the_family_manager(tmp_path):
    for text, have, want in (
            ('ID=ubuntu\nID_LIKE=debian\n', ('apt',), 'apt'),
            ('ID=debian\n', ('apt',), 'apt'),
            ('ID="fedora"\n', ('dnf',), 'dnf'),
            ('ID="centos"\nID_LIKE="rhel fedora"\n', ('dnf',), 'dnf'),
            ('ID=arch\n', ('pacman',), 'pacman'),
            ('ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n', ('zypper',), 'zypper'),
            ('ID=somethingnew\n', ('dnf',), 'dnf'),          # unknown: whichever manager is installed
            ('ID=somethingnew\n', (), None)):
        path = tmp_path / 'os-release'
        path.write_text(text)
        assert hs.package_manager(hs.os_release(path), which_only(*have)) == want, text


def test_every_manager_lists_a_package_per_component():
    packages = MANIFEST['packages']
    assert set(packages) == {'apt', 'dnf', 'pacman', 'zypper'}
    for manager, names in packages.items():
        assert len(names) == len(set(names)) and 'mpd' in names and 'upmpdcli' in names and 'shairport-sync' in names, manager
    assert set(MANIFEST['package_sources']['upmpdcli']) == {'apt', 'dnf'}


def test_a_package_from_an_unconfigured_repository_is_named_not_installed(capsys):
    def run(command, **_):
        # apt-cache policy: upmpdcli has no candidate until its repository is added
        return SimpleNamespace(stdout='upmpdcli:\n  Installed: (none)\n  Candidate: (none)\n', returncode=0)
    calls = []
    assert hs.install_packages(MANIFEST, apply=True, assume_yes=True, manager='apt', run=run,
                               call=lambda c: calls.append(c) or 0) == 0
    command = calls[0]
    assert 'upmpdcli' not in command and 'mpd' in command and '-y' in command
    assert 'lesbonscomptes.com' in capsys.readouterr().err


def test_install_commands_per_manager():
    assert hs.install_command('dnf', ['mpd'], True) == ['dnf', 'install', '--setopt=install_weak_deps=False', '-y', 'mpd']
    assert hs.install_command('pacman', ['mpd'], False) == ['pacman', '-S', '--needed', 'mpd']
    assert hs.install_command('zypper', ['mpd'], True) == ['zypper', '--non-interactive', 'install', '--no-recommends', 'mpd']


def test_version_floor():
    assert hs.version_below('Linked with libwireplumber 0.4.17', '0.5')
    assert not hs.version_below('Linked with libwireplumber 0.5.8', '0.5')
    assert not hs.version_below('Linked with libpipewire 1.4.2', '1.0')
    assert hs.version_below('Linked with libpipewire 0.3.65', '1.0')
