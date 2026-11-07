#!/usr/bin/env python3
"""The official CamillaDSP release HiFiMate runs, unmodified.

usage: camilladsp_release.py prepare OUTPUT [--archive PATH [--sha256 HEX]]

Fetches (or takes) the upstream PulseAudio build for this machine, checks it
against the pinned SHA256, unpacks the single executable and checks that it
is CamillaDSP 4.1.3 with the PulseAudio backend. OUTPUT then holds
camilladsp and release.json, which install_files.py install-build copies into
the prefix. The archive is cached in ~/.cache/hifimate; nothing else changes.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

VERSION = '4.1.3'
COMMIT = '05e9cfc'
URL = 'https://github.com/HEnquist/camilladsp/releases/download/v{version}/{name}'
ASSETS = {   # machine: (asset, sha256 of the release archive)
    'x86_64': ('camilladsp-linux-pulseaudio-amd64.tar.gz', '357e68f8d2245d97ca682e04db54b4bfa34f11283a9796d695013b225282860d'),
    'aarch64': ('camilladsp-linux-pulseaudio-aarch64.tar.gz', 'fb3b2917c4a9a5b9a518445d945256173f48ac8d516f744384dbf88f1466675b'),
}
MAX_ARCHIVE = 64 * 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def asset(machine=None):
    machine = machine or platform.machine()
    if machine not in ASSETS:
        raise RuntimeError(f'no official CamillaDSP build for {machine}; pass --archive with a PulseAudio build')
    return ASSETS[machine]


def fetch(cache, machine=None, opener=urllib.request.urlopen):
    """The pinned archive for this machine, downloaded once into cache."""
    name, expected = asset(machine)
    target = Path(cache) / f'camilladsp-{VERSION}-{name}'
    if target.is_file() and sha(target.read_bytes()) == expected:
        return target, expected
    target.parent.mkdir(parents=True, exist_ok=True)
    with opener(URL.format(version=VERSION, name=name), timeout=60) as response:
        data = response.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise RuntimeError('CamillaDSP download too large')
    if sha(data) != expected:
        raise RuntimeError('CamillaDSP download does not match the pinned SHA256')
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stage:
        stage.write(data)
    os.replace(stage.name, target)
    return target, expected


def executable(archive):
    """The bytes of the only member, a regular file called camilladsp."""
    with tarfile.open(fileobj=io.BytesIO(Path(archive).read_bytes()), mode='r:gz') as tar:
        members = [m for m in tar.getmembers() if not m.isdir()]
        if len(members) != 1 or Path(members[0].name).name != 'camilladsp' or not members[0].isfile():
            raise RuntimeError('unexpected CamillaDSP archive layout')
        return tar.extractfile(members[0]).read()


def check(binary):
    """CamillaDSP 4.1.3 with the PulseAudio backend (the capture the arbiter uses)."""
    # --version names the release; --help lists the features it was built with.
    version = subprocess.run([str(binary), '--version'], capture_output=True, text=True, timeout=10)
    features = subprocess.run([str(binary), '--help'], capture_output=True, text=True, timeout=10)
    if (version.returncode or f'CamillaDSP {VERSION}' not in version.stdout
            or 'pulse-backend' not in features.stdout + features.stderr):
        raise RuntimeError('not CamillaDSP ' + VERSION + ' with pulse-backend: ' + version.stdout.strip()[:120])


def prepare(output, archive=None, expected=None, cache=None, machine=None, opener=urllib.request.urlopen):
    output = Path(output)
    if archive is None:
        cache = Path(cache or os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'hifimate'
        archive, expected = fetch(cache, machine, opener)
        name = asset(machine)[0]
    else:
        archive = Path(archive)
        name = archive.name
        expected = expected or dict(ASSETS.values()).get(name)
        if not expected:
            raise RuntimeError(f'{name} is not a pinned release; pass --sha256')
        if sha(archive.read_bytes()) != expected:
            raise RuntimeError('CamillaDSP archive does not match the expected SHA256')
    binary = executable(archive)
    output.mkdir(parents=True, exist_ok=True)
    stage = output / '.camilladsp.new'
    stage.write_bytes(binary)
    stage.chmod(0o755)
    check(stage)
    os.replace(stage, output / 'camilladsp')
    (output / 'release.json').write_text(json.dumps({
        'version': VERSION, 'asset': name, 'archive_sha256': expected,
        'binary_sha256': sha(binary), 'source': URL.format(version=VERSION, name=name)}, indent=2) + '\n')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    parser.add_argument('action', choices=('prepare',))
    parser.add_argument('output', type=Path)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--sha256')
    args = parser.parse_args()
    if args.sha256 and not args.archive:
        parser.error('--sha256 needs --archive')
    try:
        prepare(args.output, args.archive, args.sha256)
    except (OSError, RuntimeError, tarfile.TarError, subprocess.SubprocessError) as error:
        raise SystemExit(f'CamillaDSP: {error}')
    print(f'CamillaDSP {VERSION} ready: {args.output / "camilladsp"}')


if __name__ == '__main__':
    sys.exit(main())
