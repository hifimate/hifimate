"""Speaker-style Bluetooth pairing window (like a speaker's pairing button).

For a bounded time the adapters are discoverable and pairable and a
NoInputNoOutput agent accepts the pairing ("Just Works": confirm on the phone
only, no code on this headless receiver). The first device that pairs is
marked trusted so it can reconnect later without an agent; then the window
closes. Outside this window HiFi is never discoverable or pairable.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import time

ADAPTER = 'org.bluez.Adapter1'
DEVICE = 'org.bluez.Device1'
AGENT_PATH = '/org/hifimate/pairing_agent'


def window_file():
    runtime = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.geteuid()}'
    return Path(runtime) / 'hifimate-bluetooth-pairing.json'


def window_active(path=None, now=time.time):
    """True while a live ``hifimate-bluetooth pair`` process holds an unexpired window."""
    try:
        data = json.loads((path or window_file()).read_text())
        os.kill(int(data['pid']), 0)
        return now() < float(data['until'])
    except (OSError, ValueError, KeyError, TypeError):
        return False


async def close_unless_pairing(bluez, path=None):
    """Outside a pairing window the receiver is never discoverable/pairable.

    bluetoothd starts pairable after every restart; hifimate-bluetooth.service calls
    this periodically so that state never persists.
    """
    if window_active(path):
        return []
    from dbus_next import Variant
    closed = []
    for adapter, interfaces in (await bluez.objects()).items():
        props = interfaces.get(ADAPTER)
        if not props:
            continue
        for name in ('Discoverable', 'Pairable'):
            if _value(props.get(name)):
                await bluez.set(adapter, ADAPTER, name, Variant('b', False))
                closed.append((adapter, name))
    return closed


def paired(objects):
    """Paths of paired BlueZ devices in a GetManagedObjects result."""
    return {path for path, interfaces in objects.items()
            if DEVICE in interfaces and _value(interfaces[DEVICE].get('Paired'))}


def _value(variant):
    return getattr(variant, 'value', variant)


def agent_interface(handled=None):
    """BlueZ calls this agent while pairing; every request is accepted.

    ``handled`` collects the device paths BlueZ asked about, so that a phone
    re-pairing (already known here, e.g. after it forgot HiFi) also counts.
    """
    handled = set() if handled is None else handled
    from dbus_next.service import ServiceInterface, method

    class Agent(ServiceInterface):
        def __init__(self):
            super().__init__('org.bluez.Agent1')

        @method()
        def Release(self): pass

        @method()
        def RequestPinCode(self, device: 'o') -> 's':
            handled.add(device); return '0000'  # legacy pre-2.1 devices

        @method()
        def DisplayPinCode(self, device: 'o', pincode: 's'): pass

        @method()
        def RequestPasskey(self, device: 'o') -> 'u':
            handled.add(device); return 0

        @method()
        def DisplayPasskey(self, device: 'o', passkey: 'u', entered: 'q'): pass

        @method()
        def RequestConfirmation(self, device: 'o', passkey: 'u'):
            handled.add(device)

        @method()
        def RequestAuthorization(self, device: 'o'):
            handled.add(device)

        @method()
        def AuthorizeService(self, device: 'o', uuid: 's'):
            handled.add(device)

        @method()
        def Cancel(self): pass

    return Agent()


async def window(bluez, seconds, report=print, sleep=asyncio.sleep, clock=None):
    """Open the pairing window on every adapter; return the newly trusted device."""
    from dbus_next import Variant
    loop = asyncio.get_running_loop()
    clock = clock or loop.time
    objects = await bluez.objects()
    adapters = [path for path, interfaces in objects.items() if ADAPTER in interfaces]
    if not adapters:
        raise RuntimeError('no Bluetooth adapter')
    before = paired(objects)
    handled = set()
    marker = window_file()
    marker.write_text(json.dumps({'pid': os.getpid(), 'until': time.time() + seconds + 5}))
    await bluez.register_agent(AGENT_PATH, 'NoInputNoOutput', handled)
    try:
        for path in adapters:
            await bluez.set(path, ADAPTER, 'Pairable', Variant('b', True))
            await bluez.set(path, ADAPTER, 'DiscoverableTimeout', Variant('u', int(seconds)))
            await bluez.set(path, ADAPTER, 'Discoverable', Variant('b', True))
        names = ', '.join(sorted({_value(objects[p][ADAPTER].get('Alias')) or p for p in adapters}))
        report(f'配对窗口已打开 {int(seconds)} 秒：在手机蓝牙设置中选择 “{names}” 并确认配对。')
        end = clock() + seconds
        while clock() < end:
            await sleep(1)
            current = await bluez.objects()
            # New bonds, and known devices that just re-paired through our agent.
            for path in sorted(paired(current) - before | paired(current) & handled):
                await bluez.set(path, DEVICE, 'Trusted', Variant('b', True))
                name = _value(current[path][DEVICE].get('Alias')) or path
                report(f'已配对并信任：{name}。以后在手机上直接连接即可。')
                return path
        report('配对窗口已结束，没有新设备配对。')
        return None
    finally:
        # Unregister first: BlueZ re-enables Pairable when the default agent
        # goes away, which would leave the receiver pairable afterwards.
        try:
            await bluez.unregister_agent(AGENT_PATH)
        finally:
            for path in adapters:
                for name in ('Discoverable', 'Pairable'):
                    try:
                        await bluez.set(path, ADAPTER, name, Variant('b', False))
                    except Exception as error:  # still close the remaining state
                        print(f'关闭 {name} 失败：{error}', file=sys.stderr)
            marker.unlink(missing_ok=True)


class Bus:
    """Minimal BlueZ client on the system bus, exporting the pairing agent."""
    async def connect(self):
        from dbus_next.aio import MessageBus
        from dbus_next import BusType
        self.bus = await asyncio.wait_for(MessageBus(bus_type=BusType.SYSTEM).connect(), 10)
        return self

    async def call(self, path, interface, member, signature='', body=None):
        from dbus_next import Message, MessageType
        reply = await asyncio.wait_for(self.bus.call(Message(
            destination='org.bluez', path=path, interface=interface, member=member,
            signature=signature, body=body or [])), 10)
        if reply.message_type == MessageType.ERROR:
            raise RuntimeError(f'BlueZ {member}: {reply.error_name}: {reply.body}')
        return reply.body

    async def objects(self):
        return (await self.call('/', 'org.freedesktop.DBus.ObjectManager', 'GetManagedObjects'))[0]

    async def set(self, path, interface, name, value):
        await self.call(path, 'org.freedesktop.DBus.Properties', 'Set', 'ssv', [interface, name, value])

    async def register_agent(self, path, capability, handled=None):
        self.bus.export(path, agent_interface(handled))
        await self.call('/org/bluez', 'org.bluez.AgentManager1', 'RegisterAgent', 'os', [path, capability])
        await self.call('/org/bluez', 'org.bluez.AgentManager1', 'RequestDefaultAgent', 'o', [path])

    async def unregister_agent(self, path):
        try:
            await self.call('/org/bluez', 'org.bluez.AgentManager1', 'UnregisterAgent', 'o', [path])
        finally:
            self.bus.unexport(path)

    def close(self):
        self.bus.disconnect()


async def run(seconds):
    if not 10 <= seconds <= 600:
        raise ValueError('pairing window must be 10..600 seconds')
    bluez = await Bus().connect()
    try:
        return await window(bluez, seconds)
    finally:
        bluez.close()
