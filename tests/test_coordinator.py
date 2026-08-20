"""Coordinator/entity tests using the HA test harness."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comed_ev.const import (
    CONF_CAPACITY_KWH,
    CONF_CURRENT_SOC_ENTITY,
    CONF_TARGET_SOC_ENTITY,
    CONF_THRESHOLD_MODE,
    DOMAIN,
    MODE_AUTO,
)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="ComEd EV Charging",
        data={
            CONF_CAPACITY_KWH: 75.0,
            "efficiency": 0.9,
            CONF_CURRENT_SOC_ENTITY: "sensor.ev_soc",
            CONF_TARGET_SOC_ENTITY: "number.ev_target",
            "charge_rate_kw": 11.0,
        },
        options={CONF_THRESHOLD_MODE: MODE_AUTO},
    )


async def test_setup_creates_entities_and_decides(
    hass: HomeAssistant, mock_client
) -> None:
    """A low SOC on a cheap live price turns charge_now on."""
    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")

    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Live price from the sample feed's last point is 0.04 $/kWh -> 4 ¢/kWh.
    price = hass.states.get("sensor.comed_ev_charging_current_price")
    assert price is not None
    assert float(price.state) == 4.0

    charge = hass.states.get("binary_sensor.comed_ev_charging_charge_now")
    assert charge is not None
    assert charge.state == "on"
    assert charge.attributes["reason"] == "below_threshold"


async def test_backfill_seeds_history_and_suggestion(
    hass: HomeAssistant, mock_client
) -> None:
    """Empty history triggers chunked backfill that seeds the rolling window."""
    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")

    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    # Backfill ran with start/end ranges and populated the merged history.
    assert coordinator._history
    assert any(
        call.kwargs.get("start") is not None
        for call in mock_client.get_five_minute_feed.call_args_list
    )
    # A seeded window yields a real percentile suggestion.
    assert coordinator._suggestion.sample_size > 0


async def test_target_reached_turns_off(hass: HomeAssistant, mock_client) -> None:
    """At/above target the binary sensor stays off."""
    hass.states.async_set("sensor.ev_soc", "80")
    hass.states.async_set("number.ev_target", "80")

    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    charge = hass.states.get("binary_sensor.comed_ev_charging_charge_now")
    assert charge.state == "off"
    assert charge.attributes["reason"] == "target_reached"
