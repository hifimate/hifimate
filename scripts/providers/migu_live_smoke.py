"""
Live smoke test of the Migu provider against a running migu-api-enhanced service.

Runs the provider with a mocked Music Assistant core (no cache, no server) but a real
HTTP session, walks search -> track -> album -> artist -> charts -> playlist, and
decodes a few seconds of the selected stream with ffmpeg.

Usage (from a Music Assistant server checkout with the provider synced in):
    python scripts/live_smoke.py [--api http://127.0.0.1:3004/migu] [--token PACMTOKEN]
                                 [--query 晴天] [--quality hires]
The token can also be given as the MIGU_PACMTOKEN environment variable.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock, Mock

import aiohttp
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Track

from music_assistant.providers.migu import SUPPORTED_FEATURES, MiguProvider


def _make_provider(
    session: aiohttp.ClientSession, api: str, token: str, quality: str
) -> MiguProvider:
    """Create the provider with a stub core."""
    mass = Mock()
    mass.http_session = session
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    mass.create_task = Mock(side_effect=lambda coro, *_a, **_k: asyncio.ensure_future(coro))
    manifest = Mock()
    manifest.domain = "migu"
    config = Mock()
    config.instance_id = "migu--smoke"
    config.name = "Migu smoke"
    config.enabled = True
    options = {"log_level": "INFO", "audio_quality": quality}
    config.get_value.side_effect = lambda key, default=None: options.get(key, default)
    prov = MiguProvider(mass, manifest, config, SUPPORTED_FEATURES)
    setup = {"api_base_url": api, "pacmtoken": token}
    prov.get_setup_value = lambda key, default=None: setup.get(key, default)  # type: ignore[method-assign]
    prov._update_setup_data = Mock()  # type: ignore[method-assign]
    return prov


def _fmt(track: Track) -> str:
    artists = ", ".join(a.name for a in track.artists)
    album = track.album.name if track.album else "-"
    return (
        f"{track.item_id:<20} {track.duration or 0:>4}s  {track.name[:30]}  — {artists} / {album}"
    )


async def _step(name: str, coro: Any) -> Any:
    try:
        result = await coro
    except Exception as err:  # noqa: BLE001 - report every failure and continue
        print(f"[FAIL] {name}: {type(err).__name__}: {err}")
        return None
    print(f"[ OK ] {name}")
    return result


def _decode(url: str, extra: list[str]) -> str:
    """Decode 5 seconds with ffmpeg; return a short result line."""
    if not shutil.which("ffmpeg"):
        return "ffmpeg not installed"
    cmd = ["ffmpeg", "-v", "error", *extra, "-t", "5", "-i", url, "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    return "decoded 5s" if proc.returncode == 0 else f"ffmpeg failed: {proc.stderr[-300:]}"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:3004/migu")
    parser.add_argument("--token", default=os.environ.get("MIGU_PACMTOKEN", ""))
    parser.add_argument("--query", default="晴天 周杰伦")
    parser.add_argument("--quality", default="hires")
    args = parser.parse_args()

    async with aiohttp.ClientSession() as session:
        prov = _make_provider(session, args.api, args.token, args.quality)
        if await _step("init", prov.handle_async_init()) is None and prov._client is None:
            return 1
        print(f"       logged in: {prov.client.is_logged_in} ({prov._user_name})")

        result = await _step(
            "search",
            prov.search(
                args.query,
                [MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST, MediaType.PLAYLIST],
                limit=5,
            ),
        )
        if result is None or not result.tracks:
            return 1
        for track in result.tracks:
            print("       ", _fmt(track))
        print("        albums:", [a.name for a in result.albums])
        print("        artists:", [a.name for a in result.artists])
        print("        playlists:", [p.name for p in result.playlists])

        track = await _step("get_track", prov.get_track(result.tracks[0].item_id))
        if track:
            print("       ", _fmt(track), "| lrc:", bool(track.metadata.lrc_lyrics))
        if track and track.album:
            album = await _step("get_album", prov.get_album(track.album.item_id))
            tracks = await _step("get_album_tracks", prov.get_album_tracks(track.album.item_id))
            if album and tracks:
                print(
                    f"        {album.name} ({album.year}, {album.album_type}): {len(tracks)} tracks"
                )
        if result.albums:
            digital = next((a for a in result.albums if a.item_id.startswith("d")), None)
            if digital:
                tracks = await _step("digital album tracks", prov.get_album_tracks(digital.item_id))
                print(f"        {digital.name}: {len(tracks or [])} tracks")
        if track and track.artists:
            artist_id = track.artists[0].item_id
            artist = await _step("get_artist", prov.get_artist(artist_id))
            top = await _step("get_artist_toptracks", prov.get_artist_toptracks(artist_id))
            albums = await _step("get_artist_albums", prov.get_artist_albums(artist_id))
            if artist:
                print(
                    f"        {artist.name}: {len(top or [])} top tracks, {len(albums or [])} albums"
                )

        folders = await _step("get_recommendations", prov.get_recommendations())
        for folder in folders or []:
            items = await _step(
                f"recommendation {folder.item_id}", prov.get_recommendation_items(folder.item_id)
            )
            print(f"        {folder.name}: {[i.name for i in (items or [])][:8]}")
            if folder.item_id == "charts" and items:
                chart_tracks = await _step(
                    "chart tracks", prov.get_playlist_tracks(items[0].item_id)
                )
                print(f"        {items[0].name}: {len(chart_tracks or [])} tracks")

        if result.playlists:
            playlist = result.playlists[0]
            await _step("get_playlist", prov.get_playlist(playlist.item_id))
            page0 = await _step(
                "get_playlist_tracks", prov.get_playlist_tracks(playlist.item_id, 0)
            )
            print(f"        {playlist.name}: page 0 has {len(page0 or [])} tracks")

        if prov.client.is_logged_in:
            library = [p async for p in prov.get_library_playlists()]
            print(f"[ OK ] library playlists: {[(p.item_id, p.name) for p in library]}")
            for playlist in library[:3]:
                tracks = await _step(
                    f"playlist {playlist.name}", prov.get_playlist_tracks(playlist.item_id, 0)
                )
                print(f"        {len(tracks or [])} tracks")

        for candidate in result.tracks[:3]:
            stream = await _step(
                f"stream {candidate.name}",
                prov.get_stream_details(candidate.item_id, MediaType.TRACK),
            )
            if stream:
                fmt = stream.audio_format
                print(
                    f"        {fmt.content_type} {fmt.bit_depth}bit {fmt.bit_rate or ''} -> {str(stream.path)[:90]}"
                )
                print("       ", _decode(str(stream.path), stream.extra_input_args or []))
        print("token rotated:", prov._update_setup_data.call_count, "time(s)")  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
