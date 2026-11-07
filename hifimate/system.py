"""The system install: a dedicated `hifimate` user, set up by root.

`sudo hifimate install` (or the one-line installer piped into `sudo sh`)
installs the distribution packages, creates the system user `hifimate`
and runs HiFiMate as that user: its own systemd user manager (lingering,
so it starts at boot with nobody logged in), its own PipeWire and the Music
Assistant containers. The settings that need root are made here, once, so
nothing at run time asks for sudo:

- realtime scheduling limits for the user's manager (realtime_permissions.py);
- the Bluetooth loudspeaker class (bluetooth_speaker.py), when there is BlueZ;
- a Samba share `Music` of the music library, so other computers can copy
  music in (user `hifimate`, the control password);
- the control password itself: it is the Linux password of `hifimate`,
  checked by the control page through PAM (/etc/pam.d/hifimate, pam_unix
  only; see ctrl_auth.py). The account keeps its nologin shell, and SSH
  refuses it (/etc/ssh/sshd_config.d/hifimate.conf), so the password opens
  only the control page and the share.

Layout (see README, "安装后的文件"):

  /opt/hifimate            programs, settings, the user's EQ and room correction
  /var/lib/hifimate        the user's home: PipeWire and systemd user config,
                           Music Assistant data
  /srv/hifimate/music      the music library, shared as `Music`
  /etc/hifimate            what `hifimate install` remembers

Everything here takes `run` (subprocess.run-like) so it can be tested
without root.
"""
import grp
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import time

USER = 'hifimate'
HOME = Path('/var/lib/hifimate')
PREFIX = Path('/opt/hifimate')
MUSIC = Path('/srv/hifimate/music')
MA_DATA = HOME / 'music-assistant'
RECORD = Path('/etc/hifimate/install.json')
SMB_CONF = Path('/etc/samba/smb.conf')
BLUEZ_CONF = Path('/etc/bluetooth/main.conf')
AVAHI_SMB = Path('/etc/avahi/services/hifimate-smb.service')
PAM_SERVICE = Path('/etc/pam.d/hifimate')
SSHD_DROPIN = Path('/etc/ssh/sshd_config.d/hifimate.conf')
SHADOW = Path('/etc/shadow')
PAM_TEXT = '''# Managed by hifimate (removed with the user: uninstall --purge): the HiFiMate control page checks
# the password of the user it runs as. Password check only, nothing else.
auth     required pam_unix.so
'''
SSHD_TEXT = '''# Managed by hifimate (removed with the user: uninstall --purge): the hifimate account has a
# password for the control page and the music share, never for SSH.
DenyUsers hifimate
'''
SHARE = 'Music'
BEGIN = '# BEGIN HiFiMate share (managed by hifimate; removed on uninstall)'
END = '# END HiFiMate share'


def uid():
    """The service user's uid, or None before it exists."""
    try:
        return pwd.getpwnam(USER).pw_uid
    except KeyError:
        return None


def groups_wanted(music_assistant):
    names = ['audio', 'bluetooth'] + (['docker'] if music_assistant else [])
    out = []
    for name in names:
        try:
            grp.getgrnam(name)
            out.append(name)
        except KeyError:
            pass
    return out


def ensure_user(music_assistant, run=subprocess.run):
    """Create the service user (no login shell) and put it in the device groups.
    Returns (uid, changed): changed means a running manager needs a restart.

    Deliberately not `useradd --system`: journald gives a private journal file
    only to users outside the system UID range. With one, the control page
    reads its own services' logs and nothing else; a system user would need
    the systemd-journal group, which reads every log on the machine (and the
    control page's log view is visible on the LAN)."""
    changed = False
    if uid() is None:
        shell = shutil.which('nologin') or '/usr/sbin/nologin'
        run(['useradd', '--user-group', '--home-dir', str(HOME), '--create-home',
             '--shell', shell, '--comment', 'HiFiMate audio receiver', USER], check=True)
        changed = True
    current = {g.gr_name for g in grp.getgrall() if USER in g.gr_mem}
    missing = [g for g in groups_wanted(music_assistant) if g not in current]
    if missing:
        run(['usermod', '--append', '--groups', ','.join(missing), USER], check=True)
        changed = True
    HOME.mkdir(mode=0o750, parents=True, exist_ok=True)
    return uid(), changed


