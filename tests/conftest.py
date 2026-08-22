"""Shared pytest fixtures for HA-harness tests."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from comed_hourly_pricing import PricePoint
import pytest

pytest_plugins = "pytest_homeassistant_custom_component"

CENTRAL = ZoneInfo("America/Chicago")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading of the custom integration in every test."""
    yield


@pytest.fixture(autouse=True)
async def _unload_entries(hass):
    """Unload any entry after each test so entry-scoped background tasks (the
    settled-cost pass) are cancelled before the hass config dir is torn down."""
    yield
    from homeassistant.config_entries import ConfigEntryState

    from custom_components.comed_ev.const import DOMAIN

    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)


def _sample_points() -> tuple[PricePoint, ...]:
    base = datetime(2026, 8, 19, 3, 0, tzinfo=CENTRAL)
    from datetime import timedelta

    # A cheap overnight run with one spike, in dollars/kWh, chronological order.
    prices = [0.02, 0.025, 0.03, 0.02, 0.5, 0.03, 0.04]
    chronological = [
        PricePoint(base + timedelta(minutes=5 * i), p) for i, p in enumerate(prices)
    ]
    # The real ComEd feed is newest-first; return in that order so tests exercise
    # the same ordering production sees (newest at index 0, oldest at index -1).
    return tuple(reversed(chronological))


@pytest.fixture
def mock_client():
    """Patch the library Client used by the coordinator."""
    with patch("custom_components.comed_ev.coordinator.Client") as coord_cls:
        instance = AsyncMock()
        instance.get_five_minute_feed = AsyncMock(return_value=_sample_points())
        instance.get_current_hour_average = AsyncMock(
            return_value=PricePoint(datetime.now(CENTRAL), 0.035)
        )
        instance.get_dual = AsyncMock(return_value=())
        instance.get_next_day = AsyncMock(return_value=())
        coord_cls.return_value = instance
        yield instance
