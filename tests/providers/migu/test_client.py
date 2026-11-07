"""Tests for the Migu API service client (HTTP faked)."""

from __future__ import annotations

import json
from types import TracebackType
from typing import Any, Self
from unittest.mock import Mock

import pytest
from aiohttp import ClientConnectionError

from music_assistant.providers.migu.migu_client import (
    MiguClient,
    MiguConnectionError,
    MiguError,
    MiguLoginRequiredError,
    MiguNotFoundError,
)

BASE = "http://127.0.0.1:3004/migu"


class _Response:
    """Minimal aiohttp response context manager."""

    def __init__(self, body: Any, status: int = 200) -> None:
        self._body = body if isinstance(body, str) else json.dumps(body)
        self.status = status

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        return None

    async def text(self) -> str:
        return self._body


class _Session:
    """Fake aiohttp session answering by path."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params: dict[str, str], **_kwargs: Any) -> _Response:
        path = url.removeprefix(BASE + "/")
        self.calls.append((path, params))
        answer = self.routes[path]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, _Response):
            return answer
        return _Response(answer)


def _client(routes: dict[str, Any], token: str = "") -> tuple[MiguClient, _Session, Mock]:
    session = _Session(routes)
    on_refresh = Mock()
    client = MiguClient(session, BASE + "/", token, on_token_refresh=on_refresh)  # type: ignore[arg-type]
    return client, session, on_refresh


async def test_search_passes_text_and_page() -> None:
    """Song search hits /search, other kinds /search/<kind>."""
    client, session, _ = _client(
        {
            "search": {"success": True, "code": "000000", "data": {"items": [1]}},
            "search/album": {"success": True, "code": "000000", "data": {"items": [2]}},
        }
    )
    assert await client.search("song", "晴天", 2) == {"items": [1]}
    assert await client.search("album", "x") == {"items": [2]}
    assert session.calls[0] == ("search", {"text": "晴天", "page": "2"})


async def test_auth_calls_send_token_and_adopt_rotated_token() -> None:
    """The token goes out as query parameter; a rotated one is kept and reported."""
    client, session, on_refresh = _client(
        {"user/myList": {"success": True, "code": "000000", "data": [], "pacmtoken": "new"}},
        token="old",
    )
    await client.user_playlists()
    assert session.calls[0][1]["pacmtoken"] == "old"
    assert client.token == "new"
    on_refresh.assert_called_once_with("new")
    # an empty or unchanged token is not a rotation
    session.routes["user/myList"] = {"success": True, "code": "000000", "pacmtoken": ""}
    await client.user_playlists()
    on_refresh.assert_called_once()


async def test_auth_call_without_token_fails_fast() -> None:
    """Account endpoints need a token."""
    client, session, _ = _client({})
    with pytest.raises(MiguLoginRequiredError):
        await client.user_info()
    assert session.calls == []


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"success": True, "code": "290001", "info": "请先登录"}, MiguLoginRequiredError),
        ({"success": True, "code": "299999", "info": "uid不能为空"}, MiguLoginRequiredError),
        ({"success": True, "code": "200000", "info": "歌曲下线"}, MiguNotFoundError),
        ({"success": True, "code": "123456", "info": "boom"}, MiguError),
        ({"success": False, "error": {"code": "290001", "info": "x"}}, MiguLoginRequiredError),
        ({"success": False, "error": "Not Found"}, MiguError),
    ],
)
async def test_result_codes(payload: dict[str, Any], error: type[Exception]) -> None:
    """Result codes map to specific errors."""
    client, _, _ = _client({"user/info": payload}, token="t")
    with pytest.raises(error):
        await client.user_info()


async def test_transport_errors_are_connection_errors() -> None:
    """Unreachable service and non-JSON answers raise MiguConnectionError."""
    client, session, _ = _client({"version": ClientConnectionError("refused")})
    with pytest.raises(MiguConnectionError, match="unreachable"):
        await client.version()
    session.routes["version"] = _Response("<html>bad gateway</html>", status=502)
    with pytest.raises(MiguConnectionError, match="invalid JSON"):
        await client.version()
    session.routes["version"] = _Response({"success": False}, status=500)
    with pytest.raises(MiguError, match="HTTP 500"):
        await client.version()


async def test_play_url_returns_data_and_sends_token() -> None:
    """Play info is the decrypted data; the token is sent when signed in."""
    data = {"url": "https://freetyst.nf.migu.cn/a.flac", "audioFormatType": "SQ"}
    client, session, _ = _client(
        {"url/v2": {"success": True, "code": "000000", "data": data}}, token="t"
    )
    assert await client.play_url("600", "", "SQ") == data
    path, params = session.calls[0]
    assert path == "url/v2"
    assert params == {"contentId": "600", "toneFlag": "SQ", "pacmtoken": "t"}
    session.routes["url/v2"] = {"success": True, "code": "000000", "data": None}
    with pytest.raises(MiguError, match="no play info"):
        await client.play_url("600", "c", "SQ")


async def test_singer_info_and_song_infos() -> None:
    """Singer info comes from resourceinfo; song infos are batched."""
    client, session, _ = _client(
        {
            "resourceinfo": {"success": True, "code": "000000", "resource": [{"singerId": "1"}]},
            "songinfo/contentId": {"success": True, "code": "000000", "data": [{"a": 1}, "x"]},
        }
    )
    assert await client.singer_info("1") == {"singerId": "1"}
    assert await client.song_infos(["1", "2"]) == [{"a": 1}]
    assert session.calls[1][1] == {"contentIds": "1,2"}
    assert await client.song_infos([]) == []
    session.routes["resourceinfo"] = {"success": True, "code": "000000", "resource": []}
    with pytest.raises(MiguNotFoundError):
        await client.singer_info("1")


async def test_lyric() -> None:
    """Lyrics are the lrc text; a failed lookup is empty."""
    client, session, _ = _client({"lyric": {"success": True, "lrc": "[00:01.00]a"}})
    assert await client.lyric("1") == "[00:01.00]a"
    session.routes["lyric"] = {"success": False, "error": "can not find resource!"}
    assert await client.lyric("1") == ""


async def test_sms_login() -> None:
    """The SMS flow sends the code, then exchanges it for a pacmtoken."""
    client, session, _ = _client(
        {
            "login/phone/msgCode": {"success": True, "status": 2000},
            "login/phone/authn": {"success": True, "data": {"pacmToken": "tok"}},
        }
    )
    await client.send_sms_code("13800000000")
    assert await client.sms_login("13800000000", "123456") == "tok"
    assert client.token == "tok"
    assert session.calls[1][1] == {"phone": "13800000000", "msgCode": "123456", "isNeedPacm": "1"}

    session.routes["login/phone/msgCode"] = {
        "success": False,
        "error": {"status": 4001, "message": "需要图形验证码"},
    }
    with pytest.raises(MiguError, match="需要图形验证码"):
        await client.send_sms_code("13800000000")
    session.routes["login/phone/authn"] = {"success": False, "error": {"message": "验证码错误"}}
    with pytest.raises(MiguLoginRequiredError, match="验证码错误"):
        await client.sms_login("13800000000", "000000")
    session.routes["login/phone/authn"] = {"success": True, "data": {"pacmToken": ""}}
    with pytest.raises(MiguLoginRequiredError, match="no pacmtoken"):
        await client.sms_login("13800000000", "123456")
