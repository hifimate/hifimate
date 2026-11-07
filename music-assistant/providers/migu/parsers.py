"""Parsers turning (plain dict) Migu API data into Music Assistant media items."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from contextlib import suppress
from typing import Any
from urllib.parse import quote

from music_assistant_models.enums import AlbumType, ContentType, ImageType, MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from .constants import (
    ALBUM_PREFIX_DIGITAL,
    IMAGE_HOST,
    PLAYLIST_PREFIX_MINE,
    PLAYLIST_PREFIX_RANK,
    TONE_HQ,
    TONE_PQ,
    TONE_SQ,
    TONE_ZQ24,
    TONES,
    WEB_ALBUM_TYPE,
    WEB_DIGITAL_ALBUM_TYPE,
    WEB_URL,
)

_LRC_TAG_PATTERN = re.compile(r"\[[^\]]*\]")
_LRC_TIMESTAMP_PATTERN = re.compile(r"\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]")
_YEAR_PATTERN = re.compile(r"^(\d{4})")
# separators of joined artist names: comma, fullwidth comma, ideographic comma, slash
_NAME_SEPARATORS = re.compile("[,\uff0c\u3001/]")
# lyric credit lines like "作词: 周杰伦" (also with a fullwidth colon)
_LRC_CREDIT_PATTERN = re.compile("^\\s*(作词|作曲|编曲|制作人|词|曲)\\s*[:\uff1a]")

_ALBUM_TYPES = {
    "录音室专辑": AlbumType.ALBUM,
    "专辑": AlbumType.ALBUM,
    "单曲发行": AlbumType.SINGLE,
    "单曲": AlbumType.SINGLE,
    "EP": AlbumType.EP,
    "精选集": AlbumType.COMPILATION,
    "合辑": AlbumType.COMPILATION,
    "Live专辑": AlbumType.LIVE,
    "现场专辑": AlbumType.LIVE,
    "原声带": AlbumType.SOUNDTRACK,
    "影视原声": AlbumType.SOUNDTRACK,
}


class ParseContext:
    """What the parsers need to know about the provider instance."""

    def __init__(self, domain: str, instance_id: str) -> None:
        """
        Initialize the context.

        :param domain: Provider domain.
        :param instance_id: Provider instance id.
        """
        self.domain = domain
        self.instance_id = instance_id

    def mapping(self, item_id: str, url: str | None = None, **kwargs: Any) -> ProviderMapping:
        """
        Create the provider mapping of an item.

        :param item_id: Provider item id.
        :param url: Web url of the item.
        """
        return ProviderMapping(
            item_id=item_id,
            provider_domain=self.domain,
            provider_instance=self.instance_id,
            url=url,
            **kwargs,
        )

    def item_mapping(self, media_type: MediaType, item_id: str, name: str) -> ItemMapping:
        """
        Create a lightweight reference to another item.

        :param media_type: Media type of the referenced item.
        :param item_id: Provider item id.
        :param name: Display name.
        """
        return ItemMapping(
            media_type=media_type, item_id=item_id, provider=self.instance_id, name=name
        )

    def images(self, url: Any) -> UniqueList[MediaItemImage]:
        """
        Create the image list for a Migu image url.

        :param url: Absolute or host-relative (``/data/oss/...``) image url.
        """
        image_url = normalize_image_url(url)
        if not image_url:
            return UniqueList()
        return UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=self.instance_id,
                    remotely_accessible=True,
                )
            ]
        )


# ---------------------------------------------------------------- helpers


def normalize_image_url(url: Any) -> str:
    """
    Return an absolute https image url ('' for none).

    :param url: Image url as returned by the API.
    """
    if not isinstance(url, str) or not url.strip():
        return ""
    image_url = url.strip()
    if image_url.startswith("//"):
        return f"https:{image_url}"
    if image_url.startswith("/"):
        return f"{IMAGE_HOST}{image_url}"
    if image_url.startswith("http://"):
        return f"https://{image_url[7:]}"
    return image_url


def _int(value: Any) -> int:
    """Return value as int (0 when not convertible)."""
    if isinstance(value, bool):
        return 0
    with suppress(TypeError, ValueError):
        return int(float(value))
    return 0


def _str(value: Any) -> str:
    """Return a stripped string ('' for None/non scalars)."""
    if value is None or isinstance(value, (dict, list)):
        return ""
    return str(value).strip()


def _listish(value: Any) -> list[Any]:
    """
    Return a list from a list or its JSON/Python-repr string form.

    Chart entries carry their song as a flattened dict whose nested values are
    Python reprs (``"[{'id': '112', ...}]"``).
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip().startswith("["):
        with suppress(ValueError):
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        with suppress(ValueError, SyntaxError, MemoryError, RecursionError):
            parsed = ast.literal_eval(value)
            if isinstance(parsed, list):
                return parsed
    return []


