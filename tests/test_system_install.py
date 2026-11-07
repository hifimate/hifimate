"""The system install: dedicated user, root-only settings and the Samba share (all faked, no root)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
from hifimate import cli, system  # noqa: E402


def done(code=0, out=''):
    return SimpleNamespace(returncode=code, stdout=out, stderr='')


# --- system.py ------------------------------------------------------------------

def test_share_is_added_once_and_removed_without_touching_the_rest(tmp_path):
    conf = tmp_path / 'smb.conf'
    conf.write_text('[global]\n   workgroup = HOME\n\n[printers]\n   path = /var/spool\n')
    assert system.write_share_conf('/srv/hifimate/music', conf) is True
    text = conf.read_text()
    assert '[Music]' in text and 'path = /srv/hifimate/music' in text and 'valid users = hifimate' in text
    assert text.count(system.BEGIN) == 1 and '[printers]' in text and 'workgroup = HOME' in text
    assert system.write_share_conf('/srv/hifimate/music', conf) is False          # idempotent
    system.write_share_conf('/data/music', conf)                                  # moved library: replaced
    assert conf.read_text().count('[Music]') == 1 and 'path = /data/music' in conf.read_text()
    assert system.strip_share(conf.read_text()) == '[global]\n   workgroup = HOME\n\n[printers]\n   path = /var/spool\n'


def test_a_foreign_music_share_is_left_alone(tmp_path):
    conf = tmp_path / 'smb.conf'
    conf.write_text('[global]\n[music]\n   path = /home/me/music\n')
    with pytest.raises(RuntimeError, match='不是 HiFiMate'):
        system.write_share_conf('/srv/hifimate/music', conf)
    assert conf.read_text() == '[global]\n[music]\n   path = /home/me/music\n'


def test_missing_smb_conf_gets_a_minimal_global_section(tmp_path):
    conf = tmp_path / 'samba' / 'smb.conf'                       # Arch ships none
    system.write_share_conf('/srv/hifimate/music', conf)
    assert conf.read_text().startswith('[global]\n   workgroup = WORKGROUP')


def test_setup_share_sets_the_password_and_starts_samba(tmp_path, monkeypatch):
    monkeypatch.setattr(system.shutil, 'which', lambda name: f'/usr/bin/{name}' if name == 'smbpasswd' else None)
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw.get('input')))
        return done(0 if argv[:2] != ['systemctl', 'cat'] or argv[2] == 'smbd.service' else 1)
    avahi = tmp_path / 'avahi' / 'services' / 'hifimate-smb.service'
    avahi.parent.mkdir(parents=True)
    service = system.setup_share('/srv/hifimate/music', 'long enough', run, tmp_path / 'smb.conf', avahi)
    assert service == 'smbd.service'
    assert (['smbpasswd', '-s', '-a', 'hifimate'], 'long enough\nlong enough\n') in calls
    assert (['systemctl', 'enable', '--now', 'smbd.service'], None) in calls
    assert '_smb._tcp' in avahi.read_text()
    calls.clear()
    system.setup_share('/srv/hifimate/music', None, run, tmp_path / 'smb.conf', avahi)   # reinstall, no password
    assert not any(argv[0] == 'smbpasswd' for argv, _ in calls)
    system.remove_share(run, tmp_path / 'smb.conf', avahi)
    assert system.BEGIN not in (tmp_path / 'smb.conf').read_text() and not avahi.exists()


def test_without_samba_the_share_reports_why(tmp_path, monkeypatch):
    monkeypatch.setattr(system.shutil, 'which', lambda name: None)
    with pytest.raises(RuntimeError, match='samba'):
        system.setup_share('/srv/hifimate/music', 'x' * 8, lambda *a, **k: done(), tmp_path / 'smb.conf')


def test_commands_run_as_the_service_user_with_its_bus():
    argv = system.as_user(['systemctl', '--user', 'is-active', 'hifimate.service'], 991)
    assert argv[:4] == ['runuser', '-u', 'hifimate', '--'] and argv[4] == 'env'
    assert 'XDG_RUNTIME_DIR=/run/user/991' in argv and 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/991/bus' in argv
    assert 'HOME=/var/lib/hifimate' in argv and argv[-4:] == ['systemctl', '--user', 'is-active', 'hifimate.service']


def test_root_helpers_act_for_the_service_user():
    seen = {}
    def run(argv, **kw):
        seen['argv'], seen['env'] = argv, kw['env']
        return done()
    system.root_helper('/src', 'realtime_permissions.py', 'install', 991, run)
    assert seen['argv'][-3:] == ['install', '991', '/opt/hifimate'] and seen['env']['SUDO_UID'] == '991'


# --- the root install flow ---------------------------------------------------------

class FakeSystem:
    """system.py with every root operation recorded instead of done."""
    USER, HOME, PREFIX, MUSIC, SHARE = 'hifimate', None, None, None, 'Music'

    def __init__(self, tmp):
        self.HOME, self.PREFIX = tmp / 'var-lib-hifimate', tmp / 'opt-hifimate'
        self.MUSIC, self.MA_DATA = tmp / 'srv-music', tmp / 'var-lib-hifimate' / 'music-assistant'
        self.log, self.user, self.password = [], None, None
        self.BLUEZ_CONF = tmp / 'bluetooth-main.conf'
        self.BLUEZ_CONF.write_text('[General]\n')

    def uid(self): return self.user
    def ensure_user(self, ma, run):
        self.log.append(('user', ma)); new = self.user is None; self.user = 991; return 991, new
    def ensure_directories(self, music, run): self.log.append(('dirs', str(music)))
    def root_helper(self, root, script, action, uid, run):
        self.log.append(('helper', script, action)); return done()
    def start_manager(self, uid, restart, run): self.log.append(('manager', restart))
    def as_user(self, argv, uid): return ['AS', str(uid), *argv]
    def setup_share(self, music, password, run): self.log.append(('share', str(music), password))
    def write_pam(self, run): self.log.append(('pam',))
    def has_password(self): return self.password is not None
    def set_password(self, password, run): self.log.append(('password', password)); self.password = password


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(system, 'RECORD', tmp_path / 'etc' / 'install.json')
    monkeypatch.setattr(cli.sys.stdin, 'isatty', lambda: False, raising=False)
    monkeypatch.setattr(cli, 'check_docker', lambda: None)
    class Staged:
        def __init__(self, owner=None): pass
        def __enter__(self): return ROOT
        def __exit__(self, *a): pass
    monkeypatch.setattr(cli, 'Tree', Staged)
    for name in ('HIFIMATE_CTRL_PASSWORD', 'HIFIMATE_MA_ADMIN_PASSWORD'):
        monkeypatch.delenv(name, raising=False)
    return FakeSystem(tmp_path)


def parse(argv):
    seen = {}
    original = cli.install
    cli.install = lambda args, rest, *a, **k: seen.update(args=args, rest=rest) or 0
    try:
        cli.main(argv)
    finally:
        cli.install = original
    return seen['args'], seen['rest']


def test_root_install_creates_the_user_then_installs_as_it(root, monkeypatch, tmp_path):
    monkeypatch.setenv('HIFIMATE_CTRL_PASSWORD', 'correct horse')
    calls = []
    def run(argv, env=None):
        calls.append((argv, env))
        return 0
    args, rest = parse(['install', '--name', '客厅', '--setup-realtime'])
    assert cli.system_install(args, rest, run, sys_=root) == 0
    deps, install = calls
    assert deps[0][3:6] == ['--install-deps', '--apply', '--yes'] and '--with-share' in deps[0]
    argv, env = install
    assert argv[:2] == ['AS', '991'] and argv[3].endswith('scripts/install.sh')
    assert argv[argv.index('--prefix') + 1] == str(root.PREFIX)
    assert argv[argv.index('--music-directory') + 1] == str(root.MUSIC)
    assert argv[argv.index('--music-assistant-data-dir') + 1] == str(root.MA_DATA)
    assert '--with-music-assistant' in argv and '--install-deps' not in argv
    assert '--setup-realtime' not in argv                       # done by root, not via sudo
    assert 'HIFIMATE_CTRL_PASSWORD' not in env                  # nothing stores it; PAM checks it
    kinds = [entry[0] for entry in root.log]
    assert kinds == ['user', 'dirs', 'pam', 'helper', 'helper', 'manager', 'share', 'password']
    assert ('helper', 'bluetooth_speaker.py', 'install') in root.log
    assert ('manager', True) in root.log                        # a new user: a fresh manager
    assert ('share', str(root.MUSIC), None) in root.log and ('password', 'correct horse') in root.log
    # a reinstall without a password keeps the Linux password and asks nothing
    root.log.clear()
    monkeypatch.delenv('HIFIMATE_CTRL_PASSWORD')
    assert cli.system_install(*parse(['install']), run, sys_=root) == 0
    assert not any(entry[0] == 'password' for entry in root.log)
    saved = json.loads(system.RECORD.read_text())
    assert saved['prefix'] == str(root.PREFIX) and saved['share'] is True
    assert saved['options'] == ['--name', '客厅']      # neither --setup-realtime nor --non-interactive


def test_root_install_reads_password_files_itself(root, tmp_path):
    secret = tmp_path / 'pw'
    secret.write_text('from a file\n')
    secret.chmod(0o600)
    calls = []
    args, rest = parse(['install', '--ctrl-password-file', str(secret), '--without-share', '--without-music-assistant'])
    cli.system_install(args, rest, lambda argv, env=None: calls.append((argv, env)) or 0, sys_=root)
    argv, env = calls[-1]
    assert '--ctrl-password-file' not in argv and 'HIFIMATE_CTRL_PASSWORD' not in env
    assert ('password', 'from a file') in root.log
    assert '--with-music-assistant' not in argv and not any(e[0] == 'share' for e in root.log)
    assert ('user', False) in root.log


def test_first_root_install_needs_a_password(root):
    args, rest = parse(['install'])
    with pytest.raises(SystemExit, match='HIFIMATE_CTRL_PASSWORD'):
        cli.system_install(args, rest, lambda *a, **k: 0, sys_=root)


def test_root_dry_run_only_explains(root, capsys):
    args, rest = parse(['install', '--dry-run'])
    assert cli.system_install(args, rest, lambda *a, **k: pytest.fail('nothing runs'), sys_=root) == 0
    out = capsys.readouterr().out
    assert '创建用户 hifimate' in out and 'Music' in out and root.log == []


def test_system_prefix_is_fixed(root):
    args, rest = parse(['install', '--prefix', '/home/me/hifi'])
    with pytest.raises(SystemExit, match='固定'):
        cli.system_install(args, rest, lambda *a, **k: 0, sys_=root)


def test_a_normal_user_is_sent_to_sudo_once_the_system_install_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(system, 'RECORD', tmp_path / 'install.json')
    system.RECORD.write_text('{}')
    with pytest.raises(SystemExit, match='sudo hifimate'):
        cli.main(['status'])


def test_tools_run_as_the_service_user(root, monkeypatch):
    monkeypatch.setattr(system, 'uid', lambda: 991)
    calls = []
    run = lambda argv: calls.append(argv) or 0
    def call(argv):
        parser_args = {}
        original = cli.tool
        cli.tool = lambda args, rest, *a, **k: parser_args.update(args=args) or 0
        try:
            cli.main(argv)
        finally:
            cli.tool = original
        return cli.tool(parser_args['args'], [], run)
    call(['pair', '--seconds', '60'])
    call(['reload-filters'])
    call(['output', '--device', 'hw:CARD=DAC,DEV=0'])
    call(['restart'])
    assert all(c[:3] == ['runuser', '-u', 'hifimate'] for c in calls)
    assert calls[0][-4:] == [str(system.PREFIX / 'bin/hifimate-bluetooth'), 'pair', '--seconds', '60'][-4:]
    assert calls[1][-2:] == [str(system.PREFIX / 'bin/hifimate-ctrl'), 'reload-filters']
    assert calls[2][-3:] == ['--device', 'hw:CARD=DAC,DEV=0', '--apply']
    assert calls[3][-12:-10] == ['--user', 'restart']
    calls.clear()
    call(['logs', 'airplay', '-n', '20'])
    assert calls[0][:3] == ['journalctl', '_UID=991', '_SYSTEMD_USER_UNIT=hifimate-airplay.service']


def test_password_is_hifimates_linux_password_and_the_shares(tmp_path, monkeypatch):
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw.get('input'))); return done()
    conf = tmp_path / 'smb.conf'
    monkeypatch.setattr(system.shutil, 'which', lambda name: '/usr/bin/' + name)
    assert system.set_password('long enough', run, conf) is False          # no share yet
    assert calls == [(['chpasswd'], 'hifimate:long enough\n')]
    system.write_share_conf('/srv/hifimate/music', conf)
    assert system.set_password('long enough', run, conf) is True
    assert (['smbpasswd', '-s', '-a', 'hifimate'], 'long enough\nlong enough\n') in calls


def test_has_password_reads_shadow(tmp_path):
    shadow = tmp_path / 'shadow'
    for field, expected in (('!', False), ('!*', False), ('*', False), ('', False), ('$y$j9T$abc$def', True)):
        shadow.write_text(f'root:$y$x:1::::::\nhifimate:{field}:20000:0:99999:7:::\n')
        assert system.has_password(shadow) is expected, field
    assert system.has_password(tmp_path / 'missing') is False


def test_pam_service_and_no_ssh_for_the_account(tmp_path):
    reloads = []
    pam, sshd = tmp_path / 'pam.d' / 'hifimate', tmp_path / 'sshd_config.d' / 'hifimate.conf'
    sshd.parent.mkdir()
    system.write_pam(pam, sshd, lambda argv, **k: reloads.append(argv) or done())
    assert 'auth     required pam_unix.so' in pam.read_text() and 'pam_permit' not in pam.read_text()
    assert 'DenyUsers hifimate' in sshd.read_text() and reloads
    reloads.clear()
    system.write_pam(pam, sshd, lambda argv, **k: reloads.append(argv) or done())
    assert reloads == []                                         # unchanged: sshd left alone
    system.remove_pam(pam, sshd, lambda argv, **k: done())
    assert not pam.exists() and not sshd.exists()


def test_password_tool_sets_both_and_logs_everyone_out(root, monkeypatch, capsys):
    monkeypatch.setattr(system, 'uid', lambda: 991)
    monkeypatch.setenv('HIFIMATE_CTRL_PASSWORD', 'brand new pw')
    changed = []
    monkeypatch.setattr(system, 'set_password', lambda password, run=None: changed.append(password) or True)
    calls = []
    original = cli.tool
    seen = {}
    cli.tool = lambda args, rest, *a, **k: seen.update(args=args) or 0
    try:
        cli.main(['password'])
    finally:
        cli.tool = original
    assert cli.tool(seen['args'], [], lambda argv: calls.append(argv) or 0) == 0
    assert changed == ['brand new pw']
    assert calls[-1][-3:] == ['--user', 'restart', 'hifimate-ctrl.service'] and calls[-1][:3] == ['runuser', '-u', 'hifimate']
    assert '音乐共享的密码也已修改' in capsys.readouterr().out


def test_a_reinstall_without_any_password_warns_but_installs(root, capsys):
    system.RECORD.parent.mkdir(parents=True, exist_ok=True)
    system.RECORD.write_text('{"prefix": "%s", "options": []}' % root.PREFIX)
    assert cli.system_install(*parse(['install']), lambda *a, **k: 0, sys_=root) == 0
    assert 'sudo hifimate password' in capsys.readouterr().err
    assert not any(entry[0] == 'password' for entry in root.log)


def test_the_control_service_can_run_pams_setgid_helper():
    # NoNewPrivileges strips unix_chkpwd's setgid, and sandboxing in a user unit means a
    # user namespace without the shadow group: either way every password would fail
    unit = (ROOT / 'systemd/hifimate-ctrl.service').read_text()
    settings = {line.split('=')[0].strip() for line in unit.splitlines() if '=' in line and not line.startswith('#')}
    assert not settings & {'NoNewPrivileges', 'PrivateTmp', 'ProtectSystem', 'ProtectHome', 'PrivateUsers',
                           'PrivateDevices', 'RestrictSUIDSGID'}


def test_the_service_user_gets_its_own_journal(monkeypatch, tmp_path):
    # a regular (not --system) user: journald keeps a private journal file for it
    created = []
    monkeypatch.setattr(system, 'uid', lambda: None if not created else 1001)
    monkeypatch.setattr(system, 'HOME', tmp_path / 'home')
    monkeypatch.setattr(system.grp, 'getgrall', lambda: [])
    monkeypatch.setattr(system, 'groups_wanted', lambda ma: [])
    system.ensure_user(True, lambda argv, **k: created.append(argv) or done())
    useradd = created[0]
    assert useradd[0] == 'useradd' and '--system' not in useradd and '--shell' in useradd
    assert useradd[useradd.index('--shell') + 1].endswith('nologin')


def test_pam_service_only_authenticates():
    lines = [l.split() for l in system.PAM_TEXT.splitlines() if l and not l.startswith('#')]
    assert lines == [['auth', 'required', 'pam_unix.so']]


def test_uninstall_keeps_the_share_account_purge_removes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(system.shutil, 'which', lambda name: '/usr/bin/' + name)
    calls = []
    run = lambda argv, **k: calls.append(argv) or done()
    system.remove_share(run, tmp_path / 'smb.conf', tmp_path / 'avahi')
    assert ['smbpasswd', '-x', 'hifimate'] not in calls          # the password stays with the user
    system.remove_share(run, tmp_path / 'smb.conf', tmp_path / 'avahi', account=True)
    assert ['smbpasswd', '-x', 'hifimate'] in calls


def test_uninstall_keeps_the_share_account_purge_removes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(system.shutil, 'which', lambda name: '/usr/bin/' + name)
    calls = []
    run = lambda argv, **k: calls.append(argv) or done()
    system.remove_share(run, tmp_path / 'smb.conf', tmp_path / 'avahi')
    assert ['smbpasswd', '-x', 'hifimate'] not in calls          # the password stays with the user
    system.remove_share(run, tmp_path / 'smb.conf', tmp_path / 'avahi', account=True)
    assert ['smbpasswd', '-x', 'hifimate'] in calls
