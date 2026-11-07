"""
Thin adapter between the Bilibili provider and the bpi-py SDK.

The provider only talks to :class:`BiliClient`; nothing else imports bpi-py. That keeps
the blast radius of SDK or Bilibili API changes inside this module:

* Requests run over a curl_cffi transport impersonating Chrome. Bilibili's risk control
  fingerprints TLS, so plain httpx/aiohttp gets HTTP 412 on several endpoints.
* bpi-py validates responses with strict pydantic models. When Bilibili adds, drops or
  retypes a field the SDK raises ``ResponseDecodeError`` carrying the raw body; we then
  fall back to lenient decoding of that body instead of failing.
* Endpoints the SDK does not cover (music center charts, the space archive listing)
  are requested directly on the same HTTP client.
* All results are plain ``dict``/``list`` data so the provider parses them defensively
  and never depends on SDK model classes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import parse_qsl, urlparse

import httpx
from bpi import AsyncBpiClient
from bpi.errors import (
    ApiError,
    BpiError,
    HttpStatusError,
    MissingDataError,
    ResponseDecodeError,
)
from httpx_curl_cffi import AsyncCurlTransport, BrowserTypeLiteral
from pydantic import BaseModel

from .constants import WEB_ORIGIN, WEB_REFERER

LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.bilibili.com"
# browser fingerprint the transport impersonates (curl_cffi target alias)
IMPERSONATE_TARGET: BrowserTypeLiteral = "chrome"
# max parallel requests against Bilibili; bursts are what trips the risk control
MAX_CONCURRENT_REQUESTS = 4
# how long fetched buvid device cookies are trusted before being refreshed
BUVID_TTL = 12 * 3600
# base delay (seconds) between retries after risk control rejections
RISK_RETRY_DELAY = 1.5
# cookies that make up a logged-in web session
LOGIN_COOKIE_NAMES = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")
DEVICE_COOKIE_NAMES = ("buvid3", "buvid4")

# QR login poll states (data.code of /x/passport-login/web/qrcode/poll)
QR_CONFIRMED = 0
QR_EXPIRED = 86038
QR_SCANNED = 86090
QR_WAITING = 86101

_LOGIN_REQUIRED_CODES = {-101, -111, -401, 4_100_000}
_RISK_CONTROL_CODES = {-352, -412, -509, -799}
_NOT_FOUND_CODES = {-404, 62002, 62004, 62012, 11010}
_VIP_CODES = {-106, -650, 87007, 87008}


class BiliError(Exception):
    """A Bilibili request failed."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        """
        Initialize the error.

        :param message: Human readable description.
        :param code: Bilibili API code (or HTTP status) when known.
        """
        self.code = code
        super().__init__(message if code is None else f"{message} (code {code})")


class BiliRiskControlError(BiliError):
    """Bilibili's anti-crawler rejected the request (412, -352, v_voucher...)."""


class BiliLoginRequiredError(BiliError):
    """The request needs a (valid) logged in session."""


class BiliNotFoundError(BiliError):
    """The requested video/user/folder does not exist or is not visible."""


class BiliVipRequiredError(BiliError):
    """The requested content needs a premium (大会员) account."""


def error_for_code(code: int, message: str = "") -> BiliError:
    """
    Map a Bilibili API code to the matching exception.

    :param code: The (non-zero) Bilibili API code.
    :param message: The message Bilibili returned with it.
    """
    text = message or "Bilibili API error"
    if code in _RISK_CONTROL_CODES:
        return BiliRiskControlError(text, code=code)
    if code in _LOGIN_REQUIRED_CODES:
        return BiliLoginRequiredError(text, code=code)
    if code in _NOT_FOUND_CODES:
        return BiliNotFoundError(text, code=code)
    if code in _VIP_CODES:
        return BiliVipRequiredError(text, code=code)
    return BiliError(text, code=code)


