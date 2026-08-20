"""Constants, config keys, and defaults for the ComEd EV Charging integration."""

from __future__ import annotations

DOMAIN = "comed_ev"

# --- Config / options keys ---------------------------------------------------
CONF_CAPACITY_KWH = "capacity_kwh"
CONF_EFFICIENCY = "efficiency"
CONF_CURRENT_SOC_ENTITY = "current_soc_entity"
CONF_TARGET_SOC_ENTITY = "target_soc_entity"
CONF_CHARGE_RATE_ENTITY = "charge_rate_entity"
CONF_CHARGE_RATE_KW = "charge_rate_kw"
CONF_DEPARTURE_ENTITY = "departure_entity"

CONF_THRESHOLD_MODE = "threshold_mode"
CONF_PRICE_FLOOR = "price_floor"
CONF_PRICE_CEILING = "price_ceiling"
CONF_MIN_SOC = "min_soc"
CONF_GAMMA = "gamma"

CONF_FLOOR_PCT = "floor_pct"
CONF_CEILING_PCT = "ceiling_pct"
CONF_WINDOW_DAYS = "window_days"
CONF_POLL_INTERVAL = "poll_interval"

# --- Threshold modes ---------------------------------------------------------
MODE_AUTO = "auto"
MODE_MANUAL = "manual"

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

# --- Storage -----------------------------------------------------------------
STORAGE_VERSION = 1
STORAGE_KEY = "comed_ev.history"

# Fetch the heavier hourly-estimate feeds at most this often (seconds).
HOURLY_FEED_INTERVAL = 3600
# Central-local hour after which we refresh the next-day feed to catch publish.
NEXT_DAY_PUBLISH_HOUR = 16

# Setup backfill: seed the rolling window from the 5-minute API in chunks.
BACKFILL_CHUNK_DAYS = 7
BACKFILL_PAUSE_SECONDS = 0.5
# Skip backfill when stored history already covers this fraction of the window.
BACKFILL_COVERAGE_SKIP = 0.8
