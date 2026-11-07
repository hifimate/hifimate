"""Tests for the Migu provider (API client mocked)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import (
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
    SetupFailedError,
    UnplayableMediaError,
)

from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers import migu as migu_module
from music_assistant.providers.migu import MiguProvider, tone_ladder
from music_assistant.providers.migu.migu_client import (
    MiguConnectionError,
    MiguLoginRequiredError,
    MiguNotFoundError,
)

SONG: dict[str, Any] = {
    "resourceType": "2",
    "contentId": "600",
    "songName": "晴天",
    "duration": 270,
    "copyrightId": "c600",
    "albumId": "8592",
    "album": "叶惠美",
    "singerList": [{"id": "112", "name": "周杰伦"}],
    "audioFormats": [{"formatType": "PQ"}, {"formatType": "HQ"}, {"formatType": "SQ"}],
}


def _song(content_id: str, **extra: Any) -> dict[str, Any]:
    return {**SONG, "contentId": content_id, **extra}


def test_tone_ladder() -> None:
    """The ladder starts at the option's cap, keeps offered tones and ends with PQ."""
    offered = ["ZQ24", "SQ", "HQ", "PQ"]
    assert tone_ladder(offered, "hires") == ["ZQ24", "SQ", "HQ", "PQ"]
    assert tone_ladder(offered, "lossless") == ["SQ", "HQ", "PQ"]
    assert tone_ladder(["SQ", "HQ", "PQ"], "hires") == ["SQ", "HQ", "PQ"]
    assert tone_ladder(["HQ"], "standard") == ["PQ"]
    assert tone_ladder(["HQ"], "bogus") == ["HQ", "PQ"]


async def test_stream_steps_down_until_a_tone_plays(provider: MiguProvider, api: AsyncMock) -> None:
    """Unavailable tones are skipped; the format follows what Migu actually returned."""
    api.song_infos.return_value = [SONG]
    api.play_url.side_effect = [
        {"url": "", "cannotCode": "440022", "dialogInfo": {"text": "会员专属音质"}},
        {"url": "https://freetyst.nf.migu.cn/x/MP3_320_16_Stero/a.mp3", "audioFormatType": "HQ"},
    ]
    stream = await provider.get_stream_details("600", MediaType.TRACK)
    assert [c.args[2] for c in api.play_url.call_args_list] == ["SQ", "HQ"]
    assert api.play_url.call_args_list[0].args[:2] == ("600", "c600")
    assert stream.stream_type == StreamType.HTTP
    assert isinstance(stream.path, str)
    assert stream.path.endswith("a.mp3")
    assert stream.audio_format.content_type == ContentType.MP3
    assert stream.audio_format.bit_rate == 320
    assert stream.duration == 270
    assert stream.can_seek


async def test_stream_uses_url_tone_when_type_missing(
    provider: MiguProvider, api: AsyncMock, options: dict[str, Any]
) -> None:
    """Without audioFormatType the tone is read from the url path."""
    options["audio_quality"] = "lossless"
    api.song_infos.return_value = [SONG]
    api.play_url.return_value = {"url": "https://cdn/歌曲下载/flac/a.flac"}
    stream = await provider.get_stream_details("600", MediaType.TRACK)
    assert stream.audio_format.content_type == ContentType.FLAC
    assert api.play_url.call_args.args[2] == "SQ"


async def test_stream_unplayable_reports_reason(provider: MiguProvider, api: AsyncMock) -> None:
    """When no tone plays, Migu's reason is in the error."""
    api.song_infos.return_value = [SONG]
    api.play_url.return_value = {
        "url": "",
        "cannotCode": "440013",
        "dialogInfo": {"text": "该歌曲需开通白金会员后播放"},
    }
    with pytest.raises(UnplayableMediaError, match=r"白金会员.*440013"):
        await provider.get_stream_details("600", MediaType.TRACK)
    assert api.play_url.call_count == 3  # SQ, HQ, PQ


async def test_unknown_song_is_not_found(provider: MiguProvider, api: AsyncMock) -> None:
    """A content id without song info raises MediaNotFoundError."""
    api.song_infos.return_value = []
    with pytest.raises(MediaNotFoundError):
        await provider.get_track("404")


