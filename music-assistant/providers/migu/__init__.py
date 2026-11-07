"""
Migu Music (咪咕音乐) provider for Music Assistant.

Talks to a local migu-api-enhanced service
(https://github.com/Domdkw/miguMusic-api-enhanced), the way the NetEase providers talk
to a NeteaseCloudMusicApi service. Catalog search, albums, singers, playlists, charts
and lyrics work without an account; a signed in (VIP) account adds VIP songs, lossless
and hi-res audio, the user's playlists and daily recommendations.

All service traffic goes through :mod:`.migu_client`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import (
    ConfigEntryType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    ResourceTemporarilyUnavailable,
    SetupFailedError,
    UnplayableMediaError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
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

from .constants import (
    CONF_API_BASE_URL,
    CONF_AUDIO_QUALITY,
    CONF_PACM,
    DEFAULT_API_BASE_URL,
    PLAYLIST_PREFIX_MINE,
    PLAYLIST_PREFIX_RANK,
    QUALITY_HIGH,
    QUALITY_HIRES,
    QUALITY_LOSSLESS,
    QUALITY_MAX_TONE,
    QUALITY_STANDARD,
    TONE_PQ,
    TONES,
)
from .migu_client import (
    MiguClient,
    MiguConnectionError,
    MiguError,
    MiguLoginRequiredError,
    MiguNotFoundError,
)
from .parsers import (
    ParseContext,
    has_timestamps,
    iter_playlists,
    iter_ranks,
    iter_search_albums,
    iter_search_singers,
    iter_songs,
    lrc_to_plain,
    normalize_lrc,
    parse_album,
    parse_digital_album,
    parse_playlist,
    parse_rank_playlist,
    parse_singer,
    parse_singer_album_item,
    parse_song,
    song_copyright_id,
    song_tones,
    split_album_id,
    tone_audio_format,
    tone_for_url,
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
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.RECOMMENDATIONS,
    ProviderFeature.LYRICS,
}

# Migu returns 20 search results per page
SEARCH_PAGE_SIZE = 20
MAX_SEARCH_PAGES = 3
# album songs come in pages; singer albums 10 per page
MAX_ALBUM_PAGES = 10
MAX_ARTIST_ALBUM_PAGES = 10
USER_PAGE_SIZE = 50
# public playlists come 20 songs per page
PLAYLIST_PAGE_SIZE = 20
MAX_USER_PAGES = 20
# signed CDN urls carry no readable expiry; refresh well before any plausible one
STREAM_EXPIRATION = 600
# Migu slides the login forward on every signed-in call (a new pacmtoken comes
# back each time and is saved), but drops it after a few idle hours: observed
# fine after 1.4 h idle, signed out after 3.2 h. A cheap signed-in call keeps it.
KEEPALIVE_SECONDS = 40 * 60

RECOMMENDATION_CHARTS = "charts"
RECOMMENDATION_DAILY = "daily"

CACHE_VERSION = "1"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider instance with given configuration."""
    return MiguProvider(mass, manifest, config, SUPPORTED_FEATURES)


