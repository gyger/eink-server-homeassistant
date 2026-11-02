"""Shared fixtures."""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
import socket
from typing import Any

from homeassistant.const import CONF_PORT
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.eink_server.const import DOMAIN

from . import Tablet


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture
def port() -> int:
    """A currently free localhost port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def entry(hass: HomeAssistant, port: int) -> MockConfigEntry:
    """Config entry listening on the free port."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_PORT: port}, unique_id=str(port))
    entry.add_to_hass(hass)
    return entry


type Connect = Callable[[], Coroutine[Any, Any, Tablet]]


@pytest.fixture
async def connect(port: int) -> AsyncIterator[Connect]:
    """Open simulated tablet connections; closed after the test."""
    tablets: list[Tablet] = []

    async def _connect() -> Tablet:
        tablet = Tablet(*await asyncio.open_connection("127.0.0.1", port))
        tablets.append(tablet)
        return tablet

    yield _connect

    for tablet in tablets:
        await tablet.close()
