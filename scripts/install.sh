#!/bin/sh
set -eu
export PYTHONDONTWRITEBYTECODE=1

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
PREFIX=${HIFIMATE_PREFIX:-"$HOME/hifimate"}
APPLY=0
ENABLE=0
REALTIME=auto
SETUP_REALTIME=0
SETUP_BT_SPEAKER=0
OUTPUT_DEVICE=
CHOOSE_OUTPUT=0
LIST_OUTPUTS=0
OUTPUT_OPTIONS=0
INSTALL_DEPS=0
# An offline or mirrored copy of the official CamillaDSP archive may come from the environment.
CAMILLA_ARCHIVE=${HIFIMATE_CAMILLADSP_ARCHIVE:-}
CAMILLA_SHA256=${HIFIMATE_CAMILLADSP_SHA256:-}
WITH_MUSIC_ASSISTANT=0
# Music Assistant providers bundled with HiFi (music-assistant/providers/<domain>), installed by default.
MA_PROVIDERS='bilibili migu'
SKIP_MA_PROVIDERS=
DEVICE_NAME=
DEVICE_NAME_SET=0
MUSIC_ASSISTANT_DATA_DIR=${MUSIC_ASSISTANT_DATA_DIR:-"$HOME/.local/share/hifimate/music-assistant"}
MUSIC_LIBRARY_DIR=${MUSIC_LIBRARY_DIR:-"$HOME/Music"}
JOURNAL=.hifimate-install-in-progress
# Unattended: never ask; anything that would be asked must be given. Passwords
# never go on the command line (the process list): HIFIMATE_MA_ADMIN_PASSWORD
# or --ma-admin-password-file (install_secrets.py). There is no control
# password to give: the control page checks the Linux password of the user it
# runs as (hifimate/ctrl_auth.py, PAM service "hifimate").
NON_INTERACTIVE=0

usage() {
    echo "Output: --list-outputs | --output-device hw:CARD=<id>,DEV=<device> | --choose-output"
    echo "Scheduling: --realtime auto|on|off (default auto), --setup-realtime (explicit sudo)"
    echo "Bluetooth: --setup-bluetooth-speaker (explicit sudo: loudspeaker device class in BlueZ main.conf)"
    echo "usage: $0 [--prefix /absolute/path] [--apply] [--enable] [--install-deps]"
    echo "          [--camilladsp-archive /path/camilladsp-linux-pulseaudio-<arch>.tar.gz [--camilladsp-sha256 HEX]]"
    echo "          [--with-music-assistant] [--music-assistant-data-dir /path] [--music-directory /path]"
    echo "          [--without-ma-provider bilibili|migu (repeatable) | --without-ma-providers]"
    echo "          [--name <统一设备名称>]"
    echo "Unattended: --non-interactive (alias --yes) never asks; give instead"
    echo "          --ma-admin-user NAME or HIFIMATE_MA_ADMIN_USER, with --ma-admin-password-file FILE"
    echo "          or HIFIMATE_MA_ADMIN_PASSWORD (Music Assistant's owner; required for a new MA)"
}
while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; PREFIX=$2; shift 2 ;;
        --list-outputs) LIST_OUTPUTS=1; OUTPUT_OPTIONS=$((OUTPUT_OPTIONS + 1)); shift ;;
        --output-device) [ "$#" -ge 2 ] || exit 2; OUTPUT_DEVICE=$2; OUTPUT_OPTIONS=$((OUTPUT_OPTIONS + 1)); shift 2 ;;
        --choose-output) CHOOSE_OUTPUT=1; OUTPUT_OPTIONS=$((OUTPUT_OPTIONS + 1)); shift ;;
        --setup-realtime) SETUP_REALTIME=1; shift ;;
        --setup-bluetooth-speaker) SETUP_BT_SPEAKER=1; shift ;;
        --realtime) [ "$#" -ge 2 ] || exit 2; REALTIME=$2; shift 2 ;;
        --apply) APPLY=1; shift ;;
        --enable) ENABLE=1; shift ;;
        --camilladsp-archive) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; CAMILLA_ARCHIVE=$2; shift 2 ;;
        --camilladsp-sha256) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; CAMILLA_SHA256=$2; shift 2 ;;
        --install-deps) INSTALL_DEPS=1; shift ;;
        --with-music-assistant) WITH_MUSIC_ASSISTANT=1; shift ;;
        --without-ma-providers) SKIP_MA_PROVIDERS="$MA_PROVIDERS"; shift ;;
        --without-ma-provider)
            [ "$#" -ge 2 ] || { usage >&2; exit 2; }
            case " $MA_PROVIDERS " in *" $2 "*) ;; *) echo "unknown MA provider: $2 (bundled: $MA_PROVIDERS)" >&2; exit 2;; esac
            SKIP_MA_PROVIDERS="$SKIP_MA_PROVIDERS $2"; shift 2 ;;
        --name) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; DEVICE_NAME=$2; DEVICE_NAME_SET=1; shift 2 ;;
        --music-assistant-data-dir) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; MUSIC_ASSISTANT_DATA_DIR=$2; shift 2 ;;
        --music-directory) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; MUSIC_LIBRARY_DIR=$2; shift 2 ;;
        --non-interactive|--yes) NON_INTERACTIVE=1; shift ;;
        --ma-admin-user) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; HIFIMATE_MA_ADMIN_USER=$2; export HIFIMATE_MA_ADMIN_USER; shift 2 ;;
        --ma-admin-password-file) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; HIFIMATE_MA_ADMIN_PASSWORD_FILE=$2; export HIFIMATE_MA_ADMIN_PASSWORD_FILE; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
