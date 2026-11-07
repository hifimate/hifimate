"""The hifimate command: options, remembered settings, upgrade, status, packaging."""
import json
import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
from hifimate import cli, __version__  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.delenv('HIFIMATE_PREFIX', raising=False)
    monkeypatch.setattr(cli, 'check_docker', lambda: None)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True, raising=False)
    return tmp_path


def recorder():
    calls = []
    def run(command, **_):
        calls.append(command)
        return 0
    return calls, run


def test_a_dry_run_tells_the_scripts_to_say_leave_out_dry_run(home, tmp_path):
    seen = []
    assert cli.install(*parse(['install', '--dry-run', '--prefix', str(tmp_path / 'p')]),
                       lambda command, env=None: seen.append((command, env)) or 0) == 0
    command, env = seen[0]
    assert '--apply' not in command and '去掉 --dry-run' in env['HIFIMATE_DRY_RUN_HINT']


def test_install_applies_enables_and_remembers_without_secrets(home, tmp_path):
    calls, run = recorder()
    secret = tmp_path / 'pw'
    secret.write_text('long enough\n')
    argv = ['install', '--prefix', str(tmp_path / 'p'), '--name', '客厅', '--setup-realtime',
            '--ma-admin-user', 'alice', '--ma-admin-password-file', str(secret)]
    args, rest = parse(argv)
    assert cli.install(args, rest, run) == 0
    command = calls[0]
    assert command[:2] == ['sh', str(ROOT / 'scripts/install.sh')]     # a checkout runs its own tree
    for part in ('--apply', '--enable', '--with-music-assistant', '--install-deps', '--setup-realtime'):
        assert part in command
    assert command[command.index('--prefix') + 1] == str(tmp_path / 'p')
    assert command[command.index('--ma-admin-password-file') + 1] == str(secret)
    saved = json.loads(cli.config_path().read_text())
    assert saved['prefix'] == str(tmp_path / 'p') and saved['options'] == ['--name', '客厅', '--setup-realtime']
    text = cli.config_path().read_text()
    assert 'alice' not in text and str(secret) not in text and 'long enough' not in text


def parse(argv):
    """main()'s parsing, without running the command."""
    seen = {}
    def capture(args, rest, *a, **k):
        seen['args'], seen['rest'] = args, rest
        return 0
    original = cli.install, cli.upgrade, cli.status, cli.uninstall
    cli.install = cli.upgrade = cli.status = cli.uninstall = capture
    try:
        cli.main(argv)
    finally:
        cli.install, cli.upgrade, cli.status, cli.uninstall = original
    return seen['args'], seen['rest']


def test_dry_run_and_options(home, tmp_path):
    calls, run = recorder()
    args, rest = parse(['install', '--dry-run', '--without-music-assistant', '--no-install-deps', '--prefix', str(tmp_path)])
    assert cli.install(args, rest, run) == 0
    assert not {'--apply', '--enable', '--with-music-assistant', '--install-deps'} & set(calls[0])
    assert not cli.config_path().exists()                 # a dry run remembers nothing
    with pytest.raises(SystemExit, match='不认识的选项'):
        cli.install(*parse(['install', '--apply']), run)    # applying is the command's job
    with pytest.raises(SystemExit, match='需要一个值'):
        cli.install(*parse(['install', '--name']), run)


def test_no_terminal_means_unattended(home, monkeypatch, tmp_path):
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False, raising=False)
    calls, run = recorder()
    cli.install(*parse(['install', '--prefix', str(tmp_path)]), run)
    assert '--non-interactive' in calls[0]
    assert '--non-interactive' not in json.loads(cli.config_path().read_text())['options']


def test_music_assistant_needs_docker(monkeypatch):
    monkeypatch.setattr(cli.shutil, 'which', lambda name: None)
    with pytest.raises(SystemExit, match='Docker'):
        cli.check_docker()
    monkeypatch.setattr(cli.shutil, 'which', lambda name: '/usr/bin/docker')
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stderr='permission denied\n'))
    with pytest.raises(SystemExit, match='docker 组'):
        cli.check_docker()