async def test_get_track_adds_lyrics(provider: MiguProvider, api: AsyncMock) -> None:
    """Synced lyrics fill both lrc_lyrics and plain lyrics."""
    api.song_infos.return_value = [SONG]
    api.lyric.return_value = "[00:01.00]作词\uff1a周杰伦\r\n[00:29.12]故事的小黄花\r\n"
    track = await provider.get_track("600")
    assert track.metadata.lrc_lyrics == "[00:01.00]作词\uff1a周杰伦\n[00:29.12]故事的小黄花"
    assert track.metadata.lyrics == "故事的小黄花"


async def test_get_track_survives_lyric_failure(provider: MiguProvider, api: AsyncMock) -> None:
    """A failing lyric lookup does not fail the track."""
    api.song_infos.return_value = [SONG]
    api.lyric.side_effect = MiguConnectionError("down")
    track = await provider.get_track("600")
    assert track.metadata.lyrics is None


async def test_errors_are_translated(provider: MiguProvider, api: AsyncMock) -> None:
    """Client errors become Music Assistant errors."""
    api.album_info.side_effect = MiguNotFoundError("gone")
    with pytest.raises(MediaNotFoundError):
        await provider.get_album("1")
    api.singer_info.side_effect = MiguConnectionError("down")
    with pytest.raises(ResourceTemporarilyUnavailable):
        await provider.get_artist("1")
    api.playlist_info.side_effect = MiguLoginRequiredError("请先登录")
    with pytest.raises(LoginFailed):
        await provider.get_playlist("1")


async def test_search_all_kinds(provider: MiguProvider, api: AsyncMock) -> None:
    """Search fans out per media type and pages until the limit is reached."""

    async def _search(kind: str, _text: str, page: int) -> dict[str, Any]:
        if kind == "song":
            items = [{"song": _song(f"{page}-{i}")} for i in range(20)]
            return {"items": items, "hasNext": True}
        if kind == "album":
            return {"items": [{"album": {"albumId": "1", "title": "A", "singer": "s"}}]}
        if kind == "singer":
            return {"items": [{"singer": {"singerId": "112", "singer": "周杰伦"}}]}
        return {"items": [{"musicList": {"musicListId": "9", "title": "P"}}]}

    api.search.side_effect = _search
    result = await provider.search(
        "q", [MediaType.TRACK, MediaType.ALBUM, MediaType.ARTIST, MediaType.PLAYLIST], limit=25
    )
    assert len(result.tracks) == 25
    assert [a.item_id for a in result.albums] == ["1"]
    assert [a.item_id for a in result.artists] == ["112"]
    assert [p.item_id for p in result.playlists] == ["9"]
    song_pages = [c.args[2] for c in api.search.call_args_list if c.args[0] == "song"]
    assert song_pages == [1, 2]


async def test_search_failure_is_empty(provider: MiguProvider, api: AsyncMock) -> None:
    """A failing search returns no results instead of raising."""
    api.search.side_effect = MiguConnectionError("down")
    result = await provider.search("q", [MediaType.TRACK], limit=5)
    assert result.tracks == []


async def test_album_tracks_regular_and_digital(provider: MiguProvider, api: AsyncMock) -> None:
    """Regular albums page until hasNext is false; digital albums come in one list."""
    api.album_songs.side_effect = [
        {"songList": [_song("1"), _song("2")], "hasNext": True},
        {"songList": [_song("3")], "hasNext": False},
    ]
    tracks = await provider.get_album_tracks("8592")
    assert [(t.item_id, t.position, t.disc_number) for t in tracks] == [
        ("1", 1, 1),
        ("2", 2, 1),
        ("3", 3, 1),
    ]
    api.digital_album_songs.return_value = {"songList": [_song("7")]}
    tracks = await provider.get_album_tracks("d6009")
    api.digital_album_songs.assert_called_once_with("6009")
    assert [t.item_id for t in tracks] == ["7"]


async def test_artist_by_name_and_albums(provider: MiguProvider, api: AsyncMock) -> None:
    """Name-only artists are resolved by search; albums page via header.hasNext."""
    api.search.return_value = {
        "items": [
            {"singer": {"singerId": "1", "singer": "周杰伦 Jay"}},
            {"singer": {"singerId": "112", "singer": "周杰伦"}},
        ]
    }
    artist = await provider.get_artist("name:周杰伦")
    assert artist.item_id == "112"
    api.singer_albums.side_effect = [
        {"header": {"hasNext": True}, "contents": [{"resType": "1", "resId": "6", "txt": "A"}]},
        {"header": {"hasNext": False}, "contents": [{"resType": "0", "resId": "7", "txt": "B"}]},
    ]
    albums = await provider.get_artist_albums("name:周杰伦")
    assert [a.item_id for a in albums] == ["d6", "7"]
    assert api.singer_albums.call_args_list[0].args == ("112", 1)


