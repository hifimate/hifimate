"""Tests for the Bilibili interactive setup flow."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Self
from unittest.mock import Mock, patch

import pytest
from music_assistant_models.enums import FlowStepType

from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.bilibili import setup_flow as bili_flow
from music_assistant.providers.bilibili.bili_client import (
    QR_CONFIRMED,
    QR_EXPIRED,
    QR_SCANNED,
    QR_WAITING,
    BiliError,
    QrLoginState,
)
from music_assistant.providers.bilibili.constants import (
    CONF_AUTH_METHOD,
    CONF_COOKIE,
    CONF_REFRESH,
)


class _FakeClient:
    """Canned BiliClient walking a QR login through the given poll codes."""

    def __init__(self, poll_codes: list[int] | None = None, fail: bool = False) -> None:
        self._poll_codes = poll_codes or [QR_CONFIRMED]
        self._fail = fail
        self.generated = 0
        self.logged_in = False

    async def __aenter__(self) -> Self:
        if self._fail:
            raise BiliError("network down")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    @property
    def is_logged_in(self) -> bool:
        return self.logged_in

    def export_cookies(self) -> str:
        device = "buvid3=dev; buvid4=dev4"
        return (
            f"SESSDATA=sess; bili_jct=csrf; DedeUserID=42; {device}" if self.logged_in else device
        )

    async def qr_generate(self) -> tuple[str, str]:
        self.generated += 1
        return f"https://account.bilibili.com/qr?key={self.generated}", f"key{self.generated}"

    async def qr_poll(self, _key: str) -> QrLoginState:
        code = self._poll_codes.pop(0) if len(self._poll_codes) > 1 else self._poll_codes[0]
        if code == QR_CONFIRMED:
            self.logged_in = True
            return QrLoginState(code=code, refresh_token="rt")
        return QrLoginState(code=code)


def _make_session(collected: dict[str, Any]) -> tuple[SetupSession, Mock]:
    async def finish_handler(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "bilibili--test"}

    mass = Mock()
    context = SetupFlowContext(kind="setup", reason="user", domain="bilibili")
    return SetupSession(mass, "flow-test", context, finish_handler), mass


async def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


def _current_form(session: SetupSession, *, with_errors: bool = False) -> Any:
    step = session.current_step
    if step and step.type == FlowStepType.FORM and (step.errors or not with_errors):
        return step
    return None


@pytest.fixture(autouse=True)
def _fast_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bili_flow, "_QR_POLL_INTERVAL", 0.01)


async def test_qr_login_refreshes_expired_code_and_stores_session() -> None:
    """An expired code is replaced; the confirmed session cookies are persisted."""
    collected: dict[str, Any] = {}
    session, mass = _make_session(collected)
    fake = _FakeClient([QR_WAITING, QR_EXPIRED, QR_SCANNED, QR_CONFIRMED])
    with patch.object(bili_flow, "BiliClient", return_value=fake):
        task = asyncio.create_task(bili_flow.run_setup(session))
        step = await _wait_for(lambda: _current_form(session))
        assert step.step_id == "user"
        session.handle_submit({CONF_AUTH_METHOD: "qr"})
        await _wait_for(lambda: session.finished)
        await task

    assert fake.generated == 2
    assert "SESSDATA=sess" in collected[CONF_COOKIE]
    assert collected[CONF_REFRESH] == "rt"
    progress = [
        call.kwargs["data"]
        for call in mass.signal_event.call_args_list
        if call.kwargs["data"].type == FlowStepType.PROGRESS
    ]
    assert progress
    assert all(step.step_id == "scan_qr" for step in progress)
    assert all(step.image.startswith("data:image/png;base64,") for step in progress)


async def test_anonymous_setup_keeps_device_cookies_only() -> None:
    """Continuing without an account skips the QR step."""
    collected: dict[str, Any] = {}
    session, _mass = _make_session(collected)
    fake = _FakeClient()
    with patch.object(bili_flow, "BiliClient", return_value=fake):
        task = asyncio.create_task(bili_flow.run_setup(session))
        await _wait_for(lambda: _current_form(session))
        session.handle_submit({CONF_AUTH_METHOD: "anonymous"})
        await _wait_for(lambda: session.finished)
        await task

    assert fake.generated == 0
    assert collected == {CONF_COOKIE: "buvid3=dev; buvid4=dev4", CONF_REFRESH: ""}


async def test_connection_error_reshows_form() -> None:
    """A network failure loops back to the first form with an error."""
    collected: dict[str, Any] = {}
    session, _mass = _make_session(collected)
    clients = iter([_FakeClient(fail=True), _FakeClient()])
    with patch.object(bili_flow, "BiliClient", side_effect=lambda *_a, **_k: next(clients)):
        task = asyncio.create_task(bili_flow.run_setup(session))
        await _wait_for(lambda: _current_form(session))
        session.handle_submit({CONF_AUTH_METHOD: "anonymous"})
        error_form = await _wait_for(lambda: _current_form(session, with_errors=True))
        assert "network down" in str(error_form.errors["base"])
        session.handle_submit({CONF_AUTH_METHOD: "anonymous"})
        await _wait_for(lambda: session.finished)
        await task
    assert collected[CONF_COOKIE].startswith("buvid3=")


def test_qr_data_uri_is_png() -> None:
    """The QR image is a PNG data URI."""
    assert bili_flow.qr_data_uri("https://example.com").startswith("data:image/png;base64,")
