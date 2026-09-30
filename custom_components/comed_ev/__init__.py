"""The ComEd EV Charging integration."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .coordinator import ComEdConfigEntry, ComEdCoordinator
from .frontend import async_register_frontend
from .const import DOMAIN
from .services import async_setup_services, async_unload_services

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SWITCH,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the cards at integration load, not after the first refresh.

    Entry setup waits on the ComEd API; a slow or failed first refresh would
    delay (or, on ConfigEntryNotReady, block) the card module, and clients
    that load a dashboard in that window show "Custom element doesn't exist".
    """
    await async_register_frontend(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ComEdConfigEntry) -> bool:
    """Set up ComEd EV Charging from a config entry."""
    coordinator = ComEdCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_setup_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ComEdConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
        async_unload_services(hass)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ComEdConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
