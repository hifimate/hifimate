"""Shared fixtures for Migu provider tests."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from music_assistant.providers.migu import SUPPORTED_FEATURES, MiguProvider
from music_assistant.providers.migu.parsers import ParseContext


def _run_task(target: Any, *_args: Any, **_kwargs: Any) -> asyncio.Future[Any]:
    """Schedule coroutines handed to mass.create_task on the running loop."""
    future: asyncio.Future[Any] = asyncio.ensure_future(target)
    return future


@pytest.fixture
def ctx() -> ParseContext:
    """Return a parse context."""
    return ParseContext("migu", "migu--test")


@pytest.fixture
def options() -> dict[str, Any]:
    """Return the provider options (tests may change them)."""
    return {"log_level": "GLOBAL"}


@pytest.fixture
def provider(options: dict[str, Any]) -> MiguProvider:
    """Create a real MiguProvider with a mocked API client (anonymous)."""
    mass = Mock()
    # @use_cache: always a miss, stores are dropped
    mass.cache.get_with_freshness = AsyncMock(return_value=(None, False, False))
    mass.cache.set = AsyncMock()
    mass.create_task = Mock(side_effect=_run_task)
    manifest = Mock()
    manifest.domain = "migu"
    config = Mock()
    config.instance_id = "migu--test"
    config.name = "Migu Test"
    config.enabled = True
    config.get_value.side_effect = lambda key, default=None: options.get(key, default)
    prov = MiguProvider(mass, manifest, config, SUPPORTED_FEATURES)
    # normally set in handle_async_init
    prov._ctx = ParseContext("migu", "migu--test")
    prov._public_own_playlists = set()
    client = AsyncMock()
    client.is_logged_in = False
    prov._client = client
    return prov


@pytest.fixture
def api(provider: MiguProvider) -> AsyncMock:
    """Return the provider's mocked API client."""
    assert isinstance(provider._client, AsyncMock)
    return provider._client