def decode_envelope(body: bytes, *, allow_codes: frozenset[int] = frozenset()) -> Any:
    """
    Leniently decode a Bilibili JSON envelope and return its payload.

    :param body: Raw response body.
    :param allow_codes: Non-zero codes whose payload should be returned instead of raised.
    """
    try:
        raw = json.loads(body)
    except (ValueError, UnicodeDecodeError) as err:
        raise BiliError("Bilibili returned a non-JSON response") from err
    if not isinstance(raw, dict):
        raise BiliError("Bilibili returned an unexpected response")
    code = raw.get("code", 0)
    if not isinstance(code, int):
        try:
            code = int(code)
        except TypeError, ValueError:
            code = -1
    payload = raw.get("data", raw.get("result"))
    if code != 0 and code not in allow_codes:
        message = raw.get("message") or raw.get("msg") or ""
        raise error_for_code(code, str(message))
    # a soft block: code 0, but the payload is only a captcha voucher
    if isinstance(payload, dict) and "v_voucher" in payload and len(payload) <= 2:
        raise BiliRiskControlError("Bilibili requested a captcha (v_voucher)", code=code)
    return payload


def to_plain(value: Any) -> Any:
    """
    Convert SDK models (and containers of them) into plain JSON-like data.

    Fields are dumped by alias so names match the raw API wherever the SDK declares one.

    :param value: SDK result.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    if isinstance(value, tuple):
        return [to_plain(item) for item in value]
    return value


def parse_cookie_string(cookie: str) -> dict[str, str]:
    """
    Parse a ``name=value; name2=value2`` cookie string.

    :param cookie: The cookie header style string.
    """
    result: dict[str, str] = {}
    for part in cookie.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip():
            result[name.strip()] = value.strip()
    return result


@dataclass
class QrLoginState:
    """Result of one QR login poll."""

    code: int
    message: str = ""
    cookies: dict[str, str] = field(default_factory=dict)
    refresh_token: str = ""


class _BrowserHeadersTransport(httpx.AsyncBaseTransport):
    """
    Wrap the curl transport so every request carries the impersonated browser headers.

    httpx (and bpi-py) set their own User-Agent; a non-browser UA on top of a Chrome TLS
    fingerprint is an easy risk-control signal, so it is dropped and curl_cffi fills in
    the UA that matches the fingerprint.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "user-agent" in request.headers:
            del request.headers["user-agent"]
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


