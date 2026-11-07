"""Constants for the Migu Music provider."""

from __future__ import annotations

from typing import Final

# setup data (written by the setup flow, never shown as an option)
CONF_API_BASE_URL: Final[str] = "api_base_url"
CONF_PACM: Final[str] = "pacmtoken"

# setup flow forms
CONF_AUTH_METHOD: Final[str] = "auth_method"
AUTH_METHOD_SMS: Final[str] = "sms"
AUTH_METHOD_PASTE: Final[str] = "token"
AUTH_METHOD_ANONYMOUS: Final[str] = "anonymous"
CONF_PHONE: Final[str] = "phone"
CONF_SMS_CODE: Final[str] = "sms_code"

# provider options
CONF_AUDIO_QUALITY: Final[str] = "audio_quality"
QUALITY_HIRES: Final[str] = "hires"
QUALITY_LOSSLESS: Final[str] = "lossless"
QUALITY_HIGH: Final[str] = "high"
QUALITY_STANDARD: Final[str] = "standard"

# the migu-api-enhanced service (docker compose: 127.0.0.1:3004 -> container 6200)
DEFAULT_API_BASE_URL: Final[str] = "http://127.0.0.1:3004/migu"

# Migu tone flags, best first
TONE_ZQ24: Final[str] = "ZQ24"  # 24 bit FLAC
TONE_SQ: Final[str] = "SQ"  # 16 bit FLAC
TONE_HQ: Final[str] = "HQ"  # 320 kbps MP3
TONE_PQ: Final[str] = "PQ"  # 128 kbps MP3
TONES: Final[tuple[str, ...]] = (TONE_ZQ24, TONE_SQ, TONE_HQ, TONE_PQ)

# the best tone each quality option may use
QUALITY_MAX_TONE: Final[dict[str, str]] = {
    QUALITY_HIRES: TONE_ZQ24,
    QUALITY_LOSSLESS: TONE_SQ,
    QUALITY_HIGH: TONE_HQ,
    QUALITY_STANDARD: TONE_PQ,
}

IMAGE_HOST: Final[str] = "https://d.musicapp.migu.cn"
WEB_URL: Final[str] = "https://music.migu.cn/v5/#"
# playlistType values of the v5 web app routes
WEB_ALBUM_TYPE: Final[str] = "2003"
WEB_DIGITAL_ALBUM_TYPE: Final[str] = "5"

# provider id prefixes
ALBUM_PREFIX_DIGITAL: Final[str] = "d"  # digital album (数字专辑) content id
PLAYLIST_PREFIX_RANK: Final[str] = "rank_"  # chart
PLAYLIST_PREFIX_MINE: Final[str] = "my_"  # playlist owned by the signed in user
