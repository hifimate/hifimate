"""Music Assistant MilkDrop plugin seeding is safe and idempotent."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "enable_ma_visualizer", ROOT / "scripts/enable_ma_visualizer.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_seed_creates_enabled_plugin_and_is_idempotent(tmp_path: Path) -> None:
    assert MODULE.ensure_plugin(tmp_path) is True
    settings = json.loads((tmp_path / "settings.json").read_text())
    provider = settings["providers"]["milkdrop_visualizer"]
    assert provider["enabled"] is True
    assert provider["type"] == "plugin"
    assert provider["instance_id"] == "milkdrop_visualizer"
    assert MODULE.ensure_plugin(tmp_path) is False
    assert json.loads((tmp_path / "settings.json").read_text()) == settings


def test_seed_preserves_existing_disabled_provider(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    original = {
        "providers": {
            "milkdrop_visualizer": {
                "domain": "milkdrop_visualizer",
                "instance_id": "milkdrop_visualizer",
                "enabled": False,
                "values": {"custom": "keep"},
            }
        }
    }
    settings_path.write_text(json.dumps(original) + "\n")
    assert MODULE.ensure_plugin(tmp_path) is False
    assert json.loads(settings_path.read_text()) == original


def test_seed_rejects_invalid_settings_without_replacing_file(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("[]\n")
    with pytest.raises(ValueError, match="JSON object"):
        MODULE.ensure_plugin(tmp_path)
    assert settings_path.read_text() == "[]\n"



def test_ma_page_gets_audio_tap_before_its_scripts_and_embedded_overlay():
    nginx = (ROOT / 'music-assistant/nginx.conf').read_text()
    root = nginx[nginx.index('location ~ ^/(index\\.html)?$ {'):nginx.index('location / {')]
    assert 'proxy_set_header Accept-Encoding "";' in root  # sub_filter needs plain HTML
    # A browser's cached pre-injection page must never be revalidated as 304.
    for line in ('proxy_set_header If-None-Match "";', 'proxy_set_header If-Modified-Since "";',
                 'proxy_hide_header ETag;', 'proxy_hide_header Last-Modified;', 'add_header Cache-Control "no-store" always;'):
        assert line in root
    assert 'sub_filter' not in nginx[nginx.index('location / {'):]  # assets keep compression/caching
    inject = root[root.index("sub_filter '</head>'"):].split('\n', 1)[0]
    tap = inject.index('<script src="/_hifimate/spectrum-tap.js"></script>')
    module = inject.index('<script type="module" src="/_hifimate/spectrum-overlay.js?embed=ma"></script>')
    assert tap < module and inject.rstrip().endswith("</head>';")
    assert 'sub_filter_once on;' in root


def test_embedded_spectrum_stays_above_ma_layers():
    css = (ROOT / 'gui/spectrum-overlay.css').read_text()
    assert '#hifimate-spectrum-overlay.hifimate-spectrum-embedded { z-index: 2147483647; }' in css
