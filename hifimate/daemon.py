"""Linux receiver event host and source format controller."""

from __future__ import annotations

import argparse
from pathlib import Path


def run(config: Path | None = None) -> int:
    """Run until TERM/INT; return zero on a normal service stop."""
    import json
    if config is None:
        raise ValueError('explicit Linux format JSON configuration required')
    # config/hifimate.conf, with the playback settings the control page saved
    # (config/settings.json) laid over it.
    from .settings import effective
    settings = effective(config)
    from .format_daemon import serve
    return serve(settings)


def main() -> int:
    parser = argparse.ArgumentParser(description="HiFiMate arbiter service host")
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    return run(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
