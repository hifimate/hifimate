"""
Client for a local migu-api-enhanced service.

The service (https://github.com/Domdkw/miguMusic-api-enhanced) wraps the Migu Music
web/app APIs behind plain GET endpoints. This module is the only one that talks to it;
it returns plain dicts and raises the ``Migu*Error`` exceptions below.

The login credential is a ``pacmtoken``. Endpoints that use it may answer with a
refreshed token; the client keeps the newest one and reports it through
``on_token_refresh`` so the provider can persist it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientTimeout

if TYPE_CHECKING:
    from aiohttp import ClientSession

CODE_OK = "000000"
# "请先登录" / "uid不能为空": the token is missing, invalid or expired
CODES_LOGIN_REQUIRED = frozenset({"290001", "299999"})
# "歌曲下线暂不支持播放"
CODES_NOT_FOUND = frozenset({"200000"})

_TIMEOUT = ClientTimeout(total=30)
_MAX_CONCURRENCY = 4


class MiguError(Exception):
    """A request to the Migu API service failed."""


class MiguConnectionError(MiguError):
    """The migu-api-enhanced service could not be reached or answered garbage."""


class MiguLoginRequiredError(MiguError):
    """The request needs a (valid) login."""


class MiguNotFoundError(MiguError):
    """The requested item does not exist (or is offline)."""


class MiguClient:
    """Thin async wrapper around the migu-api-enhanced endpoints."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        token: str = "",
        *,
        on_token_refresh: Callable[[str], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Initialize the client.

        :param session: aiohttp session to use.
        :param base_url: Service base url including the route prefix (``.../migu``).
        :param token: pacmtoken of the signed in user (empty: anonymous).
        :param on_token_refresh: Called with the new token when the service rotates it.
        :param logger: Logger for debug output.
        """
        self._session = session
        self.base_url = base_url.rstrip("/")
        self._token = token.strip()
        self._on_token_refresh = on_token_refresh
        self._logger = logger or logging.getLogger(__name__)
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    @property
    def token(self) -> str:
        """Return the current pacmtoken (empty when anonymous)."""
        return self._token

    @property
    def is_logged_in(self) -> bool:
        """Return whether a token is set."""
        return bool(self._token)

    # ------------------------------------------------------------- catalog

    async def version(self) -> dict[str, Any]:
        """Return the Migu app version info (cheap reachability check)."""
        return _as_dict(await self._data("version"))

    async def search(self, kind: str, text: str, page: int = 1) -> dict[str, Any]:
        """
        Search the catalog.

        :param kind: ``song``, ``album``, ``singer`` or ``playlist``.
        :param text: Search text.
        :param page: 1-based page (20 items each).
        """
        path = "search" if kind == "song" else f"search/{kind}"
        return _as_dict(await self._data(path, {"text": text, "page": page}))

    async def song_infos(self, content_ids: list[str]) -> list[dict[str, Any]]:
        """
        Return song details by content id.

        :param content_ids: Content ids (one request for all of them).
        """
        if not content_ids:
            return []
        data = await self._data("songinfo/contentId", {"contentIds": ",".join(content_ids)})
        return [song for song in data or [] if isinstance(song, dict)]

    async def album_info(self, album_id: str) -> dict[str, Any]:
        """Return album details."""
        return _as_dict(await self._data("album/info", {"albumId": album_id}))

    async def album_songs(self, album_id: str, page: int = 1) -> dict[str, Any]:
        """Return one page of album songs (``songList``, ``hasNext``)."""
        return _as_dict(await self._data("album/song", {"albumId": album_id, "page": page}))

    async def digital_album_info(self, dalbum_id: str) -> dict[str, Any]:
        """Return digital album (数字专辑) details."""
        return _as_dict(await self._data("dalbum/info", {"dalbumId": dalbum_id}))

    async def digital_album_songs(self, dalbum_id: str) -> dict[str, Any]:
        """Return the songs of a digital album (``songList``)."""
        return _as_dict(await self._data("dalbum/song", {"dalbumId": dalbum_id}))

    async def singer_info(self, singer_id: str) -> dict[str, Any]:
        """Return singer details."""
        payload = self._check(
            await self._get("resourceinfo", {"resourceIds": singer_id, "resourceType": 2002}),
            "/resourceinfo",
        )
        resources = [r for r in payload.get("resource") or [] if isinstance(r, dict)]
        if not resources:
            raise MiguNotFoundError(f"Singer {singer_id} not found")
        return resources[0]

    async def singer_songs(self, singer_id: str, page: int = 1) -> dict[str, Any]:
        """Return one page of a singer's songs (page layout with nested ``songItem``)."""
        return _as_dict(await self._data("singer/song", {"singerId": singer_id, "page": page}))

    async def singer_albums(self, singer_id: str, page: int = 1) -> dict[str, Any]:
        """Return one page of a singer's albums (``contents`` of album items)."""
        return _as_dict(await self._data("singer/album", {"singerId": singer_id, "page": page}))

    async def playlist_info(self, playlist_id: str) -> dict[str, Any]:
        """Return public playlist details."""
        return _as_dict(await self._data("playlist/info", {"playlistId": playlist_id}))

    async def playlist_songs(self, playlist_id: str, page: int = 1) -> dict[str, Any]:
        """Return one page of a public playlist (``songList``, ``totalCount``)."""
        return _as_dict(
            await self._data("playlist/song", {"playlistId": playlist_id, "page": page})
        )

    async def rank_index(self) -> dict[str, Any]:
        """Return the charts overview page."""
        return _as_dict(await self._data("rank/index"))

    async def rank_songs(self, rank_id: str) -> dict[str, Any]:
        """Return a chart (``contents`` with a ``songData`` JSON string each)."""
        return _as_dict(await self._data("rank/info", {"rankId": rank_id, "page": 1}))

    async def lyric(self, content_id: str) -> str:
        """Return the LRC lyrics of a song (empty when there are none)."""
        payload = await self._get("lyric", {"contentId": content_id})
        if payload.get("success") is False:
            return ""
        return str(payload.get("lrc") or "")

    async def play_url(self, content_id: str, copyright_id: str, tone: str) -> dict[str, Any]:
        """
        Return the play info of a song at one tone.

        Uses the token when signed in. The result has ``url`` (empty when the tone is
        not available to this account) and ``cannotCode``/``dialogInfo`` explaining why.

        :param content_id: Song content id.
        :param copyright_id: Song copyright id (may be empty).
        :param tone: Tone flag (PQ/HQ/SQ/ZQ24).
        """
        params: dict[str, Any] = {
            "contentId": content_id,
            "copyrightId": copyright_id or None,
            "toneFlag": tone,
        }
        if self._token:
            params["pacmtoken"] = self._token
        payload = await self._get("url/v2", params)
        if payload.get("data") is None and str(payload.get("code") or "") == CODE_OK:
            # the service failed to decrypt the answer
            raise MiguError(f"Migu returned no play info for {content_id}")
        return _as_dict(self._check(payload, f"play url {content_id}").get("data"))

    # ------------------------------------------------------------- account

    async def user_info(self) -> dict[str, Any]:
        """Return the signed in user's profile."""
        payload = self._check(await self._get("user/info", auth=True), "/user/info")
        return _as_dict(payload.get("data")) or payload

    async def user_playlists(self, page: int = 1, size: int = 50) -> Any:
        """Return one page of the playlists the user created."""
        return await self._data("user/myList", {"page": page, "size": size}, auth=True)

    async def user_collected_playlists(self, page: int = 1, size: int = 50) -> Any:
        """Return one page of the playlists the user collected (收藏)."""
        return await self._data("user/collect/list", {"page": page, "size": size}, auth=True)

    async def user_playlist_songs(self, playlist_id: str, page: int = 1, size: int = 50) -> Any:
        """Return one page of a playlist owned by the user (incl. 我喜欢)."""
        return await self._data(
            "user/like/list",
            {"playlistId": playlist_id, "page": page, "size": size},
            auth=True,
        )

    async def user_daily_recommend(self) -> Any:
        """Return today's personal song recommendations."""
        return await self._data("user/recommend/today", auth=True)

    # ------------------------------------------------------------- login

    async def send_sms_code(self, phone: str) -> None:
        """
        Send a login code to a phone number.

        :param phone: Mainland China mobile number.
        """
        payload = await self._get("login/phone/msgCode", {"phone": phone})
        if payload.get("success") is False:
            error = payload.get("error")
            message = ""
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("msg") or error.get("status"))
            raise MiguError(f"Migu did not send the code: {message or error or 'unknown error'}")

    async def sms_login(self, phone: str, code: str) -> str:
        """
        Sign in with a phone number and SMS code; return (and adopt) the pacmtoken.

        :param phone: Phone number the code was sent to.
        :param code: The SMS code.
        """
        payload = await self._get(
            "login/phone/authn", {"phone": phone, "msgCode": code, "isNeedPacm": 1}
        )
        if payload.get("success") is False:
            error = payload.get("error")
            message = ""
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("msg") or error.get("status"))
            raise MiguLoginRequiredError(f"Migu login failed: {message or error or 'unknown'}")
        data = _as_dict(payload.get("data"))
        token = str(data.get("pacmToken") or "").strip()
        if not token:
            raise MiguLoginRequiredError("Migu login succeeded but returned no pacmtoken")
        self._token = token
        return token

    def set_token(self, token: str) -> None:
        """Replace the token (after a login)."""
        self._token = token.strip()

    # ------------------------------------------------------------- transport

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False
    ) -> dict[str, Any]:
        """
        GET an endpoint and return its JSON payload.

        :param path: Endpoint path below the base url.
        :param params: Query parameters (None values are dropped).
        :param auth: Send the pacmtoken.
        """
        query = {k: str(v) for k, v in (params or {}).items() if v is not None}
        if auth:
            if not self._token:
                raise MiguLoginRequiredError("Not signed in to Migu")
            query["pacmtoken"] = self._token
        url = f"{self.base_url}/{path.lstrip('/')}"
        async with self._semaphore:
            try:
                async with self._session.get(url, params=query, timeout=_TIMEOUT) as resp:
                    body = await resp.text()
                    status = resp.status
            except (ClientError, TimeoutError) as err:
                raise MiguConnectionError(
                    f"Migu API service unreachable at {self.base_url}: {err or type(err).__name__}"
                ) from err
        try:
            payload = json.loads(body)
        except ValueError as err:
            raise MiguConnectionError(
                f"Migu API service returned invalid JSON (HTTP {status}) for /{path}"
            ) from err
        if not isinstance(payload, dict):
            raise MiguConnectionError(f"Migu API service returned unexpected data for /{path}")
        if status >= 500:
            raise MiguError(f"Migu API service error (HTTP {status}) for /{path}")
        if auth:
            self._maybe_refresh_token(payload.get("pacmtoken"))
        return payload

    def _maybe_refresh_token(self, value: Any) -> None:
        """Adopt a rotated token returned by the service."""
        new_token = str(value or "").strip()
        if not new_token or new_token == self._token:
            return
        self._logger.debug("Migu rotated the login token")
        self._token = new_token
        if self._on_token_refresh is not None:
            self._on_token_refresh(new_token)

    @staticmethod
    def _check(payload: dict[str, Any], what: str) -> dict[str, Any]:
        """
        Raise for an error payload; return it otherwise.

        :param payload: Service response.
        :param what: Description of the request, for error messages.
        """
        code = str(payload.get("code") or "")
        if payload.get("success") is False:
            error = payload.get("error")
            if isinstance(error, dict):
                code = str(error.get("code") or code)
                message = str(error.get("info") or error.get("message") or error)
            else:
                message = str(error or payload.get("message") or "unknown error")
            _raise_for_code(code, f"{what}: {message}")
            raise MiguError(f"{what}: {message}")
        if code and code != CODE_OK:
            _raise_for_code(code, f"{what}: {payload.get('info') or code}")
            raise MiguError(f"{what}: {payload.get('info') or code}")
        return payload

    async def _data(
        self, path: str, params: dict[str, Any] | None = None, *, auth: bool = False
    ) -> Any:
        """GET an endpoint, check the result code and return its ``data``."""
        payload = self._check(await self._get(path, params, auth=auth), f"/{path}")
        return payload.get("data")


def _raise_for_code(code: str, message: str) -> None:
    """Raise the specific error for a known result code."""
    if code in CODES_LOGIN_REQUIRED:
        raise MiguLoginRequiredError(message)
    if code in CODES_NOT_FOUND:
        raise MiguNotFoundError(message)


def _as_dict(value: Any) -> dict[str, Any]:
    """Return value if it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}
