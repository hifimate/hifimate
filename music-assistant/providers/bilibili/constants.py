"""Constants for the Bilibili provider."""

from __future__ import annotations

from typing import Final

# setup data (written by the setup flow, never shown as an option)
CONF_COOKIE: Final[str] = "cookie"
CONF_REFRESH: Final[str] = "refresh_token"

# setup flow form
CONF_AUTH_METHOD: Final[str] = "auth_method"
AUTH_METHOD_QR: Final[str] = "qr"
AUTH_METHOD_ANONYMOUS: Final[str] = "anonymous"

# provider options
CONF_AUDIO_QUALITY: Final[str] = "audio_quality"
QUALITY_LOSSLESS: Final[str] = "lossless"
QUALITY_HIGH: Final[str] = "high"
QUALITY_STANDARD: Final[str] = "standard"
CONF_MUSIC_ZONE_ONLY: Final[str] = "music_zone_only"
CONF_CLEAN_TITLES: Final[str] = "clean_titles"

# Bilibili DASH audio stream ids
AUDIO_ID_64K: Final[int] = 30216
AUDIO_ID_132K: Final[int] = 30232
AUDIO_ID_192K: Final[int] = 30280
AUDIO_ID_DOLBY: Final[int] = 30250
AUDIO_ID_HIRES: Final[int] = 30251

# Bilibili partition (tid) of the music zone
MUSIC_ZONE_TID: Final[int] = 3

WEB_REFERER: Final[str] = "https://www.bilibili.com/"
WEB_ORIGIN: Final[str] = "https://www.bilibili.com"

# fav folder media types: only plain videos (2) are playable as tracks
FAV_MEDIA_TYPE_VIDEO: Final[int] = 2

# playlist id prefixes
PLAYLIST_PREFIX_FAV: Final[str] = "fav_"
