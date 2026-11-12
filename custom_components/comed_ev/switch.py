"""Switch entities: threshold mode (auto/manual) and the manual charge override."""

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
    """Set up the switches from a config entry."""
    async_add_entities(
        [
            ComEdThresholdModeSwitch(entry.runtime_data),
            ComEdChargeOverrideSwitch(entry.runtime_data),
        ]
    )


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


class ComEdChargeOverrideSwitch(ComEdEntity, SwitchEntity):
    """Force charge_now on while the hourly average is <= the effective ceiling."""

    _attr_translation_key = "charge_override"

    def __init__(self, coordinator: ComEdCoordinator) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, "charge_override")

    @property
    def is_on(self) -> bool:
        """Return True while the override is active."""
        return self.coordinator.settings.charge_override

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the override on."""
        await self.coordinator.async_update_setting("charge_override", True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the override off."""
        await self.coordinator.async_update_setting("charge_override", False)
