"""Shared base entity for the ComEd EV Charging integration."""

from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ComEdCoordinator


class ComEdEntity(CoordinatorEntity[ComEdCoordinator]):
    """Base entity binding all entities to one device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ComEdCoordinator, key: str) -> None:
        """Initialize with a unique key suffix."""
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="ComEd EV Charging",
            manufacturer="ComEd (community integration)",
            entry_type=None,
        )
