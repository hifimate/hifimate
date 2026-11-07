"""
Live smoke test of the Bilibili provider against the real Bilibili API.

Runs the provider with a mocked Music Assistant core (no cache, no server) but a real
API client, then decodes a few seconds of the selected audio stream with ffmpeg using
the exact input arguments Music Assistant would use.

Usage (from a Music Assistant server checkout with the provider synced in):
    python scripts/live_smoke.py [--cookie "SESSDATA=...; ..."] [--query 晴天]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Track

from music_assistant.providers.bilibili import SUPPORTED_FEATURES, BilibiliProvider


def _make_provider(cookie: str) -> BilibiliProvider:
    """Create the provider with a stub core."""
    mass = Mock()
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    mass.create_task = Mock(side_effect=lambda coro, *_a, **_k: asyncio.ensure_future(coro))
    manifest = Mock()
    manifest.domain = "bilibili"
    config = Mock()
    config.instance_id = "bilibili--smoke"
    config.name = "Bilibili smoke"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {"log_level": "INFO"}.get(
        key, default
    )
    prov = BilibiliProvider(mass, manifest, config, SUPPORTED_FEATURES)
    prov.get_setup_value = lambda key, default=None: cookie if key == "cookie" else default  # type: ignore[method-assign]
    prov._update_setup_data = Mock()  # type: ignore[method-assign]
    return prov


def _fmt(track: Track) -> str:
    artists = ", ".join(a.name for a in track.artists)
    return f"{track.item_id:<18} {track.duration or 0:>5}s  {track.name[:40]}  — {artists}"


async def _step(name: str, coro: Any) -> Any:
    try:
        result = await coro
    except Exception as err:  # noqa: BLE001 - report every failure and continue
        print(f"[FAIL] {name}: {type(err).__name__}: {err}")
        return None
    print(f"[ OK ] {name}")
    return result


async def main() -> int:
    """Run the smoke test, return the number of failed steps."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--cookie", default="")
    parser.add_argument("--query", default="晴天 周杰伦")
    parser.add_argument("--multipart", default="BV1FPjy6TEiE", help="a multi-part video bvid")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    prov = _make_provider(args.cookie)
    failures = 0
    await prov.handle_async_init()
    print(f"logged in: {prov.client.is_logged_in}  mid: {prov._mid}  name: {prov._uname}")
    try:
        result = await _step("search", prov.search(args.query, [MediaType.TRACK, MediaType.ARTIST], 8))
        if result is None or not result.tracks:
            return 1
        for track in result.tracks[:5]:
            print("   ", _fmt(track))
        print("    artists:", [a.name for a in result.artists[:5]])

        track = await _step("get_track", prov.get_track(result.tracks[0].item_id))
        artist_id = track.artists[0].item_id if track and track.artists else None
        if artist_id:
            artist = await _step("get_artist", prov.get_artist(artist_id))
            print("   ", artist.name if artist else None)
            top = await _step("get_artist_toptracks", prov.get_artist_toptracks(artist_id))
            print("    uploads:", len(top or []), _fmt(top[0]) if top else "")

        album = await _step("get_album (multi-part)", prov.get_album(args.multipart))
        pages = await _step("get_album_tracks", prov.get_album_tracks(args.multipart))
        if album and pages:
            print(f"    {album.name[:40]}: {len(pages)} pages, p2 = {_fmt(pages[1]) if len(pages) > 1 else '-'}")

        for row in await prov.get_recommendations():
            items = await _step(f"recommendations/{row.item_id}", prov.get_recommendation_items(row.item_id))
            for item in list(items or [])[:3]:
                if isinstance(item, Track):
                    print("   ", _fmt(item))

        if prov._mid:
            playlists = await _step("library playlists", _collect(prov.get_library_playlists()))
            for playlist in playlists or []:
                tracks = await _step(f"playlist {playlist.name}", prov.get_playlist_tracks(playlist.item_id, 0))
                print(f"    {playlist.item_id}: {len(tracks or [])} tracks")
                break

        details = await _step("get_stream_details", prov.get_stream_details(result.tracks[0].item_id, MediaType.TRACK))
        if details:
            print(f"    {details.audio_format}  expires in {details.expiration}s")
            failures += _decode(details.path, details.extra_input_args)
    finally:
        await prov.unload()
    return failures


async def _collect(gen: Any) -> list[Any]:
    return [item async for item in gen]


def _decode(url: str, extra_input_args: list[str]) -> int:
    """Decode 5 seconds with ffmpeg the way Music Assistant opens HTTP sources."""
    if not shutil.which("ffmpeg"):
        print("[SKIP] ffmpeg not installed")
        return 0
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", *extra_input_args,
        "-i", url, "-t", "5", "-f", "s16le", "-ac", "2", "-ar", "44100", "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=60, check=False)
    pcm = len(proc.stdout)
    ok = proc.returncode == 0 and pcm > 44100 * 4 * 4
    print(f"[{' OK ' if ok else 'FAIL'}] ffmpeg decode: {pcm} bytes PCM ({pcm / 176400:.1f}s) {proc.stderr.decode()[:200]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
