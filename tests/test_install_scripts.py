from pathlib import Path
import os
import subprocess
import json
import pytest
from conftest import record_file


ROOT = Path(__file__).parents[1]
INSTALL = ROOT / "scripts" / "install.sh"
UNINSTALL = ROOT / "scripts" / "uninstall.sh"
HOST_CHECK = ROOT / "scripts" / "host_stack.py"


def run(script, prefix, *args, env=None):
    return subprocess.run(
        [str(script), "--prefix", str(prefix), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_a_dry_run_names_the_callers_way_to_apply(tmp_path):
    import os
    env = {k: v for k, v in os.environ.items() if k != "HIFIMATE_DRY_RUN_HINT"}
    prefix = tmp_path / "p"
    for script, own in ((INSTALL, "pass --apply to write files"), (UNINSTALL, "pass --apply to remove files")):
        if script == UNINSTALL:
            assert run(INSTALL, prefix, "--apply", env=env).returncode == 0   # something to remove
        assert own in run(script, prefix, env=env).stdout               # run directly: its own --apply
        told = run(script, prefix, env={**env, "HIFIMATE_DRY_RUN_HINT": "drop --dry-run"}).stdout
        assert "drop --dry-run" in told and "--apply" not in told.splitlines()[-1]


def test_install_uninstall_round_trip(tmp_path):
    prefix = tmp_path / "hifimate"
    dry = run(INSTALL, prefix)
    assert dry.returncode == 0
    assert not prefix.exists()

    installed = run(INSTALL, prefix, "--apply")
    assert installed.returncode == 0, installed.stderr
    assert (prefix / "bin/hifimate-runner").is_file()
    assert (prefix / ".hifimate-install-manifest").is_file()
    assert (prefix / ".hifimate-install-manifest.sha256").is_file()
    unit = (prefix / "systemd/hifimate.service").read_text()
    assert "@PREFIX@" not in unit
    assert str(prefix / "bin/hifimate-runner") in unit
    for name in (
        "hifimate-camilladsp.service", "hifimate-spectrum.service",
        "hifimate-airplay.service", "hifimate-dlna.service",
        "hifimate-bluetooth.service", "hifimate-mpd.service", "hifimate-music-mpd.service",
    ):
        assert (prefix / "systemd" / name).is_file()
    assert (prefix / "config/host-stack/pipewire/pipewire-pulse.conf.d/50-hifimate-inputs.conf").is_file()

    removed = run(UNINSTALL, prefix, "--apply")
    assert removed.returncode == 0, removed.stderr
    assert not prefix.exists()


def test_uninstall_refuses_modified_owned_file(tmp_path):
    prefix = tmp_path / "hifimate"
    assert run(INSTALL, prefix, "--apply").returncode == 0
    (prefix / "config/hifimate.conf").write_text("changed\n")
    result = run(UNINSTALL, prefix, "--apply")
    assert result.returncode != 0
    assert prefix.exists()
    assert (prefix / "bin/hifimate-runner").exists()


def test_unmanaged_prefix_is_not_overwritten(tmp_path):
    prefix = tmp_path / "hifimate"
    prefix.mkdir()
    keep = prefix / "keep.txt"
    keep.write_text("keep")
    result = run(INSTALL, prefix, "--apply")
    assert result.returncode != 0
    assert keep.read_text() == "keep"


def test_uninstall_keeps_unrelated_prefix_file(tmp_path):
    prefix = tmp_path / "hifimate"
    assert run(INSTALL, prefix, "--apply").returncode == 0
    keep = prefix / "user-data.txt"
    keep.write_text("keep")
    result = run(UNINSTALL, prefix, "--apply")
    assert result.returncode == 0
    assert keep.read_text() == "keep"


def test_host_stack_checker_reports_external_gap_without_installing(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        '{"schema": 1, "packages": {"apt": []}, "external_packages": ["demo"], '
        '"executables": {"missing-demo": "definitely-not-installed-hifimate-demo"}}'
    )
    result = subprocess.run(
        [str(HOST_CHECK), "--check", "--manifest", str(manifest)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "missing-demo" in result.stdout


@pytest.fixture
def enabled_install(tmp_path, monkeypatch):
    prefix = tmp_path / "hifimate"
    fake_bin = tmp_path / "venv/bin"
    fake_bin.mkdir(parents=True)
    log = tmp_path / 'services.log'
    manifest = json.loads((ROOT / 'config/host-stack-manifest.json').read_text())
    for name in {*manifest['executables'].values(), 'systemctl'} - {'python3'}:
        tool = fake_bin / name
        tool.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$SERVICE_TEST_LOG"\n'
                        'if [ "$1" = --version ]; then echo "Output plugins: pipewire CamillaDSP 4.1.3 Built with features: pulse-backend"; fi\nexit 0\n')
        tool.chmod(0o755)
    modules = tmp_path / 'modules'
    modules.mkdir()
    for name in manifest['python_modules']:
        (modules / (name + '.py')).touch()
    monkeypatch.setenv('PYTHONPATH', str(modules))
    monkeypatch.setenv('PATH', f'{fake_bin}:{os.environ["PATH"]}')
    monkeypatch.setenv('SERVICE_TEST_LOG', str(log))
    result = run(INSTALL, prefix, '--apply')
    assert result.returncode == 0, result.stderr
    from test_output_devices import card
    proc = tmp_path / 'proc'
    card(proc, 0, 'TestDAC')
    conf = prefix / 'config/hifimate.conf'
    data = json.loads(conf.read_text())
    data['proc_asound'] = str(proc)
    conf.write_text(json.dumps(data))
    record_file(prefix, 'config/hifimate.conf')
    return prefix, Path(os.environ['XDG_CONFIG_HOME']), fake_bin, log


def test_enable_scoped_audio_fragments_are_restored(enabled_install):
    prefix, config_home, fake_bin, log = enabled_install
    installed = run(INSTALL, prefix, '--apply', '--enable')
    assert installed.returncode == 0, installed.stderr
    pulse = config_home / "pipewire/pipewire-pulse.conf.d/50-hifimate-inputs.conf"
    native = config_home / "pipewire/pipewire.conf.d/50-hifimate-inputs.conf"
    assert pulse.is_file()
    assert native.is_file() and 'audio.rate =' not in native.read_text()
    repeated = run(INSTALL, prefix, '--apply', '--enable')
    assert repeated.returncode == 0, repeated.stderr
    assert 'restart pipewire.service pipewire-pulse.service wireplumber.service' in repeated.stdout
    removed = run(UNINSTALL, prefix, '--apply')
    assert removed.returncode == 0, removed.stderr
    assert not pulse.exists()
    assert not native.exists()
    assert not prefix.exists()


def fake_docker(tmp_path, monkeypatch):
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        '#!/bin/sh\n'
        'printf "%s\\n" "$*" >> "$DOCKER_TEST_LOG"\n'
        'case " $* " in\n'
        '  *" down "*) exit "${DOCKER_DOWN_STATUS:-0}" ;;\n'
        '  *" pull "*) exit "${DOCKER_PULL_STATUS:-0}" ;;\n'
        '  *" up "*) exit "${DOCKER_UP_STATUS:-0}" ;;\n'
        'esac\nexit 0\n'
    )
    docker.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("DOCKER_TEST_LOG", str(log))
    return log


def test_music_assistant_dry_run_never_calls_docker(tmp_path, monkeypatch):
    log = fake_docker(tmp_path, monkeypatch)
    prefix = tmp_path / "prefix"
    result = run(INSTALL, prefix, "--with-music-assistant")
    assert result.returncode == 0, result.stderr
    assert "docker compose" in result.stdout
    assert not prefix.exists()
    assert not log.exists()


def test_music_assistant_lifecycle_upgrade_and_data_preservation(tmp_path, monkeypatch):
    log = fake_docker(tmp_path, monkeypatch)
    prefix = tmp_path / "prefix"
    data = tmp_path / "ma-data"
    music = tmp_path / "music"
    args = ("--apply", "--with-music-assistant", "--music-assistant-data-dir", str(data),
            "--music-directory", str(music))
    result = run(INSTALL, prefix, *args)
    assert result.returncode == 0, result.stderr
    assert "pull" in log.read_text()
    assert "stop server" in log.read_text()
    assert "up -d --wait --wait-timeout 180" in log.read_text()
    ma_env = prefix / "music-assistant/.env"
    assert ma_env.stat().st_mode & 0o777 == 0o600
    assert "MUSIC_ASSISTANT_HTTP_PORT=80" in ma_env.read_text()
    assert "MUSIC_ASSISTANT_IMAGE=ghcr.io/neqq3/ma-custom-loader-amd64:2.10.9" in ma_env.read_text()
    assert "MUSIC_ASSISTANT_TZ=" in ma_env.read_text()
    assert "MUSIC_ASSISTANT_NCM_API_IMAGE=moefurina/ncm-api:latest" in ma_env.read_text()
    assert "MUSIC_ASSISTANT_MIGU_API_PORT=3004" in ma_env.read_text()
    # migu-api is built locally; pulling it would fail the install
    assert "pull --ignore-buildable" in log.read_text()
    options = prefix / "music-assistant/provider-subscriber/options.json"
    # third-party provider sources are opt-in: none by default
    assert json.loads(options.read_text())["sources"] == []
    assert "music-assistant/nginx.conf" in (prefix / ".hifimate-install-manifest").read_text()
    upgraded_env = ma_env.read_text().replace("ma-custom-loader-amd64:2.10.9", "ma-custom-loader-amd64:9.9.9")
    assert upgraded_env != ma_env.read_text()
    ma_env.write_text(upgraded_env)
    # Subscriber options/state and downloaded providers are runtime-owned and
    # must survive a reinstall without invalidating the installer manifest.
    dynamic = prefix / "music-assistant/provider-subscriber"
    dynamic.mkdir(parents=True, exist_ok=True)
    (dynamic / "options.json").write_text('{"sources":["reviewed/provider"]}\n')
    (dynamic / "state.json").write_text('{"sources":{}}\n')
    provider = prefix / "music-assistant/custom_providers/reviewed"
    provider.mkdir(parents=True, exist_ok=True)
    (provider / "__init__.py").write_text("# runtime provider\n")
    (provider / "manifest.json").write_text("{}\n")
    (data / "database").write_text("keep-account-data")
    (music / "song.flac").write_text("keep-music")
    # A locally selected version survives reinstall and remains uninstallable.
    result = run(INSTALL, prefix, *args)
    assert result.returncode == 0, result.stderr
    assert ma_env.read_text() == upgraded_env
    assert (dynamic / "options.json").read_text() == '{"sources":["reviewed/provider"]}\n'
    assert (provider / "manifest.json").read_text() == "{}\n"
    before = log.read_text()
    result = run(UNINSTALL, prefix)
    assert result.returncode == 0, result.stderr
    assert log.read_text() == before
    result = run(UNINSTALL, prefix, "--apply")
    assert result.returncode == 0, result.stderr
    assert log.read_text().splitlines()[-1].endswith(" down")
    # Runtime subscriber files and downloaded providers are intentionally
    # preserved across uninstall so a user can retain reviewed providers.
    assert prefix.exists()
    assert (prefix / "music-assistant/provider-subscriber/options.json").exists()
    assert (prefix / "music-assistant/custom_providers/reviewed/manifest.json").exists()
    assert (data / "database").read_text() == "keep-account-data"
    assert (music / "song.flac").read_text() == "keep-music"


def test_music_assistant_stop_failure_keeps_deployment(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    prefix = tmp_path / "prefix"
    result = run(INSTALL, prefix, "--apply", "--with-music-assistant",
                 "--music-assistant-data-dir", str(tmp_path / "data"),
                 "--music-directory", str(tmp_path / "music"))
    assert result.returncode == 0, result.stderr
    monkeypatch.setenv("DOCKER_DOWN_STATUS", "1")
    result = run(UNINSTALL, prefix, "--apply")
    assert result.returncode != 0
    assert (prefix / "music-assistant/compose.yml").exists()
    assert (prefix / ".hifimate-install-manifest").exists()


def test_music_assistant_pull_failure_can_be_uninstalled(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("DOCKER_PULL_STATUS", "1")
    prefix = tmp_path / "prefix"
    result = run(INSTALL, prefix, "--apply", "--with-music-assistant",
                 "--music-assistant-data-dir", str(tmp_path / "data"),
                 "--music-directory", str(tmp_path / "music"))
    assert result.returncode != 0
    assert (prefix / ".hifimate-install-manifest").exists()
    result = run(UNINSTALL, prefix, "--apply")
    assert result.returncode == 0, result.stderr
    # The generated subscriber options remain as the runtime-owned directory;
    # uninstall removes only empty provider directories.
    assert (prefix / "music-assistant/provider-subscriber/options.json").exists()


def test_music_assistant_up_failure_keeps_recoverable_deployment(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    monkeypatch.setenv("DOCKER_UP_STATUS", "1")
    prefix = tmp_path / "prefix"
    args = ("--apply", "--with-music-assistant", "--music-assistant-data-dir",
            str(tmp_path / "data"), "--music-directory", str(tmp_path / "music"))
    result = run(INSTALL, prefix, *args)
    assert result.returncode != 0
    assert (prefix / ".hifimate-install-manifest").exists()
    assert (prefix / ".hifimate-install-in-progress").exists()
    monkeypatch.setenv("DOCKER_UP_STATUS", "0")
    result = run(INSTALL, prefix, *args)
    assert result.returncode == 0, result.stderr
    assert not (prefix / ".hifimate-install-in-progress").exists()
    snapshots = list((tmp_path / "data/.hifimate-deployment-backups").glob("*/compose.yml"))
    assert len(snapshots) == 1
    assert (snapshots[0].parent / "nginx.conf").read_bytes() == (prefix / "music-assistant/nginx.conf").read_bytes()
    assert run(UNINSTALL, prefix, "--apply").returncode == 0


def test_music_assistant_rejects_prefix_overlap_and_symlink(tmp_path, monkeypatch):
    log = fake_docker(tmp_path, monkeypatch)
    prefix = tmp_path / "prefix"
    alias = tmp_path / "alias"
    alias.symlink_to(prefix, target_is_directory=True)
    for data in (prefix / "config/host-stack/data", alias / "data"):
        result = run(INSTALL, prefix, "--apply", "--with-music-assistant",
                     "--music-assistant-data-dir", str(data),
                     "--music-directory", str(tmp_path / "music"))
        assert result.returncode != 0
        assert "overlaps" in result.stderr
        assert not prefix.exists()
        assert not log.exists()


def test_music_assistant_file_install_failure_can_be_retried(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    prefix = tmp_path / "prefix"
    data = tmp_path / "data"
    data.write_text("not-a-directory")
    args = ("--apply", "--with-music-assistant", "--music-assistant-data-dir", str(data),
            "--music-directory", str(tmp_path / "music"))
    result = run(INSTALL, prefix, *args)
    assert result.returncode != 0
    assert not (prefix / ".hifimate-install-manifest").exists()
    assert not prefix.exists()
    data.unlink()
    result = run(INSTALL, prefix, *args)
    assert result.returncode == 0, result.stderr
    assert run(UNINSTALL, prefix, "--apply").returncode == 0


def test_music_assistant_uninstall_rejects_edited_overlapping_path(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    prefix = tmp_path / "prefix"
    data = tmp_path / "data"
    result = run(INSTALL, prefix, "--apply", "--with-music-assistant",
                 "--music-assistant-data-dir", str(data),
                 "--music-directory", str(tmp_path / "music"))
    assert result.returncode == 0, result.stderr
    ma_env = prefix / "music-assistant/.env"
    ma_env.write_text(ma_env.read_text().replace(str(data), str(prefix / "config/host-stack/data")))
    result = run(UNINSTALL, prefix, "--apply")
    assert result.returncode != 0
    assert "overlaps" in result.stderr
    assert (prefix / "music-assistant/compose.yml").exists()


def test_changed_nginx_config_restarts_web_container(tmp_path, monkeypatch):
    # nginx renders its config only at container start: a new or replaced
    # template restarts web, an unchanged reinstall leaves it running.
    log = fake_docker(tmp_path, monkeypatch)
    prefix = tmp_path / "prefix"
    args = ("--apply", "--with-music-assistant", "--music-assistant-data-dir", str(tmp_path / "ma-data"),
            "--music-directory", str(tmp_path / "music"))
    assert run(INSTALL, prefix, *args).returncode == 0
    assert log.read_text().count(" restart web") == 1
    assert run(INSTALL, prefix, *args).returncode == 0
    assert log.read_text().count(" restart web") == 1


@pytest.mark.parametrize('answer, status, expected', [
    ('eyJ.fake.token', 0, 'eyJ.fake.token\n'),   # MA's script printed a token
    ('', 3, None),                                # MA has no user yet: skipped, install continues
    ('', 1, 'kept\n'),                            # failure: previous token kept
])
def test_install_prepares_the_ma_token_without_printing_it(tmp_path, monkeypatch, answer, status, expected):
    log = fake_docker(tmp_path, monkeypatch)
    fake = Path(os.environ['PATH'].split(':')[0]) / 'docker'
    fake.write_text(fake.read_text().replace(
        'case " $* " in\n',
        'case " $* " in\n  *" run --rm -T --no-deps --entrypoint /app/venv/bin/python "*)\n'
        f'    cat > "$DOCKER_TEST_LOG.stdin"; printf "%s" "{answer}" | sed "/^$/d"; [ -n "{answer}" ] && echo; exit {status} ;;\n'))
    prefix = tmp_path / "prefix"
    args = ("--apply", "--with-music-assistant", "--music-assistant-data-dir", str(tmp_path / "ma-data"),
            "--music-directory", str(tmp_path / "music"))
    token = prefix / 'config/ma-token'
    if expected == 'kept\n':
        assert run(INSTALL, prefix, *args).returncode == 0
        token.write_text('kept\n'); token.chmod(0o600)
    result = run(INSTALL, prefix, *args)
    assert result.returncode == 0, result.stderr
    assert 'hifimate_ctrl_token.py:/opt/hifimate_ctrl_token.py:ro server /opt/hifimate_ctrl_token.py' in log.read_text()
    assert answer not in result.stdout + result.stderr or not answer       # never echoed
    if expected is None:
        assert not token.exists() and 'has no user yet' in result.stderr
    else:
        assert token.read_text() == expected and token.stat().st_mode & 0o777 == 0o600
    assert not (prefix / 'config/ma-token.new').exists()
    if expected == 'kept\n':
        assert Path(str(log) + '.stdin').read_text() == 'kept\n'          # the current token goes in on stdin


def test_bundled_ma_providers_install_by_default_and_can_be_skipped(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    prefix = tmp_path / "prefix"
    args = ("--apply", "--with-music-assistant", "--music-assistant-data-dir", str(tmp_path / "ma-data"),
            "--music-directory", str(tmp_path / "music"))
    custom = prefix / "music-assistant/custom_providers"
    result = run(INSTALL, prefix, *args)
    assert result.returncode == 0, result.stderr
    for domain in ("bilibili", "migu"):
        assert (custom / domain / "manifest.json").read_text() == (ROOT / "music-assistant/providers" / domain / "manifest.json").read_text()
        assert not list((custom / domain).rglob("__pycache__"))
    assert not list(custom.glob(".*.new"))
    # A reinstall refreshes the bundled copy; it is runtime state, not in the manifest.
    (custom / "bilibili/stale.py").write_text("old")
    assert run(INSTALL, prefix, *args).returncode == 0
    assert not (custom / "bilibili/stale.py").exists()
    assert "custom_providers/bilibili" not in (prefix / ".hifimate-install-manifest").read_text()
    # Skipped: left as it is (it may be the user's own copy).
    (custom / "migu/mine.py").write_text("user")
    result = run(INSTALL, prefix, *args, "--without-ma-provider", "migu")
    assert result.returncode == 0 and "skipping bundled MA provider: migu" in result.stdout
    assert (custom / "migu/mine.py").exists()
    result = run(INSTALL, prefix, *args, "--without-ma-providers")
    assert result.returncode == 0 and "installed MA provider" not in result.stdout
    assert run(INSTALL, prefix, "--without-ma-provider", "spotify").returncode == 2


def test_ma_reinstall_keeps_env_and_data_and_snapshots_the_previous_config(tmp_path, monkeypatch):
    fake_docker(tmp_path, monkeypatch)
    prefix = tmp_path / "prefix"
    data = tmp_path / "data"
    args = ("--apply", "--with-music-assistant", "--music-assistant-data-dir", str(data),
            "--music-directory", str(tmp_path / "music"))
    assert run(INSTALL, prefix, *args).returncode == 0
    env = prefix / "music-assistant/.env"
    env.write_text(env.read_text().replace("ma-custom-loader-amd64:2.10.9", "ma-custom-loader-amd64:future") + "# user setting\n")
    saved = env.read_bytes()
    (data / ".database").write_bytes(b"user database untouched")
    result = run(INSTALL, prefix, *args)
    assert result.returncode == 0, result.stderr
    assert env.read_bytes() == saved and (data / ".database").read_bytes() == b"user database untouched"
    backup, = (data / ".hifimate-deployment-backups").iterdir()
    assert (backup / ".env").read_bytes() == saved and (backup / "compose.yml").is_file()



def test_install_stores_no_control_password(tmp_path, monkeypatch):
    # the control page checks the Linux password (PAM): an unattended install needs none
    prefix = tmp_path / "hifimate"
    done = run(INSTALL, prefix, "--apply", "--non-interactive", "--name", "Living Room")
    assert done.returncode == 0, done.stderr
    assert not list(prefix.rglob("ctrl-auth*"))
    assert run(INSTALL, prefix, "--apply", "--ctrl-password-file", str(tmp_path / "pw")).returncode == 2


def test_non_interactive_music_assistant_creates_its_owner(tmp_path, monkeypatch, fake_ma):
    fake_docker(tmp_path, monkeypatch)
    prefix = tmp_path / "prefix"
    args = ("--apply", "--non-interactive", "--with-music-assistant",
            "--music-assistant-data-dir", str(tmp_path / "ma-data"), "--music-directory", str(tmp_path / "music"))
    fake_ma.has_user = False
    # no skipping the owner: refused before anything is written
    missing = run(INSTALL, prefix, *args)
    assert missing.returncode == 2 and "--ma-admin-user" in missing.stderr and not prefix.exists()
    half = run(INSTALL, prefix, *args, "--ma-admin-user", "alice")
    assert half.returncode == 2 and not prefix.exists()
    secret = tmp_path / "ma.pw"
    secret.write_text("ma owner secret\n")
    done = run(INSTALL, prefix, *args, "--ma-admin-user", "alice", "--ma-admin-password-file", str(secret))
    assert done.returncode == 0, done.stderr
    assert fake_ma.created == [("alice", "ma owner secret")]
    assert "ma owner secret" not in done.stdout + done.stderr


def test_interactive_install_cannot_finish_without_an_ma_owner(tmp_path, monkeypatch, fake_ma):
    # no terminal and nothing given: a fresh MA fails the install instead of skipping its owner
    fake_docker(tmp_path, monkeypatch)
    fake_ma.has_user = False
    result = run(INSTALL, tmp_path / "prefix", "--apply", "--with-music-assistant",
                 "--music-assistant-data-dir", str(tmp_path / "ma-data"), "--music-directory", str(tmp_path / "music"))
    assert result.returncode == 1 and "HIFIMATE_MA_ADMIN_PASSWORD" in result.stderr
    assert fake_ma.created == []


def test_choose_output_is_not_unattended(tmp_path):
    result = run(INSTALL, tmp_path / "prefix", "--apply", "--non-interactive", "--choose-output")
    assert result.returncode == 2 and "--output-device" in result.stderr


def test_units_run_host_programs_found_on_path(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    import install_files
    prefix = tmp_path / "prefix"
    (prefix / "systemd").mkdir(parents=True)
    for name in ("hifimate-mpd.service", "hifimate-dlna.service", "hifimate-airplay.service", "hifimate-camilladsp.service"):
        (prefix / "systemd" / name).write_text((ROOT / "systemd" / name).read_text())
    found = {"mpd": "/usr/local/bin/mpd", "upmpdcli": "/opt/upnp/bin/upmpdcli"}
    install_files.render_units(prefix, which=found.get)
    units = {p.name: p.read_text() for p in (prefix / "systemd").iterdir()}
    assert "hifimate-rt /usr/local/bin/mpd --no-daemon" in units["hifimate-mpd.service"]
    assert "ExecStart=/opt/upnp/bin/upmpdcli -c" in units["hifimate-dlna.service"]
    # not found now: the bare name, for systemd's own search path
    assert "hifimate-rt shairport-sync -c" in units["hifimate-airplay.service"]
    assert all("@" not in line for text in units.values() for line in text.splitlines()
               if line.startswith("ExecStart=") and "@PREFIX@" not in line)


def test_a_moved_prefix_keeps_its_dsp_base_and_corrections(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    assert run(INSTALL, old, "--apply").returncode == 0
    conf = json.loads((old / "config/hifimate.conf").read_text())
    assert conf["base_config"] == str(old / "config/host-stack/camilladsp.yml")
    old.rename(new)
    result = run(INSTALL, new, "--apply")
    assert result.returncode == 0, result.stderr
    conf = json.loads((new / "config/hifimate.conf").read_text())
    assert conf["base_config"] == str(new / "config/host-stack/camilladsp.yml")


def test_reinstall_after_uninstall_keeps_the_users_data(tmp_path, monkeypatch):
    prefix = tmp_path / "hifimate"
    assert run(INSTALL, prefix, "--apply", "--yes").returncode == 0
    (prefix / "config/room-correction.json").write_text('{"enabled": false}\n')
    assert run(UNINSTALL, prefix, "--apply").returncode == 0
    assert (prefix / "config/room-correction.json").is_file() and not (prefix / ".hifimate-install-manifest").exists()
    again = run(INSTALL, prefix, "--apply", "--yes")
    assert again.returncode == 0, again.stderr
    assert (prefix / "config/room-correction.json").read_text() == '{"enabled": false}\n'
    # but a stray program file still means "half-finished, recover first"
    assert run(UNINSTALL, prefix, "--apply").returncode == 0
    (prefix / "bin").mkdir(exist_ok=True)
    (prefix / "bin/leftover").write_text("x")
    assert run(INSTALL, prefix, "--apply", "--yes").returncode != 0
