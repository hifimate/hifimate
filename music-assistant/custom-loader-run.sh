#!/usr/bin/env bash
set -euo pipefail

# The custom-loader image exposes the MA provider package and copies reviewed
# providers into it before starting Music Assistant.  The package tree in the
# published image is writable by the configured service UID, so this wrapper
# keeps the stock unprivileged runtime model.
custom_dir="${CUSTOM_PROVIDERS_DIR:-/share/music_assistant/custom_providers}"
providers_dir="$(/app/venv/bin/python - <<'PY'
import importlib
from pathlib import Path

providers = importlib.import_module("music_assistant.providers")
paths = list(getattr(providers, "__path__", ()))
if not paths or not Path(paths[0]).is_dir():
    raise SystemExit("unable to locate Music Assistant providers directory")
print(paths[0])
PY
)"

if [[ -d "$custom_dir" ]]; then
  while IFS= read -r -d '' plugin; do
    name="$(basename "$plugin")"
    [[ "$name" != .* && "$name" != */* ]] || {
      echo "Skipping unsafe provider path: $plugin" >&2
      continue
    }
    if [[ -f "$plugin/__init__.py" && -f "$plugin/manifest.json" ]]; then
      echo "Injecting custom provider: $name"
      # Do not preserve source mtimes/ownership: the image's provider tree may
      # be overlayfs-backed and utimensat can fail even when the service UID
      # can write the files. Content and directory layout are sufficient.
      cp -rT --no-preserve=mode,ownership,timestamps -- "$plugin" "$providers_dir/$name"
    else
      echo "Skipping $name: missing __init__.py or manifest.json" >&2
    fi
  done < <(find "$custom_dir" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
fi

uid="${MUSIC_ASSISTANT_UID:?MUSIC_ASSISTANT_UID is not set}"
gid="${MUSIC_ASSISTANT_GID:?MUSIC_ASSISTANT_GID is not set}"
args=(/usr/local/bin/entrypoint.sh --data-dir /data --cache-dir /data/.cache)
if [[ "$(id -u)" == 0 && "$uid" != 0 ]]; then
  exec setpriv --reuid="$uid" --regid="$gid" --clear-groups "${args[@]}"
fi
exec "${args[@]}"
