"""Number entities: live tuning knobs for thresholds, analytics, and rates."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ComEdConfigEntry, ComEdCoordinator
from .entity import ComEdEntity

CENTS = "¢/kWh"
PERCENT = "%"
DAYS = "days"


@dataclass(frozen=True, kw_only=True)
class ComEdNumberDescription(NumberEntityDescription):
    """Describes a ComEd number bound to a ComEdSettings field."""

    field: str
    # True for knobs that feed the daily analytics suggestion and so warrant an
    # immediate recompute when changed (floor_pct/ceiling_pct/window_days).
    recompute: bool = False


NUMBERS: tuple[ComEdNumberDescription, ...] = (
    ComEdNumberDescription(
        key="price_floor",
        translation_key="price_floor",
        field="price_floor",
        native_min_value=0,
        native_max_value=100,
        native_step=0.1,
        native_unit_of_measurement=CENTS,
    ),
    ComEdNumberDescription(
        key="price_ceiling",
        translation_key="price_ceiling",
        field="price_ceiling",
        native_min_value=0,
        native_max_value=100,
        native_step=0.1,
        native_unit_of_measurement=CENTS,
    ),
    ComEdNumberDescription(
        key="min_soc",
        translation_key="min_soc",
        field="min_soc",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENT,
    ),
    ComEdNumberDescription(
        key="gamma",
        translation_key="gamma",
        field="gamma",
        native_min_value=1.0,
        native_max_value=6.0,
        native_step=0.1,
    ),
    ComEdNumberDescription(
        key="floor_pct",
        translation_key="floor_pct",
        field="floor_pct",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENT,
        recompute=True,
    ),
    ComEdNumberDescription(
        key="ceiling_pct",
        translation_key="ceiling_pct",
        field="ceiling_pct",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        native_unit_of_measurement=PERCENT,
        recompute=True,
    ),
    ComEdNumberDescription(
        key="window_days",
        translation_key="window_days",
        field="window_days",
        native_min_value=1,
        native_max_value=365,
        native_step=1,
        native_unit_of_measurement=DAYS,
        recompute=True,
    ),
    ComEdNumberDescription(
        key="flat_rate",
        translation_key="flat_rate",
        field="flat_rate",
        native_min_value=0,
        native_max_value=100,
        native_step=0.1,
        native_unit_of_measurement=CENTS,
    ),
    ComEdNumberDescription(
        key="distribution_rate",
        translation_key="distribution_rate",
        field="distribution_rate",
        native_min_value=0,
        native_max_value=100,
        native_step=0.1,
        native_unit_of_measurement=CENTS,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ComEdConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up ComEd number controls from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(ComEdNumber(coordinator, desc) for desc in NUMBERS)


class ComEdNumber(ComEdEntity, NumberEntity):
    """A coordinator-backed tuning knob persisted in the settings store."""

    entity_description: ComEdNumberDescription
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX

    def __init__(
        self, coordinator: ComEdCoordinator, description: ComEdNumberDescription
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float:
        """Return the current setting value."""
        return getattr(self.coordinator.settings, self.entity_description.field)

    async def async_set_native_value(self, value: float) -> None:
        """Persist the new value and republish the decision."""
        # Integer-valued knobs must stay ints so downstream math/serialization
        # matches their config defaults.
        if float(self.entity_description.native_step or 1).is_integer():
            value = int(value)
        await self.coordinator.async_update_setting(
            self.entity_description.field,
            value,
            recompute=self.entity_description.recompute,
        )
