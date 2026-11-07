"""Read-only supported ALSA output discovery and managed controller output selection."""
import argparse
import base64
import stat
import collections
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def pcm_info(path):
    fields = dict(line.split(':', 1) for line in path.read_text().splitlines() if ':' in line)
    fields = {k.strip(): v.strip() for k, v in fields.items()}
    number = int(path.parent.name[3:-1])
    if fields.get('device') != str(number) or fields.get('stream') != 'PLAYBACK':
        raise ValueError('PCM metadata does not identify this playback device')
    return number, fields


def converter_rates(node):
    if not re.match(r'Node \w+ \[Audio Output\].*\bStereo\b', node) or re.search(r'\bDigital\b', node.splitlines()[0]):
        raise ValueError('require non-digital stereo Audio Output')
    fields = {}
    pcm = re.search(r'^  PCM:\n((?:[ \t]+[^\n]*\n?)*)', node, re.M)
    if not pcm:
        raise ValueError('explicit converter PCM unavailable; default inheritance not inferred')
    for field in ('rates', 'bits', 'formats'):
        match = re.search(r'^    '+field+r' \[[^\]]+\]: ([^\n]+)', pcm[1], re.M)
        if not match:
            raise ValueError('missing converter '+field)
        fields[field] = match[1].split()
    if '32' not in fields['bits'] or 'PCM' not in fields['formats']:
        raise ValueError('converter lacks 32-bit integer PCM')
    if not fields['rates'] or not all(x.isdecimal() for x in fields['rates']):
        raise ValueError('invalid converter discrete rates')
    return {int(x) for x in fields['rates']}


def builtin_rates(card, number, info):
    matches = []
    for path in card.glob('codec#*'):
        text = path.read_text()
        model = re.search(r'^Codec: (.+)$', text, re.M)
        if not model:
            continue
        nodes = re.split(r'(?=^Node )', text, flags=re.M)[1:]
        analog_pcms = set()
        for node in nodes:
            endpoint = re.search(r'^  Device: name="([^"\n]+)", type="Audio", device=(0|[1-9][0-9]*)(?:,|\s|$)', node, re.M)
            if (endpoint and re.match(r'Node \w+ \[Audio Output\]', node)
                    and re.search(r'\bAnalog\b', endpoint[1], re.I)
                    and not re.search(r'HDMI|DisplayPort|Digital|SPDIF', endpoint[1], re.I)):
                analog_pcms.add((int(endpoint[2]), endpoint[1]))
        related = []
        for node in nodes:
            if not re.match(r'Node \w+ \[Audio Output\]', node):
                continue
            device = re.search(r'^  Device: name="([^"\n]+)", type="Audio", device=(0|[1-9][0-9]*)(?:,|\s|$)', node, re.M)
            speaker = re.search(r'Control: name="Speaker Playback (?:Volume|Switch)",[^\n]*device=(0|[1-9][0-9]*)(?:,|\s|$)', node)
            exact = device and int(device[2]) == number and device[1] == info.get('id')
            if device and not exact:
                continue
            if exact:
                # Require analog evidence; HDMI/digital codecs are never speaker fallback.
                if not re.search(r'\bAnalog\b', device[1], re.I) or re.search(r'HDMI|DisplayPort|Digital|SPDIF', device[1], re.I):
                    raise ValueError('PCM lacks analog output evidence')
            # A control's device index alone is not a PCM mapping. A Speaker
            # converter without its own Device is usable only when the codec
            # has one unambiguous analog PCM to which it can belong.
            speaker_only = (not device and speaker and int(speaker[1]) == number
                            and analog_pcms == {(number, info.get('id'))})
            if exact or speaker_only:
                related.append((node, bool(exact)))
        if any(exact for _, exact in related):
            rates = set.intersection(*(converter_rates(node) for node, _ in related))
            matches.append((model[1], rates))
    if len(matches) != 1:
        raise ValueError('analog PCM to codec mapping unavailable or ambiguous')
    return matches[0]


def usb_rates(text):
    if 'Playback:' not in text:
        raise ValueError('no USB playback capability')
    playback = text.split('Playback:', 1)[1].split('Capture:', 1)[0]
    rates = set()
    # Split alternate settings as well as interfaces; never merge DSD/mono formats.
    for block in re.split(r'(?=^\s*(?:Interface|Altset) \d+)', playback, flags=re.M):
        if not re.search(r'Format:\s*S32_LE(?:\s|$)', block) or not re.search(r'Channels:\s*2(?:\s|$)', block):
            continue
        for line in re.findall(r'Rates:\s*([^\n]+)', block):
            if not re.fullmatch(r'\s*\d+(?:\s*,\s*\d+)*\s*', line):
                raise ValueError('invalid/non-discrete stereo S32_LE rates')
            rates.update(int(x) for x in line.split(','))
    return rates


