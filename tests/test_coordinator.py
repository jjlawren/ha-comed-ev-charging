"""Coordinator/entity tests using the HA test harness."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comed_ev.const import (
    CONF_CAPACITY_KWH,
    CONF_CURRENT_SOC_ENTITY,
    CONF_ENERGY_EVSE_ENTITY,
    CONF_ENERGY_VEHICLE_ENTITY,
    CONF_TARGET_SOC_ENTITY,
    CONF_THRESHOLD_MODE,
    DOMAIN,
    MODE_AUTO,
)


def _entry(extra_data: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="ComEd EV Charging",
        data={
            CONF_CAPACITY_KWH: 75.0,
            "efficiency": 0.9,
            CONF_CURRENT_SOC_ENTITY: "sensor.ev_soc",
            CONF_TARGET_SOC_ENTITY: "number.ev_target",
            "charge_rate_kw": 11.0,
            **(extra_data or {}),
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


async def test_measured_efficiency_from_energy_meters(
    hass: HomeAssistant, mock_client
) -> None:
    """Meter advances accumulate into a measured vehicle/wall ratio."""
    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    hass.states.async_set("sensor.ev_delivered", "0")
    hass.states.async_set("sensor.evse_drawn", "0")

    entry = _entry(
        {
            CONF_ENERGY_VEHICLE_ENTITY: "sensor.ev_delivered",
            CONF_ENERGY_EVSE_ENTITY: "sensor.evse_drawn",
        }
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    # Below the sample-size gate, no measured value yet.
    assert coordinator._measured_efficiency() is None

    # A charging session: 10 kWh into the battery, 11.1 kWh drawn -> ~0.9.
    hass.states.async_set("sensor.ev_delivered", "10.0")
    hass.states.async_set("sensor.evse_drawn", "11.1")
    await hass.async_block_till_done()

    measured = coordinator._measured_efficiency()
    assert measured is not None
    assert round(measured, 3) == round(10.0 / 11.1, 3)

    eff = hass.states.get("sensor.comed_ev_charging_measured_efficiency")
    assert eff is not None
    assert round(float(eff.state), 3) == round(10.0 / 11.1, 3)


async def test_meter_reset_is_ignored(hass: HomeAssistant, mock_client) -> None:
    """A counter reset does not subtract from the accumulated totals."""
    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    hass.states.async_set("sensor.ev_delivered", "0")
    hass.states.async_set("sensor.evse_drawn", "0")

    entry = _entry(
        {
            CONF_ENERGY_VEHICLE_ENTITY: "sensor.ev_delivered",
            CONF_ENERGY_EVSE_ENTITY: "sensor.evse_drawn",
        }
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    hass.states.async_set("sensor.ev_delivered", "9.0")
    hass.states.async_set("sensor.evse_drawn", "10.0")
    await hass.async_block_till_done()
    # Meters reset to a lower value; the drop must not be counted.
    hass.states.async_set("sensor.ev_delivered", "0.9")
    hass.states.async_set("sensor.evse_drawn", "1.0")
    await hass.async_block_till_done()

    # The reset delta is dropped; totals keep the pre-reset advance (bar a
    # negligible sub-second decay), not the tiny post-reset reading.
    assert coordinator._energy_vehicle_total == pytest.approx(9.0, abs=1e-3)
    assert coordinator._energy_evse_total == pytest.approx(10.0, abs=1e-3)


async def test_decay_reweights_recent_sessions(
    hass: HomeAssistant, mock_client
) -> None:
    """Old totals decay before a new delta lands, so it weighs more."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    hass.states.async_set("sensor.ev_delivered", "0")
    hass.states.async_set("sensor.evse_drawn", "0")

    entry = _entry(
        {
            CONF_ENERGY_VEHICLE_ENTITY: "sensor.ev_delivered",
            CONF_ENERGY_EVSE_ENTITY: "sensor.evse_drawn",
        }
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    # An old poor session: 8 kWh delivered, 10 drawn (0.80).
    hass.states.async_set("sensor.ev_delivered", "8.0")
    hass.states.async_set("sensor.evse_drawn", "10.0")
    await hass.async_block_till_done()

    # Age it 30 days, then a fresh strong session: +9.5 kWh, +10 drawn (0.95).
    coordinator._last_energy_decay -= timedelta(days=30)
    hass.states.async_set("sensor.ev_delivered", "17.5")
    hass.states.async_set("sensor.evse_drawn", "20.0")
    await hass.async_block_till_done()

    # The decayed old session pulls the blended ratio above its own 0.80/0.875
    # midpoint toward the recent 0.95.
    measured = coordinator._measured_efficiency()
    assert measured is not None
    assert 0.875 < measured < 0.95
    # The stored totals are smaller than a plain sum thanks to the decay.
    assert coordinator._energy_evse_total < 20.0


async def test_energy_needed_and_cost_sensors(
    hass: HomeAssistant, mock_client
) -> None:
    """Energy-needed and overnight cost/avg-price sensors are published."""
    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")

    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # 60% of 75 kWh = 45 kWh to battery / 0.9 efficiency = 50 kWh from the wall.
    energy = hass.states.get("sensor.comed_ev_charging_energy_needed_to_target")
    assert energy is not None
    assert float(energy.state) == pytest.approx(50.0)

    # No departure -> the overnight window still yields a cost estimate.
    cost = hass.states.get("sensor.comed_ev_charging_estimated_charge_cost")
    assert cost is not None
    assert float(cost.state) > 0.0

    avg = hass.states.get("sensor.comed_ev_charging_estimated_charge_average_price")
    assert avg is not None
    assert float(avg.state) > 0.0


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
