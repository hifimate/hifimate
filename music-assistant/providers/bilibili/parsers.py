"""Parsers turning (plain dict) Bilibili API data into Music Assistant media items."""

from __future__ import annotations

import html
import re
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any

from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemImage,
    Playlist,
    ProviderMapping,
    Track,
    UniqueList,
)

from .constants import FAV_MEDIA_TYPE_VIDEO, PLAYLIST_PREFIX_FAV

_TRACK_ID_PATTERN = re.compile(r"^(BV[0-9A-Za-z]{10})(?:_p(\d{1,4}))?$")
_HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
_BOOK_TITLE_PATTERN = re.compile(r"《([^《》]+)》")
# leading 【Hi-Res无损】/[4K]/「MV」 decorations and trailing 【...】 ones; a trailing
# [...] is kept, it usually carries meaning ("[Taylor's Version]")
_DECORATION_PATTERN = re.compile(
    r"^\s*(?:【[^】]*】|\[[^\]]*\]|「[^」]*」|\s|[|\uff5c/\uff0f])+|(?:【[^】]*】|\s)+$"
)
_INVALID_FAV_TITLE = "已失效视频"


class ParseContext:
    """What the parsers need to know about the provider instance."""

    def __init__(self, domain: str, instance_id: str, *, clean_titles: bool = True) -> None:
        """
        Initialize the context.

        :param domain: Provider domain.
        :param instance_id: Provider instance id.
        :param clean_titles: Strip decorations from video titles.
        """
        self.domain = domain
        self.instance_id = instance_id
        self.clean_titles = clean_titles

    def mapping(self, item_id: str, url: str, **kwargs: Any) -> ProviderMapping:
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

    def images(self, url: Any, *, square: bool = False) -> UniqueList[MediaItemImage]:
        """
        Create the image list for a Bilibili image url.

        Bilibili's image CDN rejects foreign Referers (HTTP 403), so the images are
        marked not remotely accessible and are served through the MA image proxy.

        :param url: Image url (may be protocol-relative or http).
        :param square: Request a center-cropped square variant (video covers are 16:9).
        """
        image_url = normalize_image_url(url, square=square)
        if not image_url:
            return UniqueList()
        return UniqueList(
            [
                MediaItemImage(
                    type=ImageType.THUMB,
                    path=image_url,
                    provider=self.instance_id,
                    remotely_accessible=False,
                )
            ]
        )

    def title(self, raw_title: Any) -> str:
        """
        Return a display title for a video title.

        :param raw_title: Title as returned by the API (may contain search highlight html).
        """
        title = strip_html(raw_title)
        return clean_title(title) if self.clean_titles else title


# ---------------------------------------------------------------- helpers


def make_track_id(bvid: str, page: int = 1) -> str:
    """
    Build the provider track id of a video page.

    :param bvid: BV id of the video.
    :param page: 1-based page (P) number.
    """
    return bvid if page <= 1 else f"{bvid}_p{page}"


def split_track_id(track_id: str) -> tuple[str, int]:
    """
    Split a provider track id into ``(bvid, page)``.

    :param track_id: Provider track id.
    """
    match = _TRACK_ID_PATTERN.match(track_id.strip())
    if not match:
        raise InvalidDataError(f"Invalid Bilibili track id: {track_id}")
    return match.group(1), int(match.group(2) or 1)


def strip_html(value: Any) -> str:
    """
    Remove html tags (search keyword highlights) and entities from a string.

    :param value: The raw string.
    """
    if not isinstance(value, str):
        return ""
    return " ".join(html.unescape(_HTML_TAG_PATTERN.sub("", value)).split())


def clean_title(title: str) -> str:
    """
    Strip common video title decorations, keeping the song name when obvious.

    ``【Hi-Res无损音质】|《晴天》- 周杰伦`` becomes ``晴天``. When the title does not
    contain exactly one 《》 pair only the surrounding 【】/[] decorations are removed.
    Falls back to the original title rather than returning an empty one.

    :param title: Title without html.
    """
    book_titles = _BOOK_TITLE_PATTERN.findall(title)
    if len(book_titles) == 1 and book_titles[0].strip():
        return str(book_titles[0]).strip()
    cleaned = _DECORATION_PATTERN.sub("", title).strip(" -|\uff5c")
    return cleaned or title