def test_upgrade_upgrades_the_package_then_reinstalls_the_same_way(home, monkeypatch, tmp_path):
    calls, run = recorder()
    cli.install(*parse(['install', '--prefix', str(tmp_path / 'p'), '--name', 'Den', '--without-music-assistant']), run)
    monkeypatch.setattr(cli, 'pipx_managed', lambda: True)
    execs = []
    status = cli.upgrade(*parse(['upgrade']), run=run, execv=lambda path, argv: execs.append(argv))
    assert calls[-1] == ['pipx', 'upgrade', 'hifimate'] and execs[0][1:3] == ['install', '--saved']
    cli.upgrade(*parse(['upgrade', '--version', '0.2.0']), run=run, execv=lambda path, argv: None)
    assert calls[-1] == ['pipx', 'install', '--force', 'hifimate==0.2.0']
    # the new package then reinstalls with what was remembered
    args, rest = parse(['install', '--saved'])
    cli.install(args, rest, run)
    command = calls[-1]
    assert command[command.index('--name') + 1] == 'Den' and '--with-music-assistant' not in command
    assert command[command.index('--prefix') + 1] == str(tmp_path / 'p')
    # without pipx: only the reinstall
    monkeypatch.setattr(cli, 'pipx_managed', lambda: False)
    before = len(calls)
    cli.upgrade(*parse(['upgrade']), run=run, execv=lambda *a: pytest.fail('no exec'))
    assert len(calls) == before + 1 and calls[-1][0] == 'sh'


def test_upgrade_needs_an_install(home):
    with pytest.raises(SystemExit, match='hifimate install'):
        cli.upgrade(*parse(['upgrade']))


def test_status_reports_versions_and_services(home, tmp_path, capsys):
    prefix = tmp_path / 'p'
    (prefix / 'share/hifimate').mkdir(parents=True)
    (prefix / 'config').mkdir()
    (prefix / 'share/hifimate/version').write_text('0.0.9\n')
    (prefix / 'share/hifimate/commit').write_text('abcdef0123456789\n')
    (prefix / 'config/hifimate.conf').write_text('{"alsa_device": "hw:CARD=DAC,DEV=0"}')
    cli.save(prefix, [], False, True)
    def run(command, **_):
        assert command[:3] == ['systemctl', '--user', 'is-active']
        return SimpleNamespace(stdout='\n'.join(['active'] * (len(cli.UNITS) - 1) + ['failed']), returncode=3)
    assert cli.status(*parse(['status']), run=run) == 1
    out = capsys.readouterr().out
    assert '0.0.9' in out and 'hifimate upgrade' in out and 'hw:CARD=DAC,DEV=0' in out and 'abcdef012345' in out
    assert '✗ hifimate-music-mpd.service' in out and '仅本机' in out
    assert cli.status(*parse(['status', '--prefix', str(tmp_path / 'none')]), run=run) == 1