[ "$(id -u)" -ne 0 ] || { echo "run install as the audio user, not root/sudo" >&2; exit 2; }
[ "$SETUP_REALTIME" -eq 0 ] || [ "$REALTIME" != off ] || { echo "--setup-realtime conflicts with --realtime off" >&2; exit 2; }
case "$REALTIME" in auto|on|off) ;; *) echo "--realtime requires auto/on/off" >&2; exit 2;; esac
[ "$OUTPUT_OPTIONS" -le 1 ] || { echo "conflicting output options" >&2; exit 2; }
[ "$NON_INTERACTIVE" -eq 0 ] || [ "$CHOOSE_OUTPUT" -eq 0 ] || { echo "--choose-output asks; use --output-device with --non-interactive" >&2; exit 2; }
# Check given passwords now, before anything changes (nothing secret is printed).
python3 -B "$ROOT/scripts/install_secrets.py" check --ma || exit 2
if [ "$NON_INTERACTIVE" -eq 1 ]; then
    SUDO='sudo -n'
else
    SUDO=sudo
fi
echo "Realtime policy: $REALTIME (auto probes PREEMPT_RT at audio process startup; permissions are separate)"
case "$PREFIX" in /*) ;; *) echo 'prefix must be absolute' >&2; exit 2 ;; esac
case "$PREFIX" in *[[:space:]%]*) echo 'prefix may not contain whitespace or %' >&2; exit 2;; esac
case "$PREFIX" in /|/usr|/usr/local|/etc|/home|/root)
    echo "refusing broad prefix: $PREFIX" >&2; exit 2;;
esac
if [ -e "$PREFIX/.hifimate-output-transaction.json" ] || [ -L "$PREFIX/.hifimate-output-transaction.json" ]; then
    echo "pending output transaction; run $PREFIX/bin/hifimate-output --recover --apply first" >&2
    exit 1
fi
# Verify ownership and every existing path before reads, copies or side effects.
python3 -B "$ROOT/scripts/install_files.py" preflight "$PREFIX" --source "$ROOT"
if [ "$DEVICE_NAME_SET" -eq 1 ]; then
    DEVICE_NAME=$(python3 -B "$ROOT/hifimate/identity.py" select "$PREFIX" --name="$DEVICE_NAME") || exit 2
elif [ "$LIST_OUTPUTS" -eq 0 ] && [ "$NON_INTERACTIVE" -eq 0 ]; then
    DEVICE_NAME=$(python3 -B "$ROOT/hifimate/identity.py" select "$PREFIX" --prompt) || exit 2
else
    DEVICE_NAME=$(python3 -B "$ROOT/hifimate/identity.py" select "$PREFIX") || exit 2
fi
if [ "$NON_INTERACTIVE" -eq 1 ] && [ "$APPLY" -eq 1 ]; then
    # A first install has no MA owner yet; it cannot be skipped (hifimate-ctrl needs it).
    if [ "$WITH_MUSIC_ASSISTANT" -eq 1 ] && [ ! -s "$PREFIX/config/ma-token" ] && [ -z "${HIFIMATE_MA_ADMIN_USER:-}" ]; then
        echo 'Music Assistant needs its admin: give --ma-admin-user with --ma-admin-password-file or HIFIMATE_MA_ADMIN_PASSWORD' >&2; exit 2
    fi
fi
if [ "$ENABLE" -eq 1 ]; then
    python3 -B "$ROOT/scripts/install_files.py" check-fragments "$PREFIX" --source "$ROOT/config/host-stack"
fi
[ -z "$CAMILLA_SHA256" ] || [ -n "$CAMILLA_ARCHIVE" ] || { echo "--camilladsp-sha256 needs --camilladsp-archive" >&2; exit 2; }
# Resolve before any dependency installation, file writes or service changes.
if [ "$LIST_OUTPUTS" -eq 1 ]; then
    exec "$ROOT/scripts/output-device.sh" --prefix "$PREFIX" --list
fi
existing_output=$(python3 -B - "$PREFIX/config/hifimate.conf" <<'PYCONF'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1])
print(json.loads(p.read_text()).get('alsa_device','auto') if p.exists() else 'auto')
PYCONF
)
if [ -n "$OUTPUT_DEVICE" ]; then
    OUTPUT_DEVICE=$("$ROOT/scripts/output-device.sh" --prefix "$PREFIX" --resolve-install --device "$OUTPUT_DEVICE")
elif [ "$CHOOSE_OUTPUT" -eq 1 ]; then
    OUTPUT_DEVICE=$("$ROOT/scripts/output-device.sh" --prefix "$PREFIX" --resolve-install --choose)
elif [ "$APPLY" -eq 1 ] && [ "$ENABLE" -eq 1 ] && [ "$existing_output" = auto ]; then
    OUTPUT_DEVICE=$("$ROOT/scripts/output-device.sh" --prefix "$PREFIX" --resolve-install)
elif [ "$APPLY" -eq 1 ] && [ "$ENABLE" -eq 1 ]; then
    OUTPUT_DEVICE=$("$ROOT/scripts/output-device.sh" --prefix "$PREFIX" --resolve-install --device "$existing_output")
fi
if [ "$WITH_MUSIC_ASSISTANT" -eq 1 ]; then
    # Resolve existing .env before touching any deployment files.
    ma_dir="$PREFIX/music-assistant"
    ma_env="$ma_dir/.env"
    if [ -e "$ma_env" ] || [ -L "$ma_env" ]; then
        [ -f "$ma_env" ] && [ ! -L "$ma_env" ] || { echo 'refusing nonregular MA .env' >&2; exit 1; }
        MUSIC_ASSISTANT_DATA_DIR=$(awk -F= '$1 == "MUSIC_ASSISTANT_DATA_DIR" {print substr($0,index($0,"=")+1); exit}' "$ma_env")
        MUSIC_LIBRARY_DIR=$(awk -F= '$1 == "MUSIC_LIBRARY_DIR" {print substr($0,index($0,"=")+1); exit}' "$ma_env")
    fi
    for ma_path in "$MUSIC_ASSISTANT_DATA_DIR" "$MUSIC_LIBRARY_DIR"; do
        case "$ma_path" in /*) ;; *) echo 'MA directories must be absolute' >&2; exit 2;; esac
        case "$ma_path" in *[!a-zA-Z0-9_./-]*) echo 'MA directory paths must use letters, digits, _, ., / or -' >&2; exit 2;; esac
    done
    python3 -B - "$PREFIX" "$MUSIC_ASSISTANT_DATA_DIR" "$MUSIC_LIBRARY_DIR" <<'PYVALIDATE'
from pathlib import Path
import sys
prefix, data, music = (Path(p).resolve() for p in sys.argv[1:])
for label, path in (("MA data", data), ("music", music)):
    if path == prefix or prefix in path.parents or path in prefix.parents:
        raise SystemExit(f"{label} directory overlaps installation prefix: {path}")
    for parent in (path, *path.parents):
        if parent.exists() and not parent.is_dir():
            raise SystemExit(f"{label} path is not a directory: {parent}")
PYVALIDATE
    # Compose process receives exactly the paths validated above.
    export MUSIC_ASSISTANT_DATA_DIR MUSIC_LIBRARY_DIR
    if [ "$APPLY" -eq 1 ]; then
        command -v docker >/dev/null 2>&1 || { echo 'Music Assistant requires docker' >&2; exit 1; }
        docker compose version >/dev/null 2>&1 || { echo 'Music Assistant requires the docker compose plugin' >&2; exit 1; }
        docker info >/dev/null 2>&1 || { echo 'Docker daemon is not accessible to this user' >&2; exit 1; }
    fi
fi

run() {
    echo "+ $*"
    if [ "$APPLY" -eq 1 ]; then
        "$@"
    fi
}
for required in hifimate/core.py hifimate/daemon.py README.md INSTALL.md NOTICE.md docs/ARCHITECTURE.md docs/SPECTRUM.md \
    config/hifimate.conf config/host-stack-manifest.json scripts/host_stack.py \
    scripts/camilladsp_release.py hifimate/spectrum.py hifimate/spectrum_proxy.py hifimate/dsp_model.py \
    bin/hifimate-spectrum systemd/hifimate-spectrum.service config/host-stack/spectrum.json \
    config/host-stack/camilladsp.yml gui/spectrum-overlay.css gui/spectrum-overlay.js gui/spectrum-tap.js \
    gui/ctrl.html gui/ctrl.css gui/ctrl.js gui/nowplaying.js gui/nowplaying.css hifimate/ctrl.py bin/hifimate-ctrl systemd/hifimate-ctrl.service \
    gui/roomfit.html gui/roomfit.css gui/roomfit.js gui/roomfit-worklet.js gui/eq.html gui/eq.css gui/eq.js gui/settings.html gui/settings.js hifimate/eq.py hifimate/settings.py hifimate/roomfit.py hifimate/room_eq.py hifimate/tls.py hifimate/nowplaying.py hifimate/ma_api.py \
    scripts/enable_ma_visualizer.py scripts/configure_ma_players.py scripts/bluetooth_speaker.py \
    systemd/hifimate.service bin/hifimate-runner bin/hifimate-bluetooth bin/hifimate-airplay-net systemd/hifimate-airplay-net.service \
    hifimate/identity.py hifimate/bluetooth_alias.py \
    hifimate/formats.py hifimate/receiver_events.py hifimate/remote_pause.py hifimate/mpd.py \
    music-assistant/compose.yml \
    music-assistant/custom-loader-run.sh \
    music-assistant/providers/bilibili/__init__.py music-assistant/providers/bilibili/manifest.json \
    music-assistant/providers/migu/__init__.py music-assistant/providers/migu/manifest.json \
    music-assistant/nginx.conf music-assistant/nginx-entrypoint.sh music-assistant/hifimate_ctrl_token.py \
    music-assistant/.env.example music-assistant/README.md; do
    [ -f "$ROOT/$required" ] || { echo "missing repository file: $required" >&2; exit 1; }
done

# The official CamillaDSP release (pinned, unmodified), staged before the
# dependency check so --enable checks the binary that will be installed.
camilla_stage="${XDG_CACHE_HOME:-$HOME/.cache}/hifimate/camilladsp-stage"
if [ "$APPLY" -eq 1 ]; then
    set --
    [ -z "$CAMILLA_ARCHIVE" ] || set -- --archive "$CAMILLA_ARCHIVE"
    [ -z "$CAMILLA_SHA256" ] || set -- "$@" --sha256 "$CAMILLA_SHA256"
    python3 -B "$ROOT/scripts/camilladsp_release.py" prepare "$camilla_stage" "$@"
else
    echo "+ fetch and verify the official CamillaDSP release into $camilla_stage"
fi

if [ "$INSTALL_DEPS" -eq 1 ]; then
    if [ "$APPLY" -eq 1 ]; then
        if [ "$NON_INTERACTIVE" -eq 1 ]; then deps_yes=--yes; else deps_yes=; fi
        python3 -B "$ROOT/scripts/host_stack.py" --install-deps --apply $deps_yes --prefix "$PREFIX" --manifest "$ROOT/config/host-stack-manifest.json"
    else
        python3 -B "$ROOT/scripts/host_stack.py" --install-deps --prefix "$PREFIX" --manifest "$ROOT/config/host-stack-manifest.json"
    fi
fi

if [ "$ENABLE" -eq 1 ] && [ "$APPLY" -eq 0 ]; then
    echo '+ would check host dependencies against the staged CamillaDSP before enabling services'
elif [ "$ENABLE" -eq 1 ]; then
    python3 -B "$ROOT/scripts/host_stack.py" --check --prefix "$PREFIX" --manifest "$ROOT/config/host-stack-manifest.json" --camilladsp "$camilla_stage/camilladsp"
fi

if [ "$SETUP_REALTIME" -eq 1 ] && [ "$APPLY" -eq 1 ]; then
    python3 -B "$ROOT/scripts/realtime_permissions.py" check-user
fi
if { [ "$SETUP_REALTIME" -eq 1 ] || [ "$SETUP_BT_SPEAKER" -eq 1 ]; } && [ "$APPLY" -eq 1 ]; then
    # Authenticate before any change; NOPASSWD rules need no prompt (sudo -v
    # still asks when any other matching rule requires a password).
    sudo -n true 2>/dev/null || $SUDO -v
fi

if [ "$WITH_MUSIC_ASSISTANT" -eq 1 ] && [ "$APPLY" -eq 1 ] && [ -f "$ma_dir/.hifimate-managed" ]; then
    backup_dir="$MUSIC_ASSISTANT_DATA_DIR/.hifimate-deployment-backups/$(date -u +%Y%m%dT%H%M%S)-$$"
    mkdir -p "$backup_dir"
    chmod 0700 "$backup_dir"
    cp "$ma_dir/compose.yml" "$ma_env" "$backup_dir/"
    for backup_file in nginx.conf nginx-entrypoint.sh; do
        if [ -f "$ma_dir/$backup_file" ]; then cp "$ma_dir/$backup_file" "$backup_dir/"; fi
    done
    docker compose --project-directory "$ma_dir" --env-file "$ma_env" -f "$ma_dir/compose.yml" images --format json > "$backup_dir/images.json"
    echo "previous MA deployment configuration saved: $backup_dir"
fi

# The journal records incomplete activation; retry still requires a valid manifest.
if [ "$APPLY" -eq 1 ]; then
    mkdir -p "$PREFIX"
    printf '%s\n' 'hifimate-install-in-progress-v1' > "$PREFIX/$JOURNAL"
fi

run mkdir -p "$PREFIX/lib" "$PREFIX/bin" "$PREFIX/config" "$PREFIX/gui" "$PREFIX/systemd" "$PREFIX/share/doc/hifimate"
run python3 -B "$ROOT/scripts/install_files.py" copy-package "$PREFIX" --source "$ROOT/hifimate"
run cp "$ROOT/scripts/realtime_permissions.py" "$PREFIX/bin/hifimate-realtime-permissions"
run cp "$ROOT/scripts/bluetooth_speaker.py" "$PREFIX/bin/hifimate-bluetooth-speaker"
run cp "$ROOT/bin/hifimate-rt" "$PREFIX/bin/hifimate-rt"
run chmod 0755 "$PREFIX/bin/hifimate-rt"
run cp "$ROOT/bin/hifimate-output" "$PREFIX/bin/hifimate-output"
run chmod 0755 "$PREFIX/bin/hifimate-output"
run cp "$ROOT/bin/hifimate-bluetooth" "$PREFIX/bin/"
run chmod 0755 "$PREFIX/bin/hifimate-bluetooth"
run cp "$ROOT/bin/hifimate-spectrum" "$PREFIX/bin/hifimate-spectrum"
run cp "$ROOT/bin/hifimate-airplay-net" "$PREFIX/bin/hifimate-airplay-net"
run chmod 0755 "$PREFIX/bin/hifimate-airplay-net"
run chmod 0755 "$PREFIX/bin/hifimate-spectrum"
run cp "$ROOT/bin/hifimate-ctrl" "$PREFIX/bin/hifimate-ctrl"
run chmod 0755 "$PREFIX/bin/hifimate-ctrl"
run cp "$ROOT/bin/hifimate-runner" "$PREFIX/bin/hifimate-runner"
run cp "$ROOT/bin/hifimate-receiver-hook" "$PREFIX/bin/hifimate-receiver-hook"
run chmod 0755 "$PREFIX/bin/hifimate-receiver-hook"
run chmod 0755 "$PREFIX/bin/hifimate-runner"
if [ ! -e "$PREFIX/config/hifimate.conf" ]; then
    run cp "$ROOT/config/hifimate.conf" "$PREFIX/config/hifimate.conf"
fi
run cp "$ROOT/config/host-stack-manifest.json" "$PREFIX/config/host-stack-manifest.json"
run cp "$ROOT/scripts/host_stack.py" "$PREFIX/bin/host-stack-checker"
run chmod 0755 "$PREFIX/bin/host-stack-checker"
prefix_escaped=$(printf '%s' "$PREFIX" | sed 's/[&|\\]/\\&/g')
home_escaped=$(printf '%s' "$HOME" | sed 's/[&|\\]/\\&/g')
# Running services read these configs. Render the complete set (placeholders
# and receiver names) in a staging copy first, so an interrupted install can
# never leave a live config with unrendered @PLACEHOLDERS@.
if [ "$APPLY" -eq 1 ]; then
    stack_stage=$(mktemp -d "${TMPDIR:-/tmp}/hifimate-host-stack.XXXXXX")
    cp -R "$ROOT/config/host-stack/." "$stack_stage/"
    find "$stack_stage" -type f -print | while read -r config_file; do
        sed -e "s|@PREFIX@|$prefix_escaped|g" -e "s|@HOME@|$home_escaped|g" -e "s|@RUNTIME@|/run/user/$(id -u)/hifimate|g" "$config_file" > "$config_file.tmp"
        mv "$config_file.tmp" "$config_file"
    done
    python3 -B "$ROOT/hifimate/identity.py" render "$PREFIX" --name="$DEVICE_NAME" --config-dir "$stack_stage"
    mkdir -p "$PREFIX/config/host-stack"
    cp -R "$stack_stage/." "$PREFIX/config/host-stack/"
    rm -rf "$stack_stage"
else
    echo "+ render $ROOT/config/host-stack in a staging directory, then copy it to $PREFIX/config/host-stack"
fi
run mkdir -p "$PREFIX/music-assistant"
# nginx renders its config only when the web container starts; remember the
# installed copy so a changed config restarts web below instead of being ignored.
web_config() { cat "$PREFIX/music-assistant/nginx.conf" "$PREFIX/music-assistant/nginx-entrypoint.sh" 2>/dev/null | sha256sum; }
web_before=$(web_config)
run cp "$ROOT/music-assistant/compose.yml" "$ROOT/music-assistant/custom-loader-run.sh" "$ROOT/music-assistant/nginx.conf" "$ROOT/music-assistant/nginx-entrypoint.sh" "$ROOT/music-assistant/hifimate_ctrl_token.py" "$ROOT/music-assistant/.env.example" "$ROOT/music-assistant/README.md" "$PREFIX/music-assistant/"
run chmod 0755 "$PREFIX/music-assistant/custom-loader-run.sh"
run chmod 0755 "$PREFIX/music-assistant/nginx-entrypoint.sh"
run mkdir -p "$PREFIX/share/hifimate/camilladsp"
if [ "$APPLY" -eq 1 ]; then
    # A release package records its commit at build time (hifimate/_commit.txt);
    # a git checkout knows its own.
    if [ -f "$ROOT/hifimate/_commit.txt" ]; then
        installed_commit=$(head -c 64 "$ROOT/hifimate/_commit.txt" | tr -cd '0-9a-f')
    else
        installed_commit=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || printf 'unknown')
        if ! git -C "$ROOT" diff --quiet 2>/dev/null; then installed_commit="$installed_commit+dirty"; fi
    fi
    printf '%s\n' "${installed_commit:-unknown}" > "$PREFIX/share/hifimate/commit"
    sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' "$ROOT/hifimate/__init__.py" > "$PREFIX/share/hifimate/version"
fi
run python3 -B "$ROOT/scripts/install_files.py" install-build "$PREFIX" --source "$camilla_stage"
run cp "$ROOT/gui/spectrum-overlay.css" "$ROOT/gui/spectrum-overlay.js" "$ROOT/gui/spectrum-tap.js" "$ROOT/gui/ctrl.html" "$ROOT/gui/ctrl.css" "$ROOT/gui/ctrl.js" "$ROOT/gui/nowplaying.js" "$ROOT/gui/nowplaying.css" "$ROOT/gui/roomfit.html" "$ROOT/gui/roomfit.css" "$ROOT/gui/roomfit.js" "$ROOT/gui/roomfit-worklet.js" "$ROOT/gui/eq.html" "$ROOT/gui/eq.css" "$ROOT/gui/eq.js" "$ROOT/gui/settings.html" "$ROOT/gui/settings.js" "$PREFIX/gui/"
if [ "$APPLY" -eq 1 ]; then
    printf '{"mode":"%s"}\n' "$REALTIME" > "$PREFIX/config/realtime.json"
    if [ -f "$PREFIX/config/hifimate.conf" ]; then
        sed "s|@PREFIX@|$prefix_escaped|g" "$PREFIX/config/hifimate.conf" > "$PREFIX/config/hifimate.conf.tmp"
        mv "$PREFIX/config/hifimate.conf.tmp" "$PREFIX/config/hifimate.conf"
        # The managed DSP base lives in this prefix; room correction and
        # personal filters are found beside it. Kept current on every install,
        # so a prefix that was moved keeps its corrections.
        python3 -B - "$PREFIX/config/hifimate.conf" "$PREFIX/config/host-stack/camilladsp.yml" <<'PYBASE'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1]); data=json.loads(p.read_text())
if data.get('base_config') != sys.argv[2]:
    data['base_config']=sys.argv[2]
    p.write_text(json.dumps(data,indent=2)+'\n')
PYBASE
    fi
    if [ -n "$OUTPUT_DEVICE" ]; then
        python3 -B - "$PREFIX/config/hifimate.conf" "$OUTPUT_DEVICE" <<'PYOUTPUT'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1]); data=json.loads(p.read_text()); data['alsa_device']=sys.argv[2]
p.write_text(json.dumps(data,indent=2)+'\n')
PYOUTPUT
    fi
    for unit in "$ROOT"/systemd/*.service; do
        name=$(basename "$unit")
        sed -e "s|@PREFIX@|$prefix_escaped|g" -e "s|@HOME@|$home_escaped|g" -e "s|@RUNTIME@|/run/user/$(id -u)/hifimate|g" "$unit" > "$PREFIX/systemd/$name"
    done
    python3 -B "$ROOT/scripts/install_files.py" render-units "$PREFIX"
else
    echo "+ render $PREFIX/systemd/*.service"
    echo "+ configure receiver name: $DEVICE_NAME"
fi
if [ "$WITH_MUSIC_ASSISTANT" -eq 1 ]; then
    ma_dir="$PREFIX/music-assistant"
    ma_env="$ma_dir/.env"
    if [ "$APPLY" -eq 0 ]; then
        echo "+ create $ma_env"
        echo "+ docker compose --project-directory $ma_dir --env-file $ma_env -f $ma_dir/compose.yml pull --ignore-buildable"
        echo "+ docker compose --project-directory $ma_dir --env-file $ma_env -f $ma_dir/compose.yml up -d"
    else
        # systemd's answer first (all systemd distributions), then the
        # /etc/localtime link (Debian, Fedora, Arch...), then Debian's file.
        system_timezone=$(timedatectl show -p Timezone --value 2>/dev/null || true)
        if [ -z "$system_timezone" ] && [ -L /etc/localtime ]; then
            system_timezone=$(readlink -f /etc/localtime | sed 's#^.*/zoneinfo/##')
        fi
        if [ -z "$system_timezone" ]; then
            system_timezone=$(cat /etc/timezone 2>/dev/null || true)
        fi
        case "$system_timezone" in
            ''|*[!A-Za-z0-9_./+-]*) system_timezone=UTC ;;
        esac
        if [ -e "$ma_env" ] || [ -L "$ma_env" ]; then
            [ ! -L "$ma_env" ] || { echo "refusing symlink Music Assistant env: $ma_env" >&2; exit 1; }
        else
            old_umask=$(umask)
            umask 077
            cat > "$ma_env" <<EOF
