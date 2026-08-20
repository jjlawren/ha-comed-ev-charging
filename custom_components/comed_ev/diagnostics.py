"""Diagnostics: redacted config plus the last computed plan."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CURRENT_SOC_ENTITY,
    CONF_DEPARTURE_ENTITY,
    CONF_TARGET_SOC_ENTITY,
)
from .coordinator import ComEdConfigEntry

TO_REDACT = {CONF_CURRENT_SOC_ENTITY, CONF_TARGET_SOC_ENTITY, CONF_DEPARTURE_ENTITY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ComEdConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    decision = None
    if data and data.decision is not None:
        decision = asdict(data.decision)

    return {
        "config": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "state": {
            "live_price": data.live_price if data else None,
            "hourly_price": data.hourly_price if data else None,
            "effective_floor": data.effective_floor if data else None,
            "effective_ceiling": data.effective_ceiling if data else None,
            "mode": data.mode if data else None,
            "suggestion": asdict(data.suggestion) if data else None,
            "decision": decision,
        },
    }
