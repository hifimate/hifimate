"""Persist and restore only our BlueZ Adapter1.Alias changes, keyed by address."""
import argparse
import asyncio
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import stat
import sys
import tempfile

from .identity import load

ADAPTER = 'org.bluez.Adapter1'
READBACK_TRIES, READBACK_INTERVAL = 30, 0.1   # bluetoothd sets a name asynchronously: up to 3 s


def notify_ready():
    address = os.environ.get('NOTIFY_SOCKET')
    if address:
        if address.startswith('@'):
            address = '\0' + address[1:]
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(b'READY=1')


def safe(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise RuntimeError(f'refusing symlink Bluetooth state: {part}')
    if path.exists():
        mode = path.stat()
        if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_nlink != 1:
            raise RuntimeError(f'unsafe Bluetooth state: {path}')


class Journal:
    def __init__(self, path, prefix):
        self.path = Path(path)
        self.prefix = str(Path(prefix).absolute())
        safe(self.path)
        self.data = {'schema': 1, 'prefix': self.prefix, 'adapters': {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())
            if self.data.get('schema') != 1 or self.data.get('prefix') != self.prefix:
                raise RuntimeError('Bluetooth journal belongs to another installation')
            for address, record in self.data['adapters'].items():
                if (not isinstance(address, str) or not isinstance(record['original'], str)
                        or not isinstance(record['owned'], str) or not isinstance(record['released'], bool)):
                    raise ValueError('invalid Bluetooth journal')

    def save(self):
        safe(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix='.hifimate-alias-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as out:
                json.dump(self.data, out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)


class BlueZ:
    async def connect(self):
        from dbus_next.aio import MessageBus
        from dbus_next import BusType
        self.bus = await asyncio.wait_for(MessageBus(bus_type=BusType.SYSTEM).connect(), 10)
        return self

    async def call(self, path, interface, member, signature='', body=None):
        from dbus_next import Message, MessageType
        reply = await asyncio.wait_for(self.bus.call(
            Message(destination='org.bluez', path=path, interface=interface,
                    member=member, signature=signature, body=body or [])), 10)
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f'BlueZ {member}: {reply.error_name}: {reply.body}. '
                               '需要系统 BlueZ D-Bus 写权限；检查发行版 bluetooth 组/Polkit 会话授权，不要用 root 运行安装器。')
        return reply.body

    async def adapters(self):
        objects = (await self.call('/', 'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects'))[0]
        return {props[ADAPTER]['Address'].value: {
            'path': path, 'alias': props[ADAPTER]['Alias'].value}
            for path, props in objects.items() if ADAPTER in props}

    async def objects(self):
        return (await self.call('/', 'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects'))[0]

    async def set(self, path, interface, name, value):
        await self.call(path, 'org.freedesktop.DBus.Properties', 'Set', 'ssv', [interface, name, value])

    async def set_alias(self, path, value):
        from dbus_next import Variant
        await self.call(path, 'org.freedesktop.DBus.Properties', 'Set', 'ssv',
                        [ADAPTER, 'Alias', Variant('s', value)])
        # bluetoothd applies the name through the kernel asynchronously: right
        # after Set, Get may still return the old one. Wait for it to settle.
        for attempt in range(READBACK_TRIES):
            actual = await self.call(path, 'org.freedesktop.DBus.Properties', 'Get', 'ss', [ADAPTER, 'Alias'])
            if actual[0].value == value:
                return
            await asyncio.sleep(READBACK_INTERVAL)
        raise RuntimeError(f'BlueZ Alias readback mismatch: {actual[0].value!r} instead of {value!r}')

    def close(self):
        self.bus.disconnect()


async def apply_alias(bluez, journal, desired):
    adapters = await bluez.adapters()
    for address, adapter in adapters.items():
        records = journal.data['adapters']
        record = records.get(address)
        if record and record['released']:
            continue
        if record and adapter['alias'] not in (
                (record['owned'], record.get('previous'), record['original'])
                if record.get('pending', False) else (record['owned'],)):
            record['released'] = True  # an administrator has taken ownership
            journal.save()
            print(f'保留管理员 Bluetooth Alias: {address}', file=sys.stderr)
            continue
        if record is None:
            # Already matching is not evidence of ownership; never restore someone else's alias.
            if adapter['alias'] == desired:
                continue
            record = {'original': adapter['alias'], 'owned': desired, 'released': False}
            records[address] = record
        if adapter['alias'] == desired and not record.get('pending', False):
            continue
        previous = record['owned']
        record['previous'] = previous
        record['owned'] = desired
        record['pending'] = True
        # Journal before Set: recovery handles a crash on either side of the D-Bus call.
        journal.save()
        try:
            await bluez.set_alias(adapter['path'], desired)
        except BaseException:
            # On error, Set may have succeeded; preserve both previous and desired as owned.
            record['previous'] = previous
            journal.save()
            raise
        record['pending'] = False
        record.pop('previous', None)
        journal.save()


async def restore_alias(bluez, journal):
    adapters = await bluez.adapters()
    missing = []
    for address, record in list(journal.data['adapters'].items()):
        if record['released']:
            del journal.data['adapters'][address]
            journal.save()
            continue
        if address not in adapters:
            missing.append(address)
            continue
        current = adapters[address]['alias']
        if current in (record['owned'], record.get('previous')):
            await bluez.set_alias(adapters[address]['path'], record['original'])
        elif current != record['original']:
            print(f'保留管理员 Bluetooth Alias: {address}', file=sys.stderr)
        del journal.data['adapters'][address]
        journal.save()
    if missing:
        raise RuntimeError('请重新连接以下蓝牙适配器后重试恢复/卸载；恢复日志已保留: ' + ', '.join(missing))
    journal.path.unlink(missing_ok=True)


async def execute(args):
    config = load(args.prefix)
    journal = Journal(config['bluetooth_state'], args.prefix)
    lock_path = journal.path.with_suffix('.lock')
    if args.action == 'restore' and not journal.path.exists() and not lock_path.exists():
        return
    safe(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'restore' and not journal.path.exists():
            return
        bluez = await BlueZ().connect()
        try:
            if args.action == 'restore':
                await restore_alias(bluez, journal)
            else:
                stop = asyncio.Event()
                for sig in (signal.SIGTERM, signal.SIGINT):
                    asyncio.get_running_loop().add_signal_handler(sig, stop.set)
                first = True
                # An absent adapter is supported: watch for hotplug and BlueZ object recreation.
                from .bluetooth_pair import close_unless_pairing
                while not stop.is_set():
                    await apply_alias(bluez, journal, config['name'] + ' Bluetooth')
                    for adapter, name in await close_unless_pairing(bluez):
                        print(f'关闭 {adapter} 的 {name}（不在配对窗口内）', file=sys.stderr, flush=True)
                    if first:
                        notify_ready()
                        first = False
                    try:
                        await asyncio.wait_for(stop.wait(), 10)
                    except asyncio.TimeoutError:
                        pass
                # Alias persists over service restarts; explicit uninstall restores it.
        finally:
            bluez.close()
    finally:
        os.close(fd)
        # Keep the lock inode during daemon restarts; remove only after complete restoration.
        if args.action == 'restore' and not journal.path.exists():
            lock_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'restore', 'pair'))
    parser.add_argument('--prefix', required=True, type=Path)
    parser.add_argument('--seconds', type=int, default=120,
                        help='pair: length of the speaker-style pairing window (10..600)')
    args = parser.parse_args()
    try:
        if args.action == 'pair':
            from .bluetooth_pair import run
            raise SystemExit(0 if asyncio.run(run(args.seconds)) else 1)
        asyncio.run(execute(args))
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(str(exc))


if __name__ == '__main__':
    main()