def ensure_directories(music, run=subprocess.run):
    """The prefix and the music library belong to the service user; the
    library is group-writable (setgid) so files copied in stay shared."""
    PREFIX.mkdir(mode=0o755, parents=True, exist_ok=True)
    music = Path(music)
    music.mkdir(parents=True, exist_ok=True)
    run(['chown', f'{USER}:{USER}', str(HOME), str(PREFIX)], check=True)
    if music.stat().st_uid == 0:          # created now (or root's): hand it over, never someone else's
        run(['chown', f'{USER}:{USER}', str(music)], check=True)
        music.chmod(0o2775)


def start_manager(user_id, restart=False, run=subprocess.run, wait=30, sleep=time.sleep):
    """Lingering user manager, up and reachable on its bus."""
    run(['loginctl', 'enable-linger', USER], check=True)
    if restart:
        run(['systemctl', 'restart', f'user@{user_id}.service'], check=True)
    else:
        run(['systemctl', 'start', f'user@{user_id}.service'], check=True)
    bus = Path(f'/run/user/{user_id}/bus')
    for _ in range(int(wait * 2)):
        if bus.exists():
            return
        sleep(0.5)
    raise RuntimeError(f'{USER} 的 systemd 用户实例没有启动（{bus} 不存在）')


def user_env(user_id):
    return {'HOME': str(HOME), 'USER': USER, 'LOGNAME': USER,
            'XDG_RUNTIME_DIR': f'/run/user/{user_id}',
            'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{user_id}/bus',
            'PATH': '/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin'}


def as_user(argv, user_id):
    """argv run as the service user, with its runtime directory and session bus.
    The rest of the environment (HIFIMATE_* secrets) is passed through."""
    return ['runuser', '-u', USER, '--', 'env', *(f'{k}={v}' for k, v in user_env(user_id).items()), *argv]


def root_helper(root, script, action, user_id, run=subprocess.run):
    """realtime_permissions.py / bluetooth_speaker.py, which check that they act
    for the user who asked (SUDO_UID); here root asks for the service user."""
    env = {**os.environ, 'SUDO_UID': str(user_id)}
    return run(['python3', '-B', '-I', str(Path(root) / 'scripts' / script), action, str(user_id), str(PREFIX)],
               env=env, capture_output=True, text=True)


# --- Samba --------------------------------------------------------------------------

def share_block(music):
    return '\n'.join([
        BEGIN,
        f'[{SHARE}]',
        '   comment = HiFiMate music library',
        f'   path = {music}',
        f'   valid users = {USER}',
        f'   force user = {USER}',
        f'   force group = {USER}',
        '   read only = no',
        '   create mask = 0664',
        '   directory mask = 2775',
        END, ''])


def strip_share(text):
    """smb.conf without our block (anything else untouched)."""
    out, inside = [], False
    for line in text.splitlines(keepends=True):
        if line.rstrip('\n') == BEGIN:
            inside = True
            while out and not out[-1].strip():      # the blank line written before the block
                out.pop()
        elif inside and line.rstrip('\n') == END:
            inside = False
        elif not inside:
            out.append(line)
    return ''.join(out)


def write_share_conf(music, conf=SMB_CONF):
    """Add (or update) the share; returns True when smb.conf changed."""
    conf = Path(conf)
    text = conf.read_text() if conf.exists() else '[global]\n   workgroup = WORKGROUP\n   server string = HiFiMate\n'
    if f'[{SHARE.lower()}]' in strip_share(text).lower():
        raise RuntimeError(f'{conf} 已经有一个名为 [{SHARE}] 的共享（不是 HiFiMate 的），没有改动它')
    new = strip_share(text).rstrip('\n') + '\n\n' + share_block(music)
    if new == text:
        return False
    conf.parent.mkdir(parents=True, exist_ok=True)
    stage = conf.with_name('.smb.conf.hifimate')
    stage.write_text(new)
    if conf.exists():
        shutil.copymode(conf, stage)
    os.replace(stage, conf)
    return True


AVAHI = '''<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- Managed by hifimate: lets macOS Finder list the share. -->
<service-group>
  <name replace-wildcards="yes">%h</name>
  <service><type>_smb._tcp</type><port>445</port></service>
</service-group>
'''

SMB_SERVICES = ('smbd.service', 'smb.service')


def smb_service(run=subprocess.run):
    for name in SMB_SERVICES:
        if run(['systemctl', 'cat', name], capture_output=True).returncode == 0:
            return name
    return None


