import asyncio
import json
from pathlib import Path

import pytest

from hifimate.bluetooth_alias import Journal, apply_alias, restore_alias


class BlueZ:
    def __init__(self, fail=False):
        self.objects = {'AA:BB:CC:DD:EE:FF': {'path': '/org/bluez/hci7', 'alias': 'Original'},
                        '11:22:33:44:55:66': {'path': '/org/bluez/hci3', 'alias': 'USB'}}
        self.calls = []
        self.fail = fail

    async def adapters(self):
        return self.objects

    async def set_alias(self, path, value):
        self.calls.append((path, value))
        if self.fail:
            raise RuntimeError('AccessDenied')
        next(item for item in self.objects.values() if item['path'] == path)['alias'] = value


def test_enumerates_addresses_restores_only_owned(tmp_path):
    async def scenario():
        bus = BlueZ()
        journal = Journal(tmp_path / 'state.json', tmp_path)
        await apply_alias(bus, journal, '客厅 Bluetooth')
        assert bus.calls == [('/org/bluez/hci7', '客厅 Bluetooth'), ('/org/bluez/hci3', '客厅 Bluetooth')]
        bus.objects['AA:BB:CC:DD:EE:FF']['alias'] = 'Administrator'
        await apply_alias(bus, journal, 'New Bluetooth')
        assert bus.objects['AA:BB:CC:DD:EE:FF']['alias'] == 'Administrator'
        await restore_alias(bus, Journal(journal.path, tmp_path))
        assert bus.objects['AA:BB:CC:DD:EE:FF']['alias'] == 'Administrator'
        assert bus.objects['11:22:33:44:55:66']['alias'] == 'USB'
        assert not journal.path.exists()
    asyncio.run(scenario())


def test_unplugged_adapter_preserves_recovery_until_reconnected(tmp_path):
    async def scenario():
        bus = BlueZ()
        journal = Journal(tmp_path / 'state.json', tmp_path)
        await apply_alias(bus, journal, 'HiFi Bluetooth')
        missing = bus.objects.pop('AA:BB:CC:DD:EE:FF')
        with pytest.raises(RuntimeError, match='重新连接'):
            await restore_alias(bus, journal)
        assert journal.path.exists()
        missing['path'] = '/org/bluez/hci12'
        bus.objects['AA:BB:CC:DD:EE:FF'] = missing
        await restore_alias(bus, journal)
        assert missing['alias'] == 'Original'
    asyncio.run(scenario())


def test_permission_failure_journal_written_before_dbus(tmp_path):
    async def scenario():
        bus = BlueZ(fail=True)
        journal = Journal(tmp_path / 'state.json', tmp_path)
        with pytest.raises(RuntimeError, match='AccessDenied'):
            await apply_alias(bus, journal, 'HiFi Bluetooth')
        assert json.loads(journal.path.read_text())['adapters']['AA:BB:CC:DD:EE:FF']['original'] == 'Original'
        bus.fail = False
        await restore_alias(bus, journal)
        assert bus.objects['AA:BB:CC:DD:EE:FF']['alias'] == 'Original'
    asyncio.run(scenario())


def test_existing_matching_alias_is_not_claimed(tmp_path):
    async def scenario():
        bus = BlueZ()
        bus.objects = {'a': {'path': '/org/bluez/hci9', 'alias': 'HiFi Bluetooth'}}
        journal = Journal(tmp_path / 'state.json', tmp_path)
        await apply_alias(bus, journal, 'HiFi Bluetooth')
        assert not bus.calls
        await restore_alias(bus, journal)
        assert not bus.calls
    asyncio.run(scenario())


def test_state_symlink_rejected(tmp_path):
    outside = tmp_path / 'external'
    outside.write_text('external')
    link = tmp_path / 'state'
    link.symlink_to(outside)
    with pytest.raises(RuntimeError, match='symlink'):
        Journal(link, tmp_path)
    assert outside.read_text() == 'external'


