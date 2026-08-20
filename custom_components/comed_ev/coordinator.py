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
    HOURLY_FEED_INTERVAL,
    MODE_AUTO,
    NEXT_DAY_PUBLISH_HOUR,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .optimizer import (
    ChargeDecision,
    ForecastHour,
    build_forecast,
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
        self._store: Store[list[list]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # (timestamp, price_cents) rolling history, oldest first.
        self._history: list[tuple[datetime, float]] = []
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
        if stored:
            for ts_iso, price in stored:
                parsed = dt_util.parse_datetime(ts_iso)
                if parsed is not None:
                    self._history.append((parsed, float(price)))
            self._prune_history()

        entities = [
            e
            for e in (
                self.config_entry.data.get(CONF_CURRENT_SOC_ENTITY),
                self.config_entry.data.get(CONF_TARGET_SOC_ENTITY),
                self.config_entry.data.get(CONF_CHARGE_RATE_ENTITY),
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
        await self._store.async_save(self._serialize_history())
        self.async_set_updated_data(self._build_data())
        _LOGGER.debug("ComEd backfill added %d points", len(collected))

    @callback
    def _handle_input_change(self, _event: Event) -> None:
        """Recompute immediately when an input entity changes."""
        self.async_set_updated_data(self._build_data())

    async def async_shutdown(self) -> None:
        """Unsubscribe listeners and flush history on unload."""
        for unsub in self._unsub_state:
            unsub()
        self._unsub_state.clear()
        await self._store.async_save(self._serialize_history())
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

        if self._departure_entity and self._needs_hourly_fetch(now):
            await self._refresh_hourly_feeds()
            self._last_hourly_fetch = now

        self._maybe_recompute_suggestion(now)
        await self._store.async_save(self._serialize_history())

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

        decision: ChargeDecision | None = None
        forecast: dict[datetime, ForecastHour] = {}
        if current_soc is not None and target_soc is not None and live is not None:
            plan = None
            departure = self._get_departure()
            if departure is not None:
                now = dt_util.utcnow()
                forecast = build_forecast(
                    now,
                    departure,
                    self._day_ahead,
                    self._dual_today,
                    self._suggestion.price_ceiling or None,
                )
                plan = plan_charge(
                    now,
                    departure,
                    current_soc,
                    target_soc,
                    self.config_entry.data[CONF_CAPACITY_KWH],
                    self._charge_rate(),
                    self.config_entry.data.get(CONF_EFFICIENCY, DEFAULT_EFFICIENCY),
                    forecast,
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
            forecast=forecast,
        )

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
    def _window_days(self) -> int:
        return self.config_entry.options.get(CONF_WINDOW_DAYS, DEFAULT_WINDOW_DAYS)
