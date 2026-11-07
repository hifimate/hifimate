"""Serve AirPlay only on the default-route interface, and follow route changes.

Shairport otherwise advertises on every interface (docker0, Compose bridges,
lo). Music Assistant runs with host networking, sees all of them and may pick
a bridge address, where AirPlay timing never syncs and no audio plays. LAN
senders only see the LAN interface. ``render`` writes a runtime copy of the
installed config with ``interface`` set before every Shairport start;
``watch`` restarts Shairport when the default route moves to another interface.
"""
import argparse
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tempfile
import time

INTERFACE = re.compile(r'[A-Za-z0-9_.:@-]{1,15}')
GENERAL = re.compile(r'(\bgeneral\s*=\s*\{)')
SETTING = re.compile(r'^\s*general\s*=\s*\{[^}]*\binterface\s*=\s*"([^"]*)"', re.M)


def default_interface(run=subprocess.run):
    """Device of the lowest-metric default route (IPv4 first), or None."""
    for family in ('-4', '-6'):
        try:
            routes = json.loads(run(['ip', '-j', family, 'route', 'show', 'default'], check=True,
                                    capture_output=True, text=True, timeout=5).stdout or '[]')
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        devices = sorted((route.get('metric', 0), route['dev']) for route in routes
                         if route.get('dev') and INTERFACE.fullmatch(route['dev']))
        if devices:
            return devices[0][1]
    return None


def configured_interface(text):
    match = SETTING.search(text)
    return match.group(1) if match else None


def render_config(text, interface):
    """Insert ``interface`` into the general group unless one is configured."""
    if interface is None or configured_interface(text) is not None:
        return text
    if not INTERFACE.fullmatch(interface):
        raise ValueError(f'unsafe interface name: {interface!r}')
    rendered, count = GENERAL.subn(rf'\1 interface = "{interface}";', text, count=1)
    if count != 1:
        raise ValueError('shairport config has no general group')
    return rendered


def write(path, text):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.shairport-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
        os.chmod(name, 0o600)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def render(source, target, run=subprocess.run):
    interface = default_interface(run)
    write(target, render_config(Path(source).read_text(), interface))
    # No default route (offline): keep Shairport's automatic interface choice.
    print(f'HiFiMate AirPlay interface: {interface or "automatic (no default route)"}', file=sys.stderr)
    return interface


def watch(target, unit, run=subprocess.run, popen=subprocess.Popen, clock=time.monotonic,
          settle=2.0, recheck=60.0, once=False):
    """Restart ``unit`` when the default-route interface differs from the served one."""
    monitor = popen(['ip', '-o', 'monitor', 'route'], stdout=subprocess.PIPE, text=True)
    pending = clock()  # check once at start, too
    try:
        while True:
            timeout = max(0.0, pending - clock()) if pending is not None else recheck
            readable, _, _ = select.select([monitor.stdout], [], [], timeout)
            if readable:
                if not monitor.stdout.readline():
                    raise RuntimeError('ip monitor ended')
                pending = clock() + settle  # coalesce bursts of route updates
                continue
            pending = None
            try:
                served = configured_interface(Path(target).read_text())
            except FileNotFoundError:
                served = None  # Shairport is not running; nothing to follow
            else:
                current = default_interface(run)
                if current is not None and current != served:
                    print(f'default route moved {served} -> {current}; restarting {unit}', flush=True)
                    run(['systemctl', '--user', 'try-restart', unit], check=False, timeout=30)
            if once:
                return
    finally:
        monitor.terminate()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    r = commands.add_parser('render'); r.add_argument('source'); r.add_argument('target')
    w = commands.add_parser('watch'); w.add_argument('target'); w.add_argument('--unit', default='hifimate-airplay.service')
    args = parser.parse_args()
    if args.command == 'render':
        render(args.source, args.target)
    else:
        watch(args.target, args.unit)


if __name__ == '__main__':
    main()
