"""Tests for the Migu parsers (payloads trimmed from real API responses)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from music_assistant_models.enums import AlbumType, ContentType
from music_assistant_models.errors import InvalidDataError

from music_assistant.providers.migu.parsers import (
    ParseContext,
    has_timestamps,
    iter_playlists,
    iter_ranks,
    iter_search_albums,
    iter_songs,
    lrc_to_plain,
    normalize_image_url,
    parse_album,
    parse_digital_album,
    parse_playlist,
    parse_singer,
    parse_singer_album_item,
    parse_song,
    song_tones,
    split_album_id,
    tone_audio_format,
    tone_for_url,
)

SONG: dict[str, Any] = {
    "resourceType": "2",
    "contentId": "600902000006889366",
    "songId": "3790007",
    "songName": "晴天",
    "showTags": ["vip", "isq", "asq"],
    "audioFormats": [
        {"resourceType": "2", "formatType": "PQ"},
        {"resourceType": "2", "formatType": "HQ"},
        {"resourceType": "E", "formatType": "SQ"},
        {"resourceType": "2", "formatType": "Z3D"},
    ],
    "duration": 270,
    "copyrightId": "60054701923",
    "albumId": "8592",
    "album": "叶惠美",
    "img1": "/data/oss/resource/00/4t/9y/small.webp",
    "img3": "/data/oss/resource/00/4t/9y/large.webp",
    "singerList": [{"id": "112", "name": "周杰伦"}],
    "ext": {"disc": "Disc 1"},
}


def test_parse_song(ctx: ParseContext) -> None:
    """A song maps to a track with artist, album, cover and its best tone."""
    track = parse_song(ctx, SONG)
    assert track.item_id == "600902000006889366"
    assert track.name == "晴天"
    assert track.duration == 270
    assert [(a.item_id, a.name) for a in track.artists] == [("112", "周杰伦")]
    assert track.album is not None
    assert (track.album.item_id, track.album.name) == ("8592", "叶惠美")
    assert track.disc_number == 1
    assert track.metadata.images
    images = track.metadata.images
    assert images[0].path == "https://d.musicapp.migu.cn/data/oss/resource/00/4t/9y/large.webp"
    assert images[0].remotely_accessible
    mapping = next(iter(track.provider_mappings))
    assert mapping.audio_format.content_type == ContentType.FLAC
    assert mapping.url is not None
    assert "albumId=8592" in mapping.url


def test_parse_song_requires_id() -> None:
    """Songs without content id are rejected."""
    with pytest.raises(InvalidDataError):
        parse_song(ParseContext("migu", "x"), {"songName": "x"})


def test_song_tones_best_first() -> None:
    """Known tones are returned best first; unknown ones (Z3D) are ignored."""
    assert song_tones(SONG) == ["SQ", "HQ", "PQ"]
    assert song_tones({"audioFormats": [{"formatType": "ZQ24"}, {"formatType": "PQ"}]}) == [
        "ZQ24",
        "PQ",
    ]
    assert song_tones({}) == ["PQ"]
    # chart entries carry the formats as a Python repr string
    assert song_tones({"audioFormats": "[{'formatType': 'HQ'}, {'formatType': 'PQ'}]"}) == [
        "HQ",
        "PQ",
    ]


def test_tone_formats() -> None:
    """Tones map to FLAC/MP3 formats; urls reveal their tone."""
    assert tone_audio_format("ZQ24").bit_depth == 24
    assert tone_audio_format("SQ").content_type == ContentType.FLAC
    hq = tone_audio_format("HQ")
    assert hq.content_type == ContentType.MP3
    assert hq.bit_rate == 320
    assert tone_audio_format("PQ").bit_rate == 128
    assert tone_for_url("https://x/歌曲下载/flac_24bit/a.flac?k=1", "PQ") == "ZQ24"
    assert tone_for_url("https://x/歌曲下载/flac/a.flac", "PQ") == "SQ"
    assert tone_for_url("https://x/MP3_320_16_Stero/a.mp3", "PQ") == "HQ"
    assert tone_for_url("https://x/MP3_128_16_Stero/a.mp3", "HQ") == "PQ"
    assert tone_for_url("https://x/other.bin", "SQ") == "SQ"


def test_iter_songs_handles_wrappers_and_dedupes() -> None:
    """Songs are found in search wrappers, page layouts and chart entries, once each."""
    search_page = {"items": [{"song": SONG}, {"song": {**SONG, "more": [SONG]}}]}
    assert [s["contentId"] for s in iter_songs(search_page)] == ["600902000006889366"]

    layout = {
        "contents": [
            {"view": "ZJ-Img-Scroll", "contents": [{"songItem": {**SONG, "contentId": "1"}}]},
            {"contents": [{"songItem": {**SONG, "contentId": "1"}}, {"songItem": SONG}]},
        ]
    }
    assert [s["contentId"] for s in iter_songs(layout)] == ["1", "600902000006889366"]

    chart_song = dict(SONG)
    chart_song["singerList"] = "[{'id': '112', 'name': '周杰伦'}]"
    chart = {
        "contents": [
            {
                "resType": "2",
                "resId": "600902000006889366",
                "txt": "晴天",
                "txt2": "周杰伦",
                "songData": json.dumps(chart_song, ensure_ascii=False),
            }
        ]
    }
    songs = list(iter_songs(chart))
    assert len(songs) == 1
    track = parse_song(ParseContext("migu", "x"), songs[0])
    assert [a.name for a in track.artists] == ["周杰伦"]


def test_chart_entry_without_singer_list_uses_joined_name(ctx: ParseContext) -> None:
    """A chart entry with only ``txt2`` gets name-only artists."""
    songs = list(
        iter_songs([{"resType": "2", "resId": "9", "txt": "x", "txt2": "A、B", "songData": "{}"}])
    )
    track = parse_song(ctx, songs[0])
    assert [(a.item_id, a.name) for a in track.artists] == [("name:A", "A"), ("name:B", "B")]


def test_parse_album_and_digital_album(ctx: ParseContext) -> None:
    """Regular and digital albums get distinct ids, years, types and covers."""
    album = parse_album(
        ctx,
        {
            "albumId": "8592",
            "title": "叶惠美",
            "singer": "周杰伦",
            "singerId": "112",
            "publishDate": "2003-07-31",
            "albumClass": "录音室专辑",
            "publishCompany": "杰威尔音乐",
            "imgItems": [
                {"imgSizeType": "01", "img": "https://x/1.webp"},
                {"imgSizeType": "03", "img": "https://x/3.webp"},
            ],
            "tags": [{"tagName": "流行", "tagPathName": "ROOT|风格|通俗音乐|流行"}],
        },
    )
    assert album.item_id == "8592"
    assert album.year == 2003
    assert album.album_type == AlbumType.ALBUM
    assert album.metadata.label == "杰威尔音乐"
    assert album.metadata.genres == {"流行"}
    assert (album.metadata.images or [])[0].path == "https://x/3.webp"
    assert [a.item_id for a in album.artists] == ["112"]

    digital = parse_digital_album(
        ctx,
        {
            "contentId": "600927015009001083",
            "title": "太阳之子",
            "singer": "周杰伦",
            "singerId": "112",
            "totalCount": "13",
            "publishDate": "2026-03-25",
            "imgItem": [{"imgSizeType": "01", "img": "https://x/d.webp"}],
        },
    )
    assert digital.item_id == "d600927015009001083"
    assert split_album_id(digital.item_id) == ("600927015009001083", True)
    assert split_album_id("8592") == ("8592", False)
    assert digital.year == 2026


def test_iter_search_albums_mixes_both_kinds(ctx: ParseContext) -> None:
    """Album search returns digital and regular albums."""
    payload = {
        "items": [
            {"dalbum": {"contentId": "6009", "title": "A", "singer": "s", "singerId": "1"}},
            {"album": {"albumId": "21090", "title": "B", "singer": "s", "singerId": "1"}},
            {"other": {}},
        ]
    }
    assert [a.item_id for a in iter_search_albums(ctx, payload)] == ["d6009", "21090"]


def test_parse_singer_album_item(ctx: ParseContext) -> None:
    """Singer album list entries tell digital (resType 1) from regular albums."""
    digital = parse_singer_album_item(
        ctx,
        {
            "resType": "1",
            "resId": "6009",
            "txt": "太阳之子",
            "txt2": "周杰伦",
            "txt3": "2026-03-19",
        },
        "周杰伦",
    )
    regular = parse_singer_album_item(
        ctx, {"resType": "0", "resId": "1141456456", "txt": "即兴曲"}, "周杰伦"
    )
    assert digital.item_id == "d6009"
    assert digital.year == 2026
    assert regular.item_id == "1141456456"
    assert [a.name for a in regular.artists] == ["周杰伦"]


def test_parse_singer(ctx: ParseContext) -> None:
    """Singers map to artists with the largest image."""
    artist = parse_singer(
        ctx,
        {
            "singerId": "112",
            "singer": "周杰伦",
            "imgs": [{"imgSizeType": "02", "img": "https://x/2.webp"}],
            "detail": "姓名\uff1a周杰伦",
        },
    )
    assert (artist.item_id, artist.name) == ("112", "周杰伦")
    assert (artist.metadata.images or [])[0].path == "https://x/2.webp"
    assert artist.metadata.description == "姓名\uff1a周杰伦"


def test_playlists(ctx: ParseContext) -> None:
    """Playlists are found by id and title; own ones get the my_ prefix."""
    payload = {
        "data": {
            "items": [
                {"musicList": {"musicListId": "225", "title": "A", "ownerName": "x"}},
                {"musicList": {"musicListId": "225", "title": "A"}},
                {"playlistId": "7", "playlistName": "我喜欢", "imgItem": {"img": "/i.png"}},
            ]
        }
    }
    items = list(iter_playlists(payload))
    assert len(items) == 2
    public = parse_playlist(ctx, items[0])
    assert public.item_id == "225"
    assert public.owner == "x"
    own = parse_playlist(ctx, items[1], owned=True, owner="me")
    assert own.item_id == "my_7"
    assert own.owner == "me"
    assert (own.metadata.images or [])[0].path == "https://d.musicapp.migu.cn/i.png"


def test_iter_ranks() -> None:
    """Charts are collected once each from the nested charts page."""
    page = {
        "contents": [
            {"view": "x", "contents": [{"rankId": "1", "rankName": "热歌榜", "contents": []}]},
            {"rankId": "1", "rankName": "热歌榜"},
            {"rankId": "2", "rankName": "新歌榜"},
        ]
    }
    assert [r["rankId"] for r in iter_ranks(page)] == ["1", "2"]


def test_lyrics_helpers() -> None:
    """LRC is recognized and stripped to plain text without credits."""
    lrc = "[ti:晴天]\r\n[00:01.00]晴天 - 周杰伦\r\n[00:02.00]作词\uff1a周杰伦\r\n[00:29.12]故事的小黄花\r\n"
    assert has_timestamps(lrc)
    assert not has_timestamps("plain")
    assert lrc_to_plain(lrc) == "晴天 - 周杰伦\n故事的小黄花"


def test_normalize_image_url() -> None:
    """Relative, protocol-relative and http image urls become absolute https."""
    assert normalize_image_url("/data/a.webp") == "https://d.musicapp.migu.cn/data/a.webp"
    assert normalize_image_url("//cdn/a.webp") == "https://cdn/a.webp"
    assert normalize_image_url("http://cdn/a.webp") == "https://cdn/a.webp"
    assert normalize_image_url(None) == ""