def auto_candidates(rows):
    compatible = [r for r in rows if not r['error']]
    usb = [r for r in compatible if r['sourcekind'] == 'usb']
    return usb or [r for r in compatible if r['sourcekind'] == 'builtin']


def enumerate_outputs(proc=Path('/proc/asound')):
    rows = []
    cards = sorted(Path(proc).glob('card[0-9]*'))
    ids = {}
    for card in cards:
        try:
            ids[card] = (card/'id').read_text().strip()
        except OSError:
            ids[card] = ''
    counts = collections.Counter(ids.values())
    for card in cards:
        streams = sorted(card.glob('stream[0-9]*'))
        infos = sorted(card.glob('pcm[0-9]*p/info'))
        for path in infos:
            number = int(path.parent.name[3:-1])
            kind = 'usb' if streams else 'builtin'
            row = dict(card=card, id=ids[card], number=number, sourcekind=kind,
                       label=card.name, rates=set(), error='')
            try:
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', row['id']):
                    raise ValueError('invalid stable ALSA card ID')
                if counts[row['id']] != 1:
                    raise ValueError('duplicate ALSA card ID; stable selection is ambiguous')
                _, info = pcm_info(path)
                if streams:
                    # proc PCM info has no USB endpoint identity on many kernels.
                    # A one-to-one playback stream/PCM is provable; multiple mappings fail closed.
                    playback = [s for s in streams if 'Playback:' in s.read_text()]
                    if len(playback) != 1 or len(infos) != 1:
                        raise ValueError('USB stream to PCM endpoint mapping ambiguous')
                    text = playback[0].read_text()
                    row['rates'] = usb_rates(text)
                    row['label'] = 'USB ' + text.splitlines()[0] + (' / '+info.get('name', info.get('id', '')) if info else '')
                else:
                    model, row['rates'] = builtin_rates(card, number, info)
                    row['label'] = '本机 ' + model + ' / ' + info.get('name', info.get('id', ''))
                if any(not 8000 <= r <= 2147483647 for r in row['rates']):
                    raise ValueError('invalid discrete rate')
                if not any(44100 <= r <= 768000 for r in row['rates']):
                    raise ValueError('no runtime stereo S32_LE discrete playback rate')
            except (OSError, ValueError) as exc:
                row['error'] = str(exc)
            row['device'] = f"hw:CARD={row['id']},DEV={number}"
            rows.append(row)
    return rows


def device_capabilities(device, proc=Path('/proc/asound')):
    rows = enumerate_outputs(proc)
    selected = auto_candidates(rows) if device == 'auto' else [r for r in rows if r['device'] == device]
    if len(selected) != 1:
        raise RuntimeError('select exactly one compatible output using hw:CARD=<id>,DEV=<device>')
    row = selected[0]
    if row['error']:
        raise RuntimeError(row['error'])
    return row['device'], row['card'] / f"pcm{row['number']}p/sub0/hw_params", row['rates']


def choose(proc, device=None, interactive=False):
    if device:
        return device_capabilities(device, proc)[0]
    rows = [r for r in enumerate_outputs(proc) if not r['error']]
    automatic = auto_candidates(rows)
    if not interactive and len(automatic) == 1:
        return automatic[0]['device']
    if not interactive or not sys.stdin.isatty():
        raise RuntimeError('output ambiguous or unavailable; require --output-device / --device')
    for i, row in enumerate(rows, 1):
        print(f"{i}: {row['device']} {row['label']}", file=sys.stderr)
    try:
        print('Output number: ', end='', file=sys.stderr, flush=True)
        number = int(input())
        if not 1 <= number <= len(rows):
            raise ValueError()
        return rows[number - 1]['device']
    except ValueError:
        raise RuntimeError('invalid output selection') from None


