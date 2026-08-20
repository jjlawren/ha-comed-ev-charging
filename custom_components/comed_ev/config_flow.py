"""Config and options flow for ComEd EV Charging."""

from __future__ import annotations

from typing import Any

from comed_hourly_pricing import Client
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from .analytics import ThresholdSuggestion, suggest_thresholds
from .const import (
    CONF_CAPACITY_KWH,
    CONF_CEILING_PCT,
    CONF_CHARGE_RATE_ENTITY,
    CONF_CHARGE_RATE_KW,
    CONF_CURRENT_SOC_ENTITY,
    CONF_DEPARTURE_ENTITY,
    CONF_EFFICIENCY,
    CONF_FLOOR_PCT,
    CONF_GAMMA,
    CONF_MIN_SOC,
    CONF_POLL_INTERVAL,
    CONF_PRICE_CEILING,
    CONF_PRICE_FLOOR,
    CONF_TARGET_SOC_ENTITY,
    CONF_THRESHOLD_MODE,
    CONF_WINDOW_DAYS,
    DEFAULT_CEILING_PCT,
    DEFAULT_EFFICIENCY,
    DEFAULT_FLOOR_PCT,
    DEFAULT_GAMMA,
    DEFAULT_MIN_SOC,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PRICE_CEILING,
    DEFAULT_PRICE_FLOOR,
    DEFAULT_THRESHOLD_MODE,
    DEFAULT_WINDOW_DAYS,
    DOMAIN,
    MODE_AUTO,
    MODE_MANUAL,
)

_MODE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[MODE_AUTO, MODE_MANUAL],
        translation_key="threshold_mode",
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
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
    """Schema for the core setup fields (entities, battery, mode)."""
    return vol.Schema(
        {
            vol.Required(
                CONF_CAPACITY_KWH, default=defaults.get(CONF_CAPACITY_KWH, 75.0)
            ): _number(1, 300, 0.5, "kWh"),
            vol.Required(
                CONF_EFFICIENCY, default=defaults.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)
            ): _number(0.5, 1.0, 0.01),
            vol.Required(CONF_CURRENT_SOC_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor", "number"])
            ),
            vol.Required(CONF_TARGET_SOC_ENTITY): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor", "number", "input_number"])
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
            vol.Required(
                CONF_THRESHOLD_MODE,
                default=defaults.get(CONF_THRESHOLD_MODE, DEFAULT_THRESHOLD_MODE),
            ): _MODE_SELECTOR,
        }
    )


def _manual_schema(suggestion: ThresholdSuggestion) -> vol.Schema:
    """Manual-mode floor/ceiling, pre-filled from the live suggestion."""
    return vol.Schema(
        {
            vol.Required(
                CONF_PRICE_FLOOR,
                default=round(suggestion.price_floor, 2) or DEFAULT_PRICE_FLOOR,
            ): _number(0, 100, 0.1, "¢/kWh"),
            vol.Required(
                CONF_PRICE_CEILING,
                default=round(suggestion.price_ceiling, 2) or DEFAULT_PRICE_CEILING,
            ): _number(0, 100, 0.1, "¢/kWh"),
        }
    )


async def _live_suggestion(hass) -> ThresholdSuggestion:
    """Fetch a quick threshold suggestion from the last 24h 5-minute feed."""
    try:
        client = Client(session=async_get_clientsession(hass))
        points = await client.get_five_minute_feed()
        prices = [p.price * 100.0 for p in points]
        return suggest_thresholds(
            prices, floor_pct=DEFAULT_FLOOR_PCT, ceiling_pct=DEFAULT_CEILING_PCT
        )
    except Exception:  # noqa: BLE001 - pre-fill is best-effort
        return ThresholdSuggestion(DEFAULT_PRICE_FLOOR, DEFAULT_PRICE_CEILING, 0, 1)


class ComEdConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the ComEd EV Charging config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow state."""
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect core setup fields."""
        if user_input is not None:
            self._data = user_input
            if user_input[CONF_THRESHOLD_MODE] == MODE_MANUAL:
                return await self.async_step_manual()
            return self._create_entry()
        return self.async_show_form(step_id="user", data_schema=_base_schema({}))

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect manual floor/ceiling, pre-filled from live analytics."""
        if user_input is not None:
            self._data.update(user_input)
            return self._create_entry()
        suggestion = await _live_suggestion(self.hass)
        return self.async_show_form(
            step_id="manual", data_schema=_manual_schema(suggestion)
        )

    @callback
    def _create_entry(self) -> ConfigFlowResult:
        """Split collected input into entry data and options."""
        option_keys = {
            CONF_THRESHOLD_MODE,
            CONF_PRICE_FLOOR,
            CONF_PRICE_CEILING,
        }
        data = {k: v for k, v in self._data.items() if k not in option_keys}
        options = {k: v for k, v in self._data.items() if k in option_keys}
        return self.async_create_entry(
            title="ComEd EV Charging", data=data, options=options
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> ComEdOptionsFlow:
        """Return the options flow handler."""
        return ComEdOptionsFlow()


class ComEdOptionsFlow(OptionsFlow):
    """Reconfigure tuning knobs after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Present and store the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_THRESHOLD_MODE,
                    default=opts.get(CONF_THRESHOLD_MODE, DEFAULT_THRESHOLD_MODE),
                ): _MODE_SELECTOR,
                vol.Optional(
                    CONF_PRICE_FLOOR,
                    default=opts.get(CONF_PRICE_FLOOR, DEFAULT_PRICE_FLOOR),
                ): _number(0, 100, 0.1, "¢/kWh"),
                vol.Optional(
                    CONF_PRICE_CEILING,
                    default=opts.get(CONF_PRICE_CEILING, DEFAULT_PRICE_CEILING),
                ): _number(0, 100, 0.1, "¢/kWh"),
                vol.Optional(
                    CONF_MIN_SOC, default=opts.get(CONF_MIN_SOC, DEFAULT_MIN_SOC)
                ): _number(0, 100, 1, "%"),
                vol.Optional(
                    CONF_GAMMA, default=opts.get(CONF_GAMMA, DEFAULT_GAMMA)
                ): _number(1.0, 6.0, 0.1),
                vol.Optional(
                    CONF_FLOOR_PCT, default=opts.get(CONF_FLOOR_PCT, DEFAULT_FLOOR_PCT)
                ): _number(0, 100, 1),
                vol.Optional(
                    CONF_CEILING_PCT,
                    default=opts.get(CONF_CEILING_PCT, DEFAULT_CEILING_PCT),
                ): _number(0, 100, 1),
                vol.Optional(
                    CONF_WINDOW_DAYS,
                    default=opts.get(CONF_WINDOW_DAYS, DEFAULT_WINDOW_DAYS),
                ): _number(1, 365, 1, "days"),
                vol.Optional(
                    CONF_POLL_INTERVAL,
                    default=opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): _number(1, 60, 1, "min"),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
