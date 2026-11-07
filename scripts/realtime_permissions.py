#!/usr/bin/env python3
"""Explicit privileged operation; fixed per-UID files, no session restart."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys


def check_user_defaults(run=subprocess.run, config_home=None):
    """Refuse explicit policies that would undo the inherited manager allowance."""
    result = run(['systemd-analyze', 'cat-config', 'systemd/user.conf'],
                 check=True, capture_output=True, text=True, timeout=10)
    home = Path(config_home or os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))
    personal = home/'systemd/user.conf'
    text = result.stdout
    if personal.exists():
        # The per-user main config replaces the system main, but not its drop-ins.
        chunks = text.splitlines(keepends=True)
        include = False
        dropins = []
        for line in chunks:
            if line.startswith('# /'):
                include = '/user.conf.d/' in line
            if include:
                dropins.append(line)
        text = personal.read_text() + '\n' + ''.join(dropins)
    section = ''
    value = ''
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
        key, separator, candidate = line.partition('=')
        if section == 'Manager' and separator and key.strip() == 'DefaultLimitRTPRIO':
            value = candidate.strip()
    if value and any(part != 'infinity' and int(part) < 5 for part in value.split(':')):
        raise RuntimeError('explicit DefaultLimitRTPRIO below 5 conflicts with realtime setup; '
                           'adjust the user manager policy first (no changes made)')


def owned(path, owner=0):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != owner or st.st_mode & 0o022:
        raise RuntimeError(f'unsafe privileged file: {path}')


def directory(path, owner=0, mode=0o755):
    if not path.exists():
        directory(path.parent, owner)
        path.mkdir(mode=mode)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != owner or st.st_mode & 0o022:
        raise RuntimeError(f'unsafe privileged directory: {path}')


def apply(action, uid, prefix, root=Path('/'), run=subprocess.run, owner=0):
    prefix = Path(prefix)
    if not prefix.is_absolute() or prefix.resolve() != prefix:
        raise RuntimeError('prefix must be absolute without symlinks')
    base = root/'etc/systemd/system'/f'user@{uid}.service.d'
    state = root/'var/lib/hifimate-realtime'/f'{uid}.json'
    target = base/'90-hifimate-rtprio.conf'
    # Validate every existing privileged ancestor, not only the leaf.
    for leaf in (base, state.parent):
        relative = leaf.relative_to(root)
        current = root
        for part in relative.parts:
            current = current/part
            if current.exists() or current.is_symlink():
                st = current.lstat()
                if not stat.S_ISDIR(st.st_mode) or st.st_uid != owner or st.st_mode & 0o022:
                    raise RuntimeError(f'unsafe privileged directory: {current}')
    previous = None
    if state.exists() or state.is_symlink():
        owned(state, owner)
        previous = json.loads(state.read_text())
        if previous['uid'] != uid or previous['prefix'] != str(prefix):
            raise RuntimeError('realtime permission owned by another prefix')
        if not target.exists() and not target.is_symlink():
            if action != 'remove':
                raise RuntimeError('realtime drop-in missing; retry removal before setup')
            # Removal may have completed before deleting the ownership record failed.
            run(['systemctl', 'daemon-reload'], check=True, timeout=20)
            state.unlink()
            return
        owned(target, owner)
        if hashlib.sha256(target.read_bytes()).hexdigest() != previous['sha256']:
            raise RuntimeError('realtime drop-in modified; preserving it')
    elif target.exists() or target.is_symlink():
        raise RuntimeError('unmanaged realtime drop-in exists; preserving it')
    if action == 'remove':
        if not previous:
            return
        data = target.read_bytes()
        target.unlink()
        try:
            run(['systemctl', 'daemon-reload'], check=True, timeout=20)
        except Exception:
            target.write_bytes(data)
            raise
        state.unlink()
        try: target.parent.rmdir()      # the drop-in directory, when nothing else is in it
        except OSError: pass
        return
    if previous:
        return
    result = run(['systemctl', 'show', f'user@{uid}.service',
                  '--property=LimitRTPRIO,LimitRTPRIOSoft'], check=True,
                 capture_output=True, text=True, timeout=10)
    props = dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    def limit(name):
        value = props[name]
        return float('inf') if value == 'infinity' else int(value)
    hard, soft = max(5,limit('LimitRTPRIO')), max(5,limit('LimitRTPRIOSoft'))
    hard = max(hard,soft)
    def setting(value):
        return 'infinity' if value == float('inf') else str(value)
    data = f'# Managed by HiFiMate; do not edit\n[Service]\nLimitRTPRIO={setting(soft)}:{setting(hard)}\n'.encode()
    directory(base, owner)
    directory(state.parent, owner, 0o700)
    record = json.dumps(dict(uid=uid,prefix=str(prefix),sha256=hashlib.sha256(data).hexdigest())).encode()
    # Exclusive creation refuses collisions. Roll back both on any ordinary failure.
    created = []
    try:
        for path, content in ((state,record),(target,data)):
            fd = os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            created.append(path)
            with os.fdopen(fd,'wb') as stream:
                stream.write(content);stream.flush();os.fsync(stream.fileno())
        run(['systemctl','daemon-reload'],check=True,timeout=20)
    except Exception:
        for path in reversed(created): path.unlink()
        raise


def main():
    if sys.argv[1:] == ['check-user']:
        check_user_defaults()
        return
    if os.geteuid() != 0 or len(sys.argv) != 4 or sys.argv[1] not in ('install','remove'):
        raise SystemExit('requires sudo: realtime_permissions.py install|remove UID PREFIX')
    uid = int(sys.argv[2])
    if uid <= 0 or os.environ.get('SUDO_UID') != str(uid):
        raise SystemExit('UID must match invoking non-root sudo user')
    pwd.getpwuid(uid)
    apply(sys.argv[1],uid,sys.argv[3])


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
