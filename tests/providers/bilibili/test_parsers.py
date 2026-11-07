"""Tests for the Bilibili data parsers."""

# real-world titles use full-width punctuation on purpose
# ruff: noqa: RUF001

from __future__ import annotations

import pytest
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.bilibili.parsers import (
    ParseContext,
    clean_title,
    make_track_id,
    normalize_image_url,
    parse_duration,
    parse_fav_folder,
    parse_fav_media,
    parse_fav_playlist_id,
    parse_hot_rank_item,
    parse_search_video,
    parse_view_album,
    parse_view_track,
    split_track_id,
    strip_html,
)

CTX = ParseContext("bilibili", "bilibili--test")

VIEW_SINGLE = {
    "bvid": "BV1BZbSzZEGT",
    "title": "【Hi-Res无损音质】｜《晴天》- 周杰伦 -‘故事的小黄花’",
    "pic": "http://i0.hdslb.com/bfs/archive/abc.jpg",
    "desc": "description",
    "pubdate": 1753667700,
    "duration": 270,
    "owner": {"mid": 9666167, "name": "VV音乐局"},
    "stat": {"view": 3389646},
    "pages": [{"cid": 31268997329, "page": 1, "part": "whatever", "duration": 270}],
}

VIEW_MULTI = {
    "bvid": "BV1abcdefghi",
    "title": "周杰伦 合集",
    "pic": "//i0.hdslb.com/bfs/archive/multi.jpg",
    "owner": {"mid": 1, "name": "uploader"},
    "pages": [
        {"cid": 11, "page": 1, "part": "《晴天》", "duration": 100},
        {"cid": 22, "page": 2, "part": "七里香", "duration": 200},
    ],
}


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("【Hi-Res无损音质】｜《晴天》- 周杰伦", "晴天"),
        ("【4K修复】周杰伦《七里香》MV", "七里香"),
        # two 《》 pairs: ambiguous, only strip the decorations
        ("【MV】《A》与《B》", "《A》与《B》"),
        ("【官方MV】Love Story [Taylor's Version]", "Love Story [Taylor's Version]"),
        ("晴天 【高音质】", "晴天"),
        ("[4K] 「MV」 Song Name", "Song Name"),
        ("plain title", "plain title"),
        # never return an empty title
        ("【只有装饰】", "【只有装饰】"),
    ],
)
def test_clean_title(title: str, expected: str) -> None:
    """Decorations are stripped, the song name in 《》 is preferred."""
    assert clean_title(title) == expected


def test_strip_html_removes_search_highlight() -> None:
    """Search keyword highlight tags and entities are removed."""
    assert strip_html('《<em class="keyword">晴天</em>》 &amp; more') == "《晴天》 & more"
    assert strip_html(None) == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [(270, 270), ("4:30", 270), ("1:02:03", 3723), ("90", 90), ("bad", 0), (None, 0), (True, 0)],
)
def test_parse_duration(value: object, expected: int) -> None:
    """Durations are accepted as seconds or clock text."""
    assert parse_duration(value) == expected


def test_track_id_roundtrip() -> None:
    """Page 1 uses the bare bvid, later pages a _pN suffix."""
    assert make_track_id("BV1BZbSzZEGT") == "BV1BZbSzZEGT"
    assert make_track_id("BV1BZbSzZEGT", 3) == "BV1BZbSzZEGT_p3"
    assert split_track_id("BV1BZbSzZEGT") == ("BV1BZbSzZEGT", 1)
    assert split_track_id("BV1BZbSzZEGT_p3") == ("BV1BZbSzZEGT", 3)
    with pytest.raises(InvalidDataError):
        split_track_id("av123")


def test_normalize_image_url() -> None:
    """Image urls are upgraded to https and optionally cropped square by the CDN."""
    assert normalize_image_url("//i0.hdslb.com/bfs/a.jpg") == "https://i0.hdslb.com/bfs/a.jpg"
    assert (
        normalize_image_url("http://i0.hdslb.com/bfs/a.jpg", square=True)
        == "https://i0.hdslb.com/bfs/a.jpg@500w_500h_1c.jpg"
    )
    # already parameterized or foreign urls are not touched
    assert normalize_image_url("https://i0.hdslb.com/bfs/a.jpg@100w.jpg", square=True).endswith(
        "@100w.jpg"
    )
    assert normalize_image_url("https://example.com/a.jpg", square=True) == (
        "https://example.com/a.jpg"
    )
    assert normalize_image_url(None) == ""


def test_images_are_proxied() -> None:
    """The image CDN rejects foreign referers, so images must go through the MA proxy."""
    images = CTX.images("http://i0.hdslb.com/bfs/a.jpg")
    assert len(images) == 1
    assert images[0].remotely_accessible is False