MUSIC_ASSISTANT_IMAGE=ghcr.io/neqq3/ma-custom-loader-amd64:2.10.9
MUSIC_ASSISTANT_WEB_IMAGE=nginx:1.27-alpine
MUSIC_ASSISTANT_CONTAINER_NAME=hifimate-music-assistant
MUSIC_ASSISTANT_WEB_CONTAINER_NAME=hifimate-music-assistant-web
MUSIC_ASSISTANT_DATA_DIR=$MUSIC_ASSISTANT_DATA_DIR
MUSIC_LIBRARY_DIR=$MUSIC_LIBRARY_DIR
MUSIC_ASSISTANT_CUSTOM_PROVIDERS_DIR=$ma_dir/custom_providers
MUSIC_ASSISTANT_PROVIDER_SUBSCRIBER_CONFIG_DIR=$ma_dir/provider-subscriber
MUSIC_ASSISTANT_PROVIDER_SUBSCRIBER_IMAGE=ghcr.io/neqq3/ma-provider-subscriber-amd64:0.2.0
MUSIC_ASSISTANT_PROVIDER_SUBSCRIBER_CONTAINER_NAME=hifimate-ma-provider-subscriber
MUSIC_ASSISTANT_HTTP_BIND=0.0.0.0
MUSIC_ASSISTANT_HTTP_PORT=80
MUSIC_ASSISTANT_HTTPS_PORT=443
MUSIC_ASSISTANT_UID=$(id -u)
MUSIC_ASSISTANT_GID=$(id -g)
MUSIC_ASSISTANT_LOG_LEVEL=info
MUSIC_ASSISTANT_TZ=$system_timezone
MUSIC_ASSISTANT_NCM_API_IMAGE=moefurina/ncm-api:latest
MUSIC_ASSISTANT_NCM_API_CONTAINER_NAME=hifimate-ncm-api
MUSIC_ASSISTANT_NCM_API_BIND=127.0.0.1
MUSIC_ASSISTANT_NCM_API_PORT=3003
MUSIC_ASSISTANT_MIGU_API_CONTAINER_NAME=hifimate-migu-api
MUSIC_ASSISTANT_MIGU_API_BIND=127.0.0.1
MUSIC_ASSISTANT_MIGU_API_PORT=3004
EOF
            chmod 0600 "$ma_env"
            umask "$old_umask"
        fi
        ensure_env_var() {
            key="$1"
            value="$2"
            if ! grep -q "^${key}=" "$ma_env"; then
                printf '%s=%s\n' "$key" "$value" >> "$ma_env"
            fi
        }
        ensure_env_var MUSIC_ASSISTANT_TZ "$system_timezone"
        ensure_env_var MUSIC_ASSISTANT_NCM_API_IMAGE 'moefurina/ncm-api:latest'
        ensure_env_var MUSIC_ASSISTANT_NCM_API_CONTAINER_NAME 'hifimate-ncm-api'
        ensure_env_var MUSIC_ASSISTANT_NCM_API_BIND '127.0.0.1'
        ensure_env_var MUSIC_ASSISTANT_NCM_API_PORT '3003'
        ensure_env_var MUSIC_ASSISTANT_MIGU_API_CONTAINER_NAME 'hifimate-migu-api'
        ensure_env_var MUSIC_ASSISTANT_MIGU_API_BIND '127.0.0.1'
        ensure_env_var MUSIC_ASSISTANT_MIGU_API_PORT '3004'
        # Existing .env is user-editable and survives reinstall/upgrade.
        mkdir -p "$MUSIC_ASSISTANT_DATA_DIR" "$MUSIC_LIBRARY_DIR"
        custom_providers_dir=$(awk -F= '$1 == "MUSIC_ASSISTANT_CUSTOM_PROVIDERS_DIR" {print substr($0,index($0,"=")+1); exit}' "$ma_env")
        subscriber_config_dir=$(awk -F= '$1 == "MUSIC_ASSISTANT_PROVIDER_SUBSCRIBER_CONFIG_DIR" {print substr($0,index($0,"=")+1); exit}' "$ma_env")
        custom_providers_dir=${custom_providers_dir:-$ma_dir/custom_providers}
        subscriber_config_dir=${subscriber_config_dir:-$ma_dir/provider-subscriber}
        case "$custom_providers_dir:$subscriber_config_dir" in
            *[!a-zA-Z0-9_./:-]*) echo 'MA provider paths must use letters, digits, _, ., /, - or :' >&2; exit 2;;
        esac
        mkdir -p "$custom_providers_dir" "$subscriber_config_dir"
        if [ ! -e "$subscriber_config_dir/options.json" ]; then
            old_umask=$(umask)
            umask 077
            cat > "$subscriber_config_dir/options.json" <<'JSON'
{
  "log_level": "info",
  "update_strategy": "latest_release",
  "run_on_start": true,
  "run_forever": true,
  "interval_minutes": 360,
  "retry_attempts": 3,
  "retry_base_seconds": 180,
  "prune_removed": false,
  "github_proxy": "",
  "sources": []
}
JSON
            chmod 0600 "$subscriber_config_dir/options.json"
            umask "$old_umask"
        fi
        # Bundled providers: copied in on every install (the bundled copy wins),
        # skipped with --without-ma-provider(s). A skipped one is left as it is:
        # it may be the user's own, and removing it would not disable it in MA.
        for provider in $MA_PROVIDERS; do
            case " $SKIP_MA_PROVIDERS " in
                *" $provider "*) echo "skipping bundled MA provider: $provider" ;;
                *)
                    stage="$custom_providers_dir/.$provider.new"
                    rm -rf "$stage"
                    cp -R "$ROOT/music-assistant/providers/$provider" "$stage"
                    find "$stage" -name __pycache__ -prune -exec rm -rf {} +
                    rm -rf "$custom_providers_dir/$provider"
                    mv "$stage" "$custom_providers_dir/$provider"
                    echo "installed MA provider: $provider" ;;
            esac
        done
        printf '%s\n' 'hifimate-music-assistant-compose-v1' > "$ma_dir/.hifimate-managed"
    fi
