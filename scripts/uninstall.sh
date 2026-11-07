#!/bin/sh
set -eu
export PYTHONDONTWRITEBYTECODE=1
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)

PREFIX=${HIFIMATE_PREFIX:-"$HOME/hifimate"}
APPLY=0
usage() { echo "usage: $0 [--prefix /absolute/path] [--apply]"; }
while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix) [ "$#" -ge 2 ] || { usage >&2; exit 2; }; PREFIX=$2; shift 2 ;;
        --apply) APPLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
case "$PREFIX" in /*) ;; *) echo 'prefix must be absolute' >&2; exit 2 ;; esac
case "$PREFIX" in *[[:space:]%]*) echo 'prefix may not contain whitespace or %' >&2; exit 2;; esac
case "$PREFIX" in /|/usr|/usr/local|/etc|/home|/root)
    echo "refusing broad prefix: $PREFIX" >&2; exit 2;;
esac
run() {
    echo "+ $*"
    if [ "$APPLY" -eq 1 ]; then
        "$@"
    fi
}

if [ -e "$PREFIX/.hifimate-output-transaction.json" ] || [ -L "$PREFIX/.hifimate-output-transaction.json" ]; then
    echo "pending output transaction; run $PREFIX/bin/hifimate-output --recover --apply first" >&2
    exit 1
fi
python3 -B "$ROOT/scripts/install_files.py" check-uninstall "$PREFIX"
if [ ! -e "$PREFIX" ]; then
    echo "prefix absent: $PREFIX"
    exit 0
fi
if [ -f "$PREFIX/config/realtime-permissions.request" ]; then
    if [ "$APPLY" -eq 1 ]; then
        # NOPASSWD rules need no prompt; sudo -v alone may still ask for one.
        sudo -n true 2>/dev/null || sudo -v
        sudo python3 -B -I "$PREFIX/bin/hifimate-realtime-permissions" remove "$(id -u)" "$PREFIX"
    else
        echo '+ would authenticate with sudo and remove unchanged owned realtime permission'
    fi
fi
if [ -f "$PREFIX/config/bluetooth-speaker.request" ]; then
    if [ "$APPLY" -eq 1 ]; then
        sudo -n true 2>/dev/null || sudo -v
        sudo python3 -B -I "$PREFIX/bin/hifimate-bluetooth-speaker" remove "$(id -u)" "$PREFIX"
    else
        echo '+ would authenticate with sudo and remove the unchanged managed Bluetooth speaker Class'
    fi
fi
ma_env="$PREFIX/music-assistant/.env"
if [ -f "$PREFIX/music-assistant/.hifimate-managed" ]; then
    [ -f "$ma_env" ] && [ ! -L "$ma_env" ] || { echo 'missing or symlink Music Assistant .env; refusing uninstall' >&2; exit 1; }
    # .env may have been edited since installation; recheck data boundaries.
    python3 -B - "$PREFIX" "$ma_env" <<'PYVALIDATE'
from pathlib import Path
import os
import sys
prefix = Path(sys.argv[1]).resolve()
settings = dict(line.split("=", 1) for line in Path(sys.argv[2]).read_text().splitlines()
                if line and not line.startswith("#") and "=" in line)
for key in ("MUSIC_ASSISTANT_DATA_DIR", "MUSIC_LIBRARY_DIR"):
    raw = os.environ.get(key, settings.get(key, ""))
    if not Path(raw).is_absolute():
        raise SystemExit(f"{key} must be absolute before uninstall")
    path = Path(raw).resolve()
    if path == prefix or prefix in path.parents or path in prefix.parents:
        raise SystemExit(f"{key} overlaps installation prefix; preserve data before uninstall: {path}")
PYVALIDATE
    if [ "$APPLY" -eq 1 ]; then
        command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1 || {
            echo 'docker compose is required to stop Music Assistant before uninstall' >&2; exit 1;
        }
    fi
    run docker compose --project-directory "$PREFIX/music-assistant" --env-file "$ma_env" -f "$PREFIX/music-assistant/compose.yml" down
fi

# Close a detached pairing window through its verified process before deleting
# the installed tools. SIGINT allows the BlueZ finally block to close the window.
if [ -x "$PREFIX/bin/hifimate-ctrl" ]; then
    run "$PREFIX/bin/hifimate-ctrl" close-pair
fi

unitdir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
for unit_path in "$PREFIX"/systemd/*.service; do
    [ -f "$unit_path" ] || continue
    unit=$(basename "$unit_path")
    link="$unitdir/$unit"
    target="$PREFIX/systemd/$unit"
    if [ -e "$link" ] || [ -L "$link" ]; then
        if [ -L "$link" ] && [ "$(readlink "$link")" = "$target" ]; then
            # Stop before removing an external unit link; disable --now may
            # reload after unlinking and then fail to find the unit to stop.
            run systemctl --user stop "$unit"
            run systemctl --user disable "$unit"
            run rm -f "$link"
            run systemctl --user daemon-reload
        else
            echo "leaving unrelated user unit untouched: $link"
        fi
    fi
done

# Services are stopped before restoring aliases; disconnected owned adapters block
# deletion so their recovery journal and executable remain available for retry.
if [ -x "$PREFIX/bin/hifimate-bluetooth" ]; then
    run "$PREFIX/bin/hifimate-bluetooth" restore
fi
run python3 -B "$ROOT/scripts/install_files.py" remove-files "$PREFIX"
if [ "$APPLY" -eq 1 ]; then
    # These directories are created for the optional provider subscriber.
    # Remove only when empty; user-added provider files remain recoverable.
    rmdir "$PREFIX/music-assistant/custom_providers" "$PREFIX/music-assistant/provider-subscriber" \
          "$PREFIX/music-assistant" 2>/dev/null || true
    rmdir "$PREFIX" 2>/dev/null || true
else
    echo '+ would remove empty Music Assistant provider directories'
fi
if [ "$APPLY" -eq 0 ]; then
    echo 'dry-run only: pass --apply to remove files'
fi
