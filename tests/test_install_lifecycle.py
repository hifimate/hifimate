"""Regression coverage for managed installation ownership and upgrade failures."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from conftest import record_file
from test_install_scripts import INSTALL, ROOT, UNINSTALL, enabled_install, run

spec = importlib.util.spec_from_file_location('install_files', ROOT / 'scripts/install_files.py')
files = importlib.util.module_from_spec(spec)
spec.loader.exec_module(files)


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and not p.is_symlink()}


@pytest.fixture
def installed_prefix(tmp_path):
    prefix = tmp_path / 'prefix'
    result = run(INSTALL, prefix, '--apply')
    assert result.returncode == 0, result.stderr
    return prefix


def test_airplay_backend_is_in_general(installed_prefix):
    text = (installed_prefix / 'config/host-stack/shairport-sync.conf').read_text()
    general = re.search(r'general\s*=\s*\{([^}]+)\}', text)[1]
    assert 'output_backend = "pa";' in general
    assert 'audio_backend' not in text


@pytest.mark.parametrize('name', ['config/hifimate.conf', 'config/host-stack/mpd.conf',
                                 'bin/hifimate-runner', files.MARKER, files.MARKER + '.sha256'])
def test_reinstall_checks_old_hashes_before_writes(installed_prefix, name):
    target = installed_prefix / name
    target.write_bytes(target.read_bytes() + b'\n# administrator edit\n')
    before = snapshot(installed_prefix)
    result = run(INSTALL, installed_prefix, '--apply', '--install-deps')
    assert result.returncode != 0
    assert 'unexpected host command' not in result.stderr
    assert snapshot(installed_prefix) == before


@pytest.mark.parametrize('script', [INSTALL, UNINSTALL])
@pytest.mark.parametrize('name', ['bin/hifimate-runner', 'config/host-stack/pipewire',
                                 '.hifimate-install-manifest.sha256', 'config/hifimate.conf.tmp'])
def test_symlink_targets_and_parents_preserve_external_files(installed_prefix, tmp_path, script, name):
    target = installed_prefix / name
    outside = tmp_path / 'external'
    if target.exists():
        target.rename(outside)
    else:
        outside.write_text('external administrator data')
    target.symlink_to(outside, target_is_directory=outside.is_dir())
    before = snapshot(outside) if outside.is_dir() else outside.read_bytes()
    result = run(script, installed_prefix, '--apply')
    assert result.returncode != 0 and 'symlink' in result.stderr
    assert (snapshot(outside) if outside.is_dir() else outside.read_bytes()) == before
    assert target.is_symlink()
    assert (installed_prefix / files.MARKER).exists()


@pytest.mark.parametrize('script', [INSTALL, UNINSTALL])
def test_prefix_parent_symlink_rejected(installed_prefix, tmp_path, script):
    alias = tmp_path / 'alias'
    alias.symlink_to(tmp_path, target_is_directory=True)
    before = snapshot(installed_prefix)
    result = run(script, alias / installed_prefix.name, '--apply')
    assert result.returncode != 0 and 'symlink' in result.stderr
    assert snapshot(installed_prefix) == before


def test_multiply_linked_destination_is_not_overwritten(installed_prefix, tmp_path):
    outside = tmp_path / 'external'
    os.link(installed_prefix / 'bin/hifimate-runner', outside)
    before = outside.read_bytes()
    result = run(INSTALL, installed_prefix, '--apply')
    assert result.returncode != 0 and 'linked file' in result.stderr
    assert outside.read_bytes() == before


def test_directory_at_new_file_destination_is_rejected(installed_prefix):
    target = installed_prefix / 'config/hifimate.conf.tmp'
    target.mkdir()
    before = snapshot(installed_prefix)
    result = run(INSTALL, installed_prefix, '--apply')
    assert result.returncode != 0 and 'not a regular file' in result.stderr
    assert snapshot(installed_prefix) == before
    assert list(target.iterdir()) == []


def test_journal_alone_does_not_authorize_overwrite(tmp_path):
    prefix = tmp_path / 'incomplete'
    (prefix / 'bin').mkdir(parents=True)
    (prefix / files.JOURNAL).write_text('hifimate-install-in-progress-v1\n')
    (prefix / 'bin/hifimate-runner').write_text('administrator file')
    before = snapshot(prefix)
    result = run(INSTALL, prefix, '--apply')
    assert result.returncode != 0 and 'manifest' in result.stderr
    assert snapshot(prefix) == before


@pytest.mark.parametrize('name', ['config/host-stack/pipewire/nested/keep.conf',
                                 'lib/hifimate/nested/keep.txt', 'bin/nested/keep.txt',
                                 'systemd/nested/keep.service', 'music-assistant/nested/keep.txt',
                                 'lib/hifimate/__pycache__/foreign.cpython-313.pyc'])
def test_uninstall_preflights_nested_unowned_files(installed_prefix, name):
    path = installed_prefix / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('unowned')
    before = snapshot(installed_prefix)
    result = run(UNINSTALL, installed_prefix, '--apply')
    assert result.returncode != 0 and 'unmanaged file' in result.stderr
    assert snapshot(installed_prefix) == before


def test_uninstall_removes_every_manifest_entry_and_owned_caches(installed_prefix):
    # Future/legacy managed entries must not require a hard-coded shell list.
    path = installed_prefix / 'config/legacy/nested/owned.txt'
    path.parent.mkdir(parents=True)
    path.write_text('managed')
    record_file(installed_prefix, str(path.relative_to(installed_prefix)))
    result = subprocess.run([sys.executable, '-B', '-c',
                             'import py_compile,sys; py_compile.compile(sys.argv[1], doraise=True)',
                             str(installed_prefix / 'lib/hifimate/core.py')], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert list(installed_prefix.rglob('*.pyc'))
    result = run(INSTALL, installed_prefix, '--apply')
    assert result.returncode == 0, result.stderr
    # A managed file outside the installer output list remains managed on upgrade.
    assert 'config/legacy/nested/owned.txt' in (installed_prefix / files.MARKER).read_text()
    result = run(UNINSTALL, installed_prefix, '--apply')
    assert result.returncode == 0, result.stderr
    assert not installed_prefix.exists()


@pytest.mark.parametrize('script', [INSTALL, UNINSTALL])
def test_manifest_path_traversal_is_rejected(installed_prefix, tmp_path, script):
    outside = tmp_path / 'external'
    outside.write_text('preserve')
    marker = installed_prefix / files.MARKER
    data = marker.read_bytes() + f'{files.digest(outside.read_bytes())} ../external\n'.encode()
    marker.write_bytes(data)
    (installed_prefix / (files.MARKER + '.sha256')).write_text(files.digest(data) + '  ' + files.MARKER + '\n')
    result = run(script, installed_prefix, '--apply')
    assert result.returncode != 0 and 'unsafe manifest path' in result.stderr
    assert outside.read_text() == 'preserve'
    assert (installed_prefix / 'bin/hifimate-runner').exists()


def test_installed_python_wrappers_do_not_leave_bytecode(installed_prefix):
    for name in ('hifimate-output', 'hifimate-receiver-hook'):
        result = subprocess.run([str(installed_prefix / 'bin' / name), '--help'], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert list(installed_prefix.rglob('*.pyc')) == []
    result = run(UNINSTALL, installed_prefix, '--apply')
    assert result.returncode == 0, result.stderr
    assert not installed_prefix.exists()


@pytest.mark.parametrize('name', ['hifimate-output', 'hifimate-receiver-hook', 'hifimate-rt', 'hifimate-runner'])
def test_every_python_wrapper_passes_no_bytecode_flag(installed_prefix, tmp_path, name):
    fake = tmp_path / 'python-bin'
    fake.mkdir()
    python = fake / 'python3'
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    python.chmod(0o755)
    result = subprocess.run([str(installed_prefix / 'bin' / name)], capture_output=True, text=True,
                            env={**os.environ, 'PATH': f'{fake}:{os.environ["PATH"]}'})
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == '-B'


def test_late_fragment_conflict_has_no_partial_publication(enabled_install):
    prefix, config, _, log = enabled_install
    conflict = config / files.FRAGMENTS[-1]
    conflict.parent.mkdir(parents=True)
    conflict.write_text('administrator configuration')
    before = snapshot(prefix)
    result = run(INSTALL, prefix, '--apply', '--enable')
    assert result.returncode != 0 and 'refusing to overwrite' in result.stderr
    assert snapshot(prefix) == before
    assert not (config / files.FRAGMENTS[0]).exists()
    assert conflict.read_text() == 'administrator configuration'
    assert not log.exists()


@pytest.mark.parametrize('name', [files.FRAGMENTS[-1], 'systemd/user'])
def test_enable_checks_config_parent_symlinks(enabled_install, tmp_path, name):
    prefix, config, _, log = enabled_install
    outside = tmp_path / 'outside-config'
    outside.mkdir()
    target = config / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)
    result = run(INSTALL, prefix, '--apply', '--enable')
    assert result.returncode != 0 and 'symlink' in result.stderr
    assert list(outside.iterdir()) == []
    assert not (prefix / files.USER).exists()
    assert not log.exists()


def fragment_upgrade(prefix, tmp_path):
    config = Path(os.environ['XDG_CONFIG_HOME'])
    source = tmp_path / 'new-host-stack'
    shutil.copytree(ROOT / 'config/host-stack', source)
    files.publish_fragments(prefix, config, source)
    for name in files.FRAGMENTS:
        path = source / name
        path.write_text(path.read_text() + '\n# upgraded template\n')
    return config, source


def test_user_fragment_upgrade_updates_hashes_and_keeps_ownership(installed_prefix, tmp_path):
    config, source = fragment_upgrade(installed_prefix, tmp_path)
    files.publish_fragments(installed_prefix, config, source)
    records = files.user_records(installed_prefix, config)
    assert len(records) == len(files.FRAGMENTS)
    assert all(created == '1' for _, created in records.values())
    for name in files.FRAGMENTS:
        assert 'upgraded template' in (config / name).read_text()
    assert run(UNINSTALL, installed_prefix, '--apply').returncode == 0
    assert all(not (config / name).exists() for name in files.FRAGMENTS)


@pytest.mark.parametrize('failure', ['stage', 'fragment', 'manifest', 'signature'])
def test_failed_user_upgrade_preserves_old_files_and_signed_manifest(installed_prefix, tmp_path, monkeypatch, failure):
    config, source = fragment_upgrade(installed_prefix, tmp_path)
    before = (snapshot(installed_prefix), snapshot(config))
    real_replace = files.os.replace
    fail_path = {'fragment': config / files.FRAGMENTS[-1],
                 'manifest': installed_prefix / files.USER,
                 'signature': installed_prefix / (files.USER + '.sha256')}.get(failure)
    failed = False

    def fail_replace(src, dst):
        nonlocal failed
        if dst == fail_path and not failed:
            failed = True
            raise OSError('injected write failure')
        return real_replace(src, dst)

    if failure == 'stage':
        real_stage = files.stage

        def fail_stage(path, data, mode=0o644):
            if path == installed_prefix / (files.USER + '.sha256'):
                raise OSError('injected write failure')
            return real_stage(path, data, mode)

        monkeypatch.setattr(files, 'stage', fail_stage)
    else:
        monkeypatch.setattr(files.os, 'replace', fail_replace)
    with pytest.raises(OSError, match='injected write failure'):
        files.publish_fragments(installed_prefix, config, source)
    assert (snapshot(installed_prefix), snapshot(config)) == before
    files.user_records(installed_prefix, config)


def test_modified_user_fragment_blocks_both_upgrade_and_uninstall(installed_prefix, tmp_path):
    config, _ = fragment_upgrade(installed_prefix, tmp_path)
    (config / files.FRAGMENTS[-1]).write_text('administrator edit')
    before = (snapshot(installed_prefix), snapshot(config))
    for script in (INSTALL, UNINSTALL):
        result = run(script, installed_prefix, '--apply')
        assert result.returncode != 0 and 'modified or missing' in result.stderr
        assert (snapshot(installed_prefix), snapshot(config)) == before


def test_identical_preexisting_fragment_is_never_owned(installed_prefix, tmp_path):
    config = Path(os.environ['XDG_CONFIG_HOME'])
    source = ROOT / 'config/host-stack'
    target = config / files.FRAGMENTS[0]
    target.parent.mkdir(parents=True)
    original = files.fragment_data(source / files.FRAGMENTS[0], installed_prefix)
    target.write_bytes(original)
    files.publish_fragments(installed_prefix, config, source)
    files.publish_fragments(installed_prefix, config, source)
    assert files.user_records(installed_prefix, config)[files.FRAGMENTS[0]][1] == '0'
    result = run(UNINSTALL, installed_prefix, '--apply')
    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == original


def test_unsupported_upmpdcli_path_blocks_enable_before_installation_changes(enabled_install, monkeypatch):
    prefix, config, fake_bin, log = enabled_install
    # An executable which cannot be represented safely in a systemd unit is
    # rejected by dependency checks using the same resolution as rendering.
    bad = fake_bin.parent / 'bad path'
    bad.mkdir()
    (fake_bin / 'upmpdcli').rename(bad / 'upmpdcli')
    monkeypatch.setenv('PATH', f'{bad}:{os.environ["PATH"]}')
    before = snapshot(prefix)
    result = run(INSTALL, prefix, '--apply', '--enable')
    assert result.returncode != 0 and 'upmpdcli:unsupported-executable-path' in result.stdout
    assert snapshot(prefix) == before
    assert not (config / files.FRAGMENTS[0]).exists()
    assert 'systemctl' not in log.read_text()


def test_user_fragment_upgrade_through_installer(enabled_install, tmp_path):
    prefix, config, _, _ = enabled_install
    first = run(INSTALL, prefix, '--apply', '--enable')
    assert first.returncode == 0, first.stderr
    old_manifest = (prefix / files.USER).read_bytes()
    repository = tmp_path / 'upgrade-source'
    shutil.copytree(ROOT, repository, ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache'))
    for name in files.FRAGMENTS:
        source = repository / 'config/host-stack' / name
        source.write_text(source.read_text() + '\n# new release\n')
    result = run(repository / 'scripts/install.sh', prefix, '--apply', '--enable')
    assert result.returncode == 0, result.stderr
    assert (prefix / files.USER).read_bytes() != old_manifest
    assert all(created == '1' for _, created in files.user_records(prefix, config).values())
    assert all('new release' in (config / name).read_text() for name in files.FRAGMENTS)
    assert run(UNINSTALL, prefix, '--apply').returncode == 0
    assert not prefix.exists()
    assert all(not (config / name).exists() for name in files.FRAGMENTS)


def test_unit_parent_file_conflict_is_preflighted(enabled_install):
    prefix, config, _, log = enabled_install
    parent = config / 'systemd/user'
    parent.parent.mkdir(parents=True)
    parent.write_text('administrator file')
    before = snapshot(prefix)
    result = run(INSTALL, prefix, '--apply', '--enable')
    assert result.returncode != 0 and 'not a directory' in result.stderr
    assert snapshot(prefix) == before
    assert parent.read_text() == 'administrator file'
    assert not (prefix / files.USER).exists()
    assert not log.exists()


@pytest.mark.parametrize('script', [INSTALL, UNINSTALL])
def test_reserved_metadata_directory_is_rejected_before_changes(installed_prefix, script):
    (installed_prefix / '.hifimate-output.lock').mkdir()
    before = snapshot(installed_prefix)
    result = run(script, installed_prefix, '--apply')
    assert result.returncode != 0 and 'not a regular file' in result.stderr
    assert snapshot(installed_prefix) == before


def test_external_unit_conflict_preserves_previous_user_manifest(enabled_install):
    prefix, config, _, log = enabled_install
    assert run(INSTALL, prefix, '--apply', '--enable').returncode == 0
    link = config / 'systemd/user/hifimate-music-mpd.service'
    link.unlink()
    link.write_text('administrator unit')
    before = (snapshot(prefix), snapshot(config), log.read_bytes())
    result = run(INSTALL, prefix, '--apply', '--enable')
    assert result.returncode != 0 and 'unrelated user unit' in result.stderr
    assert (snapshot(prefix), snapshot(config), log.read_bytes()) == before


def test_missing_upmpdcli_blocks_enable_before_writes(enabled_install, monkeypatch):
    prefix, config, fake_bin, log = enabled_install
    (fake_bin / 'upmpdcli').unlink()
    # A closed PATH makes absence deterministic even on hosts with upmpdcli.
    for name in ('id', 'dirname', 'python3'):
        (fake_bin / name).symlink_to(shutil.which(name))
    monkeypatch.setenv('PATH', str(fake_bin))
    before = snapshot(prefix)
    result = run(INSTALL, prefix, '--apply', '--enable')
    assert result.returncode != 0
    assert '"upmpdcli"' in result.stdout
    assert snapshot(prefix) == before
    assert not (config / files.FRAGMENTS[0]).exists()
    assert 'systemctl' not in log.read_text()
