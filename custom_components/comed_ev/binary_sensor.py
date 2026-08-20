"""The charge_now binary sensor — the automation trigger."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ComEdConfigEntry, ComEdCoordinator
from .entity import ComEdEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ComEdConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the charge_now binary sensor."""
    async_add_entities([ChargeNowBinarySensor(entry.runtime_data)])


class ChargeNowBinarySensor(ComEdEntity, BinarySensorEntity):
    """Turns on when the component recommends charging right now."""

    _attr_translation_key = "charge_now"
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(self, coordinator: ComEdCoordinator) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, "charge_now")

    @property
    def is_on(self) -> bool | None:
        """Return whether charging is recommended now."""
        data = self.coordinator.data
        if data is None or data.decision is None:
            return None
        return data.decision.charge_now

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Surface why the decision is what it is."""
        data = self.coordinator.data
        if data is None or data.decision is None:
            return None
        d = data.decision
        attrs: dict[str, Any] = {
            "reason": d.reason,
            "live_price": round(d.live_price, 2),
            "threshold": round(d.threshold, 2),
        }
        if d.plan is not None:
            attrs["slack_hours"] = d.plan.slack_hours
            attrs["energy_needed_kwh"] = round(d.plan.energy_needed_kwh, 2)
            attrs["feasible"] = d.plan.feasible
        return attrs
