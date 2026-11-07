"""
Setup flow for the Bilibili provider.

The user either signs in by scanning a QR code with the Bilibili app, or continues
without an account (public content only, audio capped at 192 kbps AAC). The QR code
is minted afresh whenever its scan window elapses. The resulting session cookies
(plus the device ids) are stored as setup data.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import segno
from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError, StepExpiredError

from .bili_client import (
    QR_CONFIRMED,
    QR_EXPIRED,
    QR_SCANNED,
    QR_WAITING,
    BiliClient,
    BiliError,
    QrLoginState,
)
from .constants import (
    AUTH_METHOD_ANONYMOUS,
    AUTH_METHOD_QR,
    CONF_AUTH_METHOD,
    CONF_COOKIE,
    CONF_REFRESH,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

# Bilibili QR codes are valid for 180 seconds; refresh a bit before that
_QR_EXPIRES_IN = 170.0
_QR_POLL_INTERVAL = 2.0


async def run_setup(session: SetupSession) -> None:
    """
    Run the Bilibili setup: choose QR login or anonymous use, then store the session.

    :param session: The setup session driving the flow.
    """
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_AUTH_METHOD,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=AUTH_METHOD_QR,
                    options=[
                        ConfigValueOption(AUTH_METHOD_QR),
                        ConfigValueOption(AUTH_METHOD_ANONYMOUS),
                    ],
                ),
            ],
            step_id="user",
            errors=errors,
        )
        try:
            collected = await _authenticate(session, str(values[CONF_AUTH_METHOD]))
        except BiliError as err:
            errors = {"base": f"Unable to connect to Bilibili: {err}"}
            continue
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": str(err)}


async def _authenticate(session: SetupSession, method: str) -> dict[str, ConfigValueType]:
    """
    Create the session for the chosen method and return the values to persist.

    :param session: The setup session driving the flow.
    :param method: AUTH_METHOD_QR or AUTH_METHOD_ANONYMOUS.
    """
    async with BiliClient() as client:
        if method != AUTH_METHOD_QR:
            # anonymous: only keep the device ids
            return {CONF_COOKIE: client.export_cookies(), CONF_REFRESH: ""}
        state = await _run_qr_login(session, client)
        return {CONF_COOKIE: client.export_cookies(), CONF_REFRESH: state.refresh_token}


async def _run_qr_login(session: SetupSession, client: BiliClient) -> QrLoginState:
    """
    Show a QR code and wait until the login is confirmed in the Bilibili app.

    :param session: The setup session driving the flow.
    :param client: A started client; receives the login cookies.
    """
    while True:
        url, qrcode_key = await client.qr_generate()
        try:
            return await session.progress_until(
                _poll_qr_login(client, qrcode_key),
                step_id="scan_qr",
                image=qr_data_uri(url),
                expires_in=_QR_EXPIRES_IN,
            )
        except StepExpiredError:
            continue


async def _poll_qr_login(client: BiliClient, qrcode_key: str) -> QrLoginState:
    """
    Poll the QR login until it is confirmed.

    :param client: The client the login cookies are stored on.
    :param qrcode_key: Key of the QR login session.
    """
    while True:
        state = await client.qr_poll(qrcode_key)
        if state.code == QR_CONFIRMED:
            if not client.is_logged_in:
                raise SetupFlowError("Bilibili login succeeded but returned no session cookie")
            return state
        if state.code == QR_EXPIRED:
            raise StepExpiredError
        if state.code not in (QR_WAITING, QR_SCANNED):
            raise SetupFlowError(f"Bilibili QR login failed: {state.message or state.code}")
        await asyncio.sleep(_QR_POLL_INTERVAL)


def qr_data_uri(content: str) -> str:
    """
    Render a QR code as PNG data URI.

    :param content: Text to encode.
    """
    return str(segno.make(content, error="m").png_data_uri(scale=6, border=2))