def _year(value: Any) -> int | None:
    """Return the year of a ``2003-07-31`` / ``20241113...`` date string."""
    match = _YEAR_PATTERN.match(_str(value))
    if not match:
        return None
    year = int(match.group(1))
    return year if 1900 < year < 2200 else None


def album_url(album_id: str) -> str:
    """Return the web url of an album (digital albums carry the ``d`` prefix)."""
    if album_id.startswith(ALBUM_PREFIX_DIGITAL):
        content_id = album_id[len(ALBUM_PREFIX_DIGITAL) :]
        return f"{WEB_URL}/albumDetail?albumId={content_id}&playlistType={WEB_DIGITAL_ALBUM_TYPE}"
    return f"{WEB_URL}/albumDetail?albumId={album_id}&playlistType={WEB_ALBUM_TYPE}"


def artist_url(singer_id: str) -> str:
    """Return the web url of a singer."""
    return f"{WEB_URL}/singerDetail?id={singer_id}"


def playlist_url(playlist_id: str) -> str:
    """Return the web url of a public playlist."""
    return f"{WEB_URL}/playlist?playlistId={quote(playlist_id)}&playlistType=ordinary"


def best_image(item: dict[str, Any]) -> str:
    """
    Return the largest image url of a song/album/singer/playlist dict.

    :param item: API dict with ``img1..3``, ``imgItems``/``imgs``/``imgItem`` or ``img``.
    """
    for key in ("img3", "img2", "img1"):
        if url := _str(item.get(key)):
            return url
    for key in ("imgItems", "imgs", "imgItem", "singerImgs", "singerPicUrl"):
        images = item.get(key)
        if isinstance(images, dict):
            images = [images]
        if isinstance(images, list):
            candidates = [i for i in images if isinstance(i, dict) and _str(i.get("img"))]
            if candidates:
                best = max(candidates, key=lambda i: _int(i.get("imgSizeType")))
                return _str(best.get("img"))
    for key in ("originalImgUrl", "img", "imageUrl", "picUrl", "pic"):
        if url := _str(item.get(key)):
            return url
    return ""


# ---------------------------------------------------------------- tones


def song_tones(song: dict[str, Any]) -> list[str]:
    """
    Return the tones a song is offered in, best first.

    :param song: Song dict (``audioFormats`` or ``qualities``).
    """
    offered: set[str] = set()
    for fmt in _listish(song.get("audioFormats")):
        if isinstance(fmt, dict):
            offered.add(_str(fmt.get("formatType")).upper())
    for quality in _listish(song.get("qualities")):
        offered.add(_str(quality).upper())
    tones = [tone for tone in TONES if tone in offered]
    return tones or [TONE_PQ]


def tone_audio_format(tone: str) -> AudioFormat:
    """
    Return the (nominal) audio format of a tone.

    The real sample rate of hi-res files varies; ffmpeg probes it on playback.

    :param tone: Tone flag.
    """
    if tone == TONE_ZQ24:
        return AudioFormat(
            content_type=ContentType.FLAC,
            codec_type=ContentType.FLAC,
            sample_rate=48000,
            bit_depth=24,
            channels=2,
        )
    if tone == TONE_SQ:
        return AudioFormat(
            content_type=ContentType.FLAC,
            codec_type=ContentType.FLAC,
            sample_rate=44100,
            bit_depth=16,
            channels=2,
        )
    return AudioFormat(
        content_type=ContentType.MP3,
        codec_type=ContentType.MP3,
        sample_rate=44100,
        bit_depth=16,
        channels=2,
        bit_rate=320 if tone == TONE_HQ else 128,
    )


