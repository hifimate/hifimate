#!/usr/bin/env python3
"""Validate managed files before mutation and publish user fragments transactionally."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import sys

sys.dont_write_bytecode = True

MARKER = '.hifimate-install-manifest'
USER = '.hifimate-user-config-manifest'
JOURNAL = '.hifimate-install-in-progress'
FRAGMENTS = (
    'pipewire/pipewire-pulse.conf.d/50-hifimate-inputs.conf',
    'pipewire/pipewire.conf.d/50-hifimate-inputs.conf',
    'wireplumber/wireplumber.conf.d/51-hifimate.conf',
    'pipewire/pipewire.conf.d/52-hifimate-realtime.conf',
    'pipewire/pipewire-pulse.conf.d/52-hifimate-realtime.conf',
    'pipewire/client.conf.d/52-hifimate-realtime.conf',
    'wireplumber/wireplumber.conf.d/52-hifimate-realtime.conf',
)
TREES = ('lib/hifimate', 'config/host-stack', 'bin', 'systemd', 'gui',
         'share/doc/hifimate', 'share/hifimate/camilladsp', 'music-assistant')
# These directories are runtime state owned by Music Assistant and the
# optional provider subscriber. They may contain user-edited options,
# subscriber state, or downloaded providers, so they are deliberately outside
# the immutable installer manifest.
DYNAMIC_PREFIXES = (
    'music-assistant/custom_providers',
    'music-assistant/provider-subscriber',
    # Music Assistant API token for now playing (ma_api.py): user state.
    'config/ma-token',
    # Room correction filters (room_eq.py): user state, kept on reinstall.
    'config/room-correction.json',
    # Personal DSP filters for this machine's hardware (user_filters.py): user state.
    'config/dsp-filters.yml',
    # Playback settings from the control page, over hifimate.conf (settings.py): user state.
    'config/settings.json',
    # The EQ page's saved versions (eq.py).
    'state/eq',
    # Local CA and server certificate (tls.py): runtime state, kept on reinstall.
    'config/tls',
    # Room measurements (roomfit.py).
    'state/roomfit',
)
# Written by install.sh after the journal, outside TREES: a resumed install rewrites them too.
INSTALLER_FILES = {'share/hifimate/commit', 'share/hifimate/version'}
METADATA = {MARKER, MARKER + '.sha256', USER, USER + '.sha256', JOURNAL,
            '.hifimate-output.lock'}


def safe(path):
    """Reject symlinks (including dangling links), special files and linked files."""
    path = Path(path).absolute()
    for item in reversed((path, *path.parents)):
        try:
            mode = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode.st_mode):
            raise RuntimeError(f'refusing symlink path: {item}')
        if item != path and not stat.S_ISDIR(mode.st_mode):
            raise RuntimeError(f'not a directory: {item}')
        if not (stat.S_ISREG(mode.st_mode) or stat.S_ISDIR(mode.st_mode)):
            raise RuntimeError(f'not a regular file/directory: {item}')
        if stat.S_ISREG(mode.st_mode) and mode.st_nlink != 1:
            raise RuntimeError(f'refusing multiply linked file: {item}')


def relative(name):
    path = Path(name)
    if (not path.parts or path.is_absolute() or '..' in path.parts
            or str(path) != name or any(c.isspace() for c in name)):
        raise RuntimeError(f'unsafe manifest path: {name}')
    return path


def safe_directory(path):
    safe(path)
    if path.exists() and not path.is_dir():
        raise RuntimeError(f'not a directory: {path}')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def signed(prefix, name):
    path = prefix / name
    signature = prefix / (name + '.sha256')
    safe(path)
    safe(signature)
    data = path.read_bytes()
    if signature.read_text().split() != [digest(data), name]:
        raise RuntimeError(f'manifest signature mismatch: {name}')
    return data.decode()


def manifest(prefix):
    lines = signed(prefix, MARKER).splitlines()
    if not lines or lines[0] != '# hifimate-install-v1':
        raise RuntimeError('invalid installation manifest')
    result = {}
    for line in lines[1:]:
        sha, name = line.split()
        relative(name)
        if name in result or name in METADATA or not re.fullmatch('[0-9a-f]{64}', sha):
            raise RuntimeError('invalid manifest entry')
        result[name] = sha
    if not result:
        raise RuntimeError('empty installation manifest')
    return result


def cache_source(path, prefix, owned):
    rel = path.relative_to(prefix)
    if path.parent.name != '__pycache__':
        return False
    match = re.fullmatch(r'(.+)\.cpython-[0-9]+(?:\.opt-[12])?\.pyc', path.name)
    return bool(match and str(rel.parent.parent / (match[1] + '.py')) in owned)


def dynamic_path(name):
    """Return whether *name* belongs to a runtime-owned MA directory."""
    return any(name == prefix or name.startswith(prefix + '/')
               for prefix in DYNAMIC_PREFIXES)


def user_records(prefix, config):
    path = prefix / USER
    signature = prefix / (USER + '.sha256')
    safe(path)
    safe(signature)
    if not path.exists() and not signature.exists():
        return {}
    records = {}
    for line in signed(prefix, USER).splitlines():
        name, sha, created = line.split()
        if (name not in FRAGMENTS or name in records or created not in ('0', '1')
                or not re.fullmatch('[0-9a-f]{64}', sha)):
            raise RuntimeError('invalid user config manifest')
        target = config / name
        safe(target)
        if not target.is_file() or digest(target.read_bytes()) != sha:
            raise RuntimeError(f'managed user config modified or missing: {target}')
        records[name] = (sha, created)
    return records


def preflight(prefix, config, uninstall=False):
    safe(prefix)
    if not prefix.exists():
        return {}
    paths = list(prefix.rglob('*'))
    for path in paths:
        safe(path)
    for name in METADATA:
        path = prefix / name
        if path.exists() and not path.is_file():
            raise RuntimeError(f'not a regular file: {path}')
    if not (prefix / MARKER).exists():
        # After an uninstall only user data is left (passwords, EQ, room
        # correction, certificates...): installing again picks it up. Anything
        # else may be a half-finished install and is kept for recovery.
        stray = [p for p in paths if p.is_file() and not dynamic_path(str(p.relative_to(prefix)))]
        if uninstall or stray:
            raise RuntimeError('prefix has no completed managed manifest; preserve/recover it before retry')
        return {}
    # Older manifests may include files created by the subscriber before the
    # runtime directories were classified as dynamic. Ignore those entries so
    # an upgrade can repair the manifest after options/state/provider changes.
    owned = {name: sha for name, sha in manifest(prefix).items()
             if not dynamic_path(name)}
    journal = prefix / JOURNAL
    if journal.exists() and journal.read_text() != 'hifimate-install-in-progress-v1\n':
        raise RuntimeError('invalid installation journal')
    # The journal is written only after a preflight passed. An interrupted
    # install therefore left installer-written (not user-edited) files in the
    # managed trees; a retry rewrites them all, so it may resume over them.
    resuming = journal.exists() and not uninstall
    def installer_tree(name):
        return (any(name.startswith(tree + '/') for tree in TREES) or name.startswith('config/')
                or name in INSTALLER_FILES)
    for name, sha in owned.items():
        path = prefix / name
        safe(path)
        if resuming and installer_tree(name):
            if path.exists() and not path.is_file():
                raise RuntimeError(f'owned path is not a regular file: {name}')
            continue
        if not path.is_file() or digest(path.read_bytes()) != sha:
            raise RuntimeError(f'owned file modified or missing: {name}')
    reserved = METADATA.copy()
    if 'music-assistant/.hifimate-managed' in owned:
        reserved.add('music-assistant/.env')
    for path in paths:
        if not path.is_file():
            continue
        name = str(path.relative_to(prefix))
        if (name in owned or name in reserved or dynamic_path(name)
                or cache_source(path, prefix, owned) or (resuming and installer_tree(name))):
            continue
        if not uninstall or any(name.startswith(tree + '/') for tree in TREES):
            raise RuntimeError(f'unmanaged file inside prefix: {name}')
    user_records(prefix, config)
    # Unit links themselves are intentionally symlinks; their parents must not be.
    safe_directory(config / 'systemd/user')
    return owned


def check_targets(prefix, source):
    """Check new destinations too: a directory must never stand in for a file."""
    names = set(METADATA) | {
        'config/hifimate.conf', 'config/hifimate.conf.tmp', 'config/realtime.json', 'config/identity.json',
        'config/realtime-permissions.request', 'config/bluetooth-speaker.request', 'config/host-stack-manifest.json',
        'bin/hifimate-realtime-permissions', 'bin/hifimate-bluetooth-speaker', 'bin/host-stack-checker', 'bin/hifimate-camilladsp',
        'share/hifimate/commit', 'share/hifimate/camilladsp/release.json',
        'music-assistant/.env', 'music-assistant/.hifimate-managed',
    }
    for directory, target in (('hifimate', 'lib/hifimate'), ('config/host-stack', 'config/host-stack'),
                              ('bin', 'bin'), ('systemd', 'systemd'), ('gui', 'gui'),
                              ('music-assistant', 'music-assistant')):
        for path in (source / directory).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts:
                name = str(Path(target) / path.relative_to(source / directory))
                names.add(name)
                if directory == 'config/host-stack':
                    names.add(name + '.tmp')
    names.update('share/doc/hifimate/' + name for name in ('README.md', 'INSTALL.md', 'NOTICE.md', 'ARCHITECTURE.md', 'SPECTRUM.md'))
    for name in names:
        path = prefix / name
        safe(path)
        if path.exists() and not path.is_file():
            raise RuntimeError(f'not a regular file: {path}')


def fragment_data(source, prefix):
    return (source.read_text().replace('@PREFIX@', str(prefix))
            .replace('@HOME@', str(Path.home()))
            .replace('@RUNTIME@', f'/run/user/{os.getuid()}/hifimate').encode())


def check_fragments(prefix, config, source):
    previous = user_records(prefix, config)
    for name in FRAGMENTS:
        target = config / name
        safe(target)
        data = fragment_data(source / name, prefix)
        if target.exists() and not (name in previous and previous[name][1] == '1'):
            if not target.is_file() or target.read_bytes() != data:
                raise RuntimeError(f'refusing to overwrite existing user config: {target}')
    for unit in (Path(__file__).resolve().parents[1] / 'systemd').glob('*.service'):
        link = config / 'systemd/user' / unit.name
        safe_directory(link.parent)
        if link.is_symlink():
            if os.readlink(link) != str(prefix / 'systemd' / unit.name):
                raise RuntimeError(f'unrelated user unit: {link}')
        elif link.exists():
            raise RuntimeError(f'unrelated user unit: {link}')


def stage(path, data, mode=0o644):
    fd, name = tempfile.mkstemp(prefix='.hifimate-stage-', dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        staged.chmod(mode)
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def replace_files(changes, modes=None):
    """Stage every write and rollback copy before replacing any destination."""
    pending = {}
    backups = {}
    replaced = []
    try:
        for path, data in changes.items():
            safe(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            pending[path] = stage(path, data, (modes or {}).get(path, 0o644))
            backups[path] = (stage(path, path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                             if path.exists() else None)
        for path, staged in pending.items():
            os.replace(staged, path)
            replaced.append(path)
    except BaseException:
        for path in reversed(replaced):
            if backups[path] is None:
                path.unlink()
            else:
                os.replace(backups[path], path)
        raise
    finally:
        for path in (*pending.values(), *backups.values()):
            if path is not None:
                path.unlink(missing_ok=True)


def publish_fragments(prefix, config, source):
    check_fragments(prefix, config, source)
    previous = user_records(prefix, config)
    changes = {}
    lines = []
    for name in FRAGMENTS:
        target = config / name
        data = fragment_data(source / name, prefix)
        created = previous.get(name, ('', '0' if target.exists() else '1'))[1]
        if created == '1':
            changes[target] = data
        lines.append(f'{name} {digest(data)} {created}\n')
    data = ''.join(lines).encode()
    changes[prefix / USER] = data
    changes[prefix / (USER + '.sha256')] = (digest(data) + '  ' + USER + '\n').encode()
    replace_files(changes)


def remove_files(prefix, config):
    owned = preflight(prefix, config, uninstall=True)
    records = user_records(prefix, config)
    paths = [prefix / name for name in owned]
    paths += [p for p in prefix.rglob('*') if p.is_file() and cache_source(p, prefix, owned)]
    paths += [prefix / name for name in METADATA if name not in (MARKER, MARKER + '.sha256')]
    if 'music-assistant/.hifimate-managed' in owned:
        paths.append(prefix / 'music-assistant/.env')
    # The personal filter template goes with HiFi; once edited, it is the user's and stays.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hifimate'))
    from user_filters import TEMPLATE
    filters = prefix / 'config/dsp-filters.yml'
    if filters.is_file() and not filters.is_symlink() and filters.read_text() == TEMPLATE:
        paths.append(filters)
    for name, (_, created) in records.items():
        if created == '1':
            (config / name).unlink()
    for path in paths:
        path.unlink(missing_ok=True)
    for name in (MARKER, MARKER + '.sha256'):
        (prefix / name).unlink()
    # Remove only empty directories that contained managed files or caches.
    directories = {p for path in paths for p in path.parents if p == prefix or prefix in p.parents}
    for path in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


def copy_package(prefix, source):
    shutil.copytree(source, prefix / 'lib/hifimate', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))


def write_manifest(prefix):
    paths = [path for path in prefix.rglob('*') if path.is_file()]
    names = {str(path.relative_to(prefix)) for path in paths}
    lines = ['# hifimate-install-v1\n']
    for path in sorted(paths):
        safe(path)
        name = str(path.relative_to(prefix))
        if (name in METADATA or name == 'music-assistant/.env'
                or dynamic_path(name) or cache_source(path, prefix, names)):
            continue
        relative(name)
        lines.append(f'{digest(path.read_bytes())} {name}\n')
    data = ''.join(lines).encode()
    replace_files({prefix / MARKER: data,
                   prefix / (MARKER + '.sha256'): (digest(data) + '  ' + MARKER + '\n').encode()})


# Unit placeholders for host programs, resolved from PATH at install time.
HOST_PROGRAMS = {'@MPD@': 'mpd', '@UPMPDCLI@': 'upmpdcli', '@SHAIRPORT_SYNC@': 'shairport-sync'}


def render_units(prefix, which=None):
    # Resolve from the same PATH used by dependency checks, before systemd's
    # much smaller environment is involved; distributions differ (/usr/bin,
    # /usr/local/bin, /usr/sbin...). Keep the venv path (do not resolve symlinks).
    # A program not found now keeps its bare name: systemd then looks it up
    # in its own search path when the unit starts.
    from host_stack import executable_path
    which = which or executable_path
    dsp = prefix / 'bin/hifimate-camilladsp'
    programs = {'@CAMILLADSP@': str(dsp) if dsp.is_file() else which('camilladsp') or 'camilladsp'}
    programs.update({mark: which(name) or name for mark, name in HOST_PROGRAMS.items()})
    for path in sorted((prefix / 'systemd').glob('*.service')):
        text = path.read_text()
        rendered = text
        for mark, command in programs.items():
            rendered = rendered.replace(mark, command)
        if rendered != text:
            path.write_text(rendered)


def check_build(source):
    """A directory made by camilladsp_release.py prepare: the pinned official binary."""
    import json
    from camilladsp_release import VERSION, check
    for name in ('camilladsp', 'release.json'):
        safe(source / name)
        if not (source / name).is_file():
            raise RuntimeError(f'missing CamillaDSP release file: {name}')
    release = json.loads((source / 'release.json').read_text())
    if release.get('version') != VERSION or digest((source / 'camilladsp').read_bytes()) != release.get('binary_sha256'):
        raise RuntimeError('CamillaDSP binary does not match its release record')
    if not release.get('archive_sha256'):
        raise RuntimeError('CamillaDSP release record without an archive SHA256')
    check(source / 'camilladsp')


def install_build(prefix, source):
    check_build(source)
    # Never write into the running DSP's executable (ETXTBSY on Linux): stage
    # beside it and rename, so the running process keeps its old inode and the
    # next service start executes the new binary.
    binary = prefix / 'bin/hifimate-camilladsp'
    replace_files({binary: (source / 'camilladsp').read_bytes(),
                   prefix / 'share/hifimate/camilladsp/release.json': (source / 'release.json').read_bytes()},
                  {binary: 0o755})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('preflight', 'check-uninstall', 'check-fragments',
                                          'publish-fragments', 'remove-files', 'copy-package',
                                          'write-manifest', 'render-units', 'check-build', 'install-build'))
    parser.add_argument('prefix', type=Path)
    parser.add_argument('--source', type=Path)
    args = parser.parse_args()
    config = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config')))
    if args.action in ('preflight', 'check-uninstall'):
        preflight(args.prefix, config, uninstall=args.action == 'check-uninstall')
        if args.source:
            check_targets(args.prefix, args.source)
    elif args.action == 'check-fragments':
        check_fragments(args.prefix, config, args.source)
    elif args.action == 'publish-fragments':
        publish_fragments(args.prefix, config, args.source)
    elif args.action == 'render-units':
        render_units(args.prefix)
    elif args.action == 'copy-package':
        copy_package(args.prefix, args.source)
    elif args.action == 'check-build':
        check_build(args.source)
    elif args.action == 'install-build':
        install_build(args.prefix, args.source)
    elif args.action == 'write-manifest':
        write_manifest(args.prefix)
    else:
        remove_files(args.prefix, config)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc))
