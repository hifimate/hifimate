import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import re
import tarfile
import tempfile

import pytest

from conftest import fake_camilladsp_archive
from test_install_scripts import INSTALL, ROOT, UNINSTALL, enabled_install, run
from test_install_lifecycle import snapshot

sys.path.insert(0, str(ROOT / 'scripts'))
import camilladsp_release as release  # noqa: E402


def stage(tmp_path, name='stage', **kw):
    archive = fake_camilladsp_archive(tmp_path / f'{name}.tar.gz', **kw)
    return release.prepare(tmp_path / name, archive, hashlib.sha256(archive.read_bytes()).hexdigest())


def test_official_dsp_install_upgrade_enable_uninstall(enabled_install, tmp_path):
    prefix, _, _, log = enabled_install
    result = run(INSTALL, prefix, '--apply', '--enable')
    assert result.returncode == 0, result.stderr
    assert f'{prefix}/bin/hifimate-camilladsp' in (prefix / 'systemd/hifimate-camilladsp.service').read_text()
    assert b'CamillaDSP 4.1.3' in (prefix / 'bin/hifimate-camilladsp').read_bytes()
    record = json.loads((prefix / 'share/hifimate/camilladsp/release.json').read_text())
    assert record['version'] == '4.1.3' and record['archive_sha256'] == os.environ['HIFIMATE_CAMILLADSP_SHA256']
    assert 'hifimate-spectrum.service' in log.read_text()
    config = json.loads((prefix / 'config/host-stack/spectrum.json').read_text())
    assert '@' not in json.dumps(config) and config['camilladsp'] == f'{prefix}/bin/hifimate-camilladsp'
    assert config['port'] == 5008 and 'telemetry_socket' not in config
    assert 'SPECTRUM_SOCKET' not in (prefix / 'systemd/hifimate-camilladsp.service').read_text()
    manifest = (prefix / '.hifimate-install-manifest').read_text()
    for name in ('bin/hifimate-spectrum', 'bin/hifimate-camilladsp', 'share/hifimate/camilladsp/release.json',
                 'lib/hifimate/spectrum.py', 'lib/hifimate/dsp_model.py', 'lib/hifimate/spectrum_proxy.py',
                 'gui/ctrl.html', 'gui/ctrl.js', 'gui/ctrl.css', 'gui/nowplaying.js', 'gui/nowplaying.css',
                 'bin/hifimate-ctrl', 'systemd/hifimate-ctrl.service', 'gui/spectrum-tap.js', 'systemd/hifimate-spectrum.service'):
        assert name in manifest
    assert not (prefix / 'share/hifimate/camilladsp/build.json').exists()
    assert run(INSTALL, prefix, '--apply', '--enable').returncode == 0
    result = run(UNINSTALL, prefix, '--apply')
    assert result.returncode == 0, result.stderr
    assert not prefix.exists()


