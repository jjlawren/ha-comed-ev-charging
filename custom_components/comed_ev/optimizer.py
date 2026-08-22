"""Pure charge-decision logic — no Home Assistant imports.

Everything here is offline-testable. Prices are in ¢/kWh, SOC in percent (0-100).
The primary trigger is a SOC-driven price threshold compared against the running
hourly average (the best proxy for the settled hour-average price actually paid).
An optional departure adds an hourly feasibility override.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil

# Decision reason codes surfaced on the binary_sensor and in diagnostics.
REASON_TARGET_REACHED = "target_reached"
REASON_MUST_CHARGE = "must_charge"
REASON_BELOW_THRESHOLD = "below_threshold"
REASON_ABOVE_THRESHOLD = "above_threshold"

# Forecast provenance, best-to-worst.
SOURCE_DAY_AHEAD = "day_ahead"
SOURCE_DAY_OF = "day_of"
SOURCE_FALLBACK = "fallback"

# Precedence rank; lower is better (used to pick the dominant source of a window).
_SOURCE_RANK = {SOURCE_DAY_AHEAD: 0, SOURCE_DAY_OF: 1, SOURCE_FALLBACK: 2}


def clamp(value: float, low: float, high: float) -> float:
    """Clamp `value` into the inclusive range [low, high]."""
    return max(low, min(high, value))


def soc_urgency(current_soc: float, target_soc: float, min_soc: float = 0.0) -> float:
    """Return charging urgency in [0, 1] from SOC alone.

    0.0 at (or above) target, rising linearly to 1.0 at `min_soc` and below. This
    is the pre-`gamma` driver of `charge_threshold`.
    """
    span = target_soc - min_soc
    if span <= 0:
        return 1.0
    return clamp((target_soc - current_soc) / span, 0.0, 1.0)


def charge_threshold(
    current_soc: float,
    target_soc: float,
    *,
    price_floor: float,
    price_ceiling: float,
    min_soc: float = 0.0,
    gamma: float = 2.5,
) -> float:
    """Return the price ceiling T(SOC) below which we are willing to charge.

    Willingness-to-pay rises as SOC falls. With `gamma > 1` the curve stays near
    `price_floor` across the high-SOC band and only ramps toward `price_ceiling`
    when genuinely low — a gentle real-world curve rather than a linear ramp.

    Returns 0.0 once the target is reached (never charge).
    """
    if current_soc >= target_soc:
        return 0.0
    urgency = soc_urgency(current_soc, target_soc, min_soc)
    return price_floor + (price_ceiling - price_floor) * urgency**gamma


def energy_needed_kwh(
    current_soc: float,
    target_soc: float,
    capacity_kwh: float,
    efficiency: float,
) -> float:
    """Wall energy (kWh) needed to raise SOC from current to target.

    Divides the energy into the battery by charging efficiency, so the result is
    what the meter reads. Returns 0.0 once the target is reached.
    """
    to_battery = max(0.0, (target_soc - current_soc) / 100.0 * capacity_kwh)
    return to_battery / efficiency if efficiency > 0 else 0.0


def estimate_charge_cost(
    forecast: dict[datetime, ForecastHour],
    energy_needed_kwh: float,
    charge_rate_kw: float,
    distribution_rate: float = 0.0,
) -> ChargeCost | None:
    """Estimate charge cost by filling energy from the cheapest forecast hours.

    Picks the lowest-priced hours in the window and draws `charge_rate_kw` kWh
    from each (a partial final hour) until `energy_needed_kwh` is met or the
    window runs out. Forecast prices and `distribution_rate` are ¢/kWh; the
    fixed distribution rate is added to every priced kWh, matching the settled
    session cost. The returned costs are in dollars. Returns None when there is
    nothing to price.
    """
    if energy_needed_kwh <= 0 or charge_rate_kw <= 0 or not forecast:
        return None
    remaining = energy_needed_kwh
    supply_cents = 0.0
    hours_used = 0
    for hour in sorted(forecast.values(), key=lambda h: h.price):
        if remaining <= 0:
            break
        kwh = min(charge_rate_kw, remaining)
        supply_cents += kwh * hour.price
        remaining -= kwh
        hours_used += 1
    energy = energy_needed_kwh - remaining
    if energy <= 0:
        return None
    supply_cost = supply_cents / 100.0
    distribution_cost = distribution_rate * energy / 100.0
    cost = supply_cost + distribution_cost
    return ChargeCost(
        energy_kwh=energy,
        estimated_cost=cost,
        supply_cost=supply_cost,
        distribution_cost=distribution_cost,
        average_price=supply_cost / energy,
        hours_used=hours_used,
    )


def cheapest_forecast_hour(
    forecast: dict[datetime, ForecastHour],
) -> ForecastHour | None:
    """Return the lowest-priced forecast hour, earliest wins on a tie.

    None when the forecast is empty. Used to surface the cheapest upcoming
    estimated rate and when it occurs.
    """
    if not forecast:
        return None
    return min(forecast.values(), key=lambda h: (h.price, h.hour_ending))


def hour_buckets(started: datetime, ended: datetime) -> dict[datetime, float]:
    """Split [started, ended) into hour-ending buckets by time fraction.

    Returns ``{hour_ending: fraction}`` where each key is the top-of-hour that
    *ends* the hour covering that slice (ComEd's convention: the hour ending
    03:00 covers 02:00–03:00), and the fractions sum to 1.0. Assumes constant
    charging power, so a session's energy splits across its hours in proportion
    to the time spent in each. An empty or reversed interval yields ``{}``.
    """
    total = (ended - started).total_seconds()
    if total <= 0:
        return {}
    buckets: dict[datetime, float] = {}
    # Start of the hour containing `started` (top-of-hour at or below it).
    hour_start = started.replace(minute=0, second=0, microsecond=0)
    while hour_start < ended:
        hour_end = hour_start + timedelta(hours=1)
        overlap = (min(ended, hour_end) - max(started, hour_start)).total_seconds()
        if overlap > 0:
            buckets[hour_end] = overlap / total
        hour_start = hour_end
    return buckets


@dataclass(frozen=True)
class ForecastHour:
    """A single forecast hour with its price and provenance.

    `hour_ending` is a timezone-aware timestamp marking the END of the hour.
    """

    hour_ending: datetime
    price: float
    source: str


@dataclass(frozen=True)
class ChargeCost:
    """Estimated cost of the upcoming charge, priced over the cheapest hours.

    `energy_kwh` is the wall energy actually priced; it is below the energy
    needed only when the forecast window is too short to deliver all of it.
    `estimated_cost` is the total in dollars (`supply_cost` +
    `distribution_cost`); `average_price` is the supply-only dollars per kWh
    (it excludes the fixed distribution rate).
    """

    energy_kwh: float
    estimated_cost: float
    supply_cost: float
    distribution_cost: float
    average_price: float
    hours_used: int


@dataclass(frozen=True)
class ChargePlan:
    """Feasibility of reaching the target by a departure time (deadline mode)."""

    energy_needed_kwh: float
    hours_needed: int
    hours_available: int
    slack_hours: int
    projected_end_soc: float
    feasible: bool
    forecast_source: str


@dataclass(frozen=True)
class ChargeDecision:
    """The result of `should_charge_now`."""

    charge_now: bool
    reason: str
    decision_price: float
    threshold: float
    urgency: float
    gamma: float
    plan: ChargePlan | None = None


def should_charge_now(
    now: datetime,
    current_soc: float,
    target_soc: float,
    decision_price: float,
    *,
    price_floor: float,
    price_ceiling: float,
    min_soc: float = 0.0,
    gamma: float = 2.5,
    plan: ChargePlan | None = None,
) -> ChargeDecision:
    """Decide whether to charge right now against the running hourly average.

    The price actually paid is the settled hourly average, which is not known
    until the hour completes; the running current-hour average is the best live
    proxy. A single 5-minute point is too noisy to decide on, so callers pass
    the hourly average as `decision_price`.

    Decision order:
      1. target reached                    -> off  (target_reached)
      2. deadline configured, slack <= 0   -> on   (must_charge)
      3. decision_price < T(SOC)           -> on   (below_threshold)
      4. else                              -> off  (above_threshold)
    """
    del now  # part of the documented signature; decision uses `plan` for time context
    urgency = soc_urgency(current_soc, target_soc, min_soc)
    threshold = charge_threshold(
        current_soc,
        target_soc,
        price_floor=price_floor,
        price_ceiling=price_ceiling,
        min_soc=min_soc,
        gamma=gamma,
    )
    if current_soc >= target_soc:
        return ChargeDecision(
            False, REASON_TARGET_REACHED, decision_price, threshold, urgency, gamma, plan
        )
    if plan is not None and plan.slack_hours <= 0:
        return ChargeDecision(
            True, REASON_MUST_CHARGE, decision_price, threshold, urgency, gamma, plan
        )
    if decision_price < threshold:
        return ChargeDecision(
            True, REASON_BELOW_THRESHOLD, decision_price, threshold, urgency, gamma, plan
        )
    return ChargeDecision(
        False, REASON_ABOVE_THRESHOLD, decision_price, threshold, urgency, gamma, plan
    )


def build_forecast(
    now: datetime,
    departure: datetime,
    day_ahead: dict[datetime, float] | None,
    dual_today: dict[datetime, float] | None,
    current_hour_avg: float | None,
) -> dict[datetime, ForecastHour]:
    """Build an hourly price forecast for the hours ending in (now, departure].

    Precedence per hour: day-ahead estimate -> day-of dual estimate ->
    current-hour-average fallback. Each hour records which source supplied it.
    """
    day_ahead = day_ahead or {}
    dual_today = dual_today or {}
    forecast: dict[datetime, ForecastHour] = {}
    for hour_ending in _hour_ends(now, departure):
        if hour_ending in day_ahead:
            price, source = day_ahead[hour_ending], SOURCE_DAY_AHEAD
        elif hour_ending in dual_today:
            price, source = dual_today[hour_ending], SOURCE_DAY_OF
        elif current_hour_avg is not None:
            price, source = current_hour_avg, SOURCE_FALLBACK
        else:
            continue
        forecast[hour_ending] = ForecastHour(hour_ending, price, source)
    return forecast


def plan_charge(
    now: datetime,
    departure: datetime,
    current_soc: float,
    target_soc: float,
    capacity_kwh: float,
    charge_rate_kw: float,
    efficiency: float,
    forecast: dict[datetime, ForecastHour],
) -> ChargePlan:
    """Compute deadline feasibility: energy/time needed vs. time available.

    Feasibility is price-independent (time and energy only); the forecast is
    carried for provenance reporting and display.
    """
    energy_needed = energy_needed_kwh(
        current_soc, target_soc, capacity_kwh, efficiency
    )

    hours_needed = (
        ceil(energy_needed / charge_rate_kw) if charge_rate_kw > 0 else 0
    )
    hours_available = max(0, int((departure - now).total_seconds() // 3600))
    slack_hours = hours_available - hours_needed

    soc_gain = 0.0
    if capacity_kwh > 0:
        delivered = hours_available * charge_rate_kw * efficiency
        soc_gain = delivered / capacity_kwh * 100.0
    projected_end_soc = min(100.0, current_soc + soc_gain)
    feasible = projected_end_soc >= target_soc

    return ChargePlan(
        energy_needed_kwh=energy_needed,
        hours_needed=hours_needed,
        hours_available=hours_available,
        slack_hours=slack_hours,
        projected_end_soc=projected_end_soc,
        feasible=feasible,
        forecast_source=_dominant_source(forecast),
    )


def _hour_ends(now: datetime, departure: datetime) -> list[datetime]:
    """List timezone-aware hour-ending timestamps in the interval (now, departure].

    The first end is the next whole-hour boundary at or after `now`'s hour; each
    end marks the END of the hour it labels (e.g. 21:00 covers 20:00–21:00).
    """
    one_hour = timedelta(hours=1)
    cursor = now.replace(minute=0, second=0, microsecond=0) + one_hour
    ends: list[datetime] = []
    while cursor <= departure:
        ends.append(cursor)
        cursor += one_hour
    return ends


def _dominant_source(forecast: dict[datetime, ForecastHour]) -> str:
    """Return the best-precedence source present in the forecast window."""
    if not forecast:
        return SOURCE_FALLBACK
    return min(
        (h.source for h in forecast.values()),
        key=lambda s: _SOURCE_RANK.get(s, 99),
    )
