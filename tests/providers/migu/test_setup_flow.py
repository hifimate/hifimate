"""Tests for the Migu interactive setup flow."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import Mock, patch

from music_assistant_models.enums import FlowStepType

from music_assistant.models.setup_flow import SetupFlowContext, SetupSession
from music_assistant.providers.migu import setup_flow as migu_flow
from music_assistant.providers.migu.constants import (
    CONF_API_BASE_URL,
    CONF_AUTH_METHOD,
    CONF_PACM,
    CONF_PHONE,
    CONF_SMS_CODE,
)
from music_assistant.providers.migu.migu_client import (
    MiguConnectionError,
    MiguError,
    MiguLoginRequiredError,
)

API = "http://127.0.0.1:3004/migu"


class _FakeClient:
    """Canned MiguClient for the login steps."""

    def __init__(self, *, down: bool = False, accepted: str = "good") -> None:
        self.down = down
        self.accepted = accepted
        self.token = ""
        self.sent: list[str] = []
        self.captcha = False

    async def version(self) -> dict[str, Any]:
        if self.down:
            raise MiguConnectionError("refused")
        return {}

    async def send_sms_code(self, phone: str) -> None:
        if self.captcha:
            self.captcha = False
            raise MiguError("需要图形验证码")
        self.sent.append(phone)

    async def sms_login(self, phone: str, code: str) -> str:
        if code != "123456":
            raise MiguLoginRequiredError("验证码错误")
        self.token = f"tok-{phone}"
        return self.token

    def set_token(self, token: str) -> None:
        self.token = token

    async def user_info(self) -> dict[str, Any]:
        if self.token != self.accepted:
            raise MiguLoginRequiredError("请先登录")
        return {"nickName": "me"}


def _make_session(collected: dict[str, Any]) -> SetupSession:
    async def finish_handler(_session: SetupSession, values: dict[str, Any]) -> dict[str, str]:
        collected.update(values)
        return {"instance_id": "migu--test"}

    mass = Mock()
    context = SetupFlowContext(kind="setup", reason="user", domain="migu")
    return SetupSession(mass, "flow-test", context, finish_handler)


async def _wait_for(predicate: Any, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


class _Driver:
    """Submit forms of a running flow step by step."""

    def __init__(self, session: SetupSession) -> None:
        self.session = session
        self.last: Any = None

    async def form(self, step_id: str) -> Any:
        """Wait for the next (new) form step and check its id."""

        def _next() -> Any:
            step = self.session.current_step
            if step is not None and step is not self.last and step.type == FlowStepType.FORM:
                return step
            return None

        step = await _wait_for(_next)
        self.last = step
        assert step.step_id == step_id, f"expected {step_id}, got {step.step_id}"
        return step

    async def submit(self, step_id: str, values: dict[str, Any]) -> dict[str, str]:
        """Wait for a form step, submit values to it and return the errors it showed."""
        step = await self.form(step_id)
        # handle_submit clears the errors of the step object it accepts
        errors = dict(step.errors)
        self.session.handle_submit(values)
        return errors


async def _run(fake: _FakeClient, steps: Any) -> dict[str, Any]:
    collected: dict[str, Any] = {}
    session = _make_session(collected)
    with patch.object(migu_flow, "MiguClient", return_value=fake):
        task = asyncio.create_task(migu_flow.run_setup(session))
        await steps(_Driver(session))
        await _wait_for(lambda: session.finished)
        await task
    return collected


async def test_sms_login_with_retry_and_resend() -> None:
    """A wrong code shows an error; an empty code resends; the token is stored."""
    fake = _FakeClient()

    async def steps(drv: _Driver) -> None:
        await drv.submit("user", {CONF_API_BASE_URL: API + "/", CONF_AUTH_METHOD: "sms"})
        await drv.submit("phone", {CONF_PHONE: "138 0000 0000"})
        await drv.submit("sms_code", {CONF_SMS_CODE: "000000"})
        errors = await drv.submit("sms_code", {CONF_SMS_CODE: ""})
        assert "验证码错误" in errors[CONF_SMS_CODE]
        await drv.submit("phone", {CONF_PHONE: "+8613800000000"})
        await drv.submit("sms_code", {CONF_SMS_CODE: "123456"})

    collected = await _run(fake, steps)
    assert fake.sent == ["13800000000", "13800000000"]
    assert collected == {CONF_API_BASE_URL: API, CONF_PACM: "tok-13800000000"}


async def test_invalid_phone_and_captcha_reshow_phone_form() -> None:
    """A malformed number and a refused SMS request keep the user on the phone form."""
    fake = _FakeClient()
    fake.captcha = True

    async def steps(drv: _Driver) -> None:
        await drv.submit("user", {CONF_API_BASE_URL: API, CONF_AUTH_METHOD: "sms"})
        await drv.submit("phone", {CONF_PHONE: "12345"})
        errors = await drv.submit("phone", {CONF_PHONE: "13800000000"})
        assert "11 digit" in errors[CONF_PHONE]
        errors = await drv.submit("phone", {CONF_PHONE: "13800000000"})
        assert "图形验证码" in errors[CONF_PHONE]
        await drv.submit("sms_code", {CONF_SMS_CODE: "123456"})

    collected = await _run(fake, steps)
    assert collected[CONF_PACM] == "tok-13800000000"


async def test_token_login_validates_and_strips_cookie_syntax() -> None:
    """A pasted token is checked against the account API."""
    fake = _FakeClient(accepted="good")

    async def steps(drv: _Driver) -> None:
        await drv.submit("user", {CONF_API_BASE_URL: API, CONF_AUTH_METHOD: "token"})
        await drv.submit("token", {CONF_PACM: "bad"})
        errors = await drv.submit("token", {CONF_PACM: "pacmtoken=good; Path=/"})
        assert "rejected" in errors[CONF_PACM]

    collected = await _run(fake, steps)
    assert collected[CONF_PACM] == "good"


async def test_anonymous_and_unreachable_service() -> None:
    """An unreachable service reshows the first form; anonymous stores no token."""
    fake = _FakeClient(down=True)

    async def steps(drv: _Driver) -> None:
        await drv.submit("user", {CONF_API_BASE_URL: API, CONF_AUTH_METHOD: "anonymous"})
        step = await drv.form("user")
        assert "refused" in step.errors[CONF_API_BASE_URL]
        fake.down = False
        drv.session.handle_submit({CONF_API_BASE_URL: API, CONF_AUTH_METHOD: "anonymous"})

    collected = await _run(fake, steps)
    assert collected == {CONF_API_BASE_URL: API, CONF_PACM: ""}