async def test_charts_as_recommendations_and_playlists(
    provider: MiguProvider, api: AsyncMock
) -> None:
    """Charts are listed as playlists; their songs come from the chart endpoint."""
    api.rank_index.return_value = {"contents": [{"rankId": "27186466", "rankName": "热歌榜"}]}
    folders = await provider.get_recommendations()
    assert [f.item_id for f in folders] == ["charts"]
    items = await provider.get_recommendation_items("charts")
    assert [i.item_id for i in items] == ["rank_27186466"]
    playlist = await provider.get_playlist("rank_27186466")
    assert playlist.name == "热歌榜"
    api.rank_songs.return_value = {"contents": [{"resId": "600", "txt": "x", "songData": "{}"}]}
    tracks = await provider.get_playlist_tracks("rank_27186466")
    assert [t.item_id for t in tracks] == ["600"]
    assert await provider.get_playlist_tracks("rank_27186466", 1) == []
    with pytest.raises(MediaNotFoundError):
        await provider.get_playlist("rank_1")


async def test_public_playlist_pages(provider: MiguProvider, api: AsyncMock) -> None:
    """MA's 0-based pages map to Migu's 1-based ones; positions continue across pages."""
    api.playlist_songs.return_value = {"songList": [_song("1"), _song("2")]}
    tracks = await provider.get_playlist_tracks("228", 2)
    api.playlist_songs.assert_called_once_with("228", 3)
    assert [t.position for t in tracks] == [41, 42]


async def test_library_playlists_when_signed_in(provider: MiguProvider, api: AsyncMock) -> None:
    """Own playlists get the my_ prefix; collected ones keep their public id."""
    api.is_logged_in = True
    provider._user_name = "me"
    api.user_playlists.return_value = {"list": [{"playlistId": "7", "playlistName": "我喜欢"}]}
    api.user_collected_playlists.return_value = {
        "items": [{"musicListId": "225", "title": "收藏", "ownerName": "x"}]
    }
    playlists = [p async for p in provider.get_library_playlists()]
    assert [(p.item_id, p.owner) for p in playlists] == [("my_7", "me"), ("225", "x")]
    # a short page ends the paging
    api.user_playlists.assert_called_once_with(page=1, size=50)


async def test_library_playlists_anonymous_is_empty(provider: MiguProvider, api: AsyncMock) -> None:
    """Without login there are no library playlists."""
    assert [p async for p in provider.get_library_playlists()] == []
    api.user_playlists.assert_not_called()


async def test_own_playlist_falls_back_to_public_api(
    provider: MiguProvider, api: AsyncMock
) -> None:
    """An own playlist the user endpoint has no songs for is read via the public API."""
    api.is_logged_in = True
    api.user_playlist_songs.return_value = {"songList": []}
    api.playlist_songs.return_value = {"songList": [_song("1")]}
    tracks = await provider.get_playlist_tracks("my_7", 0)
    assert [t.item_id for t in tracks] == ["1"]
    api.playlist_songs.return_value = {"songList": [_song("2")]}
    tracks = await provider.get_playlist_tracks("my_7", 1)
    assert [(t.item_id, t.position) for t in tracks] == [("2", 21)]
    api.user_playlist_songs.assert_called_once()

    api.user_playlist_songs.return_value = {"data": [_song("5")]}
    tracks = await provider.get_playlist_tracks("my_8", 1)
    assert [(t.item_id, t.position) for t in tracks] == [("5", 51)]


async def test_daily_recommendations_when_signed_in(provider: MiguProvider, api: AsyncMock) -> None:
    """Signed in users get a daily songs row first."""
    api.is_logged_in = True
    api.user_daily_recommend.return_value = {"songList": [_song("1")]}
    folders = await provider.get_recommendations()
    assert [f.item_id for f in folders] == ["daily", "charts"]
    items = await provider.get_recommendation_items("daily")
    assert [i.item_id for i in items] == ["1"]


