"""
Setup flow for the Migu Music provider.

The first step asks for the address of the local migu-api-enhanced service and how to
connect:

- **SMS code** (default): enter the phone number, receive a code, enter the code.
- **Token**: paste a ``pacmtoken`` obtained elsewhere (e.g. when Migu demands an
  image captcha for the SMS request, which this flow cannot show).
- **Anonymous**: no account; only free songs at standard quality.

The phone number is only passed to the service and never stored. The resulting token
and the service address are stored as setup data.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption
from music_assistant_models.enums import ConfigEntryType

from music_assistant.models.setup_flow import SetupFlowError

from .constants import (
    AUTH_METHOD_ANONYMOUS,
    AUTH_METHOD_PASTE,
    AUTH_METHOD_SMS,
    CONF_API_BASE_URL,
    CONF_AUTH_METHOD,
    CONF_PACM,
    CONF_PHONE,
    CONF_SMS_CODE,
    DEFAULT_API_BASE_URL,
)
from .migu_client import MiguClient, MiguError, MiguLoginRequiredError

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigValueType

    from music_assistant.models.setup_flow import SetupSession

_PHONE_PATTERN = re.compile(r"^1\d{10}$")
_CODE_PATTERN = re.compile(r"^\d{4,8}$")


async def run_setup(session: SetupSession) -> None:
    """
    Run the Migu setup: pick the service and the login method, then store the token.

    :param session: The setup session driving the flow.
    """
    prefill = str(
        session.context.setup_data.get(CONF_API_BASE_URL)
        or session.context.values.get(CONF_API_BASE_URL)
        or DEFAULT_API_BASE_URL
    )
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_API_BASE_URL,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=DEFAULT_API_BASE_URL,
                    value=prefill,
                ),
                ConfigEntry(
                    key=CONF_AUTH_METHOD,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value=AUTH_METHOD_SMS,
                    options=[
                        ConfigValueOption(AUTH_METHOD_SMS),
                        ConfigValueOption(AUTH_METHOD_PASTE),
                        ConfigValueOption(AUTH_METHOD_ANONYMOUS),
                    ],
                ),
            ],
            step_id="user",
            errors=errors,
        )
        api_base_url = str(values[CONF_API_BASE_URL]).strip().rstrip("/")
        prefill = api_base_url
        client = MiguClient(session.mass.http_session, api_base_url)
        try:
            await client.version()
        except MiguError as err:
            errors = {CONF_API_BASE_URL: f"Cannot use the Migu API service: {err}"}
            continue
        method = str(values[CONF_AUTH_METHOD])
        try:
            if method == AUTH_METHOD_SMS:
                token = await _sms_login(session, client)
            elif method == AUTH_METHOD_PASTE:
                token = await _token_login(session, client)
            else:
                token = ""
        except MiguError as err:
            errors = {"base": str(err)}
            continue
        collected: dict[str, ConfigValueType] = {
            CONF_API_BASE_URL: api_base_url,
            CONF_PACM: token,
        }
        try:
            await session.finish(collected)
            return
        except SetupFlowError as err:
            errors = {"base": str(err)}


async def _sms_login(session: SetupSession, client: MiguClient) -> str:
    """
    Sign in with an SMS code and return the pacmtoken.

    Submitting the code form empty goes back to the phone form to resend the code.

    :param session: The setup session driving the flow.
    :param client: Client bound to the chosen service.
    """
    phone = ""
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_PHONE,
                    type=ConfigEntryType.STRING,
                    required=True,
                    default_value="",
                    value=phone,
                ),
            ],
            step_id="phone",
            errors=errors,
        )
        phone = re.sub(r"[\s-]", "", str(values[CONF_PHONE]))
        phone = phone.removeprefix("+86")
        if not _PHONE_PATTERN.match(phone):
            errors = {CONF_PHONE: "Enter an 11 digit mainland China mobile number"}
            continue
        try:
            await client.send_sms_code(phone)
        except MiguError as err:
            errors = {CONF_PHONE: str(err)}
            continue
        errors = None
        code_errors: dict[str, str] | None = None
        while True:
            code_values = await session.form(
                [
                    ConfigEntry(
                        key=CONF_SMS_CODE,
                        type=ConfigEntryType.STRING,
                        required=False,
                        default_value="",
                    ),
                ],
                step_id="sms_code",
                errors=code_errors,
                translation_params=[f"{phone[:3]}****{phone[-4:]}"],
            )
            code = str(code_values.get(CONF_SMS_CODE) or "").strip()
            if not code:
                break  # resend: back to the phone form
            if not _CODE_PATTERN.match(code):
                code_errors = {CONF_SMS_CODE: "Enter the digits of the code"}
                continue
            try:
                return await client.sms_login(phone, code)
            except MiguLoginRequiredError as err:
                code_errors = {CONF_SMS_CODE: str(err)}


async def _token_login(session: SetupSession, client: MiguClient) -> str:
    """
    Ask for a pacmtoken and validate it.

    :param session: The setup session driving the flow.
    :param client: Client bound to the chosen service.
    """
    errors: dict[str, str] | None = None
    while True:
        values = await session.form(
            [
                ConfigEntry(
                    key=CONF_PACM,
                    type=ConfigEntryType.SECURE_STRING,
                    required=True,
                    default_value="",
                ),
            ],
            step_id="token",
            errors=errors,
        )
        token = str(values[CONF_PACM]).strip()
        if token.lower().startswith("pacmtoken="):
            token = token.split("=", 1)[1].split(";", 1)[0].strip()
        client.set_token(token)
        try:
            await client.user_info()
        except MiguLoginRequiredError:
            errors = {CONF_PACM: "Migu rejected this token"}
            continue
        return client.token
