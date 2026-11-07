"""Tests for the Bilibili provider (API client mocked)."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
    UnplayableMediaError,
)
from music_assistant_models.media_items import Track

from music_assistant.providers.bilibili import (
    BilibiliProvider,
    select_audio_stream,
    stream_expiration,
)
from music_assistant.providers.bilibili.bili_client import BiliNotFoundError, BiliRiskControlError

VIEW = {
    "bvid": "BV1BZbSzZEGT",
    "title": "《晴天》",
    "pic": "",
    "duration": 270,
    "owner": {"mid": 7, "name": "up"},
    "pages": [
        {"cid": 111, "page": 1, "part": "p1", "duration": 100},
        {"cid": 222, "page": 2, "part": "p2", "duration": 170},
    ],
}


def _audio(stream_id: int, bandwidth: int) -> dict[str, Any]:
    return {"id": stream_id, "bandwidth": bandwidth, "base_url": f"https://cdn/{stream_id}.m4s"}


PLAY = {
    "dash": {
        "audio": [_audio(30216, 65000), _audio(30280, 184000), _audio(30232, 82000)],
        "flac": {"audio": {"id": 30251, "bandwidth": 900000, "baseUrl": "https://cdn/flac.m4s"}},
    }
}


def test_select_audio_stream_by_quality() -> None:
    """Lossless prefers FLAC, high the best AAC, standard the 132k AAC."""
    url, fmt, label = select_audio_stream(PLAY, "lossless") or ("", None, "")
    assert url == "https://cdn/flac.m4s"
    assert label == "FLAC"
    assert fmt is not None
    assert fmt.content_type == ContentType.UNKNOWN
    url, fmt, _ = select_audio_stream(PLAY, "high") or ("", None, "")
    assert url == "https://cdn/30280.m4s"
    assert fmt is not None
    assert fmt.codec_type == ContentType.AAC
    assert fmt.bit_rate == 184
    assert (select_audio_stream(PLAY, "standard") or ("",))[0] == "https://cdn/30232.m4s"


def test_select_audio_stream_lossless_falls_back() -> None:
    """Without a FLAC track the best AAC stream is used; no dash means no stream."""
    no_flac = {"dash": {"audio": PLAY["dash"]["audio"], "flac": None}}
    assert (select_audio_stream(no_flac, "lossless") or ("",))[0] == "https://cdn/30280.m4s"
    assert select_audio_stream({"durl": []}, "high") is None
    assert select_audio_stream({"dash": {"audio": []}}, "high") is None


def test_stream_expiration_from_deadline() -> None:
    """The expiration follows the signed url's deadline (with a safety margin)."""
    deadline = int(time.time()) + 7200
    assert 7000 < stream_expiration(f"https://cdn/a.m4s?deadline={deadline}&x=1") <= 7140
    assert stream_expiration("https://cdn/a.m4s") == 3600
    assert stream_expiration(f"https://cdn/a.m4s?deadline={int(time.time())}") == 60


async def test_get_stream_details_uses_page_cid(provider: BilibiliProvider, api: AsyncMock) -> None:
    """Page 2 of a video streams with that page's cid and duration."""
    api.view = AsyncMock(return_value=VIEW)
    api.play_url = AsyncMock(return_value=PLAY)
    details = await provider.get_stream_details("BV1BZbSzZEGT_p2", MediaType.TRACK)
    api.play_url.assert_awaited_once_with(cid=222, bvid="BV1BZbSzZEGT")
    assert details.stream_type == StreamType.HTTP
    assert details.path == "https://cdn/flac.m4s"
    assert details.duration == 170
    assert details.can_seek
    assert "-headers" in details.extra_input_args
    assert any("Referer: https://www.bilibili.com/" in arg for arg in details.extra_input_args)


async def test_get_stream_details_errors(provider: BilibiliProvider, api: AsyncMock) -> None:
    """Missing pages and missing audio are reported as such."""
    api.view = AsyncMock(return_value=VIEW)
    with pytest.raises(MediaNotFoundError):
        await provider.get_stream_details("BV1BZbSzZEGT_p9", MediaType.TRACK)
    api.play_url = AsyncMock(return_value={"dash": {"audio": []}})
    with pytest.raises(UnplayableMediaError):
        await provider.get_stream_details("BV1BZbSzZEGT", MediaType.TRACK)


async def test_api_errors_are_translated(provider: BilibiliProvider, api: AsyncMock) -> None:
    """Client errors surface as Music Assistant errors."""
    api.view = AsyncMock(side_effect=BiliNotFoundError("gone", code=-404))
    with pytest.raises(MediaNotFoundError):
        await provider.get_track("BV1BZbSzZEGT")
    api.view = AsyncMock(side_effect=BiliRiskControlError("412"))
    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider.get_track("BV1BZbSzZEGT")


