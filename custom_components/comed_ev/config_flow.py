"""Config and options flow for ComEd EV Charging."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol

from .const import (
    CONF_CAPACITY_ENTITY,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_ACCEPTING_ENTITY,
    CONF_CHARGE_RATE_ENTITY,
    CONF_CHARGE_RATE_KW,
    CONF_CURRENT_SOC_ENTITY,
    CONF_DEPARTURE_ENTITY,
    CONF_EFFICIENCY,
    CONF_ENERGY_EVSE_ENTITY,
    CONF_ENERGY_VEHICLE_ENTITY,
    CONF_POLL_INTERVAL,
    CONF_TARGET_SOC_ENTITY,
    DEFAULT_EFFICIENCY,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)


def _number(min_v: float, max_v: float, step: float, unit: str | None = None):
    """Build a box-mode number selector."""
    config: dict[str, Any] = {
        "min": min_v,
        "max": max_v,
        "step": step,
        "mode": selector.NumberSelectorMode.BOX,
    }
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(selector.NumberSelectorConfig(config))


def _base_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Schema for the core setup fields (entities and battery wiring).

    Tuning knobs (thresholds, mode, rates, analytics) are live entities, not
    setup fields; only ``poll_interval`` remains in the options flow.
    """
    return vol.Schema(
        {
            vol.Optional(
                CONF_CAPACITY_KWH, default=defaults.get(CONF_CAPACITY_KWH, 75.0)
            ): _number(1, 300, 0.5, "kWh"),
            vol.Optional(CONF_CAPACITY_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor", "number", "input_number"])
            ),
            vol.Required(
                CONF_EFFICIENCY, default=defaults.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)
            ): _number(0.5, 1.0, 0.01),
            vol.Optional(CONF_ENERGY_VEHICLE_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor", device_class="energy"
                )
            ),
            vol.Optional(CONF_ENERGY_EVSE_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="sensor", device_class="energy"
                )
            ),
            vol.Required(CONF_CURRENT_SOC_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor", "number"])
            ),
            vol.Required(CONF_TARGET_SOC_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor", "number", "input_number"])
            ),
            vol.Optional(CONF_CHARGE_ACCEPTING_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain=["binary_sensor", "input_boolean", "switch"]
                )
            ),
            vol.Optional(CONF_CHARGE_RATE_KW, default=defaults.get(CONF_CHARGE_RATE_KW, 11.0)): _number(
                0.5, 350, 0.5, "kW"
            ),
            vol.Optional(CONF_CHARGE_RATE_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor", "number", "input_number"])
            ),
            vol.Optional(CONF_DEPARTURE_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["input_datetime", "sensor", "schedule"])
            ),
        }
    )


class ComEdConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the ComEd EV Charging config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect core setup fields. Tuning knobs are live entities."""
        if user_input is not None:
            return self.async_create_entry(
                title="ComEd EV Charging", data=user_input, options={}
            )
        return self.async_show_form(step_id="user", data_schema=_base_schema({}))

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the wired entities and battery settings after setup."""
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            return self.async_update_reload_and_abort(entry, data=user_input)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _base_schema(dict(entry.data)), entry.data
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> ComEdOptionsFlow:
        """Return the options flow handler."""
        return ComEdOptionsFlow()


class ComEdOptionsFlow(OptionsFlow):
    """Reconfigure the poll interval; other knobs are live entities."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Present and store the poll interval."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_POLL_INTERVAL,
                    default=opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): _number(1, 60, 1, "min"),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
