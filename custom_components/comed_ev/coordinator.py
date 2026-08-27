"""DataUpdateCoordinator: fetch prices, read input entities, decide charging."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
import json
import logging
from math import ceil
import statistics

from comed_hourly_pricing import Client
from comed_hourly_pricing.const import CENTRAL
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
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
    CONF_CHARGE_ACCEPTING_ENTITY,
    CONF_CHARGE_RATE_ENTITY,
    CONF_CHARGE_RATE_KW,
    CONF_CURRENT_SOC_ENTITY,
    CONF_DEPARTURE_ENTITY,
    CONF_DISTRIBUTION_RATE,
    CONF_EFFICIENCY,
    CONF_ENERGY_EVSE_ENTITY,
    CONF_ENERGY_VEHICLE_ENTITY,
    CONF_FLAT_RATE,
    CONF_FLOOR_PCT,
    CONF_GAMMA,
    CONF_MIN_SOC,
    CONF_POLL_INTERVAL,
    CONF_PRICE_CEILING,
    CONF_PRICE_FLOOR,
    CONF_TARGET_SOC_ENTITY,
    CONF_THRESHOLD_MODE,
    CONF_WINDOW_DAYS,
    DEADBAND_K,
    DEADBAND_MAX,
    DEADBAND_MIN,
    DEFAULT_CEILING_PCT,
    DEFAULT_DISTRIBUTION_RATE,
    DEFAULT_EFFICIENCY,
    DEFAULT_FLAT_RATE,
    DEFAULT_FLOOR_PCT,
    DEFAULT_GAMMA,
    DEFAULT_MIN_SOC,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PRICE_CEILING,
    DEFAULT_PRICE_FLOOR,
    DEFAULT_THRESHOLD_MODE,
    DEFAULT_WINDOW_DAYS,
    DEFERRAL_GRACE_SECONDS,
    DEFERRAL_MIN_DURATION_SECONDS,
    DEFERRAL_RETENTION,
    EFFICIENCY_MAX,
    EFFICIENCY_MIN,
    EFFICIENCY_MIN_SAMPLE_KWH,
    ENERGY_DECAY_PER_DAY,
    EVENT_CHARGE_STARTED,
    EVENT_CHARGE_STOPPED,
    HOURLY_FEED_INTERVAL,
    MIN_OFF_SECONDS,
    MODE_AUTO,
    MODE_MANUAL,
    NEXT_DAY_PUBLISH_HOUR,
    OVERNIGHT_END_HOUR,
    SESSION_DB_FILENAME,
    SETTLE_INTERVAL_SECONDS,
    STORAGE_KEY,
    STORAGE_VERSION,
    TRANSITION_RETENTION,
    VOLATILITY_WINDOW_MINUTES,
)
from .optimizer import (
    MODE_DEADLINE,
    MODE_OPPORTUNISTIC,
    REASON_CHEAPER_LATER,
    REASON_MIN_OFF_LOCKOUT,
    SOURCE_FALLBACK,
    ChargeCost,
    ChargeDecision,
    ForecastHour,
    Schedule,
    build_forecast,
    clamp,
    energy_needed_kwh,
    estimate_charge_cost,
    hour_buckets,
    plan_charge,
    project_schedule,
    should_charge_now,
)
from .session_store import Session, SessionStore

_LOGGER = logging.getLogger(__name__)

# Decision reasons that count as a reserve-gate deferral: the optimizer would
# charge on price but held the start off to reach a cheaper hour. Scoped to
# `cheaper_later` for now — `min_off_lockout` is already surfaced on the start it
# delays (the Activity card's "held N min" note), so recording it too would
# double-count.
_DEFERRAL_REASONS = frozenset({REASON_CHEAPER_LATER})


# A start edge and its session's `started_utc` come from the same charge-start
# event, so they land within a poll of each other; this bounds the match against
# a stray edge with no session of its own.
_SESSION_EDGE_MATCH_TOL = timedelta(minutes=15)


def _session_started_at(sessions: Iterable[Session], ts: datetime) -> Session | None:
    """The session whose start is nearest `ts`, within the match tolerance."""
    best: Session | None = None
    best_delta = _SESSION_EDGE_MATCH_TOL
    for session in sessions:
        delta = abs(session.started_utc - ts)
        if delta <= best_delta:
            best_delta = delta
            best = session
    return best


type ComEdConfigEntry = ConfigEntry[ComEdCoordinator]


@dataclass
class ComEdSettings:
    """Live tuning knobs, exposed as number/switch entities.

    These were once config-flow options; they now live on the coordinator as the
    single source of truth, persisted in the history store and adjustable at
    runtime. `threshold_auto` True tracks the analytics suggestion; False pins the
    manual floor/ceiling. `flat_rate` 0.0 disables the savings comparison.
    """

    threshold_auto: bool
    price_floor: float
    price_ceiling: float
    min_soc: float
    gamma: float
    floor_pct: int
    ceiling_pct: int
    window_days: int
    flat_rate: float
    distribution_rate: float

    @classmethod
    def from_options(cls, options: dict) -> ComEdSettings:
        """Seed from legacy config-flow options, falling back to defaults.

        Lets existing installs carry their configured values forward on upgrade.
        """
        mode = options.get(CONF_THRESHOLD_MODE, DEFAULT_THRESHOLD_MODE)
        return cls(
            threshold_auto=mode == MODE_AUTO,
            price_floor=options.get(CONF_PRICE_FLOOR, DEFAULT_PRICE_FLOOR),
            price_ceiling=options.get(CONF_PRICE_CEILING, DEFAULT_PRICE_CEILING),
            min_soc=options.get(CONF_MIN_SOC, DEFAULT_MIN_SOC),
            gamma=options.get(CONF_GAMMA, DEFAULT_GAMMA),
            floor_pct=options.get(CONF_FLOOR_PCT, DEFAULT_FLOOR_PCT),
            ceiling_pct=options.get(CONF_CEILING_PCT, DEFAULT_CEILING_PCT),
            window_days=options.get(CONF_WINDOW_DAYS, DEFAULT_WINDOW_DAYS),
            flat_rate=options.get(CONF_FLAT_RATE) or DEFAULT_FLAT_RATE,
            distribution_rate=(
                options.get(CONF_DISTRIBUTION_RATE) or DEFAULT_DISTRIBUTION_RATE
            ),
        )


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
    # Forward projection of the charge decision across the forecast window,
    # None until a forecast and SOC inputs are available.
    schedule: Schedule | None = None
    # Most recent fully-settled session, None until one has settled.
    last_session: Session | None = None
    # Actual cost ($) of that session: settled supply + distribution, None
    # until the session has a settled supply cost.
    last_session_cost: float | None = None
    # Savings ($) of that session vs. the flat-rate baseline, None when the
    # baseline is unset or the session has no settled cost.
    last_session_savings: float | None = None


@dataclass
class _OpenSession:
    """An in-progress charge session, tracked in memory until it ends.

    `wall_kwh` accumulates the EVSE (wall) meter advance for the run; it is 0.0
    when no wall meter is configured, and the SOC fallback is used at close.
    """

    started_utc: datetime
    start_soc: float | None
    wall_kwh: float = 0.0


@dataclass
class _OpenDeferral:
    """An in-progress reserve-gate deferral episode, tracked in memory.

    Opened when the reserve gate first holds a would-be charge off; finalized to
    one DB row once the hold has ended and stayed ended for the grace window. Only
    the entry operands are kept — the row is a span, not a per-tick series — so no
    write happens while the hold is live.
    """

    started_utc: datetime
    reason: str
    mode: str
    decision_price: float | None
    min_ahead: float | None
    # When the hold first ended (the predicate went false); None while still
    # holding. The episode stays open across the grace window so a boundary flap
    # does not split it, but this is the true end time recorded on the row.
    pending_close_utc: datetime | None = None
    # Why the hold ended (the decision reason at `pending_close_utc`). Refreshed
    # if the hold re-asserts then ends again, so the row names the final outcome.
    ended_reason: str | None = None


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
        # Live tuning knobs, seeded from legacy options (migration) and later
        # overwritten by the persisted "settings" block in async_setup.
        self.settings = ComEdSettings.from_options(dict(entry.options))
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
        # Durable per-session cost store, and in-flight session tracking.
        self._session_store = SessionStore(
            hass.config.path(".storage", SESSION_DB_FILENAME)
        )
        # None until the first decision is observed; guards Option-A dropping of
        # a session already in progress when the process (re)starts.
        self._prev_charging: bool | None = None
        self._session: _OpenSession | None = None
        # In-flight reserve-gate deferral episode, None between holds. In-memory
        # only: a hold straddling a restart is dropped, like the session tracker.
        self._deferral: _OpenDeferral | None = None
        # Last emitted charge_now, driving both the opportunistic ON hysteresis
        # (see should_charge_now `charging`) and edge detection for transitions.
        # None until the first decision, so no synthetic edge fires at startup.
        self._charge_state: bool | None = None
        # When charging last stopped; gates the minimum-off-time lockout. Cleared
        # on restart, so no stale lockout survives a process restart.
        self._last_off_utc: datetime | None = None
        # True once the lockout has actually blocked a would-be ON since the last
        # stop; stamped onto the next start transition, then reset.
        self._lockout_held: bool = False
        # Most recent volatility (sigma) and ON deadband, surfaced in diagnostics
        # and stamped onto each recorded transition.
        self._last_volatility: float = 0.0
        self._last_deadband: float = 0.0
        # Cached most-recent settled session, refreshed by the settle pass.
        self._last_session: Session | None = None
        # Whether the one-time startup settle pass has run.
        self._settled_once = False

    # --- lifecycle -----------------------------------------------------------

    async def async_setup(self) -> None:
        """Load persisted history and subscribe to input-entity changes."""
        await self.hass.async_add_executor_job(self._session_store.setup)
        self._last_session = await self.hass.async_add_executor_job(
            self._session_store.get_last_settled_session
        )
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
            # Persisted knobs win over the option-migration seed. Merge onto the
            # current settings so a stored dict missing a newer field keeps its
            # migrated default rather than raising.
            saved = stored.get("settings")
            if isinstance(saved, dict):
                current = asdict(self.settings)
                merged = {k: saved.get(k, v) for k, v in current.items()}
                self.settings = ComEdSettings(**merged)

        entities = [
            e
            for e in (
                self.config_entry.data.get(CONF_CURRENT_SOC_ENTITY),
                self.config_entry.data.get(CONF_TARGET_SOC_ENTITY),
                self.config_entry.data.get(CONF_CHARGE_RATE_ENTITY),
                self.config_entry.data.get(CONF_CAPACITY_ENTITY),
                self.config_entry.data.get(CONF_DEPARTURE_ENTITY),
                self.config_entry.data.get(CONF_CHARGE_ACCEPTING_ENTITY),
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

        # Backfill settled prices daily; the first pass runs inline on the first
        # update tick (see _async_update_data) so no task outlives a teardown.
        self._unsub_state.append(
            async_track_time_interval(
                self.hass,
                self._handle_settle_timer,
                timedelta(seconds=SETTLE_INTERVAL_SECONDS),
            )
        )

    @callback
    def _handle_settle_timer(self, _now: datetime) -> None:
        """Kick the settled-cost backfill off the timer thread."""
        self.config_entry.async_create_background_task(
            self.hass, self._async_settle_costs(), "comed_ev_settle"
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
    def _publish(self) -> None:
        """Rebuild data and notify entities WITHOUT resetting the poll clock.

        async_set_updated_data() cancels and reschedules the update_interval
        timer. Calling it from frequently-firing external triggers (input/energy
        state changes) perpetually pushes the next API poll into the future, so
        the feeds never refresh on their own. async_update_listeners() publishes
        the new decision data to entities and leaves the poll schedule intact.
        """
        self.data = self._build_data()
        self.async_update_listeners()

    @callback
    def _handle_input_change(self, _event: Event) -> None:
        """Recompute immediately when an input entity changes."""
        self._publish()

    @callback
    def _handle_energy_change(self, _event: Event) -> None:
        """Accumulate meter advance and recompute measured efficiency."""
        self._accumulate_energy()
        self._publish()

    async def async_shutdown(self) -> None:
        """Unsubscribe listeners and flush history on unload."""
        for unsub in self._unsub_state:
            unsub()
        self._unsub_state.clear()
        await self._store.async_save(self._serialize_state())
        await super().async_shutdown()

    async def async_update_setting(
        self, field: str, value: object, *, recompute: bool = False
    ) -> None:
        """Apply a settings change from a control entity, persist, and republish.

        `recompute` forces a same-day suggestion recompute for knobs that feed the
        analytics suggestion (floor_pct/ceiling_pct/window_days); the decision is
        then pushed immediately so entities reflect it without waiting for a tick.
        """
        setattr(self.settings, field, value)
        if recompute:
            self._last_suggest_day = None
            self._maybe_recompute_suggestion(dt_util.utcnow())
        await self._store.async_save(self._serialize_state())
        self._publish()

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
            # The feed is newest-first, so the latest 5-minute point is points[0].
            self._last_live_price = points[0].price * 100.0
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

        data = self._build_data()
        await self._update_session(data, now)
        if not self._settled_once:
            # Run the startup settle inline on the first tick so it is awaited by
            # first_refresh and never lingers past teardown.
            self._settled_once = True
            await self._async_settle_costs()
            data = self._build_data()
        return data

    def _build_data(self) -> ComEdData:
        """Assemble a ComEdData from current inputs and the latest prices."""
        mode = MODE_AUTO if self.settings.threshold_auto else MODE_MANUAL
        if self.settings.threshold_auto and self._suggestion.sample_size > 0:
            floor = self._suggestion.price_floor
            ceiling = self._suggestion.price_ceiling
        else:
            floor = self.settings.price_floor
            ceiling = self.settings.price_ceiling

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

        # Decide against the running hourly average, not a single 5-minute point:
        # the price paid is the settled hour-average, and the running average is
        # the best live proxy for it.
        decision_price = self._last_hourly_price
        decision: ChargeDecision | None = None
        forecast: dict[datetime, ForecastHour] = {}
        charge_cost: ChargeCost | None = None
        schedule: Schedule | None = None
        if (
            current_soc is not None
            and target_soc is not None
            and decision_price is not None
        ):
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
                    forecast,
                    energy_needed,
                    self._charge_rate(),
                    self._distribution_rate(),
                )
            # Deadline scheduling needs the whole hours of charging still
            # required, so it can reserve the cheapest hours before departure.
            rate = self._charge_rate()
            hours_needed = (
                ceil(energy_needed / rate) if energy_needed and rate > 0 else None
            )
            # Volatility-scaled ON deadband damps short-cycling around the SOC
            # threshold (opportunistic mode only); the current charge state makes
            # it asymmetric so a spike still releases at once.
            volatility = self._price_volatility(now)
            deadband = clamp(DEADBAND_K * volatility, DEADBAND_MIN, DEADBAND_MAX)
            self._last_volatility = volatility
            self._last_deadband = deadband
            # Rate-limit re-engaging after a stop (opportunistic only); OFF and
            # already-charging runs are unaffected inside the optimizer.
            min_off_active = (
                self._last_off_utc is not None
                and (now - self._last_off_utc) < timedelta(seconds=MIN_OFF_SECONDS)
            )
            decision = should_charge_now(
                now,
                current_soc,
                target_soc,
                decision_price,
                price_floor=floor,
                price_ceiling=ceiling,
                min_soc=self.settings.min_soc,
                gamma=self.settings.gamma,
                plan=plan,
                forecast=forecast,
                hours_needed=hours_needed,
                charging=bool(self._charge_state),
                deadband=deadband,
                min_off_active=min_off_active,
                charge_accepting=self._get_bool(
                    self.config_entry.data.get(CONF_CHARGE_ACCEPTING_ENTITY)
                ),
            )
            if decision.reason == REASON_MIN_OFF_LOCKOUT:
                # A would-be ON was actually held; remember it for the eventual
                # start edge, so the record marks a lockout-delayed start.
                self._lockout_held = True
            self._note_transition(decision, now, volatility)
            self._note_deferral(decision, now)
            # The schedule card is a forward view of published rate estimates, so
            # drop hours carried only by the flat current-hour fallback (rates
            # not yet posted, e.g. past midnight) — they would render as a run of
            # identical rows. Decision and cost above still use the full forecast.
            published = {
                end: hour
                for end, hour in forecast.items()
                if hour.source != SOURCE_FALLBACK
            }
            if published:
                schedule = project_schedule(
                    now,
                    published,
                    current_soc,
                    target_soc,
                    self._capacity_kwh(),
                    rate,
                    efficiency,
                    price_floor=floor,
                    price_ceiling=ceiling,
                    min_soc=self.settings.min_soc,
                    gamma=self.settings.gamma,
                    departure=departure,
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
            schedule=schedule,
            last_session=self._last_session,
            last_session_cost=self._session_total_cost(self._last_session),
            last_session_savings=self._session_savings(self._last_session),
        )

    def _flat_rate(self) -> float | None:
        """Flat ¢/kWh baseline for savings, or None when disabled (0)."""
        return self.settings.flat_rate or None

    def _distribution_rate(self) -> float:
        """Fixed distribution ¢/kWh added to settled supply, 0 when unset."""
        return self.settings.distribution_rate

    def _session_total_cost(self, session: Session | None) -> float | None:
        """Actual cost ($) of a settled session: supply + distribution."""
        if session is None or session.settled_cost_cents is None:
            return None
        dist_cents = self._distribution_rate() * session.energy_kwh
        return (session.settled_cost_cents + dist_cents) / 100.0

    async def async_get_sessions(
        self, start_utc: datetime | None = None, end_utc: datetime | None = None
    ) -> list[dict]:
        """Return serialized session rows for the get_sessions service."""
        sessions = await self.hass.async_add_executor_job(
            partial(
                self._session_store.list_sessions,
                start_utc=start_utc,
                end_utc=end_utc,
            )
        )
        return [self._session_to_dict(s) for s in sessions]

    def _session_to_dict(self, session: Session) -> dict:
        """Serialize a session for service output, with derived cost fields."""
        supply_cost = (
            session.settled_cost_cents / 100.0
            if session.settled_cost_cents is not None
            else None
        )
        total_cost = self._session_total_cost(session)
        dist_cost = (
            round(self._distribution_rate() * session.energy_kwh / 100.0, 4)
            if session.settled_cost_cents is not None
            else None
        )
        cents_per_kwh = (
            round(total_cost * 100.0 / session.energy_kwh, 2)
            if total_cost is not None and session.energy_kwh
            else None
        )
        return {
            "id": session.id,
            "started": session.started_utc.isoformat(),
            "ended": session.ended_utc.isoformat(),
            "energy_kwh": round(session.energy_kwh, 3),
            "energy_source": session.energy_source,
            "start_soc": session.start_soc,
            "end_soc": session.end_soc,
            "supply_cost": supply_cost,
            "distribution_cost": dist_cost,
            "total_cost": total_cost,
            "cents_per_kwh": cents_per_kwh,
            "settled_complete": session.settled_complete,
            "savings": self._session_savings(session),
        }

    def _session_savings(self, session: Session | None) -> float | None:
        """Savings ($) of a settled session vs. the flat-rate baseline."""
        flat = self._flat_rate()
        if session is None or session.settled_cost_cents is None or flat is None:
            return None
        baseline_cents = flat * session.energy_kwh
        return (baseline_cents - session.settled_cost_cents) / 100.0

    # --- session tracking ----------------------------------------------------

    async def _update_session(self, data: ComEdData, now: datetime) -> None:
        """Detect charge_now edges and record a session row when one ends.

        Option A for restart handling: a session already in progress when this
        process starts (charging observed before any rising edge) is dropped, so
        every recorded row spans a full run this process actually saw.
        """
        charging = bool(data.decision and data.decision.charge_now)
        if self._prev_charging is None:
            # First observation this process; adopt no in-flight session.
            if charging:
                _LOGGER.debug(
                    "charge_now already on at startup; dropping straddling session"
                )
            self._prev_charging = charging
            return

        if charging and not self._prev_charging:
            self._session = _OpenSession(
                started_utc=now,
                start_soc=self._get_float(
                    self.config_entry.data.get(CONF_CURRENT_SOC_ENTITY)
                ),
            )
        elif not charging and self._prev_charging and self._session is not None:
            await self._close_session(now)
            self._session = None
        self._prev_charging = charging

    async def _close_session(self, now: datetime) -> None:
        """Compute a finished session's energy and persist it."""
        session = self._session
        assert session is not None
        end_soc = self._get_float(self.config_entry.data.get(CONF_CURRENT_SOC_ENTITY))
        energy_kwh, source = self._session_energy(session, end_soc)
        if energy_kwh <= 0:
            _LOGGER.debug("session closed with no measurable energy; not recording")
            return
        await self.hass.async_add_executor_job(
            partial(
                self._session_store.insert_session,
                started_utc=session.started_utc,
                ended_utc=now,
                energy_kwh=energy_kwh,
                energy_source=source,
                start_soc=session.start_soc,
                end_soc=end_soc,
            )
        )
        _LOGGER.debug(
            "recorded session: %.2f kWh (%s) over %s",
            energy_kwh,
            source,
            now - session.started_utc,
        )

    def _session_energy(
        self, session: _OpenSession, end_soc: float | None
    ) -> tuple[float, str]:
        """Return (wall_kwh, source) for a finished session.

        Prefer the accumulated EVSE (wall) meter advance; otherwise derive wall
        energy from the SOC rise using the same formula as `energy_needed_kwh`.
        """
        if self._energy_evse_entity and session.wall_kwh > 0:
            return session.wall_kwh, "meter"
        if session.start_soc is not None and end_soc is not None and end_soc > session.start_soc:
            efficiency = self._measured_efficiency()
            if efficiency is None:
                efficiency = self.config_entry.data.get(
                    CONF_EFFICIENCY, DEFAULT_EFFICIENCY
                )
            wall = energy_needed_kwh(
                session.start_soc, end_soc, self._capacity_kwh(), efficiency
            )
            return wall, "soc"
        return 0.0, "soc"

    # --- charge transitions --------------------------------------------------

    def _price_volatility(self, now: datetime) -> float:
        """Population stddev (¢/kWh) of the recent 5-minute prices.

        Measured over a trailing window rather than the current calendar-hour
        bucket, which is too sparse early in an hour to give a stable spread.
        0.0 with fewer than two points. Feeds both the ON deadband and the
        recorded transition context.
        """
        cutoff = now - timedelta(minutes=VOLATILITY_WINDOW_MINUTES)
        prices = [p for ts, p in self._history if ts >= cutoff]
        if len(prices) < 2:
            return 0.0
        return statistics.pstdev(prices)

    @callback
    def _note_transition(
        self, decision: ChargeDecision, now: datetime, volatility: float
    ) -> None:
        """Detect a charge_now edge, then log it, fire an event, and persist it.

        `_charge_state` tracks the last emitted decision across every publish
        (polls and input-change republishes), so an edge is caught wherever it
        happens. The first observation only seeds the state — no synthetic
        startup edge — mirroring the session tracker. The durable record goes to
        the integration-owned SQLite store to keep the HA recorder small; the
        bus event stays lean for live automations and the logbook.
        """
        prev = self._charge_state
        self._charge_state = decision.charge_now
        if prev is None or decision.charge_now == prev:
            return

        if decision.charge_now:
            # A start consumes any lockout that held it; capture then reset.
            lockout_held = self._lockout_held
        else:
            # A stop opens a fresh min-off window: no lockout has held yet.
            self._last_off_utc = now
            lockout_held = False
        self._lockout_held = False

        mode = MODE_DEADLINE if decision.plan is not None else MODE_OPPORTUNISTIC
        _LOGGER.info(
            "charge %s (%s): price=%.2f on_threshold=%.2f T=%.2f delta=%.2f"
            " sigma=%.2f mode=%s%s",
            "START" if decision.charge_now else "STOP",
            decision.reason,
            decision.decision_price,
            decision.on_threshold,
            decision.threshold,
            decision.deadband,
            volatility,
            mode,
            " lockout-held" if lockout_held else "",
        )
        payload = {
            "charging": decision.charge_now,
            "reason": decision.reason,
            "mode": mode,
            "decision_price": round(decision.decision_price, 2),
            "threshold": round(decision.threshold, 2),
            "on_threshold": round(decision.on_threshold, 2),
            "deadband": round(decision.deadband, 2),
            "volatility": round(volatility, 2),
            "lockout_held": lockout_held,
        }
        event = EVENT_CHARGE_STARTED if decision.charge_now else EVENT_CHARGE_STOPPED
        self.hass.bus.async_fire(event, payload)
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_record_transition(decision, now, volatility, mode, lockout_held),
            "comed_ev_transition",
        )

    async def _async_record_transition(
        self,
        decision: ChargeDecision,
        now: datetime,
        volatility: float,
        mode: str,
        lockout_held: bool,
    ) -> None:
        """Persist one transition row to the SQLite store (off the event loop)."""
        context = asdict(decision)
        context["volatility"] = volatility
        await self.hass.async_add_executor_job(
            partial(
                self._session_store.insert_transition,
                ts_utc=now,
                charging=decision.charge_now,
                reason=decision.reason,
                mode=mode,
                decision_price=decision.decision_price,
                threshold=decision.threshold,
                on_threshold=decision.on_threshold,
                deadband=decision.deadband,
                volatility=volatility,
                lockout_held=lockout_held,
                context_json=json.dumps(context, default=str),
                retention=TRANSITION_RETENTION,
            )
        )

    # --- charge deferrals ----------------------------------------------------

    @callback
    def _note_deferral(self, decision: ChargeDecision, now: datetime) -> None:
        """Track reserve-gate deferral episodes: one persisted row per hold.

        A deferral is the reserve gate (`cheaper_later`) holding a would-be charge
        off to reach a cheaper hour — a decision that never flips `charge_now`, so
        it leaves no transition edge. This records the *span* of each hold, written
        only when it settles, so the per-poll price wobble inside it never lands a
        row. Two damps keep it quiet: a re-assertion within `DEFERRAL_GRACE_SECONDS`
        holds one episode intact across the `cheaper_hours_ahead` boundary flap,
        and a hold shorter than `DEFERRAL_MIN_DURATION_SECONDS` is dropped at close.

        Idempotent under the repeated `_build_data` calls of a single tick: a still-
        holding tick only clears a pending close, and a pending close needs the full
        grace window to elapse before it finalizes.
        """
        holding = decision.reason in _DEFERRAL_REASONS
        episode = self._deferral

        if holding:
            if episode is None:
                self._deferral = _OpenDeferral(
                    started_utc=now,
                    reason=decision.reason,
                    mode=(
                        MODE_DEADLINE
                        if decision.plan is not None
                        else MODE_OPPORTUNISTIC
                    ),
                    decision_price=decision.decision_price,
                    min_ahead=decision.min_ahead,
                )
            else:
                # Still (or again) holding: cancel any pending close so a boundary
                # flap does not split the episode.
                episode.pending_close_utc = None
                episode.ended_reason = None
            return

        # Not holding. Arm a pending close on the first false tick, then finalize
        # once the hold has stayed released for the grace window.
        if episode is None:
            return
        if episode.pending_close_utc is None:
            episode.pending_close_utc = now
            episode.ended_reason = decision.reason
            return
        if now - episode.pending_close_utc >= timedelta(seconds=DEFERRAL_GRACE_SECONDS):
            self._deferral = None
            self._finalize_deferral(episode)

    def _finalize_deferral(self, episode: _OpenDeferral) -> None:
        """Persist a settled deferral episode, dropping sub-floor holds."""
        ended = episode.pending_close_utc
        assert ended is not None  # only reached after a pending close
        if ended - episode.started_utc < timedelta(
            seconds=DEFERRAL_MIN_DURATION_SECONDS
        ):
            _LOGGER.debug("deferral shorter than floor; not recording")
            return
        _LOGGER.debug(
            "recorded deferral (%s): held %s waiting for %s",
            episode.reason,
            ended - episode.started_utc,
            episode.min_ahead,
        )
        self.config_entry.async_create_background_task(
            self.hass,
            self._async_record_deferral(episode, ended),
            "comed_ev_deferral",
        )

    async def _async_record_deferral(
        self, episode: _OpenDeferral, ended: datetime
    ) -> None:
        """Persist one deferral row to the SQLite store (off the event loop)."""
        await self.hass.async_add_executor_job(
            partial(
                self._session_store.insert_deferral,
                started_utc=episode.started_utc,
                ended_utc=ended,
                reason=episode.reason,
                mode=episode.mode,
                decision_price=episode.decision_price,
                min_ahead=episode.min_ahead,
                ended_reason=episode.ended_reason,
                retention=DEFERRAL_RETENTION,
            )
        )

    async def async_get_deferrals(self, limit: int) -> list[dict]:
        """Return recent deferral episodes (newest first) for the Activity card.

        Each row is tagged `kind: "deferral"` and anchored at the hold's start
        (`ts`), so it merges into the transition timeline as a span.
        """
        deferrals = await self.hass.async_add_executor_job(
            partial(self._session_store.list_deferrals, limit=limit)
        )
        return [
            {
                "kind": "deferral",
                "ts": d.started_utc.isoformat(),
                "ended": d.ended_utc.isoformat(),
                "reason": d.reason,
                "mode": d.mode,
                "decision_price": d.decision_price,
                "min_ahead": d.min_ahead,
                "ended_reason": d.ended_reason,
            }
            for d in deferrals
        ]

    async def async_get_transitions(self, limit: int) -> list[dict]:
        """Return recent transitions (newest first) for diagnostics and the card.

        Flattens a few values out of the stored decision context so consumers
        get the deadline plan (`slack_hours`/`hours_needed`) and the opportunistic
        `min_ahead` without parsing `context_json` themselves.
        """
        transitions = await self.hass.async_add_executor_job(
            partial(self._session_store.list_transitions, limit=limit)
        )
        # The settled dot on a charge-start edge carries the *whole session's*
        # energy-weighted settled ¢/kWh, not the single hour the start fell in:
        # a charge can span several settled hours, so no one hour's price is the
        # price paid. Match each start edge to the session it began and divide
        # the settled supply cost by the energy. Surfaced beside the real-time
        # `decision_price` that triggered the edge, so the card can show where
        # the session landed. None until ComEd settles all the session's hours.
        sessions: list[Session] = []
        if transitions:
            stamps = [t.ts_utc for t in transitions]
            sessions = await self.hass.async_add_executor_job(
                partial(
                    self._session_store.list_sessions,
                    start_utc=min(stamps) - _SESSION_EDGE_MATCH_TOL,
                    end_utc=max(stamps) + _SESSION_EDGE_MATCH_TOL,
                )
            )
        rows: list[dict] = []
        for t in transitions:
            context = {}
            if t.context_json:
                try:
                    context = json.loads(t.context_json)
                except (ValueError, TypeError):
                    context = {}
            plan = context.get("plan") or {}
            # Only a start edge owns a session, so only it carries the weighted
            # settled price; a stop edge leaves it None.
            settled_price = None
            if t.charging:
                session = _session_started_at(sessions, t.ts_utc)
                if (
                    session is not None
                    and session.settled_cost_cents is not None
                    and session.energy_kwh > 0
                ):
                    settled_price = round(
                        session.settled_cost_cents / session.energy_kwh, 2
                    )
            rows.append(
                {
                    "kind": "edge",
                    "ts": t.ts_utc.isoformat(),
                    "charging": t.charging,
                    "reason": t.reason,
                    "mode": t.mode,
                    "decision_price": t.decision_price,
                    "threshold": t.threshold,
                    "on_threshold": t.on_threshold,
                    "deadband": t.deadband,
                    "volatility": t.volatility,
                    "lockout_held": t.lockout_held,
                    "min_ahead": context.get("min_ahead"),
                    "slack_hours": plan.get("slack_hours"),
                    "hours_needed": plan.get("hours_needed"),
                    "settled_price": settled_price,
                }
            )
        return rows

    async def _async_settle_costs(self) -> None:
        """Backfill settled prices and recompute cost for now-settled sessions."""
        incomplete = await self.hass.async_add_executor_job(
            self._session_store.sessions_incomplete
        )
        if not incomplete:
            return

        # Every hour-ending any incomplete session touches.
        needed: set[datetime] = set()
        for session in incomplete:
            needed |= hour_buckets(session.started_utc, session.ended_utc).keys()

        have = await self.hass.async_add_executor_job(
            self._session_store.get_settled_prices, needed
        )
        # ComEd's dual feed is per Central calendar day; fetch each missing day.
        missing_days = sorted(
            {hour.astimezone(CENTRAL).date() for hour in needed - have.keys()}
        )
        for day in missing_days:
            try:
                dual = await self._client.get_dual(day)
            except Exception as err:  # noqa: BLE001 - settle pass is best-effort
                _LOGGER.warning("ComEd settled feed for %s failed: %s", day, err)
                continue
            prices = {
                dt_util.as_utc(h.hour_ending): h.actual * 100.0
                for h in dual
                if h.actual is not None
            }
            if prices:
                await self.hass.async_add_executor_job(
                    self._session_store.upsert_settled_prices, prices
                )

        # Recompute any session whose hours are now fully settled.
        prices = await self.hass.async_add_executor_job(
            self._session_store.get_settled_prices, needed
        )
        for session in incomplete:
            buckets = hour_buckets(session.started_utc, session.ended_utc)
            if buckets and all(hour in prices for hour in buckets):
                cost_cents = sum(
                    session.energy_kwh * fraction * prices[hour]
                    for hour, fraction in buckets.items()
                )
                await self.hass.async_add_executor_job(
                    self._session_store.update_session_cost,
                    session.id,
                    cost_cents,
                    True,
                )
                _LOGGER.debug(
                    "settled session %d: %.1f¢", session.id, cost_cents
                )

        # Refresh the cached last-settled session and push it to entities.
        self._last_session = await self.hass.async_add_executor_job(
            self._session_store.get_last_settled_session
        )
        if self.data is not None:
            self.async_set_updated_data(self._build_data())

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
        """Serialize history, energy totals, and settings to JSON-storable form."""
        return {
            "history": self._serialize_history(),
            "settings": asdict(self.settings),
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
        evse_delta = self._meter_delta(self._energy_evse_entity)
        self._energy_evse_total += evse_delta
        # Attribute wall energy to the open session so its cost is meter-derived.
        if self._session is not None:
            self._session.wall_kwh += evse_delta

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
            floor_pct=self.settings.floor_pct,
            ceiling_pct=self.settings.ceiling_pct,
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

    def _get_bool(self, entity_id: str | None) -> bool | None:
        """Read a boolean state, or None if not wired/missing/unavailable.

        None means "let the SOC target decide the stop"; a real on/off means the
        vehicle owns the stop. An unavailable reading falls back to None so a
        flaky sensor cannot strand charging on or off.
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        return state.state == "on"

    def _charge_rate(self) -> float:
        """Return the charge rate in kW from the entity, else the constant.

        A non-positive entity reading is ignored: a live charge-power sensor
        reads ~0 kW whenever the car is idle — exactly when the forward schedule
        is projected — and a 0 kW rate is never a usable planning value. Zero
        would zero the projected SOC gain and disable the cheapest-hour reserve,
        painting every affordable hour as charging. Fall back to the nominal
        constant instead.
        """
        entity = self.config_entry.data.get(CONF_CHARGE_RATE_ENTITY)
        if entity:
            value = self._get_float(entity)
            if value is not None and value > 0:
                return value
        return self.config_entry.data.get(CONF_CHARGE_RATE_KW, 0.0)

    def _capacity_kwh(self) -> float:
        """Return the battery capacity in kWh from the entity, else the constant.

        A non-positive reading is ignored for the same reason as the charge
        rate: a zero capacity is never meaningful and would break the SOC
        projection. Fall back to the configured constant.
        """
        entity = self.config_entry.data.get(CONF_CAPACITY_ENTITY)
        if entity:
            value = self._get_float(entity)
            if value is not None and value > 0:
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
            departure = dt_util.utc_from_timestamp(float(timestamp))
        else:
            parsed = dt_util.parse_datetime(state.state)
            departure = dt_util.as_utc(parsed) if parsed else None
        # input_datetime cannot be cleared, so a past time means "no deadline":
        # fall back to the overnight window instead of a stale departure.
        if departure is None or departure <= dt_util.utcnow():
            return None
        return departure

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
        return self.settings.window_days