def setup_share(music, password=None, run=subprocess.run, conf=SMB_CONF, avahi=AVAHI_SMB):
    """The `Music` share for user hifimate. Without a password (a reinstall that
    did not give one), the existing Samba password is kept."""
    if not shutil.which('smbpasswd'):
        raise RuntimeError('没有找到 Samba（smbpasswd）；安装软件包 samba，或加 --without-share')
    changed = write_share_conf(music, conf)
    if password is not None:
        run(['smbpasswd', '-s', '-a', USER], input=f'{password}\n{password}\n', text=True, check=True,
            capture_output=True)
    service = smb_service(run)
    if service:
        run(['systemctl', 'enable', '--now', service], check=True, capture_output=True)
        if changed:
            run(['systemctl', 'reload-or-restart', service], check=True, capture_output=True)
    if Path(avahi).parent.is_dir() and (not Path(avahi).exists() or Path(avahi).read_text() != AVAHI):
        Path(avahi).write_text(AVAHI)
    if shutil.which('firewall-cmd') and run(['firewall-cmd', '--state'], capture_output=True).returncode == 0:
        run(['firewall-cmd', '--permanent', '--add-service=samba'], capture_output=True)
        run(['firewall-cmd', '--reload'], capture_output=True)
    return service


def remove_share(run=subprocess.run, conf=SMB_CONF, avahi=AVAHI_SMB, account=False):
    """The share goes; hifimate's Samba account (its password) only with the
    user itself (account=True, uninstall --purge), like its Linux password."""
    conf = Path(conf)
    if conf.exists():
        text = conf.read_text()
        stripped = strip_share(text)
        if stripped != text:
            conf.write_text(stripped)
            service = smb_service(run)
            if service:
                run(['systemctl', 'reload-or-restart', service], capture_output=True)
    if account and shutil.which('pdbedit') and run(['pdbedit', '-u', USER], capture_output=True).returncode == 0:
        run(['smbpasswd', '-x', USER], capture_output=True)
    Path(avahi).unlink(missing_ok=True)


def has_password(shadow=SHADOW):
    """Whether hifimate has a usable Linux password (not locked, not empty)."""
    try:
        for line in Path(shadow).read_text().splitlines():
            name, _, rest = line.partition(':')
            if name == USER:
                field = rest.split(':', 1)[0]
                return bool(field) and field[0] not in '!*'
    except OSError:
        pass
    return False


def write_pam(pam=PAM_SERVICE, sshd=SSHD_DROPIN, run=subprocess.run):
    """The PAM service the control page uses, and no SSH for the account."""
    Path(pam).parent.mkdir(parents=True, exist_ok=True)
    if not Path(pam).exists() or Path(pam).read_text() != PAM_TEXT:
        Path(pam).write_text(PAM_TEXT)
    if Path(sshd).parent.is_dir() and (not Path(sshd).exists() or Path(sshd).read_text() != SSHD_TEXT):
        Path(sshd).write_text(SSHD_TEXT)
        for service in ('ssh.service', 'sshd.service'):
            run(['systemctl', 'try-reload-or-restart', service], capture_output=True)


def remove_pam(pam=PAM_SERVICE, sshd=SSHD_DROPIN, run=subprocess.run):
    Path(pam).unlink(missing_ok=True)
    if Path(sshd).exists():
        Path(sshd).unlink()
        for service in ('ssh.service', 'sshd.service'):
            run(['systemctl', 'try-reload-or-restart', service], capture_output=True)


def set_password(password, run=subprocess.run, conf=SMB_CONF):
    """The control password: hifimate's Linux password, and the share's when there
    is one. Returns True when the share's password was set too."""
    run(['chpasswd'], input=f'{USER}:{password}\n', text=True, check=True, capture_output=True)
    if shutil.which('smbpasswd') and Path(conf).exists() and BEGIN in Path(conf).read_text():
        run(['smbpasswd', '-s', '-a', USER], input=f'{password}\n{password}\n', text=True, check=True,
            capture_output=True)
        return True
    return False


def purge(run=subprocess.run):
    """After uninstall --purge: the user, its home and the prefix go; the music stays."""
    run(['loginctl', 'disable-linger', USER], capture_output=True)
    remove_share(run, account=True)
    user_id = uid()
    if user_id is not None:
        run(['systemctl', 'stop', f'user@{user_id}.service'], capture_output=True)
        run(['userdel', USER], check=True)
    for path in (PREFIX, HOME):
        if path.exists():
            shutil.rmtree(path)
    RECORD.unlink(missing_ok=True)
    remove_pam(run=run)