def test_pinned_releases_and_fetch_verification(tmp_path):
    assert release.VERSION == '4.1.3' and set(release.ASSETS) == {'x86_64', 'aarch64'}
    assert all(name.startswith('camilladsp-linux-pulseaudio-') and len(sha) == 64 for name, sha in release.ASSETS.values())
    class Response(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    cache = tmp_path / 'cache'
    with pytest.raises(RuntimeError, match='SHA256'):
        release.fetch(cache, 'x86_64', lambda url, timeout: Response(b'not the release'))
    assert not [p for p in cache.iterdir() if not p.name.startswith('tmp')] if cache.exists() else True
    with pytest.raises(RuntimeError, match='no official'):
        release.asset('riscv64')


def test_archive_layout_version_and_checksum_are_enforced(tmp_path):
    archive = fake_camilladsp_archive(tmp_path / 'a.tar.gz')
    with pytest.raises(RuntimeError, match='SHA256'):
        release.prepare(tmp_path / 'out', archive, '0' * 64)
    with pytest.raises(RuntimeError, match='not a pinned release'):
        release.prepare(tmp_path / 'out', archive)
    alsa_only = fake_camilladsp_archive(tmp_path / 'b.tar.gz', features='websocket')
    with pytest.raises(RuntimeError, match='pulse-backend'):
        release.prepare(tmp_path / 'out', alsa_only, hashlib.sha256(alsa_only.read_bytes()).hexdigest())
    bad = tmp_path / 'c.tar.gz'
    with tarfile.open(bad, 'w:gz') as tar:
        for name in ('camilladsp', '../escape'):
            info = tarfile.TarInfo(name); info.size = 2
            tar.addfile(info, io.BytesIO(b'#!'))
    with pytest.raises(RuntimeError, match='layout'):
        release.prepare(tmp_path / 'out', bad, hashlib.sha256(bad.read_bytes()).hexdigest())
    assert not (tmp_path / 'escape').exists()
    out = stage(tmp_path)
    assert json.loads((out / 'release.json').read_text())['binary_sha256'] == hashlib.sha256((out / 'camilladsp').read_bytes()).hexdigest()


def test_install_build_replaces_a_running_dsp_binary_without_writing_into_it(tmp_path, monkeypatch):
    """A running ELF cannot be opened for writing (ETXTBSY); upgrades must rename."""
    import install_files
    monkeypatch.setattr(install_files, 'check_build', lambda source: None)
    prefix = tmp_path / 'prefix'
    (prefix / 'bin').mkdir(parents=True)
    (prefix / 'share/hifimate/camilladsp').mkdir(parents=True)
    binary = prefix / 'bin/hifimate-camilladsp'
    shutil.copy2(shutil.which('sleep'), binary)  # a real ELF image, like CamillaDSP
    output = stage(tmp_path)
    running = subprocess.Popen([str(binary), '30'])
    try:
        with pytest.raises(OSError):
            open(binary, 'wb')  # what a copy-in-place install would do
        old_inode = binary.stat().st_ino
        install_files.install_build(prefix, output)
        assert binary.read_bytes() == (output / 'camilladsp').read_bytes()
        assert binary.stat().st_ino != old_inode and binary.stat().st_mode & 0o777 == 0o755
        assert (prefix / 'share/hifimate/camilladsp/release.json').read_bytes() == (output / 'release.json').read_bytes()
        assert running.poll() is None  # the running DSP keeps its old image
        assert not list(binary.parent.glob('.hifimate-stage-*'))
    finally:
        running.kill(); running.wait()


def test_wrong_dsp_blocks_install_before_writes(enabled_install, tmp_path, monkeypatch):
    prefix, _, _, log = enabled_install
    archive = fake_camilladsp_archive(tmp_path / 'other.tar.gz', version_text='CamillaDSP 3.0.1')
    monkeypatch.setenv('HIFIMATE_CAMILLADSP_ARCHIVE', str(archive))
    monkeypatch.setenv('HIFIMATE_CAMILLADSP_SHA256', hashlib.sha256(archive.read_bytes()).hexdigest())
    before = snapshot(prefix)
    result = run(INSTALL, prefix, '--apply', '--enable')
    assert result.returncode != 0 and 'not CamillaDSP 4.1.3' in result.stderr
    assert snapshot(prefix) == before
    assert not log.exists() or 'systemctl' not in log.read_text()


def test_new_destination_symlinks_are_preflighted(enabled_install, tmp_path):
    prefix, _, _, _ = enabled_install
    external = tmp_path / 'external'
    external.write_text('keep')
    (prefix / 'bin/hifimate-camilladsp').unlink()
    (prefix / 'bin/hifimate-camilladsp').symlink_to(external)
    result = run(INSTALL, prefix, '--apply')
    assert result.returncode != 0 and 'symlink' in result.stderr
    assert external.read_text() == 'keep'


def test_sha256_option_needs_an_archive(tmp_path, monkeypatch):
    monkeypatch.delenv('HIFIMATE_CAMILLADSP_ARCHIVE'); monkeypatch.delenv('HIFIMATE_CAMILLADSP_SHA256')
    result = run(INSTALL, tmp_path / 'prefix', '--camilladsp-sha256', 'ab')
    assert result.returncode == 2 and not (tmp_path / 'prefix').exists()


def test_frontend_visibility_and_event_data():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node is needed for browser-state regression tests')
    result = subprocess.run([node, str(ROOT / 'tests/spectrum_browser.mjs'),
                             str(ROOT / 'gui/spectrum-overlay.js')], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert 'behavior passed' in result.stdout


def test_interrupted_install_is_resumable_and_never_leaves_unrendered_configs(enabled_install, tmp_path):
    prefix, _, _, _ = enabled_install
    assert run(INSTALL, prefix, '--apply', '--enable').returncode == 0
    # Simulate the state an install leaves when it dies midway: journal written,
    # newer package files copied, a brand-new file added, configs half-copied.
    (prefix / '.hifimate-install-in-progress').write_text('hifimate-install-in-progress-v1\n')
    (prefix / 'lib/hifimate/mpd.py').write_text('# newer version, copied before the failure\n')
    (prefix / 'gui/new-asset.js').write_text('// added by the interrupted version\n')
    (prefix / 'config/host-stack/mpd.conf').write_text('port "6600"\nmusic_directory "@HOME@/Music"\n')
    result = run(INSTALL, prefix, '--apply', '--enable')
    assert result.returncode == 0, result.stderr
    assert not (prefix / '.hifimate-install-in-progress').exists()
    assert (prefix / 'lib/hifimate/mpd.py').read_bytes() == (ROOT / 'hifimate/mpd.py').read_bytes()
    for path in (prefix / 'config/host-stack').rglob('*'):
        if path.is_file():
            assert not re.search(r'@[A-Z_]+@', path.read_text()), path
    assert 'friendlyname = HiFiMate DLNA' in (prefix / 'config/host-stack/upmpdcli.conf').read_text()
    assert not list(Path(tempfile.gettempdir()).glob('hifimate-host-stack.*'))
    # Without an interrupted-install journal, an edited owned file still blocks.
    (prefix / 'lib/hifimate/mpd.py').write_text('# local edit\n')
    blocked = run(INSTALL, prefix, '--apply')
    assert blocked.returncode != 0 and 'owned file modified' in blocked.stderr
    # Uninstall stays strict even when a journal exists.
    (prefix / '.hifimate-install-in-progress').write_text('hifimate-install-in-progress-v1\n')
    assert run(UNINSTALL, prefix, '--apply').returncode != 0