def tone_for_url(url: str, fallback: str) -> str:
    """
    Return the tone of a CDN url from its path (``.../flac_24bit/...``).

    :param url: Stream url.
    :param fallback: Tone to assume when the path tells nothing.
    """
    lowered = url.lower().split("?", 1)[0]
    if "flac_24bit" in lowered or "24bit" in lowered:
        return TONE_ZQ24
    if lowered.endswith(".flac") or "/flac/" in lowered:
        return TONE_SQ
    if "mp3_320" in lowered:
        return TONE_HQ
    if "mp3_128" in lowered:
        return TONE_PQ
    return fallback


# ---------------------------------------------------------------- songs


def is_song(item: Any) -> bool:
    """Return whether a dict looks like a Migu song."""
    return (
        isinstance(item, dict)
        and bool(_str(item.get("contentId")))
        and bool(_str(item.get("songName")))
        and _str(item.get("resourceType") or "2") == "2"
    )


def _rank_song(item: dict[str, Any]) -> dict[str, Any] | None:
    """Return the song of a chart entry (``songData`` JSON plus the entry fields)."""
    raw = item.get("songData")
    song: dict[str, Any] = {}
    if isinstance(raw, str):
        with suppress(ValueError):
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                song = parsed
    elif isinstance(raw, dict):
        song = dict(raw)
    song.setdefault("contentId", item.get("resId"))
    song.setdefault("songName", item.get("txt"))
    song.setdefault("copyrightId", item.get("copyrightId"))
    song.setdefault("album", item.get("txt3"))
    song.setdefault("img3", item.get("img"))
    if "qualities" in item:
        song.setdefault("qualities", item.get("qualities"))
    if not song.get("singerList") and item.get("txt2"):
        song["singerName"] = item.get("txt2")
    return song if is_song(song) else None


def iter_songs(payload: Any) -> Iterator[dict[str, Any]]:
    """
    Yield every song found in a (nested page layout) payload, without duplicates.

    Handles plain song dicts, ``songItem`` wrappers and chart ``songData`` strings.

    :param payload: Any API payload.
    """
    seen: set[str] = set()

    def _walk(value: Any) -> Iterator[dict[str, Any]]:
        if isinstance(value, list):
            for entry in value:
                yield from _walk(entry)
            return
        if not isinstance(value, dict):
            return
        song: dict[str, Any] | None = None
        if isinstance(value.get("songItem"), dict) and is_song(value["songItem"]):
            song = value["songItem"]
        elif isinstance(value.get("song"), dict) and is_song(value["song"]):
            song = value["song"]
        elif "songData" in value:
            song = _rank_song(value)
        elif is_song(value):
            song = value
        if song is not None:
            content_id = _str(song.get("contentId"))
            if content_id not in seen:
                seen.add(content_id)
                yield song
            return
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _walk(child)

    yield from _walk(payload)


def _song_artists(ctx: ParseContext, song: dict[str, Any]) -> UniqueList[Artist | ItemMapping]:
    """Return the artist references of a song."""
    artists: UniqueList[Artist | ItemMapping] = UniqueList()
    for singer in _listish(song.get("singerList")):
        if not isinstance(singer, dict):
            continue
        singer_id, name = _str(singer.get("id")), _str(singer.get("name"))
        if singer_id and name:
            artists.append(ctx.item_mapping(MediaType.ARTIST, singer_id, name))
    if not artists:
        # chart entries and some lists only carry the joined name
        names = _str(song.get("singerName") or song.get("singer"))
        singer_ids = _str(song.get("singerId")).split(",")
        for index, name in enumerate(n.strip() for n in re.split(_NAME_SEPARATORS, names)):
            if not name:
                continue
            singer_id = singer_ids[index].strip() if index < len(singer_ids) else ""
            artists.append(ctx.item_mapping(MediaType.ARTIST, singer_id or f"name:{name}", name))
    return artists