fi
# The documents change between releases; the old set was checked by preflight.
run find "$PREFIX/share/doc/hifimate" -maxdepth 1 -type f -name '*.md' -delete
run cp "$ROOT/README.md" "$ROOT/INSTALL.md" "$ROOT/NOTICE.md" "$ROOT/docs/ARCHITECTURE.md" "$ROOT/docs/SPECTRUM.md" "$PREFIX/share/doc/hifimate/"

if [ "$APPLY" -eq 1 ]; then
    if [ "$SETUP_REALTIME" -eq 1 ]; then printf "%s\n" "managed realtime permission" > "$PREFIX/config/realtime-permissions.request"; fi
    if [ "$SETUP_BT_SPEAKER" -eq 1 ]; then printf "%s\n" "managed bluetooth speaker class" > "$PREFIX/config/bluetooth-speaker.request"; fi
    # Personal DSP filters (this machine's hardware): created once, never overwritten.
    if [ ! -e "$PREFIX/config/dsp-filters.yml" ]; then
        python3 -B "$ROOT/hifimate/user_filters.py" template > "$PREFIX/config/dsp-filters.yml.new"
        mv "$PREFIX/config/dsp-filters.yml.new" "$PREFIX/config/dsp-filters.yml"
    fi
    python3 -B "$ROOT/scripts/install_files.py" write-manifest "$PREFIX"
