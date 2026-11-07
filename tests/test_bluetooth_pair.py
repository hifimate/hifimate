import asyncio
from types import SimpleNamespace
import pytest
from hifimate import bluetooth_pair as bp

A, PHONE, OLD = '/org/bluez/hci0', '/org/bluez/hci0/dev_AA', '/org/bluez/hci0/dev_BB'
V = lambda value: SimpleNamespace(value=value)


class FakeBlueZ:
    def __init__(self, pairs_after=None):
        self.ticks = 0; self.pairs_after = pairs_after; self.sets = []; self.agent = []
    async def objects(self):
        self.ticks += 1
        devices = {OLD: {bp.DEVICE: {'Paired': V(True), 'Alias': V('old')}}}
        if self.pairs_after is not None and self.ticks > self.pairs_after:
            devices[PHONE] = {bp.DEVICE: {'Paired': V(True), 'Alias': V('Phone')}}
        return {A: {bp.ADAPTER: {'Alias': V('HiFi Bluetooth')}}, **devices}
    async def set(self, path, interface, name, value):
        self.sets.append((path, name, value.value))
    async def register_agent(self, path, capability, handled=None):
        self.agent.append(('register', capability)); self.handled = handled
    async def unregister_agent(self, path):
        self.agent.append(('unregister',)); self.sets.append((A, 'agent-unregistered', True))


@pytest.fixture(autouse=True)
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    return tmp_path


def run(bluez, seconds=30):
    t = [0.0]
    async def sleep(s): t[0] += s
    out = []
    result = asyncio.run(bp.window(bluez, seconds, report=out.append, sleep=sleep, clock=lambda: t[0]))
    return result, out


def test_first_new_pairing_is_trusted_and_the_window_closes():
    bluez = FakeBlueZ(pairs_after=3)
    result, out = run(bluez)
    assert result == PHONE and 'HiFi Bluetooth' in out[0] and 'Phone' in out[-1]
    assert (PHONE, 'Trusted', True) in bluez.sets and (OLD, 'Trusted', True) not in bluez.sets
    assert bluez.sets[:3] == [(A, 'Pairable', True), (A, 'DiscoverableTimeout', 30), (A, 'Discoverable', True)]
    # BlueZ re-enables Pairable when the agent goes away: unregister first.
    assert bluez.sets[-3:] == [(A, 'agent-unregistered', True), (A, 'Discoverable', False), (A, 'Pairable', False)]
    assert bluez.agent == [('register', 'NoInputNoOutput'), ('unregister',)]


def test_timeout_without_pairing_still_closes_everything():
    bluez = FakeBlueZ()
    result, out = run(bluez, seconds=10)
    assert result is None and '没有新设备' in out[-1]
    assert bluez.sets[-2:] == [(A, 'Discoverable', False), (A, 'Pairable', False)] and bluez.agent[-1] == ('unregister',)


def test_window_bounds():
    with pytest.raises(ValueError):
        asyncio.run(bp.run(5))


def test_marker_is_live_only_during_the_window(runtime):
    bluez = FakeBlueZ(pairs_after=1)
    seen = []
    original = bluez.objects
    async def objects():
        seen.append(bp.window_active()); return await original()
    bluez.objects = objects
    run(bluez)
    assert seen[1:] and all(seen[1:])  # active while the window is open
    assert not bp.window_file().exists() and not bp.window_active()


def test_outside_a_window_discoverable_and_pairable_are_closed(runtime):
    class Adapter(FakeBlueZ):
        async def objects(self):
            return {A: {bp.ADAPTER: {'Pairable': V(True), 'Discoverable': V(False)}}}
    bluez = Adapter()
    assert asyncio.run(bp.close_unless_pairing(bluez)) == [(A, 'Pairable')]
    assert bluez.sets == [(A, 'Pairable', False)]
    import json, os, time
    bp.window_file().write_text(json.dumps({'pid': os.getpid(), 'until': time.time() + 60}))
    bluez.sets.clear(); assert asyncio.run(bp.close_unless_pairing(bluez)) == [] and not bluez.sets
    bp.window_file().write_text(json.dumps({'pid': 2 ** 22 + 12345, 'until': time.time() + 60}))  # dead pid
    assert asyncio.run(bp.close_unless_pairing(bluez)) == [(A, 'Pairable')]


def test_known_device_re_pairing_through_the_agent_completes_the_window():
    bluez = FakeBlueZ()
    original = bluez.objects
    async def objects():
        if bluez.ticks == 3: bluez.handled.add(OLD)  # BlueZ asked our agent about the known phone
        return await original()
    bluez.objects = objects
    result, out = run(bluez)
    assert result == OLD and (OLD, 'Trusted', True) in bluez.sets and 'old' in out[-1]


@pytest.mark.filterwarnings("ignore:'typing.no_type_check_decorator' is deprecated:DeprecationWarning")
def test_real_agent_records_every_device_it_was_asked_about():
    pytest.importorskip('dbus_next')
    handled = set()
    agent = bp.agent_interface(handled)
    agent.RequestConfirmation('/org/bluez/hci0/dev_A', 123456)
    agent.RequestAuthorization('/org/bluez/hci0/dev_B')
    agent.AuthorizeService('/org/bluez/hci0/dev_C', '0000110b-0000-1000-8000-00805f9b34fb')
    assert handled == {'/org/bluez/hci0/dev_A', '/org/bluez/hci0/dev_B', '/org/bluez/hci0/dev_C'}
