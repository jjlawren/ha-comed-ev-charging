"""Config-flow tests using the HA test harness."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comed_ev.const import (
    CONF_CAPACITY_KWH,
    CONF_CURRENT_SOC_ENTITY,
    CONF_POLL_INTERVAL,
    CONF_TARGET_SOC_ENTITY,
    DOMAIN,
)

BASE_INPUT = {
    CONF_CAPACITY_KWH: 75.0,
    "efficiency": 0.9,
    CONF_CURRENT_SOC_ENTITY: "sensor.ev_soc",
    CONF_TARGET_SOC_ENTITY: "number.ev_target",
    "charge_rate_kw": 11.0,
}


async def test_setup_creates_entry_without_tuning(
    hass: HomeAssistant, mock_client
) -> None:
    """Setup finishes in one step; tuning knobs are entities, not options."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], BASE_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CAPACITY_KWH] == 75.0
    # No tuning knobs land in options anymore.
    assert result["options"] == {}


async def test_options_flow_sets_poll_interval(
    hass: HomeAssistant, mock_client
) -> None:
    """The options flow now carries only the poll interval."""
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_INPUT, options={})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert {str(k) for k in result["data_schema"].schema} == {CONF_POLL_INTERVAL}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 10}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_POLL_INTERVAL] == 10


async def test_reconfigure_updates_entities(
    hass: HomeAssistant, mock_client
) -> None:
    """Reconfigure re-shows the setup form and updates entry data in place."""
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_INPUT, options={})
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    new_input = {
        **BASE_INPUT,
        CONF_CURRENT_SOC_ENTITY: "sensor.new_soc",
        CONF_TARGET_SOC_ENTITY: "number.new_target",
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], new_input
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_CURRENT_SOC_ENTITY] == "sensor.new_soc"
    assert entry.data[CONF_TARGET_SOC_ENTITY] == "number.new_target"