def test_administrator_restores_original_name_is_not_overridden(tmp_path):
    async def scenario():
        bus = BlueZ()
        journal = Journal(tmp_path / 'state.json', tmp_path)
        await apply_alias(bus, journal, 'HiFi Bluetooth')
        bus.objects['AA:BB:CC:DD:EE:FF']['alias'] = 'Original'
        count = len(bus.calls)
        await apply_alias(bus, journal, 'HiFi Bluetooth')
        assert len(bus.calls) == count
        await restore_alias(bus, journal)
        assert bus.objects['AA:BB:CC:DD:EE:FF']['alias'] == 'Original'
    asyncio.run(scenario())


def test_real_dbus_adapter_properties_protocol(monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace
    from hifimate.bluetooth_alias import BlueZ as Client
    dbus = ModuleType('dbus_next')
    dbus.Message = lambda **kwargs: SimpleNamespace(**kwargs)
    dbus.MessageType = SimpleNamespace(ERROR='ERROR')
    dbus.Variant = lambda signature, value: SimpleNamespace(signature=signature, value=value)
    monkeypatch.setitem(sys.modules, 'dbus_next', dbus)
    calls = []
    class Bus:
        async def call(self, message):
            calls.append(message)
            if message.member == 'GetManagedObjects':
                body = [{'/org/bluez/hci8': {'org.bluez.Adapter1': {
                    'Address': SimpleNamespace(value='12:34:56:78:AB:CD'),
                    'Alias': SimpleNamespace(value='Before')}}}]
            elif message.member == 'Get':
                body = [SimpleNamespace(value='客厅 Bluetooth')]
            else: body = []
            return SimpleNamespace(message_type='RETURN', body=body)
    async def scenario():
        client = Client(); client.bus = Bus()
        adapters = await client.adapters()
        assert adapters['12:34:56:78:AB:CD']['path'] == '/org/bluez/hci8'
        await client.set_alias('/org/bluez/hci8', '客厅 Bluetooth')
        setting = calls[1]
        assert setting.destination == 'org.bluez'
        assert setting.path == '/org/bluez/hci8'
        assert setting.interface == 'org.freedesktop.DBus.Properties'
        assert setting.member == 'Set' and setting.signature == 'ssv'
        assert setting.body[:2] == ['org.bluez.Adapter1', 'Alias']
        assert setting.body[2].value == '客厅 Bluetooth'
    asyncio.run(scenario())


def test_alias_readback_waits_for_bluez_to_apply_the_name(monkeypatch):
    # bluetoothd applies a new name asynchronously: the first Gets still have the old one
    import sys
    from types import ModuleType, SimpleNamespace
    import hifimate.bluetooth_alias as alias
    dbus = ModuleType('dbus_next')
    dbus.Message = lambda **kwargs: SimpleNamespace(**kwargs)
    dbus.MessageType = SimpleNamespace(ERROR='ERROR')
    dbus.Variant = lambda signature, value: SimpleNamespace(signature=signature, value=value)
    monkeypatch.setitem(sys.modules, 'dbus_next', dbus)
    monkeypatch.setattr(alias, 'READBACK_INTERVAL', 0)
    def bus_with(names):
        answers = iter(names)
        class Bus:
            async def call(self, message):
                body = [SimpleNamespace(value=next(answers))] if message.member == 'Get' else []
                return SimpleNamespace(message_type='RETURN', body=body)
        return Bus()
    async def scenario(bus):
        client = alias.BlueZ(); client.bus = bus
        await client.set_alias('/org/bluez/hci0', '客厅 Bluetooth')
    asyncio.run(scenario(bus_with(['old-name', 'old-name', '客厅 Bluetooth'])))
    # never applied: reported, with both names
    with pytest.raises(RuntimeError, match="'old-name' instead of '客厅 Bluetooth'"):
        asyncio.run(scenario(bus_with(['old-name'] * alias.READBACK_TRIES)))