def parse_song(ctx: ParseContext, song: dict[str, Any]) -> Track:
    """
    Parse a Migu song into a track.

    :param ctx: Parse context.
    :param song: Song dict (search, album, playlist, chart or songinfo).
    """
    content_id = _str(song.get("contentId"))
    name = _str(song.get("songName"))
    if not content_id or not name:
        raise InvalidDataError("Song without contentId or name")
    tones = song_tones(song)
    track = Track(
        item_id=content_id,
        provider=ctx.instance_id,
        name=name,
        duration=_int(song.get("duration")),
        provider_mappings={
            ctx.mapping(
                content_id,
                album_url(_str(song.get("albumId"))) if _str(song.get("albumId")) else None,
                audio_format=tone_audio_format(tones[0]),
            )
        },
    )
    track.artists = _song_artists(ctx, song)
    album_id, album_name = _str(song.get("albumId")), _str(song.get("album"))
    if album_id and album_name:
        track.album = ctx.item_mapping(MediaType.ALBUM, album_id, album_name)
    track.metadata.images = ctx.images(best_image(song))
    disc = _str((song.get("ext") or {}).get("disc") if isinstance(song.get("ext"), dict) else "")
    if (disc_number := _int(disc.replace("Disc", ""))) > 0:
        track.disc_number = disc_number
    return track


def song_copyright_id(song: dict[str, Any]) -> str:
    """Return the copyright id of a song dict."""
    return _str(song.get("copyrightId"))


# ---------------------------------------------------------------- albums


def _album_type(value: Any) -> AlbumType:
    """Map Migu's albumClass to an album type."""
    return _ALBUM_TYPES.get(_str(value), AlbumType.UNKNOWN)


def _single_artist(
    ctx: ParseContext, singer_id: Any, singer: Any
) -> UniqueList[Artist | ItemMapping]:
    """Return the artist list of an album/digital album (``singerId``/``singer``)."""
    artists: UniqueList[Artist | ItemMapping] = UniqueList()
    names = [n.strip() for n in re.split(_NAME_SEPARATORS, _str(singer)) if n.strip()]
    ids = [i.strip() for i in _str(singer_id).split(",")]
    for index, name in enumerate(names):
        artist_id = ids[index] if index < len(ids) and ids[index] else f"name:{name}"
        artists.append(ctx.item_mapping(MediaType.ARTIST, artist_id, name))
    return artists


def parse_album(ctx: ParseContext, album: dict[str, Any]) -> Album:
    """
    Parse a regular album (album/info, album search result).

    :param ctx: Parse context.
    :param album: Album dict with ``albumId``.
    """
    album_id = _str(album.get("albumId"))
    name = _str(album.get("title") or album.get("albumName"))
    if not album_id or not name:
        raise InvalidDataError("Album without id or title")
    result = Album(
        item_id=album_id,
        provider=ctx.instance_id,
        name=name,
        provider_mappings={ctx.mapping(album_id, album_url(album_id))},
    )
    result.artists = _single_artist(ctx, album.get("singerId"), album.get("singer"))
    result.year = _year(album.get("publishDate") or album.get("publishTime"))
    result.album_type = _album_type(album.get("albumClass"))
    result.metadata.images = ctx.images(best_image(album))
    if summary := _str(album.get("summary")):
        result.metadata.description = summary
    if label := _str(album.get("publishCompany") or album.get("publishCorp")):
        result.metadata.label = label
    genres = {
        _str(tag.get("tagName"))
        for tag in album.get("tags") or []
        if isinstance(tag, dict) and "风格" in _str(tag.get("tagPathName"))
    }
    if genres - {""}:
        result.metadata.genres = genres - {""}
    return result


def parse_digital_album(ctx: ParseContext, dalbum: dict[str, Any]) -> Album:
    """
    Parse a digital album (数字专辑; dalbum/info or album search ``dalbum``).

    :param ctx: Parse context.
    :param dalbum: Digital album dict with ``contentId``.
    """
    content_id = _str(dalbum.get("contentId"))
    name = _str(dalbum.get("title"))
    if not content_id or not name:
        raise InvalidDataError("Digital album without id or title")
    album_id = f"{ALBUM_PREFIX_DIGITAL}{content_id}"
    result = Album(
        item_id=album_id,
        provider=ctx.instance_id,
        name=name,
        provider_mappings={ctx.mapping(album_id, album_url(album_id))},
    )
    result.artists = _single_artist(ctx, dalbum.get("singerId"), dalbum.get("singer"))
    result.year = _year(dalbum.get("publishDate") or dalbum.get("firstStartDate"))
    result.album_type = AlbumType.SINGLE if _int(dalbum.get("totalCount")) == 1 else AlbumType.ALBUM
    result.metadata.images = ctx.images(best_image(dalbum))
    return result


