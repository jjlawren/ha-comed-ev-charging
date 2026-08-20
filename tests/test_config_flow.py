"""Config-flow tests using the HA test harness."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.comed_ev.const import (
    CONF_CAPACITY_KWH,
    CONF_CURRENT_SOC_ENTITY,
    CONF_PRICE_CEILING,
    CONF_PRICE_FLOOR,
    CONF_TARGET_SOC_ENTITY,
    CONF_THRESHOLD_MODE,
    DOMAIN,
    MODE_AUTO,
    MODE_MANUAL,
)

BASE_INPUT = {
    CONF_CAPACITY_KWH: 75.0,
    "efficiency": 0.9,
    CONF_CURRENT_SOC_ENTITY: "sensor.ev_soc",
    CONF_TARGET_SOC_ENTITY: "number.ev_target",
    "charge_rate_kw": 11.0,
}


async def test_auto_mode_creates_entry(hass: HomeAssistant, mock_client) -> None:
    """Auto mode finishes in one step and stores mode in options."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_THRESHOLD_MODE: MODE_AUTO}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_THRESHOLD_MODE] == MODE_AUTO
    assert result["data"][CONF_CAPACITY_KWH] == 75.0
    assert CONF_THRESHOLD_MODE not in result["data"]


async def test_manual_mode_prefills_from_suggestion(
    hass: HomeAssistant, mock_client
) -> None:
    """Manual mode shows a second form pre-filled from live analytics."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**BASE_INPUT, CONF_THRESHOLD_MODE: MODE_MANUAL}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manual"

    # The sample feed's 25th/90th pct pre-fill the schema defaults (non-zero).
    schema = result["data_schema"].schema
    defaults = {str(k): k.default() for k in schema}
    assert defaults[CONF_PRICE_FLOOR] > 0
    assert defaults[CONF_PRICE_CEILING] >= defaults[CONF_PRICE_FLOOR]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PRICE_FLOOR: 3.0, CONF_PRICE_CEILING: 14.0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_PRICE_FLOOR] == 3.0
    assert result["options"][CONF_PRICE_CEILING] == 14.0
