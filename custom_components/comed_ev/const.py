"""Constants, config keys, and defaults for the ComEd EV Charging integration."""

from __future__ import annotations

DOMAIN = "comed_ev"

# --- Config / options keys ---------------------------------------------------
CONF_CAPACITY_KWH = "capacity_kwh"
CONF_CAPACITY_ENTITY = "capacity_entity"
CONF_EFFICIENCY = "efficiency"
CONF_CURRENT_SOC_ENTITY = "current_soc_entity"
CONF_TARGET_SOC_ENTITY = "target_soc_entity"
CONF_CHARGE_RATE_ENTITY = "charge_rate_entity"
CONF_CHARGE_RATE_KW = "charge_rate_kw"
CONF_DEPARTURE_ENTITY = "departure_entity"
CONF_ENERGY_VEHICLE_ENTITY = "energy_vehicle_entity"
CONF_ENERGY_EVSE_ENTITY = "energy_evse_entity"

CONF_THRESHOLD_MODE = "threshold_mode"
CONF_PRICE_FLOOR = "price_floor"
CONF_PRICE_CEILING = "price_ceiling"
CONF_MIN_SOC = "min_soc"
CONF_GAMMA = "gamma"

CONF_FLOOR_PCT = "floor_pct"
CONF_CEILING_PCT = "ceiling_pct"
CONF_WINDOW_DAYS = "window_days"
CONF_POLL_INTERVAL = "poll_interval"
# Flat ¢/kWh baseline for session savings; unset = no savings comparison.
CONF_FLAT_RATE = "flat_rate"
# Fixed distribution ¢/kWh added to the settled supply rate for actual cost.
CONF_DISTRIBUTION_RATE = "distribution_rate"

# --- Threshold modes ---------------------------------------------------------
MODE_AUTO = "auto"
MODE_MANUAL = "manual"

# --- Measured efficiency -----------------------------------------------------
# Reject a measured vehicle/wall ratio outside this range as a bad sensor.
EFFICIENCY_MIN = 0.5
EFFICIENCY_MAX = 1.0
# Wall energy (kWh) that must accumulate before the measured ratio is trusted.
EFFICIENCY_MIN_SAMPLE_KWH = 2.0
# Per-day multiplier applied to both totals so recent sessions weigh more.
# 0.98/day is a ~34-day half-life; the ratio still tracks slow drift.
ENERGY_DECAY_PER_DAY = 0.98

# --- Defaults ----------------------------------------------------------------
DEFAULT_EFFICIENCY = 0.9
DEFAULT_GAMMA = 2.5
DEFAULT_MIN_SOC = 0.0
DEFAULT_FLOOR_PCT = 25
DEFAULT_CEILING_PCT = 90
DEFAULT_WINDOW_DAYS = 30
DEFAULT_POLL_INTERVAL = 5  # minutes
DEFAULT_THRESHOLD_MODE = MODE_AUTO

# Fallback ¢/kWh thresholds used until the analytics history has samples.
DEFAULT_PRICE_FLOOR = 3.0
DEFAULT_PRICE_CEILING = 14.0
# Flat-rate baseline for savings; 0.0 disables the savings comparison.
DEFAULT_FLAT_RATE = 0.0
# Distribution ¢/kWh added to the settled supply rate; 0.0 = none.
DEFAULT_DISTRIBUTION_RATE = 0.0

# --- Short-cycle damping (opportunistic ON deadband) -------------------------
# Trailing window (minutes) of 5-minute points used to gauge price volatility.
VOLATILITY_WINDOW_MINUTES = 60
# Opportunistic ON deadband = clamp(DEADBAND_K * sigma, DEADBAND_MIN,
# DEADBAND_MAX), in ¢/kWh. Turning ON requires the price this far below the SOC
# threshold; staying ON only needs it under the threshold itself, so a genuine
# rise still releases immediately (no intra-hour trap). Scaling by recent
# volatility keeps the band just above the noise: calm prices -> tight band and
# fast response, noisy prices -> wide band and no chatter.
DEADBAND_K = 1.0
DEADBAND_MIN = 0.2
DEADBAND_MAX = 3.0

# Minimum time (seconds) charging must stay OFF before an opportunistic ON is
# allowed again. OFF itself is never delayed, so a spike is escaped instantly;
# the lockout only caps how fast charging may re-engage, bounding cycle rate and
# protecting the EVSE contactor. Not applied in deadline mode (must_charge must
# never be blocked). In-memory only — a restart clears any active lockout.
MIN_OFF_SECONDS = 600

# --- Charge transitions ------------------------------------------------------
# Fired on the HA event bus when charge_now flips. The durable, queryable record
# lives in the session SQLite store (outside the recorder), so these events stay
# lean and rare (edges only) to keep the recorder small.
EVENT_CHARGE_STARTED = f"{DOMAIN}_charge_started"
EVENT_CHARGE_STOPPED = f"{DOMAIN}_charge_stopped"
# Rows of transition history kept in SQLite; older rows are pruned on insert.
TRANSITION_RETENTION = 500
# How many recent transitions the diagnostics dump surfaces.
DIAGNOSTICS_TRANSITIONS = 20

# --- Charge deferrals --------------------------------------------------------
# A deferral is a contiguous hold where the optimizer would charge on price but
# the reserve gate (cheaper_later) held the start off to reach a cheaper hour. It
# never flips charge_now, so it leaves no transition edge. One row per episode is
# recorded, written on close — never per poll — so the per-tick price wobble
# inside a hold produces no rows. Two damps keep it quiet:
#   - A hold shorter than this (seconds) is dropped at close, so a sub-poll flap
#     never lands a row.
DEFERRAL_MIN_DURATION_SECONDS = 900
#   - After the hold ends, wait this long (seconds) confirming it stays ended
#     before finalizing; a re-assertion inside the window keeps it one episode.
#     Damps the cheaper_hours_ahead boundary wobble so one hold is not split.
DEFERRAL_GRACE_SECONDS = 900
# Rows of deferral history kept in SQLite; older rows are pruned on insert.
DEFERRAL_RETENTION = 500

# --- Storage -----------------------------------------------------------------
STORAGE_VERSION = 1
STORAGE_KEY = "comed_ev.history"
# Dedicated SQLite file (under HA's .storage) for durable session-cost records.
SESSION_DB_FILENAME = "comed_ev_sessions.db"
# How often to backfill settled prices and recompute session costs (seconds).
SETTLE_INTERVAL_SECONDS = 86400

# Fetch the heavier hourly-estimate feeds at most this often (seconds).
HOURLY_FEED_INTERVAL = 3600
# Central-local hour after which we refresh the next-day feed to catch publish.
NEXT_DAY_PUBLISH_HOUR = 16
# Central-local hour marking the end of the "overnight" cost window used to
# estimate a charge when no departure time is configured.
OVERNIGHT_END_HOUR = 6

# Setup backfill: seed the rolling window from the 5-minute API in chunks.
BACKFILL_CHUNK_DAYS = 7
BACKFILL_PAUSE_SECONDS = 0.5
# Skip backfill when stored history already covers this fraction of the window.
BACKFILL_COVERAGE_SKIP = 0.8