def parse_singer_album_item(ctx: ParseContext, item: dict[str, Any], artist: str) -> Album:
    """
    Parse an entry of a singer's album list (``ZJ-Album-Item``).

    :param ctx: Parse context.
    :param item: Entry with ``resType`` (1: digital album, 0: album), ``resId``, ``txt``.
    :param artist: Fallback artist name.
    """
    res_id, name = _str(item.get("resId")), _str(item.get("txt"))
    if not res_id or not name:
        raise InvalidDataError("Album entry without id or title")
    digital = _str(item.get("resType")) == "1" or "digital-album" in _str(item.get("action"))
    album_id = f"{ALBUM_PREFIX_DIGITAL}{res_id}" if digital else res_id
    result = Album(
        item_id=album_id,
        provider=ctx.instance_id,
        name=name,
        provider_mappings={ctx.mapping(album_id, album_url(album_id))},
    )
    result.artists = _single_artist(ctx, "", item.get("txt2") or artist)
    result.year = _year(item.get("txt3"))
    result.metadata.images = ctx.images(best_image(item))
    return result


def iter_search_albums(ctx: ParseContext, payload: dict[str, Any]) -> Iterator[Album]:
    """Yield the albums (and digital albums) of an album search payload."""
    for entry in payload.get("items") or []:
        if not isinstance(entry, dict):
            continue
        with suppress(InvalidDataError):
            if isinstance(entry.get("album"), dict):
                yield parse_album(ctx, entry["album"])
            elif isinstance(entry.get("dalbum"), dict):
                yield parse_digital_album(ctx, entry["dalbum"])


def split_album_id(album_id: str) -> tuple[str, bool]:
    """
    Split a provider album id into ``(migu_id, is_digital)``.

    :param album_id: Provider album id.
    """
    if album_id.startswith(ALBUM_PREFIX_DIGITAL):
        return album_id[len(ALBUM_PREFIX_DIGITAL) :], True
    return album_id, False


# ---------------------------------------------------------------- artists


def parse_singer(ctx: ParseContext, singer: dict[str, Any]) -> Artist:
    """
    Parse a singer (resourceinfo or singer search result).

    :param ctx: Parse context.
    :param singer: Singer dict with ``singerId`` and ``singer``.
    """
    singer_id = _str(singer.get("singerId") or singer.get("id"))
    name = _str(singer.get("singer") or singer.get("name"))
    if not singer_id or not name:
        raise InvalidDataError("Singer without id or name")
    artist = Artist(
        item_id=singer_id,
        provider=ctx.instance_id,
        name=name,
        provider_mappings={ctx.mapping(singer_id, artist_url(singer_id))},
    )
    artist.metadata.images = ctx.images(best_image(singer))
    if detail := _str(singer.get("detail") or singer.get("intro")):
        artist.metadata.description = detail
    return artist


def iter_search_singers(ctx: ParseContext, payload: dict[str, Any]) -> Iterator[Artist]:
    """Yield the singers of a singer search payload."""
    for entry in payload.get("items") or []:
        if isinstance(entry, dict) and isinstance(entry.get("singer"), dict):
            with suppress(InvalidDataError):
                yield parse_singer(ctx, entry["singer"])


# ---------------------------------------------------------------- playlists


def _playlist_id(item: dict[str, Any]) -> str:
    """Return the Migu playlist id of a playlist-like dict."""
    return _str(item.get("musicListId") or item.get("playlistId") or item.get("musiclistId"))


def is_playlist(item: Any) -> bool:
    """Return whether a dict looks like a Migu playlist."""
    if not isinstance(item, dict) or not _playlist_id(item):
        return False
    return bool(_str(item.get("title") or item.get("playlistName") or item.get("musicListName")))