def test_the_wheel_carries_every_directory_the_installer_reads():
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    shipped = set(config['tool']['hatch']['build']['targets']['wheel']['force-include']) | {'hifimate'}
    install = (ROOT / 'scripts/install.sh').read_text() + (ROOT / 'scripts/uninstall.sh').read_text()
    import re
    read = {m.split('/')[0] for m in re.findall(r'\$ROOT/([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.*-]+)?)', install)}
    assert read <= shipped, read - shipped
    sys.path.insert(0, str(ROOT / 'packaging'))
    import check_wheel
    tracked = subprocess.run(['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True).stdout.split()
    if not tracked:
        pytest.skip('not a git checkout')
    want = check_wheel.expected(tracked)
    assert 'hifimate/_dist/scripts/install.sh' in want and 'hifimate/cli.py' in want
    assert not any(name.startswith(('tests/', 'hifimate/_dist/tests')) for name in want)


def test_the_wheel_check_requires_the_built_commit(tmp_path):
    sys.path.insert(0, str(ROOT / 'packaging'))
    import check_wheel

    def wheel(commit_file):
        path = tmp_path / 'hifimate-0-py3-none-any.whl'
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('hifimate/cli.py', '')
            z.writestr('hifimate-0.dist-info/entry_points.txt', '[console_scripts]\nhifimate = hifimate.cli:main\n')
            if commit_file is not None:
                z.writestr('hifimate/_commit.txt', commit_file)
        return path

    head = 'a' * 40
    assert check_wheel.check(wheel(head + '\n'), ['hifimate/cli.py'], head) == []
    assert check_wheel.check(wheel(None), ['hifimate/cli.py'], head) == [f"hifimate/_commit.txt is None, not the built commit {head}"]
    assert check_wheel.check(wheel('b' * 40), ['hifimate/cli.py'], head)


def test_version_is_one_number_everywhere():
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    assert config['project']['dynamic'] == ['version'] and config['tool']['hatch']['version']['path'] == 'hifimate/__init__.py'
    assert re.fullmatch(r'\d+\.\d+\.\d+', __version__)


def bootstrap_env(tmp_path, uid):
    fake = tmp_path / 'bin'
    fake.mkdir(exist_ok=True)
    log = tmp_path / 'log'
    (fake / 'pipx').write_text(f'#!/bin/sh\necho "pipx $* PIPX_HOME=${{PIPX_HOME:-}}" >> {log}\n'
                               f'[ "$1" = environment ] && echo {fake}\nexit 0\n')
    (fake / 'hifimate').write_text(f'#!/bin/sh\necho "hifimate $*" >> {log}\n')
    (fake / 'id').write_text(f'#!/bin/sh\necho {uid}\n')
    for f in fake.iterdir():
        f.chmod(0o755)
    return {'PATH': f'{fake}:/usr/bin:/bin', 'HOME': str(tmp_path)}, log


def test_bootstrap_as_root_installs_for_the_whole_system(tmp_path):
    assert subprocess.run(['sh', '-n', str(ROOT / 'packaging/install.sh')]).returncode == 0
    env, log = bootstrap_env(tmp_path, 0)
    release = tmp_path / 'install.sh'
    release.write_text((ROOT / 'packaging/install.sh').read_text().replace('@VERSION@', '0.1.0', 1))
    result = subprocess.run(['sh', str(release), '--name', 'Den'], env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, start_new_session=True)
    assert result.returncode == 0, result.stderr
    lines = log.read_text().splitlines()
    assert 'pipx install --force hifimate==0.1.0 PIPX_HOME=/opt/pipx' in lines
    assert lines[-1] == 'hifimate install --non-interactive --name Den'     # no terminal: unattended


def test_bootstrap_without_root_points_to_sudo(tmp_path):
    env, log = bootstrap_env(tmp_path, 1000)
    result = subprocess.run(['sh', str(ROOT / 'packaging/install.sh')], env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, start_new_session=True)
    assert result.returncode == 2 and '| sudo sh' in result.stderr and not log.exists()
    # the user install, on request
    result = subprocess.run(['sh', str(ROOT / 'packaging/install.sh')], env={**env, 'HIFIMATE_USER_MODE': '1'},
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, start_new_session=True)
    assert result.returncode == 0, result.stderr
    assert 'pipx install --force hifimate PIPX_HOME=' in log.read_text().splitlines()


def test_a_user_install_has_no_control_password_to_give(home, tmp_path, monkeypatch):
    # its control password is the user's own login password (PAM)
    with pytest.raises(SystemExit, match='登录密码'):
        cli.install(*parse(['install', '--ctrl-password-file', str(tmp_path / 'pw')]), lambda c, **_: 0)
    monkeypatch.setenv('HIFIMATE_CTRL_PASSWORD', 'long enough')
    with pytest.raises(SystemExit, match='登录密码'):
        cli.install(*parse(['install']), lambda c, **_: 0)


def test_a_checkout_is_staged_with_what_a_release_carries():
    import tomllib
    config = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    shipped = {name for name in config['tool']['hatch']['build']['targets']['wheel']['force-include']} | {'hifimate'}
    assert shipped <= set(cli.CHECKOUT_TREE)
    assert not {'.pytest_cache', 'tests', '.git', '.venv'} & set(cli.CHECKOUT_TREE)