def atomic(path, data, mode=0o644):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, path.stat().st_mode & 0o777 if path.exists() else mode)
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def digest(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def directory_lock(prefix):
    fd = os.open(prefix, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


FILES = ('config/hifimate.conf', '.hifimate-install-manifest', '.hifimate-install-manifest.sha256')
JOURNAL = '.hifimate-output-transaction.json'


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise RuntimeError(f'unsafe owned file: {path}')


def service_active(prefix, settings, run, require_owned=False):
    service = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config'))) / 'systemd/user/hifimate.service'
    linked = service.exists() or service.is_symlink()
    if linked and (not service.is_symlink() or service.resolve() != (prefix/'systemd/hifimate.service').resolve()):
        raise RuntimeError('hifimate.service belongs to another installation')
    result = run(['systemctl', '--user', 'show', 'hifimate.service',
                  '--property=FragmentPath,ExecStart,ActiveState,LoadState'],
                 capture_output=True, text=True, timeout=10)
    if result.returncode:
        default_runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))/'hifimate/controller.lock'
        configured_socket = settings.get('socket', str(default_runtime.parent/'format.sock')).replace('%(uid)', str(os.geteuid()))
        # A test/user manager may provide a private XDG runtime directory;
        # keep the default /run/user/<uid> template inside that directory
        # so offline checks never inspect another session's controller lock.
        runtime_root = os.environ.get('XDG_RUNTIME_DIR')
        default_root = f'/run/user/{os.geteuid()}'
        if runtime_root and configured_socket.startswith(default_root + '/'):
            configured_socket = runtime_root + configured_socket[len(default_root):]
        configured = Path(configured_socket).parent/'controller.lock'
        # Only an explicitly unavailable user bus permits offline editing.
        if require_owned or linked or not any(x in result.stderr for x in ('Failed to connect to bus', 'No medium found')):
            raise RuntimeError('cannot determine controller service state')
        for runtime in {default_runtime, configured}:
            if runtime.exists() or runtime.is_symlink():
                private_file(runtime)
                fd = os.open(runtime, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError('controller lock held; offline change refused') from None
                finally:
                    os.close(fd)
        return False
    props = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    active = props.get('ActiveState')
    if active not in ('active', 'inactive', 'failed'):
        raise RuntimeError('controller state unknown or transitioning')
    fragment = props.get('FragmentPath', '')
    if fragment and Path(fragment).resolve() != (prefix/'systemd/hifimate.service').resolve():
        raise RuntimeError('actual hifimate.service belongs to another installation')
    if active == 'active' or require_owned:
        if props.get('LoadState') != 'loaded' or not fragment:
            raise RuntimeError('active controller ownership unknown')
        command = props.get('ExecStart', '')
        expected = str(prefix/'bin/hifimate-runner')
        if not re.search(r'(?:^|[ ;{])path=' + re.escape(expected) + r'(?:[ ;}]|$)', command):
            raise RuntimeError('actual controller ExecStart belongs to another installation')
    return active == 'active'


def restart(run):
    run(['systemctl', '--user', 'restart', 'hifimate.service'], check=True, timeout=30)
    run(['systemctl', '--user', 'is-active', '--quiet', 'hifimate.service'], check=True, timeout=10)


def recover_locked(prefix, run):
    journal = prefix/JOURNAL
    if not journal.exists() and not journal.is_symlink():
        return
    private_file(journal)
    if journal.stat().st_mode & 0o077:
        raise RuntimeError('unsafe transaction permissions')
    transaction = json.loads(journal.read_text())
    if transaction.get('version') != 1 or transaction.get('phase') != 'prepared':
        raise RuntimeError('invalid output transaction')
    originals = []
    for name, entry in zip(FILES, transaction['files'], strict=True):
        if entry['name'] != name:
            raise RuntimeError('invalid transaction file')
        data = base64.b64decode(entry['old'], validate=True)
        target = base64.b64decode(entry['target'], validate=True)
        if digest(data) != entry['old_hash'] or digest(target) != entry['target_hash']:
            raise RuntimeError('transaction checksum mismatch')
        path = prefix/name
        private_file(path)
        if digest(path.read_bytes()) not in (entry['old_hash'], entry['target_hash']):
            raise RuntimeError('transaction file changed externally; recovery refused')
        originals.append(data)
    settings = json.loads(originals[0])
    active = service_active(prefix, settings, run, require_owned=transaction['active'])
    for name, data in zip(FILES, originals):
        atomic(prefix/name, data)
    if transaction['active']:
        restart(run)
    journal.unlink()
    sync_directory(prefix)
    print('Recovered interrupted output transaction', file=sys.stderr)


def recover(prefix, run=subprocess.run):
    prefix = checked_prefix(prefix)
    with directory_lock(prefix) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        recover_locked(prefix, run)


def checked_prefix(prefix):
    prefix = Path(prefix).absolute()
    if prefix.resolve() != prefix or (prefix/'config').is_symlink():
        raise RuntimeError('prefix/config must not contain symlinks')
    return prefix


def switch(prefix, device, apply=False, run=subprocess.run):
    prefix = checked_prefix(prefix)
    with directory_lock(prefix) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (prefix/JOURNAL).exists() or (prefix/JOURNAL).is_symlink():
            if not apply:
                raise RuntimeError('pending output transaction; run --recover --apply')
            recover_locked(prefix, run)
        paths = [prefix/n for n in FILES]
        for path in paths:
            private_file(path)
        original = [p.read_bytes() for p in paths]
        config, manifest, signature = original
        if signature.decode().split() != [digest(manifest), '.hifimate-install-manifest']:
            raise RuntimeError('invalid installation manifest signature')
        lines = manifest.decode().splitlines()
        if not lines or lines[0] != '# hifimate-install-v1':
            raise RuntimeError('not a managed HiFiMate installation')
        matches = [i for i, line in enumerate(lines) if line.split()[1:] == ['config/hifimate.conf']]
        if len(matches) != 1 or lines[matches[0]].split()[0] != digest(config):
            raise RuntimeError('config is not unchanged managed installation data')
        settings = json.loads(config)
        resolved, _, rates = device_capabilities(device, Path(settings.get('proc_asound', '/proc/asound')))
        available = {r for r in rates if 44100 <= r <= 768000}
        # The control page's playback settings (config/settings.json) can narrow the rates.
        from .settings import load_user
        allowed = load_user(Path(prefix) / 'config').get('rates', settings.get('rates'))
        if allowed and not available.intersection(allowed):
            raise RuntimeError('configured rates have no intersection with new output')
        print(f'Output: {resolved}; capabilities only, hardware playback NOT verified')
        if settings.get('alsa_device') == resolved:
            print('Output unchanged; no writes or restart')
            return
        if not apply:
            print('dry-run; no changes')
            return
        active = service_active(prefix, settings, run)
        settings['alsa_device'] = resolved
        new_config = (json.dumps(settings, indent=2) + '\n').encode()
        lines[matches[0]] = digest(new_config) + ' config/hifimate.conf'
        new_manifest = ('\n'.join(lines) + '\n').encode()
        values = [new_config, new_manifest, (digest(new_manifest)+'  .hifimate-install-manifest\n').encode()]
        transaction = dict(version=1, phase='prepared', active=active, files=[
            dict(name=name, old=base64.b64encode(old).decode(), target=base64.b64encode(new).decode(),
                 old_hash=digest(old), target_hash=digest(new))
            for name, old, new in zip(FILES, original, values)])
        journal = prefix/JOURNAL
        # Publish a complete private journal before touching any managed data.
        atomic(journal, json.dumps(transaction).encode(), mode=0o600)
        try:
            for path, data in zip(paths, values):
                atomic(path, data)
            if active:
                restart(run)
        except Exception:
            recover_locked(prefix, run)
            raise
        journal.unlink()
        sync_directory(prefix)
        print('Controller restarted; active playback interrupted, new Play required' if active else 'Offline config updated; service not started')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix', default=os.environ.get('HIFIMATE_PREFIX', str(Path.home()/'hifimate')))
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--list', action='store_true')
    actions.add_argument('--device')
    actions.add_argument('--choose', action='store_true')
    actions.add_argument('--recover', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--resolve-install', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not (args.list or args.device or args.choose or args.recover or args.resolve_install):
        parser.error('require --list, --device or --choose')
    try:
        if args.recover:
            if not args.apply:
                parser.error('--recover requires --apply')
            recover(args.prefix)
            return
        if args.apply and (args.device or args.choose):
            recover(args.prefix)
        conf = Path(args.prefix)/'config/hifimate.conf'
        settings = json.loads(conf.read_text()) if conf.exists() else {}
        proc = Path(settings.get('proc_asound', '/proc/asound'))
        if args.list:
            for row in enumerate_outputs(proc):
                print(f"{row['device']} | {row['sourcekind']} | {row['label']} | " + (row['error'] or ','.join(map(str, sorted(row['rates'])))))
            return
        device = choose(proc, args.device, args.choose or (args.resolve_install and sys.stdin.isatty()))
        if args.resolve_install:
            print(device)
        else:
            switch(args.prefix, device, args.apply)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'{exc}\n')

if __name__ == '__main__':
    main()