def iter_playlists(payload: Any) -> Iterator[dict[str, Any]]:
    """Yield every playlist dict found in a payload, without duplicates."""
    seen: set[str] = set()

    def _walk(value: Any) -> Iterator[dict[str, Any]]:
        if isinstance(value, list):
            for entry in value:
                yield from _walk(entry)
        elif isinstance(value, dict):
            if is_playlist(value):
                playlist_id = _playlist_id(value)
                if playlist_id not in seen:
                    seen.add(playlist_id)
                    yield value
                return
            for child in value.values():
                if isinstance(child, (dict, list)):
                    yield from _walk(child)

    yield from _walk(payload)


def parse_playlist(
    ctx: ParseContext, item: dict[str, Any], *, owned: bool = False, owner: str | None = None
) -> Playlist:
    """
    Parse a playlist.

    :param ctx: Parse context.
    :param item: Playlist dict (``musicListId``/``playlistId`` and ``title``/``playlistName``).
    :param owned: The playlist belongs to the signed in user (its songs are read with
        the user's token).
    :param owner: Owner name override.
    """
    migu_id = _playlist_id(item)
    name = _str(item.get("title") or item.get("playlistName") or item.get("musicListName"))
    if not migu_id or not name:
        raise InvalidDataError("Playlist without id or title")
    playlist_id = f"{PLAYLIST_PREFIX_MINE}{migu_id}" if owned else migu_id
    playlist = Playlist(
        item_id=playlist_id,
        provider=ctx.instance_id,
        name=name,
        owner=owner or _str(item.get("ownerName") or item.get("nickName")) or "Migu",
        is_editable=False,
        provider_mappings={ctx.mapping(playlist_id, playlist_url(migu_id))},
    )
    playlist.metadata.images = ctx.images(best_image(item))
    if summary := _str(item.get("summary")):
        playlist.metadata.description = summary
    return playlist


def parse_rank_playlist(ctx: ParseContext, rank: dict[str, Any]) -> Playlist:
    """
    Parse a chart (rankId/rankName) as playlist.

    :param ctx: Parse context.
    :param rank: Chart dict.
    """
    rank_id, name = _str(rank.get("rankId") or rank.get("columnId")), _str(rank.get("rankName"))
    if not rank_id or not name:
        raise InvalidDataError("Chart without id or name")
    playlist_id = f"{PLAYLIST_PREFIX_RANK}{rank_id}"
    playlist = Playlist(
        item_id=playlist_id,
        provider=ctx.instance_id,
        name=name,
        owner="咪咕音乐",
        is_editable=False,
        provider_mappings={ctx.mapping(playlist_id, f"{WEB_URL}/topSongs")},
    )
    playlist.metadata.images = ctx.images(best_image(rank))
    return playlist


def iter_ranks(payload: Any) -> Iterator[dict[str, Any]]:
    """Yield every chart (dict with rankId and rankName) of the charts page."""
    seen: set[str] = set()

    def _walk(value: Any) -> Iterator[dict[str, Any]]:
        if isinstance(value, list):
            for entry in value:
                yield from _walk(entry)
        elif isinstance(value, dict):
            rank_id = _str(value.get("rankId"))
            if rank_id and _str(value.get("rankName")):
                if rank_id not in seen:
                    seen.add(rank_id)
                    yield value
                return
            for child in value.values():
                if isinstance(child, (dict, list)):
                    yield from _walk(child)

    yield from _walk(payload)


# ---------------------------------------------------------------- lyrics


def has_timestamps(lrc: str) -> bool:
    """Return whether LRC text has line timestamps."""
    return bool(_LRC_TIMESTAMP_PATTERN.search(lrc))


def lrc_to_plain(lrc: str) -> str:
    """
    Return the plain text of LRC lyrics (tags and credit lines removed).

    :param lrc: LRC text.
    """
    lines: list[str] = []
    for raw_line in lrc.replace("\r\n", "\n").split("\n"):
        line = _LRC_TAG_PATTERN.sub("", raw_line).strip()
        if line and not _LRC_CREDIT_PATTERN.match(line):
            lines.append(line)
    return "\n".join(lines)


def normalize_lrc(lrc: str) -> str:
    """Return LRC text with unix line endings and no surrounding blank lines."""
    return lrc.replace("\r\n", "\n").replace("\r", "\n").strip()
