"""Integration services for ComEd EV Charging."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import DOMAIN

SERVICE_GET_SESSIONS = "get_sessions"
SERVICE_GET_TRANSITIONS = "get_transitions"

_GET_SESSIONS_SCHEMA = vol.Schema(
    {
        vol.Optional("start"): cv.datetime,
        vol.Optional("end"): cv.datetime,
    }
)

_GET_TRANSITIONS_SCHEMA = vol.Schema(
    {
        vol.Optional("limit", default=50): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=500)
        ),
    }
)


def _loaded_coordinator(hass: HomeAssistant):
    """Return the coordinator of the first loaded entry, or None."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return entry.runtime_data
    return None


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register the domain services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_GET_SESSIONS):
        return

    async def _get_sessions(call: ServiceCall) -> ServiceResponse:
        coordinator = _loaded_coordinator(hass)
        if coordinator is None:
            raise ServiceValidationError("No loaded ComEd EV Charging entry")
        start = call.data.get("start")
        end = call.data.get("end")
        sessions = await coordinator.async_get_sessions(
            start_utc=start, end_utc=end
        )
        return {"sessions": sessions}

    async def _get_transitions(call: ServiceCall) -> ServiceResponse:
        coordinator = _loaded_coordinator(hass)
        if coordinator is None:
            raise ServiceValidationError("No loaded ComEd EV Charging entry")
        limit = call.data["limit"]
        # One interleaved, newest-first timeline of charge_now edges and reserve-
        # gate deferral spans, tagged by `kind`. Merging the newest `limit` of
        # each and re-capping yields the newest `limit` of the union.
        edges = await coordinator.async_get_transitions(limit)
        deferrals = await coordinator.async_get_deferrals(limit)
        merged = sorted(edges + deferrals, key=lambda r: r["ts"], reverse=True)
        return {"transitions": merged[:limit]}

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_SESSIONS,
        _get_sessions,
        schema=_GET_SESSIONS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_TRANSITIONS,
        _get_transitions,
        schema=_GET_TRANSITIONS_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove the domain services once no loaded entry remains."""
    if _loaded_coordinator(hass) is None:
        hass.services.async_remove(DOMAIN, SERVICE_GET_SESSIONS)
        hass.services.async_remove(DOMAIN, SERVICE_GET_TRANSITIONS)