def parse_duration(value: Any) -> int:
    """
    Parse a duration given as seconds or as ``m:ss`` / ``h:mm:ss`` text.

    :param value: The duration value.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        total = 0
        try:
            for part in text.split(":"):
                total = total * 60 + int(part)
        except ValueError:
            return 0
        return total
    return 0


def normalize_image_url(url: Any, *, square: bool = False, size: int = 500) -> str:
    """
    Normalize a Bilibili image url to https, optionally as a square crop.

    :param url: The image url.
    :param square: Append the CDN's center-crop parameters.
    :param size: Edge length of the square variant.
    """
    if not isinstance(url, str) or not url.strip():
        return ""
    image_url = url.strip()
    if image_url.startswith("//"):
        image_url = f"https:{image_url}"
    elif image_url.startswith("http://"):
        image_url = f"https://{image_url[7:]}"
    if square and "hdslb.com/bfs/" in image_url and "@" not in image_url:
        image_url = f"{image_url}@{size}w_{size}h_1c.jpg"
    return image_url


def _int(value: Any) -> int:
    """Return value as int (0 when not convertible)."""
    if isinstance(value, bool):
        return 0
    with suppress(TypeError, ValueError):
        return int(value)
    return 0


def _year(timestamp: Any) -> int | None:
    """Return the year of a unix timestamp."""
    ts = _int(timestamp)
    if ts <= 0:
        return None
    with suppress(OSError, OverflowError, ValueError):
        return datetime.fromtimestamp(ts, tz=UTC).year
    return None


def video_url(bvid: str, page: int = 1) -> str:
    """Return the web url of a video page."""
    return f"https://www.bilibili.com/video/{bvid}" + (f"?p={page}" if page > 1 else "")


def space_url(mid: int | str) -> str:
    """Return the web url of a user's space."""
    return f"https://space.bilibili.com/{mid}"


# ---------------------------------------------------------------- parsers


def _base_track(
    ctx: ParseContext,
    *,
    bvid: str,
    page: int,
    name: str,
    duration: int,
    owner_mid: Any,
    owner_name: Any,
    cover: Any,
) -> Track:
    """Build a Track with the fields every source shares."""
    track_id = make_track_id(bvid, page)
    track = Track(
        item_id=track_id,
        provider=ctx.instance_id,
        name=name or bvid,
        duration=duration,
        provider_mappings={ctx.mapping(track_id, video_url(bvid, page))},
    )
    if (mid := _int(owner_mid)) > 0:
        track.artists = UniqueList(
            [ctx.item_mapping(MediaType.ARTIST, str(mid), strip_html(owner_name) or str(mid))]
        )
    track.metadata.images = ctx.images(cover, square=True)
    return track


def parse_search_video(ctx: ParseContext, item: dict[str, Any]) -> Track:
    """
    Parse a video search result.

    :param ctx: Parse context.
    :param item: Entry of the search ``result`` list.
    """
    bvid = str(item.get("bvid") or "")
    if not bvid:
        raise InvalidDataError("Search result without bvid")
    return _base_track(
        ctx,
        bvid=bvid,
        page=1,
        name=ctx.title(item.get("title")),
        duration=parse_duration(item.get("duration")),
        owner_mid=item.get("mid"),
        owner_name=item.get("author"),
        cover=item.get("pic"),
    )