class MiguProvider(MusicProvider):
    """Music provider for Migu Music, through a local migu-api-enhanced service."""

    _client: MiguClient | None = None
    _ctx: ParseContext
    _user_name: str = ""
    _keepalive: asyncio.Task[None] | None = None
    # own playlists the user endpoint returned nothing for (read via the public API)
    _public_own_playlists: set[str]

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return the option entries; the service address and login live in the setup flow."""
        return (
            CONF_ENTRY_UNOFFICIAL_PROVIDER,
            ConfigEntry(
                key=CONF_AUDIO_QUALITY,
                type=ConfigEntryType.STRING,
                default_value=QUALITY_HIRES,
                options=[
                    ConfigValueOption(QUALITY_HIRES),
                    ConfigValueOption(QUALITY_LOSSLESS),
                    ConfigValueOption(QUALITY_HIGH),
                    ConfigValueOption(QUALITY_STANDARD),
                ],
            ),
        )

    async def handle_async_init(self) -> None:
        """Connect to the service and validate the stored login (if any)."""
        base_url = str(self.get_setup_value(CONF_API_BASE_URL) or DEFAULT_API_BASE_URL)
        token = str(self.get_setup_value(CONF_PACM) or "").strip()
        self._ctx = ParseContext(self.domain, self.instance_id)
        self._public_own_playlists = set()
        client = MiguClient(
            self.mass.http_session,
            base_url,
            token,
            on_token_refresh=self._on_token_refresh,
            logger=self.logger,
        )
        try:
            await client.version()
        except MiguError as err:
            # a MusicAssistantError (not LoginFailed): MA retries the load later
            raise SetupFailedError(f"Migu API service not available: {err}") from err
        if client.is_logged_in:
            try:
                info = await client.user_info()
            except MiguLoginRequiredError as err:
                raise LoginFailed("Migu login expired, please sign in again") from err
            except MiguError as err:
                raise SetupFailedError(f"Unable to check the Migu login: {err}") from err
            self._user_name = _user_display_name(info)
            self.logger.info("Signed in to Migu as %s", self._user_name or "(unknown)")
            self._keepalive = asyncio.create_task(self._keep_login_alive())
        else:
            self.logger.info("Using Migu without login (free songs, standard quality only)")
        self._client = client

    async def unload(self, is_removed: bool = False) -> None:
        """Stop the login keep-alive."""
        if self._keepalive is not None:
            self._keepalive.cancel()
            with suppress(asyncio.CancelledError):
                await self._keepalive
            self._keepalive = None
        await super().unload(is_removed)

    async def _keep_login_alive(self) -> None:
        """Renew the login while nobody uses Migu (see KEEPALIVE_SECONDS)."""
        while True:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            try:
                await self.client.user_info()        # rotates and saves the pacmtoken
            except MiguLoginRequiredError:
                self.logger.warning("Migu login expired, please sign in again")
                return
            except MiguError as err:                  # service briefly down: try next round
                self.logger.debug("Migu keep-alive failed: %s", err)

    @property
    def client(self) -> MiguClient:
        """Return the API client."""
        if self._client is None:
            raise LoginFailed("Migu client is not initialized")
        return self._client

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Search songs, albums, singers and playlists.

        :param search_query: Search query.
        :param media_types: Requested media types.
        :param limit: Max results per media type.
        """
        result = SearchResults()
        kinds = {
            MediaType.TRACK: "song",
            MediaType.ALBUM: "album",
            MediaType.ARTIST: "singer",
            MediaType.PLAYLIST: "playlist",
        }
        tasks: dict[MediaType, asyncio.Task[list[Any]]] = {}
        async with asyncio.TaskGroup() as group:
            for media_type, kind in kinds.items():
                if media_type in media_types:
                    tasks[media_type] = group.create_task(
                        self._search_kind(kind, search_query, limit)
                    )
        if task := tasks.get(MediaType.TRACK):
            result.tracks = task.result()
        if task := tasks.get(MediaType.ALBUM):
            result.albums = task.result()
        if task := tasks.get(MediaType.ARTIST):
            result.artists = task.result()
        if task := tasks.get(MediaType.PLAYLIST):
            result.playlists = task.result()
        return result

    async def get_track(self, prov_track_id: str) -> Track:
        """Get a song (with lyrics) by content id."""
        song = await self._get_song(prov_track_id)
        try:
            track = parse_song(self._ctx, song)
        except InvalidDataError as err:
            raise MediaNotFoundError(str(err)) from err
        with suppress(MediaNotFoundError, ResourceTemporarilyUnavailable, LoginFailed):
            lrc = normalize_lrc(await self._get_lyric(prov_track_id))
            if lrc and has_timestamps(lrc):
                track.metadata.lrc_lyrics = lrc
                track.metadata.lyrics = lrc_to_plain(lrc) or lrc
            elif lrc:
                track.metadata.lyrics = lrc
        return track

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return the stream of a song at the best tone allowed by the options and account.

        :param item_id: Song content id.
        :param media_type: Media type (only tracks are supported).
        """
        if media_type != MediaType.TRACK:
            raise UnsupportedFeaturedException(f"Unsupported media type {media_type}")
        song = await self._get_song(item_id)
        copyright_id = song_copyright_id(song)
        quality = str(self.config.get_value(CONF_AUDIO_QUALITY, QUALITY_HIRES))
        reason = ""
        for tone in tone_ladder(song_tones(song), quality):
            info = await self._api(partial(self.client.play_url, item_id, copyright_id, tone))
            url = str(info.get("url") or "")
            if not url.startswith("http"):
                reason = _unplayable_reason(info) or reason
                self.logger.debug("Migu %s not playable at %s: %s", item_id, tone, reason)
                continue
            actual = str(info.get("audioFormatType") or "").upper()
            actual = actual if actual in TONES else tone_for_url(url, tone)
            self.logger.debug("Streaming Migu %s at %s (asked %s)", item_id, actual, tone)
            return StreamDetails(
                provider=self.instance_id,
                item_id=item_id,
                media_type=MediaType.TRACK,
                audio_format=tone_audio_format(actual),
                stream_type=StreamType.HTTP,
                path=url,
                duration=int(song.get("duration") or 0) or None,
                can_seek=True,
                allow_seek=True,
                expiration=STREAM_EXPIRATION,
            )
        if not self.client.is_logged_in:
            reason = reason or "a Migu account is required"
        raise UnplayableMediaError(f"Migu song {item_id} is not playable: {reason or 'no url'}")

    @use_cache(3600 * 24, cache_checksum=CACHE_VERSION)
    async def get_album(self, prov_album_id: str) -> Album:
        """Get an album or digital album by id."""
        migu_id, digital = split_album_id(prov_album_id)
        try:
            if digital:
                data = await self._api(lambda: self.client.digital_album_info(migu_id))
                return parse_digital_album(self._ctx, data)
            data = await self._api(lambda: self.client.album_info(migu_id))
            return parse_album(self._ctx, data)
        except InvalidDataError as err:
            raise MediaNotFoundError(f"Migu album {prov_album_id} not found") from err

    @use_cache(3600 * 6, cache_checksum=CACHE_VERSION)
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get the songs of an album."""
        migu_id, digital = split_album_id(prov_album_id)
        songs: list[dict[str, Any]] = []
        if digital:
            data = await self._api(lambda: self.client.digital_album_songs(migu_id))
            songs = list(iter_songs(data.get("songList") or []))
        else:
            for page in range(1, MAX_ALBUM_PAGES + 1):
                data = await self._api(partial(self.client.album_songs, migu_id, page))
                page_songs = list(iter_songs(data.get("songList") or []))
                songs.extend(page_songs)
                if not page_songs or not data.get("hasNext"):
                    break
        tracks: list[Track] = []
        for index, song in enumerate(songs, start=1):
            with suppress(InvalidDataError):
                track = parse_song(self._ctx, song)
                track.position = index
                track.disc_number = track.disc_number or 1
                tracks.append(track)
        return tracks

    @use_cache(3600 * 24, cache_checksum=CACHE_VERSION)
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get a singer by id (or by ``name:`` for entries that only carried a name)."""
        if prov_artist_id.startswith("name:"):
            return await self._find_artist_by_name(prov_artist_id[5:])
        data = await self._api(lambda: self.client.singer_info(prov_artist_id))
        try:
            return parse_singer(self._ctx, data)
        except InvalidDataError as err:
            raise MediaNotFoundError(f"Migu singer {prov_artist_id} not found") from err

    @use_cache(3600 * 6, cache_checksum=CACHE_VERSION)
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get a singer's top songs (first page of the singer's songs)."""
        singer_id = await self._real_singer_id(prov_artist_id)
        data = await self._api(partial(self.client.singer_songs, singer_id, 1))
        tracks: list[Track] = []
        for song in iter_songs(data):
            with suppress(InvalidDataError):
                tracks.append(parse_song(self._ctx, song))
        return tracks

    @use_cache(3600 * 24, cache_checksum=CACHE_VERSION)
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a singer's albums (digital albums included)."""
        singer_id = await self._real_singer_id(prov_artist_id)
        artist = await self.get_artist(prov_artist_id)
        albums: list[Album] = []
        for page in range(1, MAX_ARTIST_ALBUM_PAGES + 1):
            data = await self._api(partial(self.client.singer_albums, singer_id, page))
            entries = [e for e in data.get("contents") or [] if isinstance(e, dict)]
            for entry in entries:
                with suppress(InvalidDataError):
                    albums.append(parse_singer_album_item(self._ctx, entry, artist.name))
            header = data.get("header")
            if not entries or not isinstance(header, dict) or not header.get("hasNext"):
                break
        return albums

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Yield the playlists the signed in user created or collected."""
        if not self.client.is_logged_in:
            return
        seen: set[str] = set()
        for owned, fetch in (
            (True, self.client.user_playlists),
            (False, self.client.user_collected_playlists),
        ):
            try:
                items = await self._collect_user_pages(fetch, iter_playlists)
            except (ResourceTemporarilyUnavailable, LoginFailed) as err:
                self.logger.warning("Unable to list Migu playlists: %s", err)
                continue
            for item in items:
                try:
                    playlist = parse_playlist(
                        self._ctx, item, owned=owned, owner=self._user_name if owned else None
                    )
                except InvalidDataError as err:
                    self.report_skipped_sync_item(MediaType.PLAYLIST, None, err)
                    continue
                if playlist.item_id not in seen:
                    seen.add(playlist.item_id)
                    yield playlist

    @use_cache(3600, cache_checksum=CACHE_VERSION)
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get a playlist, chart or own playlist by id."""
        if prov_playlist_id.startswith(PLAYLIST_PREFIX_RANK):
            rank_id = prov_playlist_id[len(PLAYLIST_PREFIX_RANK) :]
            for rank in await self._get_ranks():
                if str(rank.get("rankId")) == rank_id:
                    return parse_rank_playlist(self._ctx, rank)
            raise MediaNotFoundError(f"Migu chart {rank_id} not found")
        if prov_playlist_id.startswith(PLAYLIST_PREFIX_MINE):
            async for playlist in self.get_library_playlists():
                if playlist.item_id == prov_playlist_id:
                    return playlist
            raise MediaNotFoundError(f"Migu playlist {prov_playlist_id} not found")
        data = await self._api(lambda: self.client.playlist_info(prov_playlist_id))
        try:
            return parse_playlist(self._ctx, data)
        except InvalidDataError as err:
            raise MediaNotFoundError(f"Migu playlist {prov_playlist_id} not found") from err

    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> Sequence[Track]:
        """
        Get one page of a playlist's songs.

        :param prov_playlist_id: Provider playlist id.
        :param page: 0-based page.
        """
        if prov_playlist_id.startswith(PLAYLIST_PREFIX_RANK):
            if page > 0:
                return []
            songs = await self._get_rank_songs(prov_playlist_id[len(PLAYLIST_PREFIX_RANK) :])
            offset = 0
        elif prov_playlist_id.startswith(PLAYLIST_PREFIX_MINE):
            songs, page_size = await self._get_own_playlist_songs(
                prov_playlist_id[len(PLAYLIST_PREFIX_MINE) :], page
            )
            offset = page * page_size
        else:
            songs = await self._get_public_playlist_songs(prov_playlist_id, page)
            offset = page * PLAYLIST_PAGE_SIZE
        tracks: list[Track] = []
        for index, song in enumerate(songs, start=1):
            with suppress(InvalidDataError):
                track = parse_song(self._ctx, song)
                track.position = offset + index
                tracks.append(track)
        return tracks

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Return the recommendation rows: charts, and daily songs when signed in."""
        folders = [
            RecommendationFolder(
                item_id=RECOMMENDATION_CHARTS,
                provider=self.instance_id,
                name="Migu charts",
                translation_key=RECOMMENDATION_CHARTS,
                icon="mdi:trophy",
            )
        ]
        if self.client.is_logged_in:
            folders.insert(
                0,
                RecommendationFolder(
                    item_id=RECOMMENDATION_DAILY,
                    provider=self.instance_id,
                    name="Migu daily songs",
                    translation_key=RECOMMENDATION_DAILY,
                    icon="mdi:calendar-today",
                ),
            )
        return folders

    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Return the items of one recommendation row.

        :param item_id: Row id as returned by get_recommendations.
        """
        items: UniqueList[MediaItemType | ItemMapping | BrowseFolder] = UniqueList()
        if item_id == RECOMMENDATION_CHARTS:
            for rank in await self._get_ranks():
                with suppress(InvalidDataError):
                    items.append(parse_rank_playlist(self._ctx, rank))
        elif item_id == RECOMMENDATION_DAILY and self.client.is_logged_in:
            items.extend(await self._get_daily_tracks())
        return items

    # ------------------------------------------------------------- helpers

    def _on_token_refresh(self, token: str) -> None:
        """Persist a token the service rotated."""
        self._update_setup_data(CONF_PACM, token)

    async def _search_kind(self, kind: str, query: str, limit: int) -> list[Any]:
        """Search one kind, paging until enough results are collected."""
        items: list[Any] = []
        seen: set[str] = set()
        for page in range(1, MAX_SEARCH_PAGES + 1):
            try:
                data = await self._api(partial(self.client.search, kind, query, page))
            except ResourceTemporarilyUnavailable as err:
                self.logger.warning("Migu %s search failed: %s", kind, err)
                break
            for item in self._parse_search_page(kind, data):
                if item.item_id not in seen:
                    seen.add(item.item_id)
                    items.append(item)
            if len(items) >= limit or not data.get("hasNext"):
                break
        return items[:limit]

    def _parse_search_page(self, kind: str, data: dict[str, Any]) -> list[Any]:
        """Parse one search result page."""
        if kind == "song":
            tracks: list[Track] = []
            for song in iter_songs(data.get("items") or []):
                with suppress(InvalidDataError):
                    tracks.append(parse_song(self._ctx, song))
            return tracks
        if kind == "album":
            return list(iter_search_albums(self._ctx, data))
        if kind == "singer":
            return list(iter_search_singers(self._ctx, data))
        playlists: list[Playlist] = []
        for item in iter_playlists(data.get("items") or []):
            with suppress(InvalidDataError):
                playlists.append(parse_playlist(self._ctx, item))
        return playlists

    @use_cache(3600 * 6, cache_checksum=CACHE_VERSION)
    async def _get_song(self, content_id: str) -> dict[str, Any]:
        """
        Return (cached) song details.

        :param content_id: Song content id.
        """
        songs = await self._api(lambda: self.client.song_infos([content_id]))
        song = next((s for s in songs if str(s.get("contentId")) == content_id), None)
        if song is None:
            raise MediaNotFoundError(f"Migu song {content_id} not found")
        return song

    @use_cache(3600 * 24 * 7, cache_checksum=CACHE_VERSION)
    async def _get_lyric(self, content_id: str) -> str:
        """Return (cached) LRC lyrics of a song."""
        return await self._api(lambda: self.client.lyric(content_id))

    async def _find_artist_by_name(self, name: str) -> Artist:
        """Return the singer search result exactly matching a name."""
        data = await self._api(partial(self.client.search, "singer", name, 1))
        for artist in iter_search_singers(self._ctx, data):
            if artist.name.casefold() == name.casefold():
                return artist
        raise MediaNotFoundError(f"Migu singer {name} not found")

    async def _real_singer_id(self, prov_artist_id: str) -> str:
        """Resolve a ``name:`` artist id to a Migu singer id."""
        if prov_artist_id.startswith("name:"):
            return (await self.get_artist(prov_artist_id)).item_id
        return prov_artist_id

    async def _collect_user_pages(
        self,
        fetch: Callable[..., Awaitable[Any]],
        extract: Callable[[Any], Any],
        *args: Any,
    ) -> list[dict[str, Any]]:
        """Page through a user list endpoint and return the extracted items."""
        items: list[dict[str, Any]] = []
        seen_first: set[str] = set()
        for page in range(1, MAX_USER_PAGES + 1):
            data = await self._api(partial(fetch, *args, page=page, size=USER_PAGE_SIZE))
            page_items = list(extract(data))
            # stop on an empty page, or when the service ignores paging
            first = str(page_items[0]) if page_items else ""
            if not page_items or first in seen_first:
                break
            seen_first.add(first)
            items.extend(page_items)
            if len(page_items) < USER_PAGE_SIZE:
                break
        return items

    async def _get_public_playlist_songs(self, migu_id: str, page: int) -> list[dict[str, Any]]:
        """Return one page (PLAYLIST_PAGE_SIZE songs) of a public playlist."""
        data = await self._api(partial(self.client.playlist_songs, migu_id, page + 1))
        return list(iter_songs(data.get("songList") or []))

    async def _get_own_playlist_songs(
        self, migu_id: str, page: int
    ) -> tuple[list[dict[str, Any]], int]:
        """
        Return one page of a playlist owned by the user, and the page size used.

        Falls back to the public playlist API (smaller pages) when the user endpoint
        has no songs for the playlist.
        """
        if migu_id not in self._public_own_playlists:
            data = await self._api(
                partial(self.client.user_playlist_songs, migu_id, page + 1, USER_PAGE_SIZE)
            )
            if songs := list(iter_songs(data)):
                return songs, USER_PAGE_SIZE
            if page > 0:
                return [], USER_PAGE_SIZE
            self._public_own_playlists.add(migu_id)
        try:
            return await self._get_public_playlist_songs(migu_id, page), PLAYLIST_PAGE_SIZE
        except MediaNotFoundError:
            return [], PLAYLIST_PAGE_SIZE

    @use_cache(3600 * 6, cache_checksum=CACHE_VERSION)
    async def _get_ranks(self) -> list[dict[str, Any]]:
        """Return the (cached) charts of the charts page."""
        data = await self._api(lambda: self.client.rank_index())
        return list(iter_ranks(data))

    @use_cache(3600, cache_checksum=CACHE_VERSION)
    async def _get_rank_songs(self, rank_id: str) -> list[dict[str, Any]]:
        """Return the (cached) songs of a chart."""
        data = await self._api(lambda: self.client.rank_songs(rank_id))
        return list(iter_songs(data))

    @use_cache(3600, cache_checksum=CACHE_VERSION)
    async def _get_daily_tracks(self) -> list[Track]:
        """Return today's personal recommendations."""
        data = await self._api(lambda: self.client.user_daily_recommend())
        tracks: list[Track] = []
        for song in iter_songs(data):
            with suppress(InvalidDataError):
                tracks.append(parse_song(self._ctx, song))
        return tracks

    async def _api[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """
        Run an API call, translating client errors into Music Assistant errors.

        :param call: Creates the API coroutine.
        """
        try:
            return await call()
        except MiguNotFoundError as err:
            raise MediaNotFoundError(str(err)) from err
        except MiguLoginRequiredError as err:
            raise LoginFailed(f"Migu requires a (valid) login for this: {err}") from err
        except MiguConnectionError as err:
            raise ResourceTemporarilyUnavailable(str(err), backoff_time=30) from err
        except MiguError as err:
            raise ResourceTemporarilyUnavailable(str(err)) from err


def tone_ladder(offered: list[str], quality: str) -> list[str]:
    """
    Return the tones to try for a song, best first, capped by the quality option.

    PQ is always tried last: it is the one tone every playable song has.

    :param offered: Tones the song is offered in.
    :param quality: One of the QUALITY_* option values.
    """
    max_tone = QUALITY_MAX_TONE.get(quality, QUALITY_MAX_TONE[QUALITY_HIRES])
    allowed = TONES[TONES.index(max_tone) :]
    ladder = [tone for tone in allowed if tone in offered]
    if TONE_PQ not in ladder:
        ladder.append(TONE_PQ)
    return ladder


def _unplayable_reason(info: dict[str, Any]) -> str:
    """Return Migu's explanation why a song/tone is not playable."""
    dialog = info.get("dialogInfo")
    text = str(dialog.get("text") or "") if isinstance(dialog, dict) else ""
    code = str(info.get("cannotCode") or "")
    if text and code:
        return f"{text} ({code})"
    return text or code


def _user_display_name(info: dict[str, Any]) -> str:
    """Return a display name from a user info payload."""
    for key in ("nickName", "nickname", "userName", "displayName", "msisdn", "mobile"):
        if value := str(info.get(key) or "").strip():
            return value
    for value in info.values():
        if isinstance(value, dict) and (name := _user_display_name(value)):
            return name
    return ""
