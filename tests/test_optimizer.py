"""Offline unit tests for the pure charge-decision logic."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.comed_ev.optimizer import (
    REASON_ABOVE_THRESHOLD,
    REASON_BELOW_THRESHOLD,
    REASON_MUST_CHARGE,
    REASON_TARGET_REACHED,
    SOURCE_DAY_AHEAD,
    SOURCE_DAY_OF,
    SOURCE_FALLBACK,
    ForecastHour,
    build_forecast,
    charge_threshold,
    energy_needed_kwh,
    estimate_charge_cost,
    plan_charge,
    should_charge_now,
)

CENTRAL = ZoneInfo("America/Chicago")
NOW = datetime(2026, 8, 19, 20, 15, tzinfo=CENTRAL)


def _threshold(soc: float, **kw) -> float:
    return charge_threshold(
        soc, 80.0, price_floor=3.0, price_ceiling=15.0, **kw
    )


# --- charge_threshold --------------------------------------------------------


def test_threshold_zero_at_or_above_target():
    assert charge_threshold(80, 80, price_floor=3, price_ceiling=15) == 0.0
    assert charge_threshold(95, 80, price_floor=3, price_ceiling=15) == 0.0


def test_threshold_within_floor_ceiling_band():
    assert _threshold(79) == pytest.approx(3.0, abs=0.2)  # near target -> near floor
    assert _threshold(1) <= 15.0  # near empty -> at/under ceiling, never above
    assert _threshold(0) == pytest.approx(15.0)  # empty -> exactly ceiling


def test_threshold_monotonic_decreasing_in_soc():
    vals = [_threshold(soc) for soc in range(0, 81, 5)]
    assert vals == sorted(vals, reverse=True)


def test_threshold_gamma_is_gradual():
    # With gamma > 1 the mid band stays much closer to the floor than a linear ramp.
    mid_gradual = _threshold(40, gamma=2.5)
    mid_linear = _threshold(40, gamma=1.0)
    assert mid_gradual < mid_linear


def test_threshold_min_soc_saturates_urgency():
    # Below min_soc urgency clamps to full -> ceiling.
    assert charge_threshold(
        10, 80, price_floor=3, price_ceiling=15, min_soc=20
    ) == pytest.approx(15.0)


# --- should_charge_now -------------------------------------------------------


def _decide(soc, live, **kw):
    return should_charge_now(
        NOW, soc, 80.0, live, price_floor=3.0, price_ceiling=15.0, **kw
    )


def test_decision_target_reached_off():
    d = _decide(80, 0.1)
    assert d.charge_now is False
    assert d.reason == REASON_TARGET_REACHED


def test_decision_below_threshold_on():
    d = _decide(20, 2.0)  # low SOC -> high threshold, cheap price
    assert d.charge_now is True
    assert d.reason == REASON_BELOW_THRESHOLD


def test_decision_above_threshold_off_on_spike():
    d = _decide(79, 40.0)  # near target -> low threshold, price is a spike
    assert d.charge_now is False
    assert d.reason == REASON_ABOVE_THRESHOLD


def test_must_charge_overrides_price():
    plan = plan_charge(
        NOW,
        NOW + timedelta(hours=2),
        current_soc=20,
        target_soc=80,
        capacity_kwh=75,
        charge_rate_kw=11,
        efficiency=0.9,
        forecast={},
    )
    assert plan.slack_hours <= 0  # 2h available, needs far more
    d = _decide(20, 55.0, plan=plan)  # spike price, but deadline forces it
    assert d.charge_now is True
    assert d.reason == REASON_MUST_CHARGE


def test_slack_positive_lets_threshold_govern():
    plan = plan_charge(
        NOW,
        NOW + timedelta(hours=12),
        current_soc=70,
        target_soc=80,
        capacity_kwh=75,
        charge_rate_kw=11,
        efficiency=0.9,
        forecast={},
    )
    assert plan.slack_hours > 0
    d = _decide(79, 40.0, plan=plan)  # ample slack, spike price -> stay off
    assert d.charge_now is False
    assert d.reason == REASON_ABOVE_THRESHOLD


# --- plan_charge -------------------------------------------------------------


def test_energy_and_hours_needed():
    plan = plan_charge(
        NOW,
        NOW + timedelta(hours=10),
        current_soc=50,
        target_soc=80,
        capacity_kwh=75,
        charge_rate_kw=11,
        efficiency=0.9,
        forecast={},
    )
    # 30% of 75 kWh = 22.5 kWh to battery / 0.9 = 25 kWh from grid.
    assert plan.energy_needed_kwh == pytest.approx(25.0)
    assert plan.hours_needed == 3  # ceil(25 / 11)
    assert plan.hours_available == 10
    assert plan.slack_hours == 7
    assert plan.feasible is True


def test_infeasible_when_time_too_short():
    plan = plan_charge(
        NOW,
        NOW + timedelta(hours=1),
        current_soc=10,
        target_soc=90,
        capacity_kwh=75,
        charge_rate_kw=11,
        efficiency=0.9,
        forecast={},
    )
    assert plan.slack_hours < 0
    assert plan.feasible is False
    assert plan.projected_end_soc < 90


# --- build_forecast precedence ----------------------------------------------


def test_forecast_precedence():
    h1 = (NOW + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    h2 = h1 + timedelta(hours=1)
    h3 = h2 + timedelta(hours=1)
    departure = NOW + timedelta(hours=3, minutes=30)

    day_ahead = {h1: 5.0}  # wins where present
    dual_today = {h1: 9.0, h2: 8.0}  # used where no day-ahead
    forecast = build_forecast(NOW, departure, day_ahead, dual_today, 4.0)

    assert forecast[h1].source == SOURCE_DAY_AHEAD
    assert forecast[h1].price == 5.0
    assert forecast[h2].source == SOURCE_DAY_OF
    assert forecast[h3].source == SOURCE_FALLBACK
    assert forecast[h3].price == 4.0


def test_forecast_source_recorded_on_plan():
    h1 = (NOW + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    departure = NOW + timedelta(hours=2)
    forecast = build_forecast(NOW, departure, {h1: 5.0}, {}, None)
    plan = plan_charge(
        NOW, departure, 50, 80, 75, 11, 0.9, forecast
    )
    assert plan.forecast_source == SOURCE_DAY_AHEAD


# --- energy_needed_kwh -------------------------------------------------------


def test_energy_needed_divides_by_efficiency():
    # 30% of 75 kWh = 22.5 kWh to battery / 0.9 = 25 kWh from the wall.
    assert energy_needed_kwh(50, 80, 75, 0.9) == pytest.approx(25.0)


def test_energy_needed_zero_at_or_above_target():
    assert energy_needed_kwh(80, 80, 75, 0.9) == 0.0
    assert energy_needed_kwh(90, 80, 75, 0.9) == 0.0


# --- estimate_charge_cost ----------------------------------------------------


def _forecast(*prices: float) -> dict:
    """Build a forecast from consecutive hourly prices (¢/kWh)."""
    return {
        (NOW + timedelta(hours=i + 1)): ForecastHour(
            NOW + timedelta(hours=i + 1), p, SOURCE_DAY_AHEAD
        )
        for i, p in enumerate(prices)
    }


def test_cost_picks_cheapest_hours_first():
    # Need 10 kWh at 10 kW/hr -> one full hour; the 4¢ hour is chosen over 20¢.
    forecast = _forecast(20.0, 4.0, 30.0)
    cost = estimate_charge_cost(forecast, energy_needed_kwh=10.0, charge_rate_kw=10.0)
    assert cost is not None
    assert cost.hours_used == 1
    assert cost.energy_kwh == pytest.approx(10.0)
    assert cost.estimated_cost == pytest.approx(0.40)  # 10 kWh * 4¢ = 40¢
    assert cost.average_price == pytest.approx(0.04)


def test_cost_spans_hours_with_partial_final():
    # Need 15 kWh at 10 kW/hr: full 4¢ hour (10 kWh) + 5 kWh of the 6¢ hour.
    forecast = _forecast(6.0, 4.0, 30.0)
    cost = estimate_charge_cost(forecast, energy_needed_kwh=15.0, charge_rate_kw=10.0)
    assert cost is not None
    assert cost.hours_used == 2
    # (10 * 4¢) + (5 * 6¢) = 70¢ = $0.70 over 15 kWh.
    assert cost.estimated_cost == pytest.approx(0.70)
    assert cost.average_price == pytest.approx(0.70 / 15.0)


def test_cost_capped_by_short_window():
    # Only two hours available -> 20 kWh priced though 30 kWh is needed.
    forecast = _forecast(5.0, 7.0)
    cost = estimate_charge_cost(forecast, energy_needed_kwh=30.0, charge_rate_kw=10.0)
    assert cost is not None
    assert cost.hours_used == 2
    assert cost.energy_kwh == pytest.approx(20.0)


def test_cost_none_when_nothing_to_price():
    assert estimate_charge_cost({}, 10.0, 10.0) is None
    assert estimate_charge_cost(_forecast(5.0), 0.0, 10.0) is None
    assert estimate_charge_cost(_forecast(5.0), 10.0, 0.0) is None