class BiliClient:
    """Async Bilibili web API client used by the provider and its setup flow."""

    def __init__(
        self,
        cookie: str = "",
        *,
        timeout: float = 15.0,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Initialize the client (call :meth:`start` before use).

        :param cookie: Persisted cookie string of a logged in session (may be empty).
        :param timeout: Per request timeout in seconds.
        :param logger: Logger to use.
        """
        self.logger = logger or LOGGER
        self._initial_cookie = cookie
        self._timeout = timeout
        self._http: httpx.AsyncClient | None = None
        self._bpi: AsyncBpiClient | None = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        self._buvid_lock = asyncio.Lock()
        self._buvid_fetched_at = 0.0

    async def start(self) -> None:
        """Create the HTTP session and make sure device cookies exist."""
        transport = _BrowserHeadersTransport(
            AsyncCurlTransport(impersonate=IMPERSONATE_TARGET, default_headers=True)
        )
        self._http = httpx.AsyncClient(transport=transport, timeout=self._timeout)
        for name, value in parse_cookie_string(self._initial_cookie).items():
            self._http.cookies.set(name, value, domain=".bilibili.com", path="/")
        self._bpi = AsyncBpiClient(http_client=self._http)
        if all(self._http.cookies.get(name) for name in DEVICE_COOKIE_NAMES):
            # persisted device ids are reused, a stable device looks less like a bot
            self._buvid_fetched_at = time.time()
        else:
            await self.refresh_device_cookies()

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._bpi is not None:
            await self._bpi.aclose()
            self._bpi = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def __aenter__(self) -> Self:
        """Start the client in an ``async with`` block."""
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Close the client at the end of an ``async with`` block."""
        await self.close()

    @property
    def http(self) -> httpx.AsyncClient:
        """Return the underlying HTTP client."""
        if self._http is None:
            raise BiliError("Bilibili client is not started")
        return self._http

    @property
    def sdk(self) -> AsyncBpiClient:
        """Return the underlying bpi-py client."""
        if self._bpi is None:
            raise BiliError("Bilibili client is not started")
        return self._bpi

    def get_cookie(self, name: str) -> str:
        """
        Return the value of a bilibili.com cookie (empty when absent).

        :param name: Cookie name.
        """
        if self._http is None:
            return ""
        for cookie in self._http.cookies.jar:
            if cookie.name == name and cookie.value:
                return cookie.value
        return ""

    @property
    def is_logged_in(self) -> bool:
        """Return whether a login session cookie is present."""
        return bool(self.get_cookie("SESSDATA"))

    @property
    def mid(self) -> int | None:
        """Return the user id of the logged in account."""
        try:
            return int(self.get_cookie("DedeUserID")) or None
        except ValueError:
            return None

    def export_cookies(self) -> str:
        """Serialize the login and device cookies for persisting in setup data."""
        pairs = [
            f"{name}={value}"
            for name in (*LOGIN_COOKIE_NAMES, *DEVICE_COOKIE_NAMES)
            if (value := self.get_cookie(name))
        ]
        return "; ".join(pairs)

    def set_cookies(self, cookies: dict[str, str]) -> None:
        """
        Store cookies (e.g. the result of a QR login) on the session.

        :param cookies: Mapping of cookie name to value.
        """
        for name, value in cookies.items():
            if value:
                self.http.cookies.set(name, value, domain=".bilibili.com", path="/")

    async def refresh_device_cookies(self, *, force: bool = False) -> None:
        """
        Fetch fresh buvid3/buvid4 device cookies (required to pass the risk control).

        :param force: Refetch even when the current ids are not expired yet.
        """
        async with self._buvid_lock:
            if not force and time.time() - self._buvid_fetched_at < BUVID_TTL:
                return
            try:
                data = await self._raw_get_once("/x/frontend/finger/spi", {})
            except BiliError as err:
                self.logger.debug("Unable to fetch Bilibili device cookies: %s", err)
                return
            if isinstance(data, dict):
                self.set_cookies(
                    {"buvid3": str(data.get("b_3") or ""), "buvid4": str(data.get("b_4") or "")}
                )
                self._buvid_fetched_at = time.time()

    async def nav(self) -> dict[str, Any]:
        """Return the account/nav info (``isLogin`` is false for anonymous sessions)."""
        data = await self._raw_get(
            "/x/web-interface/nav", {}, allow_codes=frozenset(_LOGIN_REQUIRED_CODES)
        )
        return data if isinstance(data, dict) else {}

    async def qr_generate(self) -> tuple[str, str]:
        """Create a QR login session, returning ``(url_to_encode, qrcode_key)``."""
        data = await self._sdk_call(lambda: self.sdk.login.qr_generate())
        if not isinstance(data, dict) or not data.get("url") or not data.get("qrcode_key"):
            raise BiliError("Bilibili did not return a QR login code")
        return str(data["url"]), str(data["qrcode_key"])

    async def qr_poll(self, qrcode_key: str) -> QrLoginState:
        """
        Poll a QR login session once.

        On success the login cookies are also stored on this client's session.

        :param qrcode_key: Key returned by :meth:`qr_generate`.
        """
        data = await self._sdk_call(lambda: self.sdk.login.qr_poll(qrcode_key=qrcode_key))
        if not isinstance(data, dict):
            raise BiliError("Bilibili returned an unexpected QR poll response")
        code = int(data.get("code", -1))
        state = QrLoginState(code=code, message=str(data.get("message") or ""))
        if code != QR_CONFIRMED:
            return state
        cookies: dict[str, str] = {}
        # the cross-domain url repeats the session cookies as query parameters
        if url := data.get("url"):
            cookies.update(
                (name, value)
                for name, value in parse_qsl(urlparse(str(url)).query)
                if name in LOGIN_COOKIE_NAMES
            )
        for pair in data.get("cookies") or []:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                cookies[str(pair[0])] = str(pair[1])
        self.set_cookies(cookies)
        state.cookies = {name: self.get_cookie(name) for name in LOGIN_COOKIE_NAMES}
        state.refresh_token = str(data.get("refresh_token") or "")
        return state

    async def search_videos(self, keyword: str, *, page: int = 1, tid: int = 0) -> dict[str, Any]:
        """
        Search videos.

        :param keyword: Search keywords.
        :param page: 1-based result page (20 results per page).
        :param tid: Restrict to a zone (partition id), 0 for all.
        """
        data = await self._sdk_call(
            lambda: self.sdk.search.video(keyword=keyword, page=page, tid=tid)
        )
        return data if isinstance(data, dict) else {}

    async def search_users(self, keyword: str, *, page: int = 1) -> dict[str, Any]:
        """
        Search uploaders (UP主).

        :param keyword: Search keywords.
        :param page: 1-based result page.
        """
        data = await self._sdk_call(lambda: self.sdk.search.bili_user(keyword=keyword, page=page))
        return data if isinstance(data, dict) else {}

    async def view(self, *, bvid: str | None = None, aid: int | None = None) -> dict[str, Any]:
        """
        Return video details (title, owner, pages with cid/duration...).

        :param bvid: BV id of the video.
        :param aid: av id of the video (used when no bvid is given).
        """
        # requested directly: the SDK model drops fields we need (duration, desc, pubdate)
        params: dict[str, Any] = {"bvid": bvid} if bvid else {"aid": aid}
        data = await self._raw_get("/x/web-interface/view", params)
        if not isinstance(data, dict):
            raise BiliNotFoundError("Video not found")
        return data

    async def play_url(
        self, *, cid: int, bvid: str | None = None, aid: int | None = None
    ) -> dict[str, Any]:
        """
        Return the DASH play info of one video page (contains the audio streams).

        :param cid: Page (cid) of the video.
        :param bvid: BV id of the video.
        :param aid: av id of the video (used when no bvid is given).
        """
        data = await self._sdk_call(
            lambda: self.sdk.video.play_url(
                cid=cid,
                bvid=bvid,
                aid=aid,
                # 16 = DASH, plus the HDR/4K/Dolby/8K/AV1 bits the web player sends;
                # Dolby audio and the Hi-Res FLAC track only appear with these set
                format_flags=4048,
                format_version=0,
                fourk=True,
            )
        )
        if not isinstance(data, dict):
            raise BiliError("Bilibili returned no play info")
        return data

    async def user_card(self, mid: int) -> dict[str, Any]:
        """
        Return the public profile card of a user.

        :param mid: User id.
        """
        data = await self._sdk_call(lambda: self.sdk.user.card(mid=mid))
        if not isinstance(data, dict):
            raise BiliNotFoundError("User not found")
        return data

    async def user_archives(
        self, mid: int, *, page: int = 1, page_size: int = 30
    ) -> dict[str, Any]:
        """
        Return the uploads of a user, newest first.

        Uses the series archive listing: the space search endpoint
        (``/x/space/wbi/arc/search``) is rejected with HTTP 412 for non-browser clients.

        :param mid: User id.
        :param page: 1-based page.
        :param page_size: Items per page (max 100).
        """
        data = await self._raw_get(
            "/x/series/recArchivesByKeywords",
            {"mid": mid, "keywords": "", "ps": page_size, "pn": page},
            referer=f"https://space.bilibili.com/{mid}/video",
            # this endpoint randomly rejects a share of requests; retrying a bit more
            # is cheap since the result is cached by the provider
            attempts=4,
        )
        return data if isinstance(data, dict) else {}

    async def fav_folders(self, mid: int) -> list[dict[str, Any]]:
        """
        Return the favorite folders created by a user.

        :param mid: User id (private folders are only listed for the own account).
        """
        data = await self._sdk_call(lambda: self.sdk.fav.created_list(up_mid=mid))
        if not isinstance(data, dict):
            return []
        folders = data.get("list") or []
        return [folder for folder in folders if isinstance(folder, dict)]

    async def fav_folder(
        self, media_id: int, *, page: int = 1, page_size: int = 20
    ) -> dict[str, Any]:
        """
        Return the info and one page of items of a favorite folder.

        :param media_id: Folder id.
        :param page: 1-based page.
        :param page_size: Items per page (Bilibili caps this at 20).
        """
        data = await self._sdk_call(
            lambda: self.sdk.fav.list_detail(media_id=media_id, page=page, page_size=page_size)
        )
        if not isinstance(data, dict):
            raise BiliNotFoundError("Favorite folder not found")
        return data

    async def music_hot_rank(self, *, page_size: int = 50) -> list[dict[str, Any]]:
        """
        Return the music center's comprehensive hot song chart.

        :param page_size: Number of entries.
        """
        data = await self._raw_get(
            "/x/centralization/interface/music/comprehensive/web/rank",
            {"pn": 1, "ps": page_size},
            referer="https://music.bilibili.com/",
        )
        return _list_of_dicts(data, "list")

    async def music_new_releases(self) -> list[dict[str, Any]]:
        """Return the music center's new release list."""
        data = await self._raw_get(
            "/x/centralization/interface/new/music", {}, referer="https://music.bilibili.com/"
        )
        return _list_of_dicts(data, "list")

    async def _sdk_call(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        """
        Run a bpi-py call, falling back to lenient decoding when its model rejects the data.

        Retries once with fresh device cookies when the risk control kicks in.

        :param factory: Creates the SDK coroutine (called again on retry).
        """
        for attempt in range(2):
            try:
                async with self._semaphore:
                    try:
                        return to_plain(await factory())
                    except ResponseDecodeError as err:
                        # strict SDK model rejected the payload; decode it ourselves
                        self.logger.debug("bpi-py model rejected response, decoding leniently")
                        return decode_envelope(err.response_body)
            except BiliRiskControlError:
                if attempt:
                    raise
            except ApiError as err:
                mapped = error_for_code(err.code, err.message)
                if not isinstance(mapped, BiliRiskControlError) or attempt:
                    raise mapped from None
            except HttpStatusError as err:
                if err.status_code != 412 or attempt:
                    raise error_for_status(err.status_code) from None
            except MissingDataError:
                return None
            except BpiError as err:
                raise BiliError(f"Bilibili request failed: {err}") from None
            self.logger.debug("Bilibili risk control triggered, retrying with fresh device ids")
            await self.refresh_device_cookies(force=True)
        raise BiliRiskControlError("Bilibili risk control")  # pragma: no cover

    async def _raw_get_once(
        self,
        path: str,
        params: dict[str, Any],
        *,
        referer: str = WEB_REFERER,
        allow_codes: frozenset[int] = frozenset(),
    ) -> Any:
        """
        GET an API path directly on the shared session (single attempt).

        :param path: API path below api.bilibili.com.
        :param params: Query parameters.
        :param referer: Referer header to send.
        :param allow_codes: Non-zero codes whose payload is returned instead of raised.
        """
        try:
            response = await self.http.get(
                f"{API_BASE}{path}",
                params=params,
                headers={"referer": referer, "origin": WEB_ORIGIN},
            )
        except httpx.HTTPError as err:
            raise BiliError(f"Bilibili request failed: {err}") from err
        if response.status_code >= 400:
            raise error_for_status(response.status_code)
        return decode_envelope(response.content, allow_codes=allow_codes)

    async def _raw_get(
        self,
        path: str,
        params: dict[str, Any],
        *,
        referer: str = WEB_REFERER,
        allow_codes: frozenset[int] = frozenset(),
        attempts: int = 2,
    ) -> Any:
        """
        GET an API path directly, retrying when the risk control rejects the request.

        The first retry runs with fresh device ids, later ones after a growing delay.

        :param path: API path below api.bilibili.com.
        :param params: Query parameters.
        :param referer: Referer header to send.
        :param allow_codes: Non-zero codes whose payload is returned instead of raised.
        :param attempts: Total number of tries.
        """
        for attempt in range(attempts):
            try:
                async with self._semaphore:
                    return await self._raw_get_once(
                        path, params, referer=referer, allow_codes=allow_codes
                    )
            except BiliRiskControlError:
                if attempt + 1 >= attempts:
                    raise
            self.logger.debug("Bilibili risk control on %s (try %s)", path, attempt + 1)
            if attempt == 0:
                await self.refresh_device_cookies(force=True)
            else:
                await asyncio.sleep(RISK_RETRY_DELAY * attempt)
        raise BiliRiskControlError("Bilibili risk control")  # pragma: no cover


def error_for_status(status: int) -> BiliError:
    """
    Map an HTTP error status to the matching exception.

    :param status: HTTP status code.
    """
    if status == 412:
        return BiliRiskControlError("Bilibili risk control (HTTP 412)", code=status)
    if status == 404:
        return BiliNotFoundError("Not found (HTTP 404)", code=status)
    return BiliError(f"HTTP status {status}", code=status)


def _list_of_dicts(data: Any, key: str) -> list[dict[str, Any]]:
    """
    Return ``data[key]`` filtered to dict entries.

    :param data: Decoded payload.
    :param key: Key holding the list.
    """
    if not isinstance(data, dict):
        return []
    items = data.get(key) or []
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]
