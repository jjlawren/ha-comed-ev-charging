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
    EFFICIENCY_MAX,
    EFFICIENCY_MIN,
    EFFICIENCY_MIN_SAMPLE_KWH,
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

    # Raw vehicle/wall ratio before the sample-size and range gates, so a value
    # that is being rejected (e.g. the two meters drift out of sync and push the
    # ratio past 1.0) is still visible here even when measured_efficiency is None.
    evse_total = coordinator._energy_evse_total
    raw_ratio = (
        coordinator._energy_vehicle_total / evse_total if evse_total > 0 else None
    )

    return {
        "config": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "settings": asdict(coordinator.settings),
        "state": {
            "live_price": data.live_price if data else None,
            "hourly_price": data.hourly_price if data else None,
            "effective_floor": data.effective_floor if data else None,
            "effective_ceiling": data.effective_ceiling if data else None,
            "mode": data.mode if data else None,
            "suggestion": asdict(data.suggestion) if data else None,
            "decision": decision,
        },
        "energy": {
            "vehicle_entity": coordinator._energy_vehicle_entity,
            "evse_entity": coordinator._energy_evse_entity,
            "vehicle_total_kwh": coordinator._energy_vehicle_total,
            "evse_total_kwh": evse_total,
            "raw_ratio": raw_ratio,
            "measured_efficiency": coordinator._measured_efficiency(),
            "min_sample_kwh": EFFICIENCY_MIN_SAMPLE_KWH,
            "accepted_ratio_range": [EFFICIENCY_MIN, EFFICIENCY_MAX],
            "last_meter_reads": dict(coordinator._last_energy),
            "last_decay": (
                coordinator._last_energy_decay.isoformat()
                if coordinator._last_energy_decay is not None
                else None
            ),
        },
    }
