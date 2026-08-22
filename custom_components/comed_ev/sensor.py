"""Sensor entities: prices, threshold, suggestions, and deadline projection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ComEdConfigEntry, ComEdCoordinator, ComEdData
from .entity import ComEdEntity

CENTS = "¢/kWh"
DOLLARS = "$"
DOLLARS_PER_KWH = "$/kWh"


@dataclass(frozen=True, kw_only=True)
class ComEdSensorDescription(SensorEntityDescription):
    """Describes a ComEd sensor with a value extractor and attribute builder."""

    value_fn: Callable[[ComEdData], float | None]
    attrs_fn: Callable[[ComEdData], Mapping[str, Any]] | None = None
    # Present the entity only when this predicate holds for the coordinator.
    available_fn: Callable[[ComEdCoordinator], bool] = lambda _c: True


def _threshold_attrs(data: ComEdData) -> Mapping[str, Any]:
    d = data.decision
    attrs: dict[str, Any] = {
        "effective_floor": round(data.effective_floor, 2),
        "effective_ceiling": round(data.effective_ceiling, 2),
        "mode": data.mode,
    }
    if d is not None:
        attrs["threshold"] = round(d.threshold, 2)
    return attrs


def _end_soc_attrs(data: ComEdData) -> Mapping[str, Any]:
    plan = data.decision.plan if data.decision else None
    if plan is None:
        return {}
    return {
        "slack_hours": plan.slack_hours,
        "hours_needed": plan.hours_needed,
        "hours_available": plan.hours_available,
        "energy_needed_kwh": round(plan.energy_needed_kwh, 2),
        "feasible": plan.feasible,
        "forecast_source": plan.forecast_source,
    }


def _charge_cost_attrs(data: ComEdData) -> Mapping[str, Any]:
    cost = data.charge_cost
    if cost is None:
        return {}
    return {
        "energy_kwh": round(cost.energy_kwh, 2),
        "average_price": round(cost.average_price, 4),
        "hours_used": cost.hours_used,
    }


def _last_session_attrs(data: ComEdData) -> Mapping[str, Any]:
    session = data.last_session
    total = data.last_session_cost
    if session is None or session.settled_cost_cents is None or total is None:
        return {}
    supply = session.settled_cost_cents / 100.0
    return {
        "energy_kwh": round(session.energy_kwh, 2),
        "cents_per_kwh": round(total * 100.0 / session.energy_kwh, 2),
        "supply_cost": round(supply, 2),
        "distribution_cost": round(total - supply, 2),
        "energy_source": session.energy_source,
        "started": session.started_utc.isoformat(),
        "ended": session.ended_utc.isoformat(),
    }


def _has_departure(coordinator: ComEdCoordinator) -> bool:
    return coordinator._departure_entity is not None


def _has_energy_meters(coordinator: ComEdCoordinator) -> bool:
    return (
        coordinator._energy_vehicle_entity is not None
        and coordinator._energy_evse_entity is not None
    )


SENSORS: tuple[ComEdSensorDescription, ...] = (
    ComEdSensorDescription(
        key="current_price",
        translation_key="current_price",
        native_unit_of_measurement=CENTS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.live_price,
    ),
    ComEdSensorDescription(
        key="hourly_price",
        translation_key="hourly_price",
        native_unit_of_measurement=CENTS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.hourly_price,
    ),
    ComEdSensorDescription(
        key="charge_threshold",
        translation_key="charge_threshold",
        native_unit_of_measurement=CENTS,
        suggested_display_precision=2,
        value_fn=lambda d: d.decision.threshold if d.decision else None,
        attrs_fn=_threshold_attrs,
    ),
    ComEdSensorDescription(
        key="suggested_floor",
        translation_key="suggested_floor",
        native_unit_of_measurement=CENTS,
        entity_registry_enabled_default=False,
        suggested_display_precision=2,
        value_fn=lambda d: d.suggestion.price_floor,
        attrs_fn=lambda d: {
            "sample_size": d.suggestion.sample_size,
            "window_days": d.suggestion.window_days,
        },
    ),
    ComEdSensorDescription(
        key="suggested_ceiling",
        translation_key="suggested_ceiling",
        native_unit_of_measurement=CENTS,
        entity_registry_enabled_default=False,
        suggested_display_precision=2,
        value_fn=lambda d: d.suggestion.price_ceiling,
        attrs_fn=lambda d: {
            "sample_size": d.suggestion.sample_size,
            "window_days": d.suggestion.window_days,
        },
    ),
    ComEdSensorDescription(
        key="projected_end_soc",
        translation_key="projected_end_soc",
        native_unit_of_measurement="%",
        device_class=SensorDeviceClass.BATTERY,
        suggested_display_precision=1,
        available_fn=_has_departure,
        value_fn=lambda d: (
            d.decision.plan.projected_end_soc
            if d.decision and d.decision.plan
            else None
        ),
        attrs_fn=_end_soc_attrs,
    ),
    ComEdSensorDescription(
        key="energy_needed",
        translation_key="energy_needed",
        native_unit_of_measurement="kWh",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.energy_needed_kwh,
    ),
    ComEdSensorDescription(
        key="estimated_charge_cost",
        translation_key="estimated_charge_cost",
        native_unit_of_measurement=DOLLARS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.charge_cost.estimated_cost if d.charge_cost else None,
        attrs_fn=_charge_cost_attrs,
    ),
    ComEdSensorDescription(
        key="charge_avg_price",
        translation_key="charge_avg_price",
        native_unit_of_measurement=DOLLARS_PER_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda d: d.charge_cost.average_price if d.charge_cost else None,
    ),
    ComEdSensorDescription(
        key="measured_efficiency",
        translation_key="measured_efficiency",
        entity_category=EntityCategory.DIAGNOSTIC,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        available_fn=_has_energy_meters,
        value_fn=lambda d: d.measured_efficiency,
    ),
    ComEdSensorDescription(
        key="last_session_cost",
        translation_key="last_session_cost",
        native_unit_of_measurement=DOLLARS,
        suggested_display_precision=2,
        value_fn=lambda d: d.last_session_cost,
        attrs_fn=_last_session_attrs,
    ),
    ComEdSensorDescription(
        key="last_session_savings",
        translation_key="last_session_savings",
        native_unit_of_measurement=DOLLARS,
        suggested_display_precision=2,
        value_fn=lambda d: d.last_session_savings,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ComEdConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up ComEd sensors from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        ComEdSensor(coordinator, desc)
        for desc in SENSORS
        if desc.available_fn(coordinator)
    )


class ComEdSensor(ComEdEntity, SensorEntity):
    """A coordinator-backed ComEd sensor."""

    entity_description: ComEdSensorDescription

    def __init__(
        self, coordinator: ComEdCoordinator, description: ComEdSensorDescription
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """Return the current sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return extra state attributes."""
        if self.coordinator.data is None or self.entity_description.attrs_fn is None:
            return None
        return dict(self.entity_description.attrs_fn(self.coordinator.data))
