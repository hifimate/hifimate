#!/usr/bin/env python3
"""Initialize Music Assistant player settings for the HiFi receivers.

Music Assistant merges protocol players of one host into a universal player.
Its "<name> AirPlay" player therefore also links HiFi's own upmpdcli renderer
("<name> DLNA"), and MA then routes that player's volume/mute (and playback,
when AirPlay is briefly unavailable) into the DLNA lane. HiFi DLNA serves
external controllers; MA plays through "HiFi MA" (MPD 6601) instead.

``apply`` (only while the MA server is stopped; it owns settings.json):
- adds MA's official "MPD Players" provider pointed at HiFiMate's MPD
  (127.0.0.1:6601), and gives that player the settings HiFiMate needs: WAV,
  every sample rate the DAC and MA share, flow sample rate "bit perfect",
  native volume. Nothing the user chose is overwritten; a provider the user
  removed is not added back (``mpd_added`` is remembered in our own state);
- disables HiFi's own DLNA protocol players in MA (MA keeps them listed as
  disabled protocols, so this stays visible and reversible in its UI);
- drops a volume/mute control explicitly pointing at them;
- sets volume normalization of the HiFi AirPlay and HiFi MA queues to
  "disabled" unless the user already chose a value other than "global".

Universal player ids are random and only exist after MA discovered the
device, so ``wait`` polls the settings MA persists after its first start.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time

SETTINGS = 'settings.json'
MPD_ADDRESS = '127.0.0.1:6601'
MPD_PLAYER = 'mpd_127.0.0.1_6601'
# What MA 2.10.9 offers for a player's "Supported sample rates" (each with 16 and 24 bit).
MA_RATES = (44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000)
# Remembered next to settings.json so a provider the user deleted stays deleted.
STATE = '.hifimate-ma-players.json'


def load(data_dir: Path) -> dict:
    path = data_dir / SETTINGS
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f'refusing nonregular settings file: {path}')
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('Music Assistant settings must be a JSON object')
    return data


def _label(player: dict) -> str:
    return str(player.get('name') or player.get('default_name') or '')


def hifimate_players(data: dict, name: str) -> tuple[str | None, list[str]]:
    """Return (HiFi AirPlay parent id, HiFi DLNA protocol player ids)."""
    players = data.get('players') or {}
    parent = None
    for player in players.values():
        if (isinstance(player, dict) and player.get('provider') == 'airplay'
                and _label(player) == f'{name} AirPlay'):
            parent = (player.get('values') or {}).get('protocol_parent_id') or parent
    dlna = sorted(pid for pid, player in players.items()
                  if isinstance(player, dict) and player.get('provider') == 'dlna' and (
                      _label(player).startswith(f'{name} DLNA')
                      or (parent and (player.get('values') or {}).get('protocol_parent_id') == parent)))
    return parent, dlna


def mpd_player(data: dict, name: str, rates, state: dict) -> list[str]:
    """MA's MPD provider and HiFiMate's MPD player, set up like a user would."""
    changes = []
    providers = data.setdefault('providers', {})
    mpd = next((p for p in providers.values() if isinstance(p, dict) and p.get('domain') == 'mpd'), None)
    if mpd is None:
        if state.get('mpd_added'):
            return changes                    # the user removed it: leave it so
        providers['mpd'] = {'values': {'manual_discovery_ip_addresses': [MPD_ADDRESS]}, 'type': 'player',
                            'domain': 'mpd', 'instance_id': 'mpd', 'enabled': True, 'name': None,
                            'default_name': 'MPD Players', 'last_error': None, 'setup_data': {}}
        state['mpd_added'] = True
        changes.append(f'added MA provider MPD Players ({MPD_ADDRESS})')
    else:
        addresses = mpd.setdefault('values', {}).setdefault('manual_discovery_ip_addresses', [])
        if MPD_ADDRESS not in addresses:
            addresses.append(MPD_ADDRESS)
            changes.append(f'MPD Players: added {MPD_ADDRESS}')
    supported = sorted(r for r in (rates or MA_RATES) if r in MA_RATES) or [44100, 48000]
    defaults = {'output_codec': 'wav', 'flow_mode_sample_rate': 'bit_perfect', 'volume_control': 'native',
                'sample_rates': [f'{r}||{bits}' for r in supported for bits in (16, 24)]}
    player = data.setdefault('players', {}).setdefault(MPD_PLAYER, {
        'values': {}, 'provider': 'mpd', 'player_id': MPD_PLAYER, 'enabled': True,
        'name': f'{name} MA', 'default_name': 'MPD (127.0.0.1)', 'player_type': 'player', 'setup_data': {}})
    values = player.setdefault('values', {})
    for key, value in defaults.items():
        if key not in values:
            values[key] = value
            changes.append(f'{MPD_PLAYER} {key}: {value if key != "sample_rates" else ",".join(map(str, supported))}')
    return changes


def apply(data: dict, name: str, rates=None, state=None) -> list[str]:
    """Edit ``data`` in place; return a description of each change."""
    changes = mpd_player(data, name, rates, {} if state is None else state)
    players = data.get('players') or {}
    parent, dlna = hifimate_players(data, name)
    for pid in dlna:
        if players[pid].get('enabled', True):
            players[pid]['enabled'] = False
            changes.append(f'disabled MA protocol player {pid} ({_label(players[pid])})')
    if parent and isinstance(players.get(parent), dict):
        values = players[parent].setdefault('values', {})
        for key in ('volume_control', 'mute_control'):
            if values.get(key) in dlna:
                del values[key]
                changes.append(f'{parent} {key}: no longer the DLNA lane')
    queues = data.setdefault('player_queues', {})
    for queue_id in (parent, MPD_PLAYER):
        if not queue_id or queue_id not in players:
            continue
        queue = queues.setdefault(queue_id, {'values': {}, 'queue_id': queue_id})
        values = queue.setdefault('values', {})
        if values.get('volume_normalization', 'global') == 'global':
            values['volume_normalization'] = 'disabled'
            changes.append(f'{queue_id} volume_normalization: disabled')
    return changes


def save(data_dir: Path, data: dict) -> None:
    path = data_dir / SETTINGS
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, name = tempfile.mkstemp(prefix='.settings.', dir=data_dir)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def wait(data_dir: Path, name: str, timeout: float, sleep=time.sleep, clock=time.monotonic) -> bool:
    end = clock() + timeout
    while True:
        try:
            parent, _ = hifimate_players(load(data_dir), name)
            if parent:
                return True
        except (OSError, ValueError):  # includes a settings file mid-write
            pass
        if clock() >= end:
            return False
        sleep(2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('found', 'wait', 'apply'))
    parser.add_argument('data_dir', type=Path)
    parser.add_argument('--name', required=True)
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--rates', default='', help='the DAC\'s sample rates, comma separated (apply)')
    args = parser.parse_args()
    if not args.data_dir.is_absolute():
        raise ValueError('Music Assistant data directory must be absolute')
    if args.action == 'found':
        return 0 if hifimate_players(load(args.data_dir), args.name)[0] else 1
    if args.action == 'wait':
        if wait(args.data_dir, args.name, args.timeout):
            return 0
        print(f'Music Assistant has not discovered "{args.name} AirPlay" yet; '
              're-run the installer later to initialize its player settings', file=sys.stderr)
        return 1
    data = load(args.data_dir)
    state_path = args.data_dir / STATE
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    rates = [int(r) for r in args.rates.split(',') if r.strip().isdecimal()]
    changes = apply(data, args.name, rates, state)
    if changes:
        save(args.data_dir, data)
        state_path.write_text(json.dumps(state) + '\n')
    print('\n'.join(changes) or 'MA player settings for HiFiMate already initialized')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc))
