#!/usr/bin/env python3
"""Explicit privileged step: advertise the Bluetooth adapter as a loudspeaker.

BlueZ 5 reads the default device class only from ``/etc/bluetooth/main.conf``
(no drop-in directory) and otherwise derives it from the host chassis, e.g.
"Computer / Laptop". A speaker uses major class Audio/Video, minor class
Loudspeaker (0x000414); BlueZ adds the Audio/Rendering service bits itself
from the registered A2DP sink. ``JustWorksRepairing = confirm`` lets a phone
that forgot HiFi pair again: BlueZ asks the agent, and only ``hifimate-bluetooth
pair`` registers one, so re-pairing still works only inside a pairing window
(the default "never" rejects it, which phones report as a wrong PIN).
BlueZ's hostname plugin would still overwrite
the class from the chassis on every power-on, so bluetoothd also gets a
systemd drop-in that adds ``--noplugin=hostname`` (adapter names are set by
hifimate-bluetooth via Alias anyway). This helper inserts exactly two marked lines
into ``[General]`` and writes that drop-in, records both in a root-owned file,
and removal deletes only unchanged managed content. An administrator's own
``Class`` or bluetoothd plugin options are never replaced.
"""
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile

CLASS = '0x000414'
MARKER = '# Managed by HiFiMate (--setup-bluetooth-speaker): Audio/Video, Loudspeaker'
LINES = [MARKER, f'Class = {CLASS}']  # first managed lines
REPAIRING = 'JustWorksRepairing = confirm'
DROPIN = 'etc/systemd/system/bluetooth.service.d/90-hifimate-speaker.conf'


def dropin(run):
    """Unit drop-in: the distribution's own bluetoothd argv plus --noplugin=hostname."""
    shown = run(['systemctl', 'show', 'bluetooth.service', '--property=ExecStart', '--value'],
                check=True, capture_output=True, text=True, timeout=10).stdout
    argv = shown.split('argv[]=', 1)[1].split(' ;', 1)[0].split() if 'argv[]=' in shown else []
    if not argv or not argv[0].startswith('/') or Path(argv[0]).name != 'bluetoothd':
        raise RuntimeError('cannot determine bluetoothd ExecStart')
    if any(arg.startswith(('-P', '--noplugin', '-p', '--plugin', '-f', '--configfile')) for arg in argv[1:]):
        raise RuntimeError('bluetoothd already has plugin/config options; leaving the administrator setup')
    return ('# Managed by HiFiMate (--setup-bluetooth-speaker): keep the loudspeaker class;\n'
            '# the hostname plugin would reset it from the machine chassis.\n'
            '[Service]\nExecStart=\nExecStart=' + ' '.join(argv + ['--noplugin=hostname']) + '\n')


def _owned(path, owner):
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != owner or st.st_mode & 0o022:
        raise RuntimeError(f'unsafe privileged file: {path}')


def _general(lines):
    """Index range (start, end) of the [General] section body, or None."""
    start = next((i for i, line in enumerate(lines) if line.strip() == '[General]'), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith('[')), len(lines))
    return start + 1, end


def active(lines, name):
    span = _general(lines)
    if not span:
        return None
    for line in lines[span[0]:span[1]]:
        key, sep, value = line.partition('=')
        if sep and not line.lstrip().startswith(('#', ';')) and key.strip() == name:
            return value.strip()
    return None


def active_class(lines):
    return active(lines, 'Class')


def add(text):
    """Return (new text, managed lines); an administrator's own keys are kept."""
    lines = text.splitlines()
    current = active_class(lines)
    if current is not None:
        raise RuntimeError(f'main.conf already sets Class = {current}; leaving the administrator setting')
    block = LINES + ([REPAIRING] if active(lines, 'JustWorksRepairing') is None else [])
    span = _general(lines)
    if span is None:
        lines = ['[General]', *block, ''] + lines
    else:
        lines[span[0]:span[0]] = block
    return '\n'.join(lines) + '\n', block


def strip(text, block=LINES):
    lines = text.splitlines()
    for i in range(len(lines) - len(block) + 1):
        if lines[i:i + len(block)] == block:
            del lines[i:i + len(block)]
            return '\n'.join(lines) + '\n'
    raise RuntimeError('managed Class lines changed or missing; preserving main.conf')


def _write(path, text, owner):
    st = path.stat()
    fd, name = tempfile.mkstemp(prefix='.main.conf.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.chmod(name, stat.S_IMODE(st.st_mode))
        if owner == 0:
            os.chown(name, st.st_uid, st.st_gid)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def apply(action, uid, prefix, root=Path('/'), run=subprocess.run, owner=0):
    prefix = Path(prefix)
    if not prefix.is_absolute() or prefix.resolve() != prefix:
        raise RuntimeError('prefix must be absolute without symlinks')
    conf = root / 'etc/bluetooth/main.conf'
    unit = root / DROPIN
    record = root / 'var/lib/hifimate-bluetooth-speaker' / f'{uid}.json'
    _owned(conf, owner)
    text = conf.read_text()
    previous = None
    if record.exists() or record.is_symlink():
        _owned(record, owner)
        previous = json.loads(record.read_text())
        if previous.get('prefix') != str(prefix) or previous.get('uid') != uid:
            raise RuntimeError('speaker class owned by another installation')
    if action == 'install':
        if previous:
            if active_class(text.splitlines()) == CLASS and MARKER in text:
                return False
            raise RuntimeError('managed Class lines changed; preserving main.conf')
        new, block = add(text)
        if unit.exists() or unit.is_symlink():
            raise RuntimeError(f'unmanaged {unit} exists; preserving it')
        unit_text = dropin(run)
        record.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(dict(uid=uid, prefix=str(prefix), lines=block,
                           dropin_sha256=hashlib.sha256(unit_text.encode()).hexdigest()), stream)
        created = []
        try:
            unit.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            fd = os.open(unit, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
            created.append(unit)
            with os.fdopen(fd, 'w') as stream:
                stream.write(unit_text)
            _write(conf, new, owner)
        except BaseException:
            for path in created: path.unlink()
            record.unlink(); raise
    else:
        if not previous:
            return False
        stripped = strip(text, previous['lines'])
        if unit.exists():
            _owned(unit, owner)
            if hashlib.sha256(unit.read_bytes()).hexdigest() != previous.get('dropin_sha256'):
                raise RuntimeError(f'{unit} changed; preserving it')
        _write(conf, stripped, owner)
        unit.unlink(missing_ok=True)
        record.unlink()
    run(['systemctl', 'daemon-reload'], check=True, timeout=30)
    # bluetoothd reads the class only at start; this drops current connections.
    run(['systemctl', 'restart', 'bluetooth.service'], check=True, timeout=30)
    return True


def main():
    if os.geteuid() != 0 or len(sys.argv) != 4 or sys.argv[1] not in ('install', 'remove'):
        raise SystemExit('requires sudo: bluetooth_speaker.py install|remove UID PREFIX')
    uid = int(sys.argv[2])
    if uid <= 0 or os.environ.get('SUDO_UID') != str(uid):
        raise SystemExit('UID must match invoking non-root sudo user')
    pwd.getpwuid(uid)
    changed = apply(sys.argv[1], uid, sys.argv[3])
    print(('Bluetooth speaker class ' + ('set' if sys.argv[1] == 'install' else 'removed')) if changed
          else 'Bluetooth speaker class already in the requested state')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