async def test_get_album_tracks_lists_pages(provider: BilibiliProvider, api: AsyncMock) -> None:
    """Every page of a multi-part video is an album track."""
    api.view = AsyncMock(return_value=VIEW)
    tracks = await provider.get_album_tracks("BV1BZbSzZEGT")
    assert [t.item_id for t in tracks] == ["BV1BZbSzZEGT", "BV1BZbSzZEGT_p2"]
    assert [t.position for t in tracks] == [1, 2]


async def test_search_pages_until_limit(provider: BilibiliProvider, api: AsyncMock) -> None:
    """Video search pages through results and restricts to the music zone by default."""

    def _page(page: int) -> dict[str, Any]:
        return {
            "numPages": 3,
            "result": [
                {"type": "video", "bvid": f"BV1{page:02d}{i:07d}", "title": f"t{i}"}
                for i in range(20)
            ],
        }

    api.search_videos = AsyncMock(side_effect=lambda *_a, page, **_k: _page(page))
    api.search_users = AsyncMock(
        return_value={"result": [{"mid": 1, "uname": "<em>up</em>"}, {"mid": 0}]}
    )
    result = await provider.search("晴天", [MediaType.TRACK, MediaType.ARTIST], limit=25)
    assert len(result.tracks) == 25
    assert api.search_videos.await_count == 2
    assert api.search_videos.await_args.kwargs["tid"] == 3
    assert [a.name for a in result.artists] == ["up"]


async def test_search_survives_risk_control(provider: BilibiliProvider, api: AsyncMock) -> None:
    """A blocked search returns no results instead of failing the global search."""
    api.search_videos = AsyncMock(side_effect=BiliRiskControlError("v_voucher"))
    result = await provider.search("晴天", [MediaType.TRACK], limit=5)
    assert result.tracks == []


async def test_playlist_tracks_paging(provider: BilibiliProvider, api: AsyncMock) -> None:
    """One playlist page spans several folder pages and stops when has_more is false."""

    def _folder(_folder_id: int, page: int, page_size: int) -> dict[str, Any]:
        medias = [
            {"type": 2, "bvid": f"BV1{page:02d}{i:07d}", "title": f"t{i}", "upper": {}}
            for i in range(page_size)
        ]
        medias[0]["title"] = "已失效视频"
        return {"medias": medias, "has_more": page < 7}

    api.fav_folder = AsyncMock(side_effect=_folder)
    first = await provider.get_playlist_tracks("fav_9", 0)
    assert api.fav_folder.await_count == 5
    assert len(first) == 5 * 19
    assert first[0].position == 2
    second = await provider.get_playlist_tracks("fav_9", 1)
    assert len(second) == 2 * 19
    assert second[-1].position == 140


async def test_library_playlists_need_login(provider: BilibiliProvider, api: AsyncMock) -> None:
    """Anonymous sessions have no library playlists; signed in ones list their folders."""
    assert [p async for p in provider.get_library_playlists()] == []
    provider._mid = 42
    provider._uname = "me"
    api.fav_folders = AsyncMock(
        return_value=[{"id": 1, "title": "默认收藏夹"}, {"title": "broken"}]
    )
    provider.report_skipped_sync_item = AsyncMock()  # type: ignore[method-assign]
    playlists = [p async for p in provider.get_library_playlists()]
    assert [(p.item_id, p.owner) for p in playlists] == [("fav_1", "me")]
    provider.report_skipped_sync_item.assert_called_once()


async def test_new_releases_resolve_uploader(provider: BilibiliProvider, api: AsyncMock) -> None:
    """New releases are looked up for their uploader and keep the curated song title."""
    api.music_new_releases = AsyncMock(
        return_value=[{"bvid": "BV1BZbSzZEGT", "music_title": "宝船"}, {"bvid": "BV1xxxxxxxxx"}]
    )

    async def _view(bvid: str) -> dict[str, Any]:
        if bvid != "BV1BZbSzZEGT":
            raise BiliNotFoundError("gone")
        return VIEW

    api.view = AsyncMock(side_effect=_view)
    items = await provider.get_recommendation_items("new_releases")
    assert [(t.item_id, t.name) for t in items] == [("BV1BZbSzZEGT", "宝船")]
    assert isinstance(items[0], Track)
    assert items[0].artists[0].item_id == "7"


async def test_handle_async_init_rejects_expired_login(
    provider: BilibiliProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stored session that Bilibili no longer accepts requires a new login."""
    fake = AsyncMock()
    fake.is_logged_in = True
    fake.nav = AsyncMock(return_value={"isLogin": False})
    monkeypatch.setattr("music_assistant.providers.bilibili.BiliClient", lambda *_a, **_k: fake)
    provider.get_setup_value = lambda *_a, **_k: "SESSDATA=old"  # type: ignore[method-assign]
    with pytest.raises(LoginFailed):
        await provider.handle_async_init()
    fake.close.assert_awaited()