def test_parse_search_video() -> None:
    """A search hit becomes a track of page 1 with the uploader as artist."""
    track = parse_search_video(
        CTX,
        {
            "bvid": "BV1BZbSzZEGT",
            "title": '【Hi-Res】｜《<em class="keyword">晴天</em>》- 周杰伦',
            "author": "VV音乐局",
            "mid": 9666167,
            "duration": "4:30",
            "pic": "//i0.hdslb.com/bfs/archive/abc.jpg",
        },
    )
    assert track.item_id == "BV1BZbSzZEGT"
    assert track.name == "晴天"
    assert track.duration == 270
    assert [(a.item_id, a.name) for a in track.artists] == [("9666167", "VV音乐局")]


def test_parse_search_video_keeps_title_when_cleaning_disabled() -> None:
    """With title cleanup off only the html is stripped."""
    ctx = ParseContext("bilibili", "bilibili--test", clean_titles=False)
    track = parse_search_video(ctx, {"bvid": "BV1BZbSzZEGT", "title": "【MV】《晴天》"})
    assert track.name == "【MV】《晴天》"


def test_parse_view_track_single_page() -> None:
    """A single page video is a plain track without album."""
    track = parse_view_track(CTX, VIEW_SINGLE)
    assert track.item_id == "BV1BZbSzZEGT"
    assert track.name == "晴天"
    assert track.duration == 270
    assert track.album is None
    assert track.metadata.description == "description"


def test_parse_view_track_multi_page() -> None:
    """Pages of a multi-part video are tracks of the album representing the video."""
    track = parse_view_track(CTX, VIEW_MULTI, page=2)
    assert track.item_id == "BV1abcdefghi_p2"
    assert track.name == "七里香"
    assert track.duration == 200
    assert track.track_number == 2
    assert track.album is not None
    assert track.album.item_id == "BV1abcdefghi"
    assert track.album.media_type == MediaType.ALBUM
    with pytest.raises(InvalidDataError):
        parse_view_track(CTX, VIEW_MULTI, page=5)


def test_parse_view_track_without_playable_page() -> None:
    """Videos without any page (removed, under review...) are not parsed as track."""
    with pytest.raises(InvalidDataError):
        parse_view_track(CTX, {"bvid": "BV1BZbSzZEGT", "title": "x", "pages": []})


def test_parse_view_album() -> None:
    """A video becomes an album with year from its publish date."""
    album = parse_view_album(CTX, VIEW_SINGLE)
    assert album.item_id == "BV1BZbSzZEGT"
    assert album.year == 2025
    assert [a.item_id for a in album.artists] == ["9666167"]


def test_parse_fav_media_skips_unplayable() -> None:
    """Only valid plain videos of a favorite folder are tracks."""
    base = {
        "type": 2,
        "bvid": "BV1BZbSzZEGT",
        "title": "《晴天》",
        "duration": 270,
        "cover": "",
        "upper": {"mid": 1, "name": "up"},
    }
    track = parse_fav_media(CTX, base)
    assert track is not None
    assert track.name == "晴天"
    assert parse_fav_media(CTX, {**base, "title": "已失效视频"}) is None
    # audio (12) and other media types are skipped
    assert parse_fav_media(CTX, {**base, "type": 12}) is None
    # SDK field naming is accepted as well
    sdk_style = {**base, "type_name": 2}
    del sdk_style["type"]
    assert parse_fav_media(CTX, sdk_style) is not None


def test_parse_hot_rank_item_uses_related_archive() -> None:
    """The chart's top-level bvid is a metadata id; the related archive is played."""
    track = parse_hot_rank_item(
        CTX,
        {
            "music_title": "ラキラキ",
            "author": "すりぃ",
            "bvid": "BV_METADATA",
            "cover": "https://i0.hdslb.com/bfs/a.jpg",
            "related_archive": {
                "bvid": "BV1qiWw6fEFF",
                "uid": 3546858492267284,
                "username": "すりぃ_THREEE",
                "duration": 203,
            },
        },
    )
    assert track.item_id == "BV1qiWw6fEFF"
    assert track.name == "ラキラキ"
    assert track.duration == 203
    assert [a.item_id for a in track.artists] == ["3546858492267284"]


def test_fav_folder_playlist_ids() -> None:
    """Favorite folders map to fav_<id> playlists."""
    playlist = parse_fav_folder(CTX, {"id": 123, "title": "默认收藏夹"}, owner="me")
    assert playlist.item_id == "fav_123"
    assert playlist.owner == "me"
    assert playlist.is_editable is False
    assert parse_fav_playlist_id("fav_123") == 123
    for bad in ("123", "fav_", "fav_x", "fav_-1"):
        with pytest.raises(InvalidDataError):
            parse_fav_playlist_id(bad)
