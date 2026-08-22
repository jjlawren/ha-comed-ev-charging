"""Switch entity: threshold mode (on = auto/track suggestion, off = manual)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ComEdConfigEntry, ComEdCoordinator
from .entity import ComEdEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ComEdConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the threshold-mode switch from a config entry."""
    async_add_entities([ComEdThresholdModeSwitch(entry.runtime_data)])


class ComEdThresholdModeSwitch(ComEdEntity, SwitchEntity):
    """Toggle between auto (track analytics) and manual (pinned) thresholds."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "threshold_auto"

    def __init__(self, coordinator: ComEdCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, "threshold_auto")

    @property
    def is_on(self) -> bool:
        """Return True when tracking the analytics suggestion (auto)."""
        return self.coordinator.settings.threshold_auto

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch to auto: track the analytics suggestion."""
        await self.coordinator.async_update_setting("threshold_auto", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch to manual: use the pinned floor/ceiling."""
        await self.coordinator.async_update_setting("threshold_auto", False)
