"""Tests for the Bilibili API adapter (no network)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from bpi.errors import ApiError, HttpStatusError, ResponseDecodeError

from music_assistant.providers.bilibili import bili_client as bili_client_module
from music_assistant.providers.bilibili.bili_client import (
    QR_CONFIRMED,
    BiliClient,
    BiliError,
    BiliLoginRequiredError,
    BiliNotFoundError,
    BiliRiskControlError,
    BiliVipRequiredError,
    decode_envelope,
    parse_cookie_string,
)


def _body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode()


def _client(handler: Any = None) -> BiliClient:
    """Create a client wired to a mock transport, without starting it."""
    client = BiliClient()
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler or (lambda _req: httpx.Response(500)))
    )
    client._bpi = Mock()
    client.refresh_device_cookies = AsyncMock()  # type: ignore[method-assign]
    return client


@pytest.mark.parametrize(
    ("code", "error"),
    [
        (-352, BiliRiskControlError),
        (-412, BiliRiskControlError),
        (-101, BiliLoginRequiredError),
        (-404, BiliNotFoundError),
        (62002, BiliNotFoundError),
        (87007, BiliVipRequiredError),
        (-400, BiliError),
    ],
)
def test_decode_envelope_error_codes(code: int, error: type[BiliError]) -> None:
    """Non-zero codes map to typed errors."""
    with pytest.raises(error):
        decode_envelope(_body({"code": code, "message": "nope"}))


def test_decode_envelope_payloads() -> None:
    """The payload is returned; a bare captcha voucher counts as risk control."""
    assert decode_envelope(_body({"code": 0, "data": {"a": 1}})) == {"a": 1}
    assert decode_envelope(_body({"code": 0, "result": [1]})) == [1]
    assert decode_envelope(
        _body({"code": -101, "data": {"isLogin": False}}), allow_codes=frozenset({-101})
    ) == {"isLogin": False}
    with pytest.raises(BiliRiskControlError):
        decode_envelope(_body({"code": 0, "data": {"v_voucher": "voucher_x"}}))
    with pytest.raises(BiliError):
        decode_envelope(b"<html>blocked</html>")


async def test_sdk_call_falls_back_to_lenient_decoding() -> None:
    """When the SDK's strict model rejects a payload, the raw body is used."""
    client = _client()
    body = _body({"code": 0, "data": {"bvid": "BV1", "new_field": True}})

    async def _call() -> Any:
        raise ResponseDecodeError(body)

    assert await client._sdk_call(_call) == {"bvid": "BV1", "new_field": True}


async def test_sdk_call_retries_once_on_risk_control() -> None:
    """Risk control refreshes the device ids and retries a single time."""
    client = _client()
    calls = 0

    async def _flaky() -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HttpStatusError(412)
        return {"ok": True}

    assert await client._sdk_call(_flaky) == {"ok": True}
    assert calls == 2
    client.refresh_device_cookies.assert_awaited_once_with(force=True)  # type: ignore[attr-defined]

    async def _always_blocked() -> Any:
        raise ApiError(-352, "blocked")

    with pytest.raises(BiliRiskControlError):
        await client._sdk_call(_always_blocked)


async def test_sdk_call_maps_api_errors_without_retry() -> None:
    """Other API errors are mapped and not retried."""
    client = _client()
    factory = AsyncMock(side_effect=ApiError(-404, "gone"))
    with pytest.raises(BiliNotFoundError):
        await client._sdk_call(factory)
    assert factory.await_count == 1


async def test_raw_get_retries_after_voucher() -> None:
    """A captcha voucher on a direct request triggers one retry with new device ids."""
    responses = iter(
        [
            httpx.Response(200, content=_body({"code": 0, "data": {"v_voucher": "v"}})),
            httpx.Response(200, content=_body({"code": 0, "data": {"list": [{"a": 1}]}})),
        ]
    )
    client = _client(lambda _req: next(responses))
    assert await client.music_hot_rank() == [{"a": 1}]
    client.refresh_device_cookies.assert_awaited_once()  # type: ignore[attr-defined]


async def test_raw_get_http_412_is_risk_control(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP 412 from the gateway is risk control; the archive listing retries 4 times."""
    monkeypatch.setattr(bili_client_module, "RISK_RETRY_DELAY", 0)
    requests: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(412, text="<html>")

    client = _client(_handler)
    with pytest.raises(BiliRiskControlError):
        await client.user_archives(1)
    assert len(requests) == 4
    # device ids are only refreshed on the first retry
    client.refresh_device_cookies.assert_awaited_once_with(force=True)  # type: ignore[attr-defined]


async def test_raw_get_recovers_on_later_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejection followed by success returns the data."""
    monkeypatch.setattr(bili_client_module, "RISK_RETRY_DELAY", 0)
    responses = iter(
        [
            httpx.Response(412),
            httpx.Response(412),
            httpx.Response(200, content=_body({"code": 0, "data": {"archives": []}})),
        ]
    )
    client = _client(lambda _req: next(responses))
    assert await client.user_archives(1) == {"archives": []}


async def test_view_requests_raw_endpoint() -> None:
    """Video details keep all fields of the raw payload."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, content=_body({"code": 0, "data": {"bvid": "BV1", "pubdate": 5}})
        )

    client = _client(_handler)
    assert await client.view(bvid="BV1") == {"bvid": "BV1", "pubdate": 5}
    assert seen[0].url.path == "/x/web-interface/view"
    assert seen[0].url.params["bvid"] == "BV1"
    assert seen[0].headers["referer"] == "https://www.bilibili.com/"


async def test_qr_poll_collects_login_cookies() -> None:
    """A confirmed QR login stores the session cookies and returns the refresh token."""
    client = _client()
    sdk = Mock()
    client._bpi = sdk
    sdk.login.qr_poll = AsyncMock(
        return_value={
            "code": QR_CONFIRMED,
            "message": "",
            "refresh_token": "rt",
            "url": "https://passport.biligame.com/x/passport-login/web/crossDomain?"
            "DedeUserID=42&DedeUserID__ckMd5=md5&SESSDATA=sess%2Cdata&bili_jct=csrf&gourl=x",
            "cookies": [["sid", "abc"]],
        }
    )
    state = await client.qr_poll("key")
    assert state.code == QR_CONFIRMED
    assert state.refresh_token == "rt"
    assert client.is_logged_in
    assert client.mid == 42
    assert client.get_cookie("SESSDATA") == "sess,data"
    exported = parse_cookie_string(client.export_cookies())
    assert exported["bili_jct"] == "csrf"
    assert exported["sid"] == "abc"
    assert "gourl" not in exported


async def test_qr_poll_pending_keeps_anonymous() -> None:
    """While waiting for the scan no cookies are stored."""
    client = _client()
    sdk = Mock()
    client._bpi = sdk
    sdk.login.qr_poll = AsyncMock(
        return_value={"code": 86101, "message": "未扫码", "url": "", "refresh_token": ""}
    )
    state = await client.qr_poll("key")
    assert state.code == 86101
    assert not client.is_logged_in


def test_parse_cookie_string() -> None:
    """Cookie strings are split into name/value pairs."""
    assert parse_cookie_string("a=1; b = 2 ;bad; c=x=y") == {"a": "1", "b": "2", "c": "x=y"}
