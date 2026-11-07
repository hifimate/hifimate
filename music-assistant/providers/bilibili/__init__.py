"""
Bilibili music provider for Music Assistant.

Plays the audio track of Bilibili videos: search, uploader (UP主) uploads, multi-part
videos as albums, favorite folders as playlists and the music center charts as
recommendations. Login (QR code) is optional; without it only public content is
available and the audio quality is capped at 192 kbps AAC.

All Bilibili traffic goes through :mod:`.bili_client`, the only module that knows the
underlying SDK.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
    UnplayableMediaError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    Playlist,
    RecommendationFolder,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import CONF_ENTRY_UNOFFICIAL_PROVIDER
from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from .bili_client import (
    BiliClient,
    BiliError,
    BiliLoginRequiredError,
    BiliNotFoundError,
    BiliRiskControlError,
    BiliVipRequiredError,
)
from .constants import (
    AUDIO_ID_132K,
    CONF_AUDIO_QUALITY,
    CONF_CLEAN_TITLES,
    CONF_COOKIE,
    CONF_MUSIC_ZONE_ONLY,
    MUSIC_ZONE_TID,
    QUALITY_HIGH,
    QUALITY_LOSSLESS,
    QUALITY_STANDARD,
    WEB_REFERER,
)
from .parsers import (
    ParseContext,
    parse_archive,
    parse_fav_folder,
    parse_fav_media,
    parse_fav_playlist_id,
    parse_hot_rank_item,
    parse_search_user,
    parse_search_video,
    parse_user_card,
    parse_view_album,
    parse_view_track,
    split_track_id,
    strip_html,
    view_pages,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.RECOMMENDATIONS,
}

# the search API returns 20 videos per page
SEARCH_PAGE_SIZE = 20
# fav folder pages are capped at 20 items; one MA playlist page spans several of them
FAV_PAGE_SIZE = 20
FAV_PAGES_PER_PLAYLIST_PAGE = 5
ARTIST_TOPTRACKS_LIMIT = 50
HOT_RANK_LIMIT = 50
# stream urls carry a `deadline` query parameter; this is the fallback lifetime
DEFAULT_STREAM_EXPIRATION = 3600

RECOMMENDATION_HOT = "hot_songs"
RECOMMENDATION_NEW = "new_releases"

CACHE_VERSION = "1"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return BilibiliProvider(mass, manifest, config, SUPPORTED_FEATURES)


class BilibiliProvider(MusicProvider):
    """Music provider streaming the audio of Bilibili videos."""

    _client: BiliClient | None = None
    _ctx: ParseContext
    _mid: int | None = None
    _uname: str = ""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the option entries; authentication runs in the setup flow."""
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(
                key=CONF_AUDIO_QUALITY,
                type=ConfigEntryType.STRING,
                default_value=QUALITY_LOSSLESS,
                options=[
                    ConfigValueOption(QUALITY_LOSSLESS),
                    ConfigValueOption(QUALITY_HIGH),
                    ConfigValueOption(QUALITY_STANDARD),
                ],
            ),
            ConfigEntry(
                key=CONF_MUSIC_ZONE_ONLY,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
            ),
            ConfigEntry(
                key=CONF_CLEAN_TITLES,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
            ),
        )

    async def handle_async_init(self) -> None:
        """Start the API client and validate the stored login (if any)."""
        cookie = str(self.get_setup_value(CONF_COOKIE) or "").strip()
        self._ctx = ParseContext(
            self.domain,
            self.instance_id,
            clean_titles=bool(self.config.get_value(CONF_CLEAN_TITLES, True)),
        )
        client = BiliClient(cookie, logger=self.logger)
        nav: dict[str, Any] = {}
        try:
            await client.start()
            if client.is_logged_in:
                nav = await client.nav()
        except BaseException as err:
            await client.close()
            if isinstance(err, BiliError):
                raise LoginFailed(f"Unable to connect to Bilibili: {err}") from err
            raise
        if client.is_logged_in and not nav.get("isLogin"):
            await client.close()
            raise LoginFailed("Bilibili login expired, please sign in again")
        if client.is_logged_in:
            self._mid = int(nav.get("mid") or 0) or client.mid
            self._uname = str(nav.get("uname") or "")
            self.logger.info("Signed in to Bilibili as %s (%s)", self._uname, self._mid)
        else:
            self.logger.info("Using Bilibili without login (public content only)")
        self._client = client
        # persist (new) device cookies: a stable device id draws less risk control
        if (exported := client.export_cookies()) != cookie:
            self._update_setup_data(CONF_COOKIE, exported)

    async def unload(self, is_removed: bool = False) -> None:
        """Close the API client."""
        if self._client is not None:
            await self._client.close()
            self._client = None
        await super().unload(is_removed)

    @property
    def client(self) -> BiliClient:
        """Return the started API client."""
        if self._client is None:
            raise LoginFailed("Bilibili client is not initialized")
        return self._client

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Search videos (as tracks) and uploaders (as artists).

        :param search_query: Search query.
        :param media_types: Requested media types.
        :param limit: Max results per media type.
        """
        result = SearchResults()
        tasks: dict[MediaType, asyncio.Task[Any]] = {}
        async with asyncio.TaskGroup() as group:
            if MediaType.TRACK in media_types:
                tasks[MediaType.TRACK] = group.create_task(self._search_tracks(search_query, limit))
            if MediaType.ARTIST in media_types:
                tasks[MediaType.ARTIST] = group.create_task(
                    self._search_artists(search_query, limit)
                )
        if task := tasks.get(MediaType.TRACK):
            result.tracks = task.result()
        if task := tasks.get(MediaType.ARTIST):
            result.artists = task.result()
        return result

    async def get_track(self, prov_track_id: str) -> Track:
        """Get a track (one page of a video) by id."""
        bvid, page = split_track_id(prov_track_id)
        view = await self._get_view(bvid)
        try:
            return parse_view_track(self._ctx, view, page)
        except InvalidDataError as err:
            raise MediaNotFoundError(str(err)) from err

    async def get_album(self, prov_album_id: str) -> Album:
        """Get a (multi-part) video as album by id."""
        bvid, _ = split_track_id(prov_album_id)
        return parse_view_album(self._ctx, await self._get_view(bvid))

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get the pages of a video as album tracks."""
        bvid, _ = split_track_id(prov_album_id)
        view = await self._get_view(bvid)
        tracks: list[Track] = []
        for index, page_obj in enumerate(view_pages(view), start=1):
            with suppress(InvalidDataError):
                track = parse_view_track(self._ctx, view, int(page_obj.get("page") or index))
                track.position = index
                tracks.append(track)
        return tracks

    @use_cache(3600 * 24, cache_checksum=CACHE_VERSION)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get an uploader (UP主) as artist by id."""
        mid = _mid(prov_artist_id)
        return parse_user_card(self._ctx, await self._api(lambda: self.client.user_card(mid)))

    @use_cache(3600 * 3, cache_checksum=CACHE_VERSION)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get the latest uploads of an uploader."""
        mid = _mid(prov_artist_id)
        artist = await self.get_artist(prov_artist_id)
        data = await self._api(
            lambda: self.client.user_archives(mid, page_size=ARTIST_TOPTRACKS_LIMIT)
        )
        tracks: list[Track] = []
        for item in data.get("archives") or []:
            if isinstance(item, dict):
                with suppress(InvalidDataError):
                    tracks.append(parse_archive(self._ctx, item, mid, artist.name))
        return tracks

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Yield the favorite folders of the signed in account."""
        if not self._mid:
            return
        folders = await self._api(lambda: self.client.fav_folders(self._mid or 0))
        for folder in folders:
            try:
                yield parse_fav_folder(self._ctx, folder, owner=self._uname or None)
            except InvalidDataError as err:
                self.report_skipped_sync_item(MediaType.PLAYLIST, None, err)

    @use_cache(3600, cache_checksum=CACHE_VERSION)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get a favorite folder as playlist by id."""
        folder_id = parse_fav_playlist_id(prov_playlist_id)
        data = await self._api(lambda: self.client.fav_folder(folder_id, page_size=1))
        info = data.get("info")
        if not isinstance(info, dict):
            raise MediaNotFoundError(f"Favorite folder {folder_id} not found")
        return parse_fav_folder(self._ctx, info)

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> Sequence[Track]:
        """
        Get one page of a favorite folder's playable videos.

        :param prov_playlist_id: Provider playlist id.
        :param page: 0-based page; each spans several Bilibili folder pages.
        """
        folder_id = parse_fav_playlist_id(prov_playlist_id)
        tracks: list[Track] = []
        first = page * FAV_PAGES_PER_PLAYLIST_PAGE + 1
        for fav_page in range(first, first + FAV_PAGES_PER_PLAYLIST_PAGE):
            data = await self._api(
                partial(self.client.fav_folder, folder_id, page=fav_page, page_size=FAV_PAGE_SIZE)
            )
            medias = data.get("medias") or []
            for index, media in enumerate(medias):
                if not isinstance(media, dict):
                    continue
                with suppress(InvalidDataError):
                    if track := parse_fav_media(self._ctx, media):
                        track.position = (fav_page - 1) * FAV_PAGE_SIZE + index + 1
                        tracks.append(track)
            if not data.get("has_more"):
                break
        return tracks

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Return the recommendation rows (music center charts)."""
        return [
            RecommendationFolder(
                item_id=RECOMMENDATION_HOT,
                provider=self.instance_id,
                name="Bilibili hot songs",
                translation_key=RECOMMENDATION_HOT,
                icon="mdi:fire",
            ),
            RecommendationFolder(
                item_id=RECOMMENDATION_NEW,
                provider=self.instance_id,
                name="Bilibili new releases",
                translation_key=RECOMMENDATION_NEW,
                icon="mdi:new-box",
            ),
        ]

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Return the items of one recommendation row.

        :param item_id: Row id as returned by get_recommendations.
        """
        items: UniqueList[MediaItemType | ItemMapping | BrowseFolder] = UniqueList()
        if item_id == RECOMMENDATION_HOT:
            items.extend(await self._get_hot_songs())
        elif item_id == RECOMMENDATION_NEW:
            items.extend(await self._get_new_releases())
        return items

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return the audio stream of a video page.

        :param item_id: Provider track id.
        :param media_type: Media type (only tracks are supported).
        """
        if media_type != MediaType.TRACK:
            raise UnsupportedFeaturedException(f"Unsupported media type {media_type}")
        bvid, page = split_track_id(item_id)
        view = await self._get_view(bvid)
        pages = view_pages(view)
        page_obj = next((p for p in pages if int(p.get("page") or 0) == page), None)
        if page_obj is None:
            if page == 1 and pages:
                page_obj = pages[0]
            else:
                raise MediaNotFoundError(f"Video {bvid} has no playable page {page}")
        cid = int(page_obj["cid"])
        play_info = await self._api(lambda: self.client.play_url(cid=cid, bvid=bvid))
        stream = select_audio_stream(
            play_info, str(self.config.get_value(CONF_AUDIO_QUALITY, QUALITY_LOSSLESS))
        )
        if stream is None:
            raise UnplayableMediaError(f"No audio stream available for {item_id}")
        url, audio_format, label = stream
        self.logger.debug("Streaming %s (cid %s) as %s", item_id, cid, label)
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            media_type=MediaType.TRACK,
            audio_format=audio_format,
            stream_type=StreamType.HTTP,
            path=url,
            # the CDN checks the Referer on some edges; ffmpeg must look like the web player
            extra_input_args=[
                "-headers",
                f"Referer: {WEB_REFERER}\r\n",
                "-user_agent",
                BROWSER_USER_AGENT,
            ],
            duration=int(page_obj.get("duration") or view.get("duration") or 0) or None,
            can_seek=True,
            allow_seek=True,
            expiration=stream_expiration(url),
        )

    async def _api[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """
        Run an API call, translating client errors into Music Assistant errors.

        :param call: Creates the API coroutine.
        """
        try:
            return await call()
        except BiliNotFoundError as err:
            raise MediaNotFoundError(str(err)) from err
        except BiliRiskControlError as err:
            raise ResourceTemporarilyUnavailable(
                "Bilibili rate limited the request, try again later", backoff_time=60
            ) from err
        except BiliLoginRequiredError as err:
            raise LoginFailed(f"Bilibili requires a login for this: {err}") from err
        except BiliVipRequiredError as err:
            raise UnplayableMediaError(f"Bilibili premium (大会员) required: {err}") from err
        except BiliError as err:
            raise ResourceTemporarilyUnavailable(str(err)) from err

    async def _search_tracks(self, query: str, limit: int) -> list[Track]:
        """Search videos, paging until enough results are collected."""
        tid = MUSIC_ZONE_TID if self.config.get_value(CONF_MUSIC_ZONE_ONLY, True) else 0
        tracks: list[Track] = []
        page = 1
        while len(tracks) < limit:
            try:
                data = await self._api(
                    partial(self.client.search_videos, query, page=page, tid=tid)
                )
            except ResourceTemporarilyUnavailable as err:
                self.logger.warning("Bilibili video search failed: %s", err)
                break
            items = data.get("result") or []
            for item in items:
                if isinstance(item, dict) and item.get("type", "video") == "video":
                    with suppress(InvalidDataError):
                        tracks.append(parse_search_video(self._ctx, item))
            if len(items) < SEARCH_PAGE_SIZE or page >= int(data.get("numPages") or 1):
                break
            page += 1
        return tracks[:limit]

    async def _search_artists(self, query: str, limit: int) -> list[Artist]:
        """Search uploaders."""
        try:
            data = await self._api(lambda: self.client.search_users(query))
        except ResourceTemporarilyUnavailable as err:
            self.logger.warning("Bilibili user search failed: %s", err)
            return []
        artists: list[Artist] = []
        for item in data.get("result") or []:
            if isinstance(item, dict):
                with suppress(InvalidDataError):
                    artists.append(parse_search_user(self._ctx, item))
        return artists[:limit]

    @use_cache(3600 * 6, cache_checksum=CACHE_VERSION)
    async def _get_view(self, bvid: str) -> dict[str, Any]:
        """
        Return (cached) video details.

        :param bvid: BV id of the video.
        """
        return await self._api(lambda: self.client.view(bvid=bvid))

    @use_cache(1800, cache_checksum=CACHE_VERSION)
    async def _get_hot_songs(self) -> list[Track]:
        """Return the music center hot chart as tracks."""
        rows = await self._api(lambda: self.client.music_hot_rank(page_size=HOT_RANK_LIMIT))
        tracks: list[Track] = []
        for row in rows:
            with suppress(InvalidDataError):
                tracks.append(parse_hot_rank_item(self._ctx, row))
        return tracks

    @use_cache(3600, cache_checksum=CACHE_VERSION)
    async def _get_new_releases(self) -> list[Track]:
        """
        Return the music center new releases as tracks.

        The listing does not include the uploader, so each video is looked up; the
        curated song title replaces the video title.
        """
        rows = await self._api(lambda: self.client.music_new_releases())

        async def _resolve(row: dict[str, Any]) -> Track | None:
            bvid = str(row.get("bvid") or "")
            if not bvid:
                return None
            try:
                track = await self.get_track(bvid)
            except MediaNotFoundError, ResourceTemporarilyUnavailable, InvalidDataError:
                return None
            if title := strip_html(row.get("music_title")):
                track.name = title
            return track

        resolved = await asyncio.gather(*(_resolve(row) for row in rows))
        return [track for track in resolved if track is not None]


# Chrome UA sent by ffmpeg for audio downloads
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def select_audio_stream(
    play_info: dict[str, Any], quality: str
) -> tuple[str, AudioFormat, str] | None:
    """
    Pick the audio stream for the configured quality.

    :param play_info: Play url payload (with ``dash``).
    :param quality: One of the QUALITY_* option values.
    :return: ``(url, audio_format, label)`` or None when there is no audio stream.
    """
    dash = play_info.get("dash")
    if not isinstance(dash, dict):
        return None
    if quality == QUALITY_LOSSLESS:
        flac = dash.get("flac")
        flac_audio = flac.get("audio") if isinstance(flac, dict) else None
        if isinstance(flac_audio, dict) and (url := _stream_url(flac_audio)):
            # sample rate/bit depth vary per upload: leave the format to be probed
            return url, AudioFormat(content_type=ContentType.UNKNOWN), "FLAC"
    streams = [s for s in dash.get("audio") or [] if isinstance(s, dict) and _stream_url(s)]
    if not streams:
        return None
    if quality == QUALITY_STANDARD:
        chosen = next(
            (s for s in streams if int(s.get("id") or 0) == AUDIO_ID_132K),
            min(streams, key=lambda s: int(s.get("bandwidth") or 0)),
        )
    else:
        chosen = max(streams, key=lambda s: int(s.get("bandwidth") or 0))
    bit_rate = int(chosen.get("bandwidth") or 0) // 1000
    audio_format = AudioFormat(
        content_type=ContentType.M4A,
        codec_type=ContentType.AAC,
        sample_rate=48000,
        bit_depth=16,
        channels=2,
        bit_rate=bit_rate or None,
    )
    return _stream_url(chosen), audio_format, f"AAC {bit_rate}kbps"


def _stream_url(stream: dict[str, Any]) -> str:
    """Return the primary url of a DASH stream (raw or SDK field naming)."""
    return str(stream.get("baseUrl") or stream.get("base_url") or "")


def stream_expiration(url: str) -> int:
    """
    Return the remaining lifetime (seconds) of a signed CDN url.

    :param url: Stream url with a ``deadline`` unix timestamp parameter.
    """
    with suppress(TypeError, ValueError):
        deadline = int(parse_qs(urlparse(url).query).get("deadline", [""])[0])
        return max(60, deadline - int(time.time()) - 60)
    return DEFAULT_STREAM_EXPIRATION


def _mid(prov_artist_id: str) -> int:
    """
    Return the user id of a provider artist id.

    :param prov_artist_id: Provider artist id.
    """
    try:
        mid = int(prov_artist_id)
    except ValueError as err:
        raise MediaNotFoundError(f"Invalid Bilibili user id: {prov_artist_id}") from err
    if mid <= 0:
        raise MediaNotFoundError(f"Invalid Bilibili user id: {prov_artist_id}")
    return mid
