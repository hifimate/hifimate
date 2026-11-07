"""Shared fixtures for Bilibili provider tests."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.bilibili import SUPPORTED_FEATURES, BilibiliProvider
from music_assistant.providers.bilibili.parsers import ParseContext


def _run_task(target: Any, *_args: Any, **_kwargs: Any) -> asyncio.Future[Any]:
    """Schedule coroutines handed to mass.create_task on the running loop."""
    future: asyncio.Future[Any] = asyncio.ensure_future(target)
    return future


@pytest.fixture
def api(provider: BilibiliProvider) -> AsyncMock:
    """Return the provider's mocked API client."""
    assert isinstance(provider._client, AsyncMock)
    return provider._client


@pytest.fixture
def provider() -> BilibiliProvider:
    """Create a real BilibiliProvider with a mocked API client."""
    mass = Mock()
    # @use_cache: always a miss, stores are dropped
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    mass.create_task = Mock(side_effect=_run_task)
    manifest = Mock()
    manifest.domain = "bilibili"
    config = Mock()
    config.instance_id = "bilibili--test"
    config.name = "Bilibili Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: {"log_level": "GLOBAL"}.get(
        key, default
    )
    prov = BilibiliProvider(mass, manifest, config, SUPPORTED_FEATURES)
    # normally set in handle_async_init
    prov._ctx = ParseContext("bilibili", "bilibili--test")
    prov._client = AsyncMock()
    return prov