fi

if [ "$SETUP_REALTIME" -eq 1 ]; then
    if [ "$APPLY" -eq 1 ]; then
        $SUDO python3 -B -I "$ROOT/scripts/realtime_permissions.py" install "$(id -u)" "$PREFIX"
        echo 'Realtime permission configured; current user manager was NOT restarted. Re-login after manager exit or reboot to inherit limits.'
    else
        echo '+ would authenticate with sudo -v and install per-UID realtime permission (no session restart)'
    fi
fi
if [ "$SETUP_BT_SPEAKER" -eq 1 ]; then
    if [ "$APPLY" -eq 1 ]; then
        $SUDO python3 -B -I "$ROOT/scripts/bluetooth_speaker.py" install "$(id -u)" "$PREFIX"
    else
        echo '+ would authenticate with sudo and add a managed loudspeaker Class to /etc/bluetooth/main.conf (restarts bluetoothd)'
    fi
fi
if [ "$ENABLE" -eq 1 ]; then
    run python3 -B "$ROOT/scripts/install_files.py" publish-fragments "$PREFIX" --source "$ROOT/config/host-stack"
    unitdir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
    run mkdir -p "$unitdir"
    for unit_path in "$PREFIX"/systemd/*.service; do
        unit=$(basename "$unit_path")
        link="$unitdir/$unit"
        if [ -e "$link" ] || [ -L "$link" ]; then
            [ -L "$link" ] && [ "$(readlink "$link")" = "$unit_path" ] || {
                echo "refusing to replace existing user unit: $link" >&2
                exit 1
            }
            echo "+ existing managed link $link"
        else
            run ln -s "$unit_path" "$link"
        fi
    done
    run systemctl --user daemon-reload
    run systemctl --user enable --now pipewire.socket pipewire-pulse.socket pipewire.service pipewire-pulse.service wireplumber.service
    # enable --now does not reload fragments in already running audio daemons.
    # Complete the graph restart before starting any HiFi receivers/controller.
    run systemctl --user restart pipewire.service pipewire-pulse.service wireplumber.service
    run systemctl --user enable --now \
        hifimate.service hifimate-camilladsp.service hifimate-spectrum.service hifimate-ctrl.service \
        hifimate-airplay.service hifimate-airplay-net.service hifimate-dlna.service hifimate-bluetooth.service \
        hifimate-mpd.service hifimate-music-mpd.service
    run systemctl --user restart hifimate-camilladsp.service hifimate.service \
        hifimate-mpd.service hifimate-music-mpd.service hifimate-airplay.service hifimate-airplay-net.service \
        hifimate-dlna.service hifimate-spectrum.service hifimate-ctrl.service hifimate-bluetooth.service
fi

if [ "$WITH_MUSIC_ASSISTANT" -eq 1 ] && [ "$APPLY" -eq 1 ]; then
    docker compose --project-directory "$ma_dir" --env-file "$ma_env" -f "$ma_dir/compose.yml" config --quiet
    # migu-api has no published image; `up` builds it from its pinned source
    docker compose --project-directory "$ma_dir" --env-file "$ma_env" -f "$ma_dir/compose.yml" pull --ignore-buildable
    # The stock MA server owns settings.json while it is running. Stop only
    # that service while seeding the MilkDrop plugin and HiFi player settings
    # (configure_ma_players.py), then start it again. HiFi's players get random
    # universal ids once MA discovers them, so a first install starts MA, waits
    # for HiFi AirPlay to be discovered (receivers are running with --enable),
    # and only then stops it to apply the settings.
    ma_settings="$MUSIC_ASSISTANT_DATA_DIR/settings.json"
    ma_compose() { docker compose --project-directory "$ma_dir" --env-file "$ma_env" -f "$ma_dir/compose.yml" "$@"; }
    # The web container mounts config/tls/nginx (HTTPS for the roomfit page);
    # issue or renew it before any start. The CA persists across reinstalls.
    tls_changed=$(python3 -B "$ROOT/hifimate/tls.py" ensure "$PREFIX/config/tls" --name "$(hostname)")
    if ! python3 -B "$ROOT/scripts/configure_ma_players.py" found "$MUSIC_ASSISTANT_DATA_DIR" --name "$DEVICE_NAME" 2>/dev/null; then
        ma_compose up -d --wait --wait-timeout 180
        if [ "$ENABLE" -eq 1 ]; then
            python3 -B "$ROOT/scripts/configure_ma_players.py" wait "$MUSIC_ASSISTANT_DATA_DIR" --name "$DEVICE_NAME" --timeout 120 || true
        fi
    fi
    # MA's owner first (scripts/ma_setup.py, MA's own /setup): given by the
    # caller or asked in a terminal, never skipped; hifimate-ctrl below is made
    # only once MA has that user. An MA that already has users is left as it is.
    ma_compose up -d --wait --wait-timeout 180 server
    if [ "$NON_INTERACTIVE" -eq 1 ]; then
        python3 -B "$ROOT/scripts/ma_setup.py" --non-interactive || exit 1
    else
        python3 -B "$ROOT/scripts/ma_setup.py" || exit 1
    fi
    ma_compose stop server
    # /ctrl reads MA's queue (now playing): a least-privileged MA user and a
    # long-lived token, made by MA's own code in its image while the server
    # is stopped (hifimate_ctrl_token.py). The token goes straight to a 0600 file
    # and is never printed; a still-valid one is kept.
    ma_token="$PREFIX/config/ma-token"
    token_status=0
    ( umask 077
      { cat "$ma_token" 2>/dev/null || true; } | ma_compose run --rm -T --no-deps --entrypoint /app/venv/bin/python \
          -v "$ma_dir/hifimate_ctrl_token.py:/opt/hifimate_ctrl_token.py:ro" server /opt/hifimate_ctrl_token.py > "$ma_token.new" ) || token_status=$?
    if [ "$token_status" -eq 0 ] && [ -s "$ma_token.new" ]; then
        mv "$ma_token.new" "$ma_token"
    else
        rm -f "$ma_token.new"
        if [ "$token_status" -eq 3 ]; then
            echo 'Music Assistant has no user yet: create it by rerunning the install in a terminal (or in the browser), for /ctrl now playing' >&2
        elif [ "$token_status" -ne 0 ]; then
            echo "warning: could not prepare the Music Assistant token for /ctrl (status $token_status); keeping the previous one" >&2
        fi
    fi
    python3 -B "$ROOT/scripts/enable_ma_visualizer.py" "$MUSIC_ASSISTANT_DATA_DIR"
    if [ -f "$ma_settings" ]; then
        # The MPD player supports what the DAC does (nothing when it is unplugged: MA's defaults).
        dac_rates=$(PYTHONPATH="$ROOT" python3 -B -c 'import json,sys
from hifimate.output_devices import device_capabilities
try:
    device=json.load(open(sys.argv[1])).get("alsa_device","auto")
    print(",".join(map(str,sorted(device_capabilities(device)[2]))) if device!="auto" else "")
except Exception:
    print("")' "$PREFIX/config/hifimate.conf" 2>/dev/null || true)
        python3 -B "$ROOT/scripts/configure_ma_players.py" apply "$MUSIC_ASSISTANT_DATA_DIR" --name "$DEVICE_NAME" --rates "$dac_rates"
    fi
    ma_compose up -d --wait --wait-timeout 180
    if [ "$(web_config)" != "$web_before" ] || [ "$tls_changed" = changed ]; then
        ma_compose restart web
    fi
fi

if [ "$APPLY" -eq 1 ]; then
    rm -f "$PREFIX/$JOURNAL"
fi
if [ "$APPLY" -eq 0 ]; then
    echo 'dry-run only: pass --apply to write files; add --enable to start the user service'
else
    echo "installed HiFiMate into $PREFIX"
fi