def view_pages(view: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Return the playable pages of a video.

    :param view: Video details (``/x/web-interface/view`` payload).
    """
    pages = view.get("pages") or []
    return [p for p in pages if isinstance(p, dict) and _int(p.get("cid")) > 0]


def parse_view_track(ctx: ParseContext, view: dict[str, Any], page: int = 1) -> Track:
    """
    Parse one page of a video into a track.

    Pages of a multi-part video are linked to the album representing the video.

    :param ctx: Parse context.
    :param view: Video details.
    :param page: 1-based page number.
    """
    bvid = str(view.get("bvid") or "")
    if not bvid:
        raise InvalidDataError("Video details without bvid")
    pages = view_pages(view)
    owner = view.get("owner") or {}
    video_title = ctx.title(view.get("title"))
    page_obj = next((p for p in pages if _int(p.get("page")) == page), None)
    if page_obj is None and page == 1 and pages:
        page_obj = pages[0]
    if page_obj is None:
        raise InvalidDataError(f"Video {bvid} has no playable page {page}")
    multi_page = len(pages) > 1
    name = ctx.title(page_obj.get("part")) if multi_page else video_title
    track = _base_track(
        ctx,
        bvid=bvid,
        page=page,
        name=name or video_title,
        duration=parse_duration(page_obj.get("duration") or view.get("duration")),
        owner_mid=owner.get("mid"),
        owner_name=owner.get("name"),
        cover=page_obj.get("first_frame")
        if multi_page and page_obj.get("first_frame")
        else view.get("pic"),
    )
    if multi_page:
        track.album = ctx.item_mapping(MediaType.ALBUM, bvid, video_title)
        track.disc_number = 1
        track.track_number = _int(page_obj.get("page")) or page
    if desc := view.get("desc"):
        track.metadata.description = str(desc).strip()
    if stat := view.get("stat"):
        track.metadata.popularity = _popularity(stat)
    return track


def parse_view_album(ctx: ParseContext, view: dict[str, Any]) -> Album:
    """
    Parse a (multi-part) video into an album whose tracks are its pages.

    :param ctx: Parse context.
    :param view: Video details.
    """
    bvid = str(view.get("bvid") or "")
    if not bvid:
        raise InvalidDataError("Video details without bvid")
    owner = view.get("owner") or {}
    album = Album(
        item_id=bvid,
        provider=ctx.instance_id,
        name=ctx.title(view.get("title")) or bvid,
        provider_mappings={ctx.mapping(bvid, video_url(bvid))},
    )
    if (mid := _int(owner.get("mid"))) > 0:
        album.artists = UniqueList(
            [ctx.item_mapping(MediaType.ARTIST, str(mid), str(owner.get("name") or mid))]
        )
    album.metadata.images = ctx.images(view.get("pic"), square=True)
    album.year = _year(view.get("pubdate"))
    if desc := view.get("desc"):
        album.metadata.description = str(desc).strip()
    return album


def parse_archive(
    ctx: ParseContext, item: dict[str, Any], owner_mid: int, owner_name: str
) -> Track:
    """
    Parse an entry of a user's upload listing.

    :param ctx: Parse context.
    :param item: Entry of ``archives``.
    :param owner_mid: Uploader id (the listing does not repeat the name).
    :param owner_name: Uploader name.
    """
    bvid = str(item.get("bvid") or "")
    if not bvid:
        raise InvalidDataError("Archive without bvid")
    return _base_track(
        ctx,
        bvid=bvid,
        page=1,
        name=ctx.title(item.get("title")),
        duration=parse_duration(item.get("duration")),
        owner_mid=item.get("upMid") or owner_mid,
        owner_name=owner_name,
        cover=item.get("pic"),
    )


def parse_fav_media(ctx: ParseContext, media: dict[str, Any]) -> Track | None:
    """
    Parse a favorite folder entry; returns None for entries that are not playable.

    :param ctx: Parse context.
    :param media: Entry of ``medias``.
    """
    media_type = _int(media.get("type", media.get("type_name")))
    bvid = str(media.get("bvid") or media.get("bv_id") or "")
    title = strip_html(media.get("title"))
    if media_type != FAV_MEDIA_TYPE_VIDEO or not bvid or title == _INVALID_FAV_TITLE:
        return None
    upper = media.get("upper") or {}
    return _base_track(
        ctx,
        bvid=bvid,
        page=1,
        name=ctx.title(title),
        duration=parse_duration(media.get("duration")),
        owner_mid=upper.get("mid"),
        owner_name=upper.get("name"),
        cover=media.get("cover"),
    )


def parse_hot_rank_item(ctx: ParseContext, item: dict[str, Any]) -> Track:
    """
    Parse an entry of the music center hot chart.

    The top-level bvid of a chart entry is a music-metadata id; the playable video is
    ``related_archive``. The song title is taken from the curated music metadata.

    :param ctx: Parse context.
    :param item: Chart entry.
    """
    archive = item.get("related_archive") or {}
    bvid = str(archive.get("bvid") or item.get("bvid") or "")
    if not bvid:
        raise InvalidDataError("Chart entry without playable video")
    name = strip_html(item.get("music_title")) or ctx.title(archive.get("title"))
    return _base_track(
        ctx,
        bvid=bvid,
        page=1,
        name=name,
        duration=parse_duration(archive.get("duration")),
        owner_mid=archive.get("uid"),
        owner_name=archive.get("username") or item.get("author"),
        cover=item.get("cover") or archive.get("cover"),
    )


def parse_user_card(ctx: ParseContext, data: dict[str, Any]) -> Artist:
    """
    Parse a user card into an artist.

    :param ctx: Parse context.
    :param data: ``/x/web-interface/card`` payload.
    """
    card: dict[str, Any] = data["card"] if isinstance(data.get("card"), dict) else data
    return _artist(ctx, card.get("mid"), card.get("name"), card.get("face"), card.get("sign"))


def parse_search_user(ctx: ParseContext, item: dict[str, Any]) -> Artist:
    """
    Parse an uploader search result into an artist.

    :param ctx: Parse context.
    :param item: Entry of the search ``result`` list.
    """
    return _artist(ctx, item.get("mid"), item.get("uname"), item.get("upic"), item.get("usign"))


def _artist(ctx: ParseContext, mid: Any, name: Any, face: Any, sign: Any) -> Artist:
    """Build an artist (uploader)."""
    artist_id = str(_int(mid))
    if artist_id == "0":
        raise InvalidDataError("User without mid")
    artist = Artist(
        item_id=artist_id,
        provider=ctx.instance_id,
        name=strip_html(name) or artist_id,
        provider_mappings={ctx.mapping(artist_id, space_url(artist_id))},
    )
    artist.metadata.images = ctx.images(face)
    if isinstance(sign, str) and sign.strip():
        artist.metadata.description = sign.strip()
    return artist


def parse_fav_folder(
    ctx: ParseContext, folder: dict[str, Any], *, owner: str | None = None
) -> Playlist:
    """
    Parse a favorite folder into a playlist.

    :param ctx: Parse context.
    :param folder: Folder entry (list item or ``info`` of the detail payload).
    :param owner: Display name of the folder owner.
    """
    folder_id = _int(folder.get("id"))
    if folder_id <= 0:
        raise InvalidDataError("Favorite folder without id")
    item_id = f"{PLAYLIST_PREFIX_FAV}{folder_id}"
    upper = folder.get("upper") or {}
    playlist = Playlist(
        item_id=item_id,
        provider=ctx.instance_id,
        name=strip_html(folder.get("title")) or item_id,
        owner=owner or str(upper.get("name") or ""),
        is_editable=False,
        provider_mappings={ctx.mapping(item_id, f"https://www.bilibili.com/list/ml{folder_id}")},
    )
    playlist.metadata.images = ctx.images(folder.get("cover"), square=True)
    if intro := folder.get("intro"):
        playlist.metadata.description = str(intro).strip()
    return playlist


def parse_fav_playlist_id(item_id: str) -> int:
    """
    Return the folder id of a favorite folder playlist id.

    :param item_id: Provider playlist id.
    """
    if item_id.startswith(PLAYLIST_PREFIX_FAV):
        folder_id = _int(item_id.removeprefix(PLAYLIST_PREFIX_FAV))
        if folder_id > 0:
            return folder_id
    raise InvalidDataError(f"Invalid Bilibili playlist id: {item_id}")


def _popularity(stat: Any) -> int | None:
    """Map play count to a 0-100 popularity score (log scale, 10M plays = 100)."""
    if not isinstance(stat, dict):
        return None
    views = _int(stat.get("view"))
    if views <= 0:
        return None
    return min(100, int(len(str(views)) / 8 * 100))
