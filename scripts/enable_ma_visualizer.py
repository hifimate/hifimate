#!/usr/bin/env python3
"""Idempotently add Music Assistant's stock MilkDrop visualizer plugin.

The Music Assistant container owns ``settings.json`` while it is running, so
the installer invokes this helper only after taking the deployment down.  The
helper changes one missing provider entry and leaves all existing configuration
and an explicitly disabled provider untouched.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile


DOMAIN = "milkdrop_visualizer"
SETTINGS = "settings.json"


def _provider() -> dict[str, object]:
    """Return the raw provider shape used by Music Assistant 2.10.9."""
    return {
        "values": {},
        "type": "plugin",
        "domain": DOMAIN,
        "instance_id": DOMAIN,
        "enabled": True,
        "name": "MilkDrop Visualizer",
        "default_name": "MilkDrop Visualizer",
        "last_error": None,
        "setup_data": {},
    }


def ensure_plugin(data_dir: Path) -> bool:
    """Ensure the stock plugin config exists; return whether settings changed.

    A missing settings file is valid on a first installation.  Music Assistant
    fills its other defaults on startup, while this entry makes the plugin
    load on that first startup as well.  Existing JSON is edited in memory and
    atomically replaced, so a parse error or write failure cannot truncate it.
    """
    if not data_dir.is_absolute():
        raise ValueError("Music Assistant data directory must be absolute")
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not data_dir.is_dir():
        raise ValueError(f"not a directory: {data_dir}")
    path = data_dir / SETTINGS
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise ValueError(f"refusing nonregular settings file: {path}")
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("Music Assistant settings must be a JSON object")
    else:
        data = {}
    providers = data.get("providers")
    if providers is None:
        providers = {}
        data["providers"] = providers
    if not isinstance(providers, dict):
        raise ValueError("Music Assistant settings providers must be an object")
    existing = providers.get(DOMAIN)
    if existing is not None:
        if not isinstance(existing, dict):
            raise ValueError(f"invalid {DOMAIN} provider configuration")
        # Never silently re-enable or rewrite a user-created provider.
        return False
    providers[DOMAIN] = _provider()

    old_mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temp_name = tempfile.mkstemp(prefix=".settings.", dir=data_dir)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, old_mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dir", type=Path)
    args = parser.parse_args()
    changed = ensure_plugin(args.data_dir)
    print("enabled" if changed else "already-configured")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc))
