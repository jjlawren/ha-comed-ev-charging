"""DataUpdateCoordinator: fetch prices, read input entities, decide charging."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import logging

from comed_hourly_pricing import Client
from comed_hourly_pricing.const import CENTRAL
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .analytics import ThresholdSuggestion, suggest_thresholds
from .const import (
    BACKFILL_CHUNK_DAYS,
    BACKFILL_COVERAGE_SKIP,
    BACKFILL_PAUSE_SECONDS,
    CONF_CAPACITY_ENTITY,
    CONF_CAPACITY_KWH,
    CONF_CEILING_PCT,
    CONF_CHARGE_RATE_ENTITY,
    CONF_CHARGE_RATE_KW,
    CONF_CURRENT_SOC_ENTITY,
    CONF_DEPARTURE_ENTITY,
    CONF_EFFICIENCY,
    CONF_ENERGY_EVSE_ENTITY,
    CONF_ENERGY_VEHICLE_ENTITY,
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
    EFFICIENCY_MAX,
    EFFICIENCY_MIN,
    EFFICIENCY_MIN_SAMPLE_KWH,
    ENERGY_DECAY_PER_DAY,
    HOURLY_FEED_INTERVAL,
    MODE_AUTO,
    NEXT_DAY_PUBLISH_HOUR,
    OVERNIGHT_END_HOUR,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .optimizer import (
    ChargeCost,
    ChargeDecision,
    ForecastHour,
    build_forecast,
    energy_needed_kwh,
    estimate_charge_cost,
    plan_charge,
    should_charge_now,
)

_LOGGER = logging.getLogger(__name__)

type ComEdConfigEntry = ConfigEntry[ComEdCoordinator]


@dataclass
class ComEdData:
    """The per-tick result exposed to entities."""

    live_price: float | None  # ¢/kWh, latest 5-minute point
    hourly_price: float | None  # ¢/kWh, current-hour average
    decision: ChargeDecision | None
    suggestion: ThresholdSuggestion
    effective_floor: float
    effective_ceiling: float
    mode: str
    # Measured charge efficiency (vehicle/wall), None until enough samples.
    measured_efficiency: float | None = None
    # Wall kWh needed to reach the target SOC, None until SOC inputs are known.
    energy_needed_kwh: float | None = None
    # Estimated cost of the upcoming charge, None until a forecast is available.
    charge_cost: ChargeCost | None = None
    forecast: dict[datetime, ForecastHour] = field(default_factory=dict)


class ComEdCoordinator(DataUpdateCoordinator[ComEdData]):
    """Polls the ComEd feeds and recomputes the charge decision every tick."""

    config_entry: ComEdConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ComEdConfigEntry) -> None:
        """Initialize the coordinator from a config entry."""
        interval = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name="ComEd EV Charging",
            config_entry=entry,
            update_interval=timedelta(minutes=interval),
        )
        self._client = Client(session=async_get_clientsession(hass))
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # (timestamp, price_cents) rolling history, oldest first.
        self._history: list[tuple[datetime, float]] = []
        # Lifetime kWh summed from the two energy meters, and their last reads.
        self._energy_vehicle_total = 0.0
        self._energy_evse_total = 0.0
        self._last_energy: dict[str, float] = {}
        # When the decay was last applied to the totals above.
        self._last_energy_decay: datetime | None = None
        self._suggestion = ThresholdSuggestion(
            DEFAULT_PRICE_FLOOR, DEFAULT_PRICE_CEILING, 0, self._window_days
        )
        self._last_live_price: float | None = None
        self._last_hourly_price: float | None = None
        self._last_hourly_fetch: datetime | None = None
        self._last_suggest_day: date | None = None
        self._day_ahead: dict[datetime, float] = {}
        self._dual_today: dict[datetime, float] = {}
        self._unsub_state: list = []

    # --- lifecycle -----------------------------------------------------------

    async def async_setup(self) -> None:
        """Load persisted history and subscribe to input-entity changes."""
        stored = await self._store.async_load()
        # Legacy stores held the bare history list; newer ones wrap it in a dict.
        history_raw = stored.get("history") if isinstance(stored, dict) else stored
        if history_raw:
            for ts_iso, price in history_raw:
                parsed = dt_util.parse_datetime(ts_iso)
                if parsed is not None:
                    self._history.append((parsed, float(price)))
            self._prune_history()
        if isinstance(stored, dict):
            energy = stored.get("energy") or {}
            self._energy_vehicle_total = float(energy.get("vehicle", 0.0))
            self._energy_evse_total = float(energy.get("evse", 0.0))
            decayed_at = energy.get("decayed_at")
            if decayed_at:
                self._last_energy_decay = dt_util.parse_datetime(decayed_at)

        entities = [
            e
            for e in (
                self.config_entry.data.get(CONF_CURRENT_SOC_ENTITY),
                self.config_entry.data.get(CONF_TARGET_SOC_ENTITY),
                self.config_entry.data.get(CONF_CHARGE_RATE_ENTITY),
                self.config_entry.data.get(CONF_CAPACITY_ENTITY),
                self.config_entry.data.get(CONF_DEPARTURE_ENTITY),
            )
            if e
        ]
        if entities:
            self._unsub_state.append(
                async_track_state_change_event(
                    self.hass, entities, self._handle_input_change
                )
            )

        energy_entities = [
            e
            for e in (self._energy_vehicle_entity, self._energy_evse_entity)
            if e
        ]
        if energy_entities:
            # Seed last-seen reads so the first change counts a real advance,
            # not the meter's whole pre-existing total.
            for e in energy_entities:
                value = self._get_float(e)
                if value is not None:
                    self._last_energy[e] = value
            self._unsub_state.append(
                async_track_state_change_event(
                    self.hass, energy_entities, self._handle_energy_change
                )
            )

        if self._needs_backfill():
            self.config_entry.async_create_background_task(
                self.hass, self._async_backfill(), "comed_ev_backfill"
            )

    def _needs_backfill(self) -> bool:
        """True when stored history covers too little of the window to skip seeding."""
        if not self._history:
            return True
        span = dt_util.utcnow() - self._history[0][0]
        return span < timedelta(days=self._window_days * BACKFILL_COVERAGE_SKIP)

    async def _async_backfill(self) -> None:
        """Seed the rolling window from the 5-minute API in day-chunked ranges."""
        end = dt_util.now(CENTRAL)
        cursor = end - timedelta(days=self._window_days)
        collected: list[tuple[datetime, float]] = []
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=BACKFILL_CHUNK_DAYS), end)
            try:
                points = await self._client.get_five_minute_feed(
                    start=cursor, end=chunk_end
                )
            except Exception as err:  # noqa: BLE001 - backfill is best-effort
                _LOGGER.warning("ComEd backfill chunk at %s failed: %s", cursor, err)
                break
            collected.extend((p.timestamp, p.price * 100.0) for p in points)
            cursor = chunk_end
            await asyncio.sleep(BACKFILL_PAUSE_SECONDS)

        if not collected:
            return
        self._merge_history(collected)
        self._last_suggest_day = None  # force a recompute over the seeded window
        self._maybe_recompute_suggestion(dt_util.utcnow())
        await self._store.async_save(self._serialize_state())
        self.async_set_updated_data(self._build_data())
        _LOGGER.debug("ComEd backfill added %d points", len(collected))

    @callback
    def _handle_input_change(self, _event: Event) -> None:
        """Recompute immediately when an input entity changes."""
        self.async_set_updated_data(self._build_data())

    @callback
    def _handle_energy_change(self, _event: Event) -> None:
        """Accumulate meter advance and recompute measured efficiency."""
        self._accumulate_energy()
        self.async_set_updated_data(self._build_data())

    async def async_shutdown(self) -> None:
        """Unsubscribe listeners and flush history on unload."""
        for unsub in self._unsub_state:
            unsub()
        self._unsub_state.clear()
        await self._store.async_save(self._serialize_state())
        await super().async_shutdown()

    # --- main update ---------------------------------------------------------

    async def _async_update_data(self) -> ComEdData:
        """Fetch feeds, update history/suggestion, and decide charging."""
        now = dt_util.utcnow()
        try:
            points = await self._client.get_five_minute_feed()
            hour_avg = await self._client.get_current_hour_average()
        except Exception as err:
            raise UpdateFailed(f"ComEd feed fetch failed: {err}") from err

        if points:
            self._last_live_price = points[-1].price * 100.0
            self._append_history(points)

        if hour_avg is not None:
            self._last_hourly_price = hour_avg.price * 100.0

        # Hourly estimate feeds drive the cost forecast, which is available with
        # or without a departure (overnight window), so refresh unconditionally.
        if self._needs_hourly_fetch(now):
            await self._refresh_hourly_feeds()
            self._last_hourly_fetch = now

        self._maybe_recompute_suggestion(now)
        await self._store.async_save(self._serialize_state())

        return self._build_data()

    def _build_data(self) -> ComEdData:
        """Assemble a ComEdData from current inputs and the latest prices."""
        mode = self.config_entry.options.get(
            CONF_THRESHOLD_MODE, DEFAULT_THRESHOLD_MODE
        )
        if mode == MODE_AUTO and self._suggestion.sample_size > 0:
            floor = self._suggestion.price_floor
            ceiling = self._suggestion.price_ceiling
        else:
            floor = self.config_entry.options.get(
                CONF_PRICE_FLOOR, self._suggestion.price_floor or DEFAULT_PRICE_FLOOR
            )
            ceiling = self.config_entry.options.get(
                CONF_PRICE_CEILING,
                self._suggestion.price_ceiling or DEFAULT_PRICE_CEILING,
            )

        current_soc = self._get_float(self.config_entry.data.get(CONF_CURRENT_SOC_ENTITY))
        target_soc = self._get_float(self.config_entry.data.get(CONF_TARGET_SOC_ENTITY))
        live = self._last_live_price
        measured_efficiency = self._measured_efficiency()
        efficiency = measured_efficiency
        if efficiency is None:
            efficiency = self.config_entry.data.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY)

        energy_needed: float | None = None
        if current_soc is not None and target_soc is not None:
            energy_needed = energy_needed_kwh(
                current_soc, target_soc, self._capacity_kwh(), efficiency
            )

        decision: ChargeDecision | None = None
        forecast: dict[datetime, ForecastHour] = {}
        charge_cost: ChargeCost | None = None
        if current_soc is not None and target_soc is not None and live is not None:
            plan = None
            now = dt_util.utcnow()
            departure = self._get_departure()
            # Cost is priced over the deadline window, or an overnight window
            # when no departure is set; the plan is deadline-only.
            window_end = departure if departure is not None else self._overnight_end(now)
            if window_end is not None and window_end > now:
                forecast = build_forecast(
                    now,
                    window_end,
                    self._day_ahead,
                    self._dual_today,
                    self._suggestion.price_ceiling or None,
                )
            if departure is not None:
                plan = plan_charge(
                    now,
                    departure,
                    current_soc,
                    target_soc,
                    self._capacity_kwh(),
                    self._charge_rate(),
                    efficiency,
                    forecast,
                )
            if energy_needed:
                charge_cost = estimate_charge_cost(
                    forecast, energy_needed, self._charge_rate()
                )
            decision = should_charge_now(
                dt_util.utcnow(),
                current_soc,
                target_soc,
                live,
                price_floor=floor,
                price_ceiling=ceiling,
                min_soc=self.config_entry.options.get(CONF_MIN_SOC, DEFAULT_MIN_SOC),
                gamma=self.config_entry.options.get(CONF_GAMMA, DEFAULT_GAMMA),
                plan=plan,
            )

        return ComEdData(
            live_price=live,
            hourly_price=self._last_hourly_price,
            decision=decision,
            suggestion=self._suggestion,
            effective_floor=floor,
            effective_ceiling=ceiling,
            mode=mode,
            measured_efficiency=measured_efficiency,
            energy_needed_kwh=energy_needed,
            charge_cost=charge_cost,
            forecast=forecast,
        )

    def _overnight_end(self, now: datetime) -> datetime:
        """Return the next OVERNIGHT_END_HOUR (Central) as a UTC datetime."""
        central = now.astimezone(CENTRAL)
        end = central.replace(
            hour=OVERNIGHT_END_HOUR, minute=0, second=0, microsecond=0
        )
        if central >= end:
            end += timedelta(days=1)
        return dt_util.as_utc(end)

    # --- feeds & history -----------------------------------------------------

    async def _refresh_hourly_feeds(self) -> None:
        """Fetch the dual (today) and next-day estimate feeds, cached hourly."""
        try:
            dual = await self._client.get_dual()
            self._dual_today = {
                h.hour_ending: h.estimated * 100.0
                for h in dual
                if h.estimated is not None
            }
            if dt_util.now(CENTRAL).hour >= NEXT_DAY_PUBLISH_HOUR:
                next_day = await self._client.get_next_day()
                self._day_ahead = {
                    h.hour_ending: h.estimated * 100.0
                    for h in next_day
                    if h.estimated is not None
                }
        except Exception as err:  # noqa: BLE001 - hourly feeds are best-effort
            _LOGGER.debug("Hourly estimate feed refresh failed: %s", err)

    def _append_history(self, points) -> None:
        """Append newer 5-minute points (in ¢/kWh) to the rolling history."""
        self._merge_history((p.timestamp, p.price * 100.0) for p in points)

    def _merge_history(self, pairs) -> None:
        """Merge (timestamp, ¢/kWh) pairs into history, de-duped and time-sorted."""
        combined = dict(self._history)
        combined.update(pairs)
        self._history = sorted(combined.items())
        self._prune_history()

    def _prune_history(self) -> None:
        """Drop history older than the configured window."""
        cutoff = dt_util.utcnow() - timedelta(days=self._window_days)
        self._history = [(ts, p) for ts, p in self._history if ts >= cutoff]

    def _serialize_history(self) -> list[list]:
        """Serialize history to a JSON-storable form."""
        return [[ts.isoformat(), p] for ts, p in self._history]

    def _serialize_state(self) -> dict:
        """Serialize history and energy totals to a JSON-storable form."""
        return {
            "history": self._serialize_history(),
            "energy": {
                "vehicle": self._energy_vehicle_total,
                "evse": self._energy_evse_total,
                "decayed_at": (
                    self._last_energy_decay.isoformat()
                    if self._last_energy_decay is not None
                    else None
                ),
            },
        }

    # --- measured efficiency -------------------------------------------------

    def _accumulate_energy(self) -> None:
        """Decay the running totals, then add each meter's positive advance."""
        self._decay_energy(dt_util.utcnow())
        self._energy_vehicle_total += self._meter_delta(self._energy_vehicle_entity)
        self._energy_evse_total += self._meter_delta(self._energy_evse_entity)

    def _decay_energy(self, now: datetime) -> None:
        """Shrink both totals for the time elapsed so new deltas weigh more.

        Uniform decay cancels in the ratio; it matters only because it runs on
        the accumulated totals just before a fresh delta is added at full weight.
        """
        if self._last_energy_decay is not None:
            days = (now - self._last_energy_decay).total_seconds() / 86400.0
            if days > 0:
                factor = ENERGY_DECAY_PER_DAY**days
                self._energy_vehicle_total *= factor
                self._energy_evse_total *= factor
        self._last_energy_decay = now

    def _meter_delta(self, entity_id: str | None) -> float:
        """Positive kWh advance since last read; 0 on first read or a reset."""
        if not entity_id:
            return 0.0
        value = self._get_float(entity_id)
        if value is None:
            return 0.0
        last = self._last_energy.get(entity_id)
        self._last_energy[entity_id] = value
        if last is None or value < last:  # first read, or the meter reset
            return 0.0
        return value - last

    def _measured_efficiency(self) -> float | None:
        """Vehicle/wall energy ratio, or None until it is trustworthy."""
        if self._energy_evse_total < EFFICIENCY_MIN_SAMPLE_KWH:
            return None
        ratio = self._energy_vehicle_total / self._energy_evse_total
        if not EFFICIENCY_MIN <= ratio <= EFFICIENCY_MAX:
            return None
        return ratio

    def _maybe_recompute_suggestion(self, now: datetime) -> None:
        """Recompute threshold suggestions once per calendar day."""
        today = now.date()
        if self._last_suggest_day == today:
            return
        self._last_suggest_day = today
        self._suggestion = suggest_thresholds(
            (p for _, p in self._history),
            floor_pct=self.config_entry.options.get(
                CONF_FLOOR_PCT, DEFAULT_FLOOR_PCT
            ),
            ceiling_pct=self.config_entry.options.get(
                CONF_CEILING_PCT, DEFAULT_CEILING_PCT
            ),
            window_days=self._window_days,
        )

    def _needs_hourly_fetch(self, now: datetime) -> bool:
        """True if the hourly-estimate feeds are due for a refresh."""
        if self._last_hourly_fetch is None:
            return True
        return (now - self._last_hourly_fetch).total_seconds() >= HOURLY_FEED_INTERVAL

    # --- input entities ------------------------------------------------------

    def _get_float(self, entity_id: str | None) -> float | None:
        """Read a numeric state, or None if missing/unavailable."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _charge_rate(self) -> float:
        """Return the charge rate in kW from the entity, else the constant."""
        entity = self.config_entry.data.get(CONF_CHARGE_RATE_ENTITY)
        if entity:
            value = self._get_float(entity)
            if value is not None:
                return value
        return self.config_entry.data.get(CONF_CHARGE_RATE_KW, 0.0)

    def _capacity_kwh(self) -> float:
        """Return the battery capacity in kWh from the entity, else the constant."""
        entity = self.config_entry.data.get(CONF_CAPACITY_ENTITY)
        if entity:
            value = self._get_float(entity)
            if value is not None:
                return value
        return self.config_entry.data.get(CONF_CAPACITY_KWH, 0.0)

    def _get_departure(self) -> datetime | None:
        """Parse the optional departure entity into a UTC datetime."""
        entity = self._departure_entity
        if not entity:
            return None
        state = self.hass.states.get(entity)
        if state is None:
            return None
        # input_datetime exposes a `timestamp` attribute; schedules vary.
        timestamp = state.attributes.get("timestamp")
        if timestamp is not None:
            return dt_util.utc_from_timestamp(float(timestamp))
        parsed = dt_util.parse_datetime(state.state)
        return dt_util.as_utc(parsed) if parsed else None

    # --- convenience ---------------------------------------------------------

    @property
    def _departure_entity(self) -> str | None:
        return self.config_entry.data.get(CONF_DEPARTURE_ENTITY)

    @property
    def _energy_vehicle_entity(self) -> str | None:
        return self.config_entry.data.get(CONF_ENERGY_VEHICLE_ENTITY)

    @property
    def _energy_evse_entity(self) -> str | None:
        return self.config_entry.data.get(CONF_ENERGY_EVSE_ENTITY)

    @property
    def _window_days(self) -> int:
        return self.config_entry.options.get(CONF_WINDOW_DAYS, DEFAULT_WINDOW_DAYS)