def _init_provider(provider: MiguProvider, setup: dict[str, str]) -> None:
    provider.get_setup_value = lambda key, default=None: setup.get(key, default)  # type: ignore[method-assign]
    provider._update_setup_data = Mock()  # type: ignore[method-assign]
    provider._client = None


async def test_init_signed_in(provider: MiguProvider) -> None:
    """A valid token signs in; rotated tokens are persisted."""
    _init_provider(provider, {"api_base_url": "http://x/migu", "pacmtoken": "tok"})
    fake = AsyncMock()
    fake.is_logged_in = True
    fake.user_info.return_value = {"nickName": "小明"}
    with patch.object(migu_module, "MiguClient", return_value=fake) as factory:
        await provider.handle_async_init()
    assert factory.call_args.args[1:] == ("http://x/migu", "tok")
    assert provider._user_name == "小明"
    factory.call_args.kwargs["on_token_refresh"]("new")
    provider._update_setup_data.assert_called_once_with("pacmtoken", "new")  # type: ignore[attr-defined]


async def test_signed_in_login_is_kept_alive_until_it_expires(provider: MiguProvider) -> None:
    """A signed-in session is renewed every KEEPALIVE_SECONDS; it stops once Migu signs it out."""
    _init_provider(provider, {"pacmtoken": "tok"})
    fake = AsyncMock()
    fake.is_logged_in = True
    calls: list[int] = []

    async def user_info() -> dict[str, Any]:
        calls.append(len(calls))
        if len(calls) == 4:  # the init check + two renewals succeed, then Migu signs out
            raise MiguLoginRequiredError("uid不能为空")
        if len(calls) == 2:
            raise MiguConnectionError("refused")  # a brief outage does not stop the keep-alive
        return {"nickName": "小明"}

    fake.user_info.side_effect = user_info
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    with patch.object(migu_module, "MiguClient", return_value=fake):
        await provider.handle_async_init()
    task = provider._keepalive
    assert task is not None
    with patch.object(migu_module.asyncio, "sleep", sleep):
        await task
    assert len(calls) == 4 and slept == [migu_module.KEEPALIVE_SECONDS] * 3
    assert migu_module.KEEPALIVE_SECONDS < 1.4 * 3600  # below the longest idle gap Migu survived


async def test_unload_stops_the_keep_alive(provider: MiguProvider) -> None:
    """Unloading cancels the keep-alive task."""
    _init_provider(provider, {"pacmtoken": "tok"})
    fake = AsyncMock()
    fake.is_logged_in = True
    fake.user_info.return_value = {"nickName": "小明"}
    with patch.object(migu_module, "MiguClient", return_value=fake):
        await provider.handle_async_init()
    task = provider._keepalive
    assert task is not None and not task.done()
    with patch.object(MusicProvider, "unload", AsyncMock()):
        await provider.unload()
    assert task.cancelled() and provider._keepalive is None


async def test_anonymous_has_no_keep_alive(provider: MiguProvider) -> None:
    """Without a login there is nothing to keep alive."""
    _init_provider(provider, {})
    fake = AsyncMock()
    fake.is_logged_in = False
    with patch.object(migu_module, "MiguClient", return_value=fake):
        await provider.handle_async_init()
    assert provider._keepalive is None


async def test_init_expired_token_asks_for_login(provider: MiguProvider) -> None:
    """An expired token raises LoginFailed (MA then asks to sign in again)."""
    _init_provider(provider, {"pacmtoken": "tok"})
    fake = AsyncMock()
    fake.is_logged_in = True
    fake.user_info.side_effect = MiguLoginRequiredError("请先登录")
    with (
        patch.object(migu_module, "MiguClient", return_value=fake),
        pytest.raises(LoginFailed),
    ):
        await provider.handle_async_init()


async def test_init_service_down_is_retryable(provider: MiguProvider) -> None:
    """An unreachable service raises SetupFailedError, which MA retries."""
    _init_provider(provider, {})
    fake = AsyncMock()
    fake.version.side_effect = MiguConnectionError("refused")
    with (
        patch.object(migu_module, "MiguClient", return_value=fake) as factory,
        pytest.raises(SetupFailedError),
    ):
        await provider.handle_async_init()
    assert factory.call_args.args[1] == "http://127.0.0.1:3004/migu"
