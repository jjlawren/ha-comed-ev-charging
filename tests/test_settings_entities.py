"""Tests for the live tuning entities (switch + numbers) and their persistence."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comed_ev.const import (
    CONF_CAPACITY_KWH,
    CONF_CURRENT_SOC_ENTITY,
    CONF_TARGET_SOC_ENTITY,
    DOMAIN,
    STORAGE_KEY,
)

SWITCH = "switch.comed_ev_charging_automatic_thresholds"
N_FLOOR = "number.comed_ev_charging_price_floor"
N_CEILING = "number.comed_ev_charging_price_ceiling"
N_WINDOW = "number.comed_ev_charging_history_window"
N_FLAT = "number.comed_ev_charging_flat_rate_baseline"


def _entry(extra_options: dict | None = None) -> MockConfigEntry:
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
        options=extra_options or {},
    )


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_switch_toggles_threshold_mode(
    hass: HomeAssistant, mock_client
) -> None:
    """Turning the switch off flips the coordinator to manual thresholds."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data

    assert hass.states.get(SWITCH).state == "on"
    assert coordinator.settings.threshold_auto is True

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
    )
    assert coordinator.settings.threshold_auto is False
    assert coordinator.data.mode == "manual"
    assert hass.states.get(SWITCH).state == "off"


async def test_manual_floor_pins_threshold(
    hass: HomeAssistant, mock_client
) -> None:
    """In manual mode the price-floor number drives the effective floor."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SWITCH}, blocking=True
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": N_FLOOR, "value": 5.5}, blocking=True
    )
    await hass.services.async_call(
        "number", "set_value", {"entity_id": N_CEILING, "value": 5.5}, blocking=True
    )

    assert coordinator.settings.price_floor == 5.5
    # Floor == ceiling collapses the willingness-to-pay curve to that value.
    assert coordinator.data.effective_floor == 5.5
    threshold = hass.states.get("sensor.comed_ev_charging_charge_threshold")
    assert float(threshold.state) == 5.5


async def test_flat_rate_zero_disables_savings(
    hass: HomeAssistant, mock_client
) -> None:
    """flat_rate 0 disables the savings comparison; a positive value enables it."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data

    assert coordinator._flat_rate() is None  # default 0.0
    await hass.services.async_call(
        "number", "set_value", {"entity_id": N_FLAT, "value": 10.0}, blocking=True
    )
    assert coordinator._flat_rate() == 10.0
    await hass.services.async_call(
        "number", "set_value", {"entity_id": N_FLAT, "value": 0}, blocking=True
    )
    assert coordinator._flat_rate() is None


async def test_integer_number_stays_int(hass: HomeAssistant, mock_client) -> None:
    """Integer-stepped knobs are stored as ints for clean math/serialization."""
    entry = await _setup(hass)
    coordinator = entry.runtime_data

    await hass.services.async_call(
        "number", "set_value", {"entity_id": N_WINDOW, "value": 45}, blocking=True
    )
    assert coordinator.settings.window_days == 45
    assert isinstance(coordinator.settings.window_days, int)


async def test_setting_change_is_persisted(
    hass: HomeAssistant, mock_client, hass_storage
) -> None:
    """A number change writes through to the coordinator's history store."""
    await _setup(hass)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": N_FLOOR, "value": 6.25}, blocking=True
    )
    saved = hass_storage[STORAGE_KEY]["data"]["settings"]
    assert saved["price_floor"] == 6.25


async def test_persisted_settings_win_over_options(
    hass: HomeAssistant, mock_client, hass_storage
) -> None:
    """A stored settings block overrides the option-migration seed on load."""
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "history": [],
            "settings": {
                "threshold_auto": False,
                "price_floor": 4.2,
                "price_ceiling": 12.0,
                "min_soc": 10,
                "gamma": 3.0,
                "floor_pct": 20,
                "ceiling_pct": 80,
                "window_days": 14,
                "flat_rate": 8.0,
                "distribution_rate": 5.0,
            },
        },
    }
    entry = await _setup(hass)
    settings = entry.runtime_data.settings

    assert settings.threshold_auto is False
    assert settings.price_floor == 4.2
    assert settings.window_days == 14
    assert settings.flat_rate == 8.0
