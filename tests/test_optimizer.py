"""Offline unit tests for the pure charge-decision logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.comed_ev.optimizer import (
    MODE_DEADLINE,
    MODE_OPPORTUNISTIC,
    REASON_ABOVE_THRESHOLD,
    REASON_BELOW_THRESHOLD,
    REASON_CHEAPER_LATER,
    REASON_CHEAPEST_HOURS,
    REASON_MIN_OFF_LOCKOUT,
    REASON_MUST_CHARGE,
    REASON_NOT_ACCEPTING,
    REASON_TARGET_REACHED,
    SOURCE_DAY_AHEAD,
    SOURCE_DAY_OF,
    SOURCE_FALLBACK,
    ForecastHour,
    build_forecast,
    charge_threshold,
    cheaper_hours_ahead,
    cheapest_forecast_hour,
    cheapest_hour_ahead_price,
    energy_needed_kwh,
    plan_charge,
    project_schedule,
    should_charge_now,
)

CENTRAL = ZoneInfo("America/Chicago")
NOW = datetime(2026, 8, 19, 20, 15, tzinfo=CENTRAL)


def _threshold(soc: float, **kw) -> float:
    return charge_threshold(
        soc, 80.0, price_floor=3.0, price_ceiling=15.0, **kw
    )


# --- charge_threshold --------------------------------------------------------


def test_threshold_floor_at_or_above_target():
    # At/above target urgency clamps to 0 -> the curve's natural floor, not a
    # misleading 0. The target_reached stop, not this value, ends the charge.
    assert charge_threshold(80, 80, price_floor=3, price_ceiling=15) == pytest.approx(3.0)
    assert charge_threshold(95, 80, price_floor=3, price_ceiling=15) == pytest.approx(3.0)


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


def test_accepting_true_suppresses_soc_target_stop():
    # SOC at target would normally stop, but the vehicle still accepts charge:
    # a cheap price keeps charging so a lagging SOC read cannot cut it short.
    d = _decide(80, 0.1, charge_accepting=True)
    assert d.charge_now is True
    assert d.reason == REASON_BELOW_THRESHOLD


def test_accepting_true_still_respects_price_above_target():
    # Suppressing the SOC stop does not force ON: a spike still releases.
    d = _decide(80, 40.0, charge_accepting=True)
    assert d.charge_now is False
    assert d.reason == REASON_ABOVE_THRESHOLD


def test_accepting_false_stops_below_target():
    # The vehicle stopped taking current before the SOC target -> off.
    d = _decide(50, 0.1, charge_accepting=False)
    assert d.charge_now is False
    assert d.reason == REASON_NOT_ACCEPTING


def test_accepting_false_takes_precedence_over_target_reached():
    d = _decide(80, 0.1, charge_accepting=False)
    assert d.charge_now is False
    assert d.reason == REASON_NOT_ACCEPTING


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


def test_slack_positive_defers_for_cheaper_hours():
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
    # Ample slack and cheaper hours ahead -> defer (threshold is not consulted
    # in deadline mode; scheduling into the cheapest hours governs).
    forecast = _hourly_forecast(40.0, 3.0, 3.0)
    d = _decide(79, 40.0, plan=plan, forecast=forecast, hours_needed=1)
    assert d.charge_now is False
    assert d.reason == REASON_CHEAPER_LATER


# --- forecast-window helpers -------------------------------------------------


def _hourly_forecast(*prices: float) -> dict:
    """Forecast at top-of-hour ends; prices[0] is the current hour (21:00)."""
    base = NOW.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return {
        base + timedelta(hours=i): ForecastHour(
            base + timedelta(hours=i), p, SOURCE_DAY_AHEAD
        )
        for i, p in enumerate(prices)
    }


def test_cheaper_hours_ahead_excludes_current_hour():
    # Current hour is 4¢; two later hours are cheaper. The current hour is not
    # counted as an alternative to itself.
    forecast = _hourly_forecast(4.0, 3.0, 3.0, 8.0)
    assert cheaper_hours_ahead(forecast, NOW, 4.0) == 2


def test_cheaper_hours_ahead_flat_forecast_none():
    # A flat forecast has nothing strictly cheaper -> no deferral.
    forecast = _hourly_forecast(4.0, 4.0, 4.0)
    assert cheaper_hours_ahead(forecast, NOW, 4.0) == 0


def test_cheapest_hour_ahead_price_excludes_current_hour():
    # Current hour (2¢) is the cheapest but is not "ahead"; the trough ahead is 3¢.
    forecast = _hourly_forecast(2.0, 5.0, 3.0, 8.0)
    assert cheapest_hour_ahead_price(forecast, NOW) == pytest.approx(3.0)


def test_cheapest_hour_ahead_price_none_with_no_future():
    forecast = _hourly_forecast(4.0)  # only the current hour
    assert cheapest_hour_ahead_price(forecast, NOW) is None


# --- opportunistic charging (reserve cheapest hours, no completion) ----------


def test_opportunistic_defers_when_enough_cheaper_hours_ahead():
    # Below threshold, but two cheaper hours are ahead and only two are needed,
    # so the current hour is not among the cheapest -> defer to the trough.
    forecast = _hourly_forecast(4.0, 2.0, 2.0)
    d = _decide(40, 4.0, forecast=forecast, hours_needed=2)
    assert d.charge_now is False
    assert d.reason == REASON_CHEAPER_LATER


def test_opportunistic_charges_when_current_among_cheapest_needed():
    # Two cheaper hours ahead but three are needed -> the current hour is among
    # the cheapest three still required, so charge it now.
    forecast = _hourly_forecast(4.0, 2.0, 2.0)
    d = _decide(40, 4.0, forecast=forecast, hours_needed=3)
    assert d.charge_now is True
    assert d.reason == REASON_BELOW_THRESHOLD


def test_opportunistic_reserve_does_not_interrupt_active_charging():
    # Same inputs as the OFF-state defer, but already charging: the reserve is a
    # start-suppression only, so it must not stop a run — else live-price wobble
    # near a forecast-price cluster would short-cycle the switch.
    forecast = _hourly_forecast(4.0, 2.0, 2.0)
    off = _decide(40, 4.0, forecast=forecast, hours_needed=2, charging=False)
    assert off.charge_now is False
    assert off.reason == REASON_CHEAPER_LATER
    on = _decide(40, 4.0, forecast=forecast, hours_needed=2, charging=True)
    assert on.charge_now is True
    assert on.reason == REASON_BELOW_THRESHOLD


def test_opportunistic_charges_when_no_cheaper_hours_ahead():
    forecast = _hourly_forecast(4.0, 5.0, 6.0)
    d = _decide(40, 4.0, forecast=forecast, hours_needed=2)
    assert d.charge_now is True
    assert d.reason == REASON_BELOW_THRESHOLD


def test_opportunistic_grabs_live_dip_below_every_forecast_hour():
    # The live price undercuts every remaining forecast hour, so its rank falls
    # below hours_needed and the unexpected dip is taken at once.
    forecast = _hourly_forecast(1.5, 5.0, 6.0)
    d = _decide(40, 1.5, forecast=forecast, hours_needed=5)
    assert d.charge_now is True
    assert d.reason == REASON_BELOW_THRESHOLD


def test_opportunistic_never_overrides_above_threshold():
    # A spike is rejected as above-threshold regardless of the forecast.
    forecast = _hourly_forecast(40.0, 3.0, 3.0)
    d = _decide(79, 40.0, forecast=forecast, hours_needed=2)
    assert d.charge_now is False
    assert d.reason == REASON_ABOVE_THRESHOLD


# --- opportunistic deadband (asymmetric short-cycle damping) -----------------


def test_deadband_blocks_turn_on_inside_band_when_off():
    # Currently OFF; price sits under T but inside the deadband -> stay off, so
    # boundary noise cannot flap the switch on.
    t = _threshold(40)
    d = _decide(40, t - 0.5, charging=False, deadband=1.0)
    assert d.charge_now is False
    assert d.reason == REASON_ABOVE_THRESHOLD
    assert d.on_threshold == pytest.approx(t - 1.0)


def test_deadband_allows_turn_on_below_band():
    # Currently OFF; a full deadband below T clears the higher ON bar -> charge.
    t = _threshold(40)
    d = _decide(40, t - 1.5, charging=False, deadband=1.0)
    assert d.charge_now is True
    assert d.reason == REASON_BELOW_THRESHOLD


def test_deadband_holds_on_inside_band_when_charging():
    # Currently ON; price under T but inside the band -> keep charging (the ON
    # bar relaxes to T once charging, so no chatter).
    t = _threshold(40)
    d = _decide(40, t - 0.5, charging=True, deadband=1.0)
    assert d.charge_now is True
    assert d.reason == REASON_BELOW_THRESHOLD
    assert d.on_threshold == pytest.approx(t)


def test_deadband_releases_immediately_above_threshold_when_charging():
    # Currently ON; a rise above T releases at once — the OFF side is undamped,
    # so an unexpected spike is escaped rather than trapped.
    t = _threshold(40)
    d = _decide(40, t + 0.5, charging=True, deadband=1.0)
    assert d.charge_now is False
    assert d.reason == REASON_ABOVE_THRESHOLD


def test_deadband_zero_is_backward_compatible():
    # No deadband -> the ON bar is exactly T for both states (prior behaviour).
    t = _threshold(40)
    off = _decide(40, t - 0.01, charging=False, deadband=0.0)
    assert off.charge_now is True
    assert off.on_threshold == pytest.approx(t)


def test_decision_records_deadband_operands():
    # The decision carries the operands it compared, for self-explaining logs.
    d = _decide(40, 4.0, forecast=_hourly_forecast(4.0, 3.5, 6.0), deadband=0.8)
    assert d.deadband == pytest.approx(0.8)
    assert d.min_ahead == pytest.approx(3.5)


# --- minimum-off-time lockout ------------------------------------------------


def test_min_off_lockout_blocks_reonset_when_off():
    # Price would charge, but we only just stopped -> hold off (rate-limit re-ON).
    d = _decide(40, 2.0, charging=False, min_off_active=True)
    assert d.charge_now is False
    assert d.reason == REASON_MIN_OFF_LOCKOUT


def test_min_off_lockout_does_not_interrupt_active_charging():
    # Already charging: the lockout never applies, so a good price keeps charging.
    d = _decide(40, 2.0, charging=True, min_off_active=True)
    assert d.charge_now is True
    assert d.reason == REASON_BELOW_THRESHOLD


def test_min_off_lockout_never_blocks_off():
    # A spike above threshold still turns OFF during the window (OFF is instant).
    d = _decide(79, 40.0, charging=True, min_off_active=True)
    assert d.charge_now is False
    assert d.reason == REASON_ABOVE_THRESHOLD


def test_min_off_lockout_ignored_in_deadline_mode():
    # Deadline mode must never be blocked by the lockout; must_charge still wins.
    plan = _deadline_plan(2, soc=20)
    assert plan.slack_hours <= 0
    d = _decide(20, 55.0, plan=plan, min_off_active=True)
    assert d.charge_now is True
    assert d.reason == REASON_MUST_CHARGE


# --- deadline scheduling (cheapest hours, deadline guaranteed) ---------------


def _deadline_plan(hours: int, soc: float = 40.0):
    return plan_charge(
        NOW,
        NOW + timedelta(hours=hours),
        current_soc=soc,
        target_soc=80,
        capacity_kwh=75,
        charge_rate_kw=11,
        efficiency=0.9,
        forecast={},
    )


def test_deadline_defers_when_enough_cheaper_hours_ahead():
    plan = _deadline_plan(12)
    assert plan.slack_hours > 0
    forecast = _hourly_forecast(4.0, 3.0, 3.0)  # two cheaper hours ahead
    d = _decide(40, 4.0, plan=plan, forecast=forecast, hours_needed=1)
    assert d.charge_now is False
    assert d.reason == REASON_CHEAPER_LATER


def test_deadline_charges_when_current_among_cheapest_needed():
    plan = _deadline_plan(12)
    forecast = _hourly_forecast(4.0, 3.0, 3.0)  # two cheaper hours ahead
    # Three hours needed, only two cheaper ahead -> current hour is in the
    # cheapest three, so charge it.
    d = _decide(40, 4.0, plan=plan, forecast=forecast, hours_needed=3)
    assert d.charge_now is True
    assert d.reason == REASON_CHEAPEST_HOURS


def test_deadline_ignores_threshold_on_expensive_night():
    plan = _deadline_plan(12)
    # Every hour is a spike (above threshold), but the current hour is the
    # cheapest ahead -> charge it; threshold does not block a required charge.
    forecast = _hourly_forecast(40.0, 45.0, 50.0)
    d = _decide(40, 40.0, plan=plan, forecast=forecast, hours_needed=1)
    assert d.charge_now is True
    assert d.reason == REASON_CHEAPEST_HOURS


def test_deadline_must_charge_when_slack_gone():
    plan = _deadline_plan(2, soc=20)  # 2h available, needs far more
    assert plan.slack_hours <= 0
    forecast = _hourly_forecast(40.0, 3.0, 3.0)  # cheaper ahead, but no time
    d = _decide(20, 55.0, plan=plan, forecast=forecast, hours_needed=1)
    assert d.charge_now is True
    assert d.reason == REASON_MUST_CHARGE


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


# --- cheapest_forecast_hour --------------------------------------------------


def _forecast(*prices: float) -> dict:
    """Build a forecast from consecutive hourly prices (¢/kWh)."""
    return {
        (NOW + timedelta(hours=i + 1)): ForecastHour(
            NOW + timedelta(hours=i + 1), p, SOURCE_DAY_AHEAD
        )
        for i, p in enumerate(prices)
    }


def test_cheapest_forecast_hour_picks_lowest_price():
    forecast = _forecast(20.0, 4.0, 30.0)
    hour = cheapest_forecast_hour(forecast)
    assert hour is not None
    assert hour.price == pytest.approx(4.0)
    assert hour.hour_ending == NOW + timedelta(hours=2)


def test_cheapest_forecast_hour_earliest_wins_tie():
    forecast = _forecast(5.0, 4.0, 4.0)
    hour = cheapest_forecast_hour(forecast)
    assert hour is not None
    assert hour.hour_ending == NOW + timedelta(hours=2)


def test_cheapest_forecast_hour_empty():
    assert cheapest_forecast_hour({}) is None


# --- hour_buckets (settled-cost attribution) --------------------------------


def test_hour_buckets_single_hour():
    from custom_components.comed_ev.optimizer import hour_buckets

    start = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)
    got = hour_buckets(start, start + timedelta(hours=1))
    assert got == {datetime(2026, 8, 20, 3, 0, tzinfo=UTC): 1.0}


def test_hour_buckets_partial_hours_sum_to_one():
    from custom_components.comed_ev.optimizer import hour_buckets

    # 02:30 -> 04:30: half in hour ending 03:00, full 03:00-04:00, half in 05:00.
    start = datetime(2026, 8, 20, 2, 30, tzinfo=UTC)
    got = hour_buckets(start, datetime(2026, 8, 20, 4, 30, tzinfo=UTC))
    assert pytest.approx(sum(got.values())) == 1.0
    assert pytest.approx(got[datetime(2026, 8, 20, 3, 0, tzinfo=UTC)]) == 0.25
    assert pytest.approx(got[datetime(2026, 8, 20, 4, 0, tzinfo=UTC)]) == 0.5
    assert pytest.approx(got[datetime(2026, 8, 20, 5, 0, tzinfo=UTC)]) == 0.25


def test_hour_buckets_empty_interval():
    from custom_components.comed_ev.optimizer import hour_buckets

    t = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)
    assert hour_buckets(t, t) == {}
    assert hour_buckets(t, t - timedelta(hours=1)) == {}


# --- project_schedule --------------------------------------------------------

# 60 kWh pack, 10 kW wall, 0.9 efficiency -> +15% SOC per charging hour, and
# 50->80 needs 20 kWh wall = 2 whole hours.
_SCHED = {"capacity_kwh": 60.0, "charge_rate_kw": 10.0, "efficiency": 0.9}
_BAND = {"price_floor": 2.0, "price_ceiling": 10.0}


def _charging_ends(schedule) -> set[datetime]:
    return {h.hour_ending for h in schedule.hours if h.charging}


def test_schedule_deadline_reserves_the_cheapest_hours():
    # Cheapest two hours (2¢ at 23:00, 3¢ at 00:00) are reserved; pricier hours
    # before and after are skipped. Departure leaves ample slack.
    forecast = _hourly_forecast(5.0, 4.0, 2.0, 3.0, 6.0, 7.0)
    base = NOW.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    schedule = project_schedule(
        NOW, forecast, 50.0, 80.0, **_SCHED, **_BAND,
        departure=base + timedelta(hours=6),
    )
    assert schedule.mode == MODE_DEADLINE
    assert schedule.charging_hours == 2
    assert _charging_ends(schedule) == {
        base + timedelta(hours=2),  # 2¢
        base + timedelta(hours=3),  # 3¢
    }
    assert schedule.charging_energy_kwh == pytest.approx(20.0)
    assert schedule.ready_time == base + timedelta(hours=3)
    # Cost prices exactly the two charged hours: 10 kWh @2¢ + 10 kWh @3¢ = 50¢.
    assert schedule.charge_cost is not None
    assert schedule.charge_cost.energy_kwh == pytest.approx(20.0)
    assert schedule.charge_cost.supply_cost == pytest.approx(0.50)
    assert schedule.charge_cost.estimated_cost == pytest.approx(0.50)
    assert schedule.charge_cost.average_price == pytest.approx(0.025)


def test_schedule_cost_adds_distribution_over_charged_energy():
    # Distribution (6.5¢) applies to every charged kWh, on top of supply.
    forecast = _hourly_forecast(5.0, 4.0, 2.0, 3.0, 6.0, 7.0)
    base = NOW.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    schedule = project_schedule(
        NOW, forecast, 50.0, 80.0, **_SCHED, **_BAND,
        departure=base + timedelta(hours=6), distribution_rate=6.5,
    )
    cost = schedule.charge_cost
    assert cost is not None
    assert cost.supply_cost == pytest.approx(0.50)  # 10@2¢ + 10@3¢
    assert cost.distribution_cost == pytest.approx(20.0 * 6.5 / 100.0)  # $1.30
    assert cost.estimated_cost == pytest.approx(1.80)
    # Average price stays supply-only; it excludes distribution.
    assert cost.average_price == pytest.approx(0.025)


def test_schedule_no_charging_has_no_cost():
    forecast = _hourly_forecast(2.0, 2.0, 2.0)
    schedule = project_schedule(NOW, forecast, 80.0, 80.0, **_SCHED, **_BAND)
    assert schedule.charge_cost is None


def test_schedule_projected_soc_rises_then_holds_at_target():
    forecast = _hourly_forecast(5.0, 4.0, 2.0, 3.0, 6.0, 7.0)
    base = NOW.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    schedule = project_schedule(
        NOW, forecast, 50.0, 80.0, **_SCHED, **_BAND,
        departure=base + timedelta(hours=6),
    )
    socs = [h.projected_soc for h in schedule.hours]
    # 50 (skip), 50 (skip), 65 (charge), 80 (charge), 80, 80 — never falls, capped.
    assert socs == pytest.approx([50.0, 50.0, 65.0, 80.0, 80.0, 80.0])


def test_schedule_deadline_charges_pricey_hours_when_window_is_tight():
    # Two hours to departure and two needed: both charge despite high prices,
    # since deferral would miss the deadline.
    forecast = _hourly_forecast(9.0, 8.0)
    base = NOW.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    schedule = project_schedule(
        NOW, forecast, 50.0, 80.0, **_SCHED, **_BAND,
        departure=base + timedelta(hours=2),
    )
    assert schedule.charging_hours == 2
    assert schedule.ready_time == base + timedelta(hours=1)


def test_schedule_opportunistic_charges_only_below_threshold():
    # No departure: only the 2¢ trough beats T(SOC); pricier hours are skipped
    # and the target is never reached.
    forecast = _hourly_forecast(5.0, 4.0, 2.0, 3.0, 6.0, 7.0)
    base = NOW.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    schedule = project_schedule(NOW, forecast, 50.0, 80.0, **_SCHED, **_BAND)
    assert schedule.mode == MODE_OPPORTUNISTIC
    assert _charging_ends(schedule) == {base + timedelta(hours=2)}
    assert schedule.charging_hours == 1
    assert schedule.ready_time is None


def test_schedule_opportunistic_reserves_cheapest_hours_under_threshold():
    # Every hour is under T(SOC), so the threshold cap does not choose for us.
    # The two cheapest hours (1.5¢, 1.6¢) must be reserved even though earlier,
    # pricier hours are also affordable — no greedy-earliest charging.
    forecast = _hourly_forecast(2.6, 2.5, 1.5, 1.6, 2.4, 2.5)
    base = NOW.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    schedule = project_schedule(NOW, forecast, 50.0, 80.0, **_SCHED, **_BAND)
    assert schedule.mode == MODE_OPPORTUNISTIC
    assert _charging_ends(schedule) == {
        base + timedelta(hours=2),  # 1.5¢
        base + timedelta(hours=3),  # 1.6¢
    }
    assert schedule.charging_hours == 2
    assert schedule.ready_time == base + timedelta(hours=3)


def test_schedule_target_reached_is_all_idle():
    forecast = _hourly_forecast(2.0, 2.0, 2.0)
    schedule = project_schedule(NOW, forecast, 80.0, 80.0, **_SCHED, **_BAND)
    assert schedule.charging_hours == 0
    assert all(not h.charging for h in schedule.hours)
    assert schedule.charging_energy_kwh == 0.0


def test_schedule_empty_forecast_is_empty():
    schedule = project_schedule(NOW, {}, 50.0, 80.0, **_SCHED, **_BAND)
    assert schedule.hours == []
    assert schedule.charging_hours == 0
    assert schedule.ready_time is None
