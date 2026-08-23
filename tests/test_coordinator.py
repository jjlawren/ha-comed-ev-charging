"""Coordinator/entity tests using the HA test harness."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.comed_ev.const import (
    CONF_CAPACITY_KWH,
    CONF_CURRENT_SOC_ENTITY,
    CONF_ENERGY_EVSE_ENTITY,
    CONF_ENERGY_VEHICLE_ENTITY,
    CONF_TARGET_SOC_ENTITY,
    CONF_THRESHOLD_MODE,
    DOMAIN,
    MODE_AUTO,
)


def _entry(
    extra_data: dict | None = None, extra_options: dict | None = None
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="ComEd EV Charging",
        data={
            CONF_CAPACITY_KWH: 75.0,
            "efficiency": 0.9,
            CONF_CURRENT_SOC_ENTITY: "sensor.ev_soc",
            CONF_TARGET_SOC_ENTITY: "number.ev_target",
            "charge_rate_kw": 11.0,
            **(extra_data or {}),
        },
        options={CONF_THRESHOLD_MODE: MODE_AUTO, **(extra_options or {})},
    )


async def test_setup_creates_entities_and_decides(
    hass: HomeAssistant, mock_client
) -> None:
    """A low SOC on a cheap running hourly average turns charge_now on."""
    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")

    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Newest point of the newest-first feed is 0.04 $/kWh -> 4 ¢/kWh.
    price = hass.states.get("sensor.comed_ev_charging_5_minute_spot_price")
    assert price is not None
    assert float(price.state) == 4.0

    charge = hass.states.get("binary_sensor.comed_ev_charging_charge_now")
    assert charge is not None
    assert charge.state == "on"
    assert charge.attributes["reason"] == "below_threshold"


async def test_current_price_uses_newest_feed_point(
    hass: HomeAssistant, mock_client
) -> None:
    """Current price is the newest point of the newest-first feed, not the oldest."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    from comed_hourly_pricing import PricePoint

    central = ZoneInfo("America/Chicago")
    base = datetime(2026, 8, 19, 3, 0, tzinfo=central)
    # Newest-first: index 0 is the latest 5-minute point (0.07 $/kWh -> 7 ¢/kWh),
    # index -1 is the oldest (0.01 $/kWh -> 1 ¢/kWh, the stale value the old bug used).
    feed = (
        PricePoint(base + timedelta(minutes=10), 0.07),
        PricePoint(base + timedelta(minutes=5), 0.03),
        PricePoint(base, 0.01),
    )
    mock_client.get_five_minute_feed.return_value = feed

    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    price = hass.states.get("sensor.comed_ev_charging_5_minute_spot_price")
    assert price is not None
    assert float(price.state) == 7.0  # newest, not the stale 1.0 at index -1


async def test_decision_follows_hourly_average_not_five_minute(
    hass: HomeAssistant, mock_client
) -> None:
    """A spike in the running hourly average keeps charging off even when the
    latest 5-minute point is cheap — the decision compares the hourly average."""
    # Latest 5-minute point is cheap (4 ¢/kWh from the sample feed) but the
    # running hourly average is a 50 ¢/kWh spike, above any threshold.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from comed_hourly_pricing import PricePoint

    from custom_components.comed_ev.const import (
        CONF_PRICE_CEILING,
        CONF_PRICE_FLOOR,
        CONF_THRESHOLD_MODE,
        MODE_MANUAL,
    )

    central = ZoneInfo("America/Chicago")
    mock_client.get_current_hour_average.return_value = PricePoint(
        datetime.now(central), 0.5
    )

    hass.states.async_set("sensor.ev_soc", "20")  # low SOC -> high willingness
    hass.states.async_set("number.ev_target", "80")
    entry = _entry(
        extra_options={
            CONF_THRESHOLD_MODE: MODE_MANUAL,
            CONF_PRICE_FLOOR: 3.0,
            CONF_PRICE_CEILING: 14.0,
        }
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    charge = hass.states.get("binary_sensor.comed_ev_charging_charge_now")
    assert charge is not None
    assert charge.state == "off"  # would be "on" if it used the cheap 5-min point
    assert charge.attributes["reason"] == "above_threshold"
    assert charge.attributes["decision_price"] == 50.0


async def test_backfill_seeds_history_and_suggestion(
    hass: HomeAssistant, mock_client
) -> None:
    """Empty history triggers chunked backfill that seeds the rolling window."""
    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")

    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    # Backfill ran with start/end ranges and populated the merged history.
    assert coordinator._history
    assert any(
        call.kwargs.get("start") is not None
        for call in mock_client.get_five_minute_feed.call_args_list
    )
    # A seeded window yields a real percentile suggestion.
    assert coordinator._suggestion.sample_size > 0


async def test_measured_efficiency_from_energy_meters(
    hass: HomeAssistant, mock_client
) -> None:
    """Meter advances accumulate into a measured vehicle/wall ratio."""
    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    hass.states.async_set("sensor.ev_delivered", "0")
    hass.states.async_set("sensor.evse_drawn", "0")

    entry = _entry(
        {
            CONF_ENERGY_VEHICLE_ENTITY: "sensor.ev_delivered",
            CONF_ENERGY_EVSE_ENTITY: "sensor.evse_drawn",
        }
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    # Below the sample-size gate, no measured value yet.
    assert coordinator._measured_efficiency() is None

    # A charging session: 10 kWh into the battery, 11.1 kWh drawn -> ~0.9.
    hass.states.async_set("sensor.ev_delivered", "10.0")
    hass.states.async_set("sensor.evse_drawn", "11.1")
    await hass.async_block_till_done()

    measured = coordinator._measured_efficiency()
    assert measured is not None
    assert round(measured, 3) == round(10.0 / 11.1, 3)

    eff = hass.states.get("sensor.comed_ev_charging_measured_efficiency")
    assert eff is not None
    assert round(float(eff.state), 3) == round(10.0 / 11.1, 3)


async def test_diagnostics_reports_energy_totals(
    hass: HomeAssistant, mock_client
) -> None:
    """Diagnostics exposes the running meter totals and the raw ratio."""
    from custom_components.comed_ev.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    hass.states.async_set("sensor.ev_delivered", "0")
    hass.states.async_set("sensor.evse_drawn", "0")

    entry = _entry(
        {
            CONF_ENERGY_VEHICLE_ENTITY: "sensor.ev_delivered",
            CONF_ENERGY_EVSE_ENTITY: "sensor.evse_drawn",
        }
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    hass.states.async_set("sensor.ev_delivered", "10.0")
    hass.states.async_set("sensor.evse_drawn", "11.1")
    await hass.async_block_till_done()

    diag = await async_get_config_entry_diagnostics(hass, entry)
    energy = diag["energy"]
    assert energy["vehicle_entity"] == "sensor.ev_delivered"
    assert energy["evse_entity"] == "sensor.evse_drawn"
    assert energy["vehicle_total_kwh"] == pytest.approx(10.0)
    assert energy["evse_total_kwh"] == pytest.approx(11.1)
    assert energy["raw_ratio"] == pytest.approx(10.0 / 11.1)
    assert energy["measured_efficiency"] == pytest.approx(10.0 / 11.1)
    assert energy["min_sample_kwh"] == 2.0
    assert energy["accepted_ratio_range"] == [0.5, 1.0]


async def test_meter_reset_is_ignored(hass: HomeAssistant, mock_client) -> None:
    """A counter reset does not subtract from the accumulated totals."""
    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    hass.states.async_set("sensor.ev_delivered", "0")
    hass.states.async_set("sensor.evse_drawn", "0")

    entry = _entry(
        {
            CONF_ENERGY_VEHICLE_ENTITY: "sensor.ev_delivered",
            CONF_ENERGY_EVSE_ENTITY: "sensor.evse_drawn",
        }
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    hass.states.async_set("sensor.ev_delivered", "9.0")
    hass.states.async_set("sensor.evse_drawn", "10.0")
    await hass.async_block_till_done()
    # Meters reset to a lower value; the drop must not be counted.
    hass.states.async_set("sensor.ev_delivered", "0.9")
    hass.states.async_set("sensor.evse_drawn", "1.0")
    await hass.async_block_till_done()

    # The reset delta is dropped; totals keep the pre-reset advance (bar a
    # negligible sub-second decay), not the tiny post-reset reading.
    assert coordinator._energy_vehicle_total == pytest.approx(9.0, abs=1e-3)
    assert coordinator._energy_evse_total == pytest.approx(10.0, abs=1e-3)


async def test_decay_reweights_recent_sessions(
    hass: HomeAssistant, mock_client
) -> None:
    """Old totals decay before a new delta lands, so it weighs more."""
    from datetime import timedelta

    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    hass.states.async_set("sensor.ev_delivered", "0")
    hass.states.async_set("sensor.evse_drawn", "0")

    entry = _entry(
        {
            CONF_ENERGY_VEHICLE_ENTITY: "sensor.ev_delivered",
            CONF_ENERGY_EVSE_ENTITY: "sensor.evse_drawn",
        }
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    # An old poor session: 8 kWh delivered, 10 drawn (0.80).
    hass.states.async_set("sensor.ev_delivered", "8.0")
    hass.states.async_set("sensor.evse_drawn", "10.0")
    await hass.async_block_till_done()

    # Age it 30 days, then a fresh strong session: +9.5 kWh, +10 drawn (0.95).
    coordinator._last_energy_decay -= timedelta(days=30)
    hass.states.async_set("sensor.ev_delivered", "17.5")
    hass.states.async_set("sensor.evse_drawn", "20.0")
    await hass.async_block_till_done()

    # The decayed old session pulls the blended ratio above its own 0.80/0.875
    # midpoint toward the recent 0.95.
    measured = coordinator._measured_efficiency()
    assert measured is not None
    assert 0.875 < measured < 0.95
    # The stored totals are smaller than a plain sum thanks to the decay.
    assert coordinator._energy_evse_total < 20.0


async def test_energy_needed_and_cost_sensors(
    hass: HomeAssistant, mock_client
) -> None:
    """Energy-needed and overnight cost/avg-price sensors are published."""
    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")

    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # 60% of 75 kWh = 45 kWh to battery / 0.9 efficiency = 50 kWh from the wall.
    energy = hass.states.get("sensor.comed_ev_charging_energy_needed_to_target")
    assert energy is not None
    assert float(energy.state) == pytest.approx(50.0)

    # No departure -> the overnight window still yields a cost estimate.
    cost = hass.states.get("sensor.comed_ev_charging_estimated_charge_cost")
    assert cost is not None
    assert float(cost.state) > 0.0

    avg = hass.states.get("sensor.comed_ev_charging_estimated_charge_average_price")
    assert avg is not None
    assert float(avg.state) > 0.0


async def test_target_reached_turns_off(hass: HomeAssistant, mock_client) -> None:
    """At/above target the binary sensor stays off."""
    hass.states.async_set("sensor.ev_soc", "80")
    hass.states.async_set("number.ev_target", "80")

    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    charge = hass.states.get("binary_sensor.comed_ev_charging_charge_now")
    assert charge.state == "off"
    assert charge.attributes["reason"] == "target_reached"


async def test_input_change_does_not_reset_poll_clock(
    hass: HomeAssistant, mock_client
) -> None:
    """An input state change republishes data but must not reschedule the poll.

    Regression: the handlers called async_set_updated_data(), which cancels and
    reschedules the update_interval timer. Frequently-updating inputs then kept
    pushing the next API poll into the future, so the feeds only refreshed on a
    reload. Publishing via async_update_listeners() leaves the poll intact.
    """
    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")

    entry = _entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = entry.runtime_data
    # The periodic poll is scheduled once entities subscribe as listeners.
    scheduled_poll = coordinator._unsub_refresh
    assert scheduled_poll is not None
    data_before = coordinator.data

    # A watched input changes: the decision must be republished to entities...
    hass.states.async_set("sensor.ev_soc", "30")
    await hass.async_block_till_done()
    assert coordinator.data is not data_before

    # ...but the pending API poll must be the very same scheduled call, untouched.
    assert coordinator._unsub_refresh is scheduled_poll


# --- session recording (phase 2) --------------------------------------------


def _decision_data(coordinator, charge_now: bool):
    """Build a minimal ComEdData carrying a charge decision for the given state."""
    from custom_components.comed_ev.coordinator import ComEdData
    from custom_components.comed_ev.optimizer import ChargeDecision

    return ComEdData(
        live_price=4.0,
        hourly_price=4.0,
        decision=ChargeDecision(
            charge_now=charge_now,
            reason="",
            decision_price=4.0,
            threshold=5.0,
            urgency=0.5,
            gamma=2.5,
        ),
        suggestion=coordinator._suggestion,
        effective_floor=3.0,
        effective_ceiling=14.0,
        mode=MODE_AUTO,
    )


async def _build_coordinator(hass, extra_data=None, extra_options=None):
    import uuid

    from custom_components.comed_ev.session_store import SessionStore

    entry = _entry(extra_data, extra_options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = entry.runtime_data
    # Isolate the session DB per test with a unique name under the hass config
    # dir (the default file is shared across tests; the config dir is cleaned by
    # the hass fixture, after background tasks drain, avoiding a teardown race).
    coordinator._session_store = SessionStore(
        hass.config.path(".storage", f"sessions_{uuid.uuid4().hex}.db")
    )
    await hass.async_add_executor_job(coordinator._session_store.setup)
    return coordinator


async def test_session_recorded_from_meter(hass: HomeAssistant, mock_client) -> None:
    """A full on->off run with a wall meter records one meter-sourced session."""
    from datetime import UTC, datetime, timedelta

    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    hass.states.async_set("sensor.evse_drawn", "0")

    coordinator = await _build_coordinator(
        hass, {CONF_ENERGY_EVSE_ENTITY: "sensor.evse_drawn"}
    )
    t0 = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)

    # Observe not-charging first, then a rising edge opens the session.
    await coordinator._update_session(_decision_data(coordinator, False), t0)
    await coordinator._update_session(
        _decision_data(coordinator, True), t0 + timedelta(minutes=5)
    )
    # Wall meter advances during the run.
    hass.states.async_set("sensor.evse_drawn", "12.0")
    await hass.async_block_till_done()
    # Falling edge closes and records the session.
    await coordinator._update_session(
        _decision_data(coordinator, False), t0 + timedelta(hours=3)
    )

    sessions = await hass.async_add_executor_job(
        coordinator._session_store.list_sessions
    )
    assert len(sessions) == 1
    assert sessions[0].energy_source == "meter"
    assert sessions[0].energy_kwh == pytest.approx(12.0, abs=1e-3)
    assert sessions[0].settled_complete is False


async def test_session_soc_fallback(hass: HomeAssistant, mock_client) -> None:
    """With no meter, energy comes from the SOC rise (source 'soc')."""
    from datetime import UTC, datetime, timedelta

    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")

    coordinator = await _build_coordinator(hass)
    t0 = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)

    await coordinator._update_session(_decision_data(coordinator, False), t0)
    await coordinator._update_session(
        _decision_data(coordinator, True), t0 + timedelta(minutes=5)
    )
    # SOC climbs 20 -> 60 over the run.
    hass.states.async_set("sensor.ev_soc", "60")
    await hass.async_block_till_done()
    await coordinator._update_session(
        _decision_data(coordinator, False), t0 + timedelta(hours=2)
    )

    sessions = await hass.async_add_executor_job(
        coordinator._session_store.list_sessions
    )
    assert len(sessions) == 1
    assert sessions[0].energy_source == "soc"
    # 40% of 75 kWh into the battery, / 0.9 efficiency = ~33.3 kWh at the wall.
    assert sessions[0].energy_kwh == pytest.approx(0.40 * 75.0 / 0.9, abs=1e-3)
    assert sessions[0].start_soc == 20.0
    assert sessions[0].end_soc == 60.0


async def test_straddling_session_dropped(hass: HomeAssistant, mock_client) -> None:
    """A charge already on at startup is dropped, not recorded (Option A)."""
    from datetime import UTC, datetime, timedelta

    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")

    coordinator = await _build_coordinator(hass)
    t0 = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)

    # First observation is already charging -> straddling, no session opened.
    await coordinator._update_session(_decision_data(coordinator, True), t0)
    assert coordinator._session is None
    # The eventual falling edge records nothing.
    await coordinator._update_session(
        _decision_data(coordinator, False), t0 + timedelta(hours=1)
    )

    sessions = await hass.async_add_executor_job(
        coordinator._session_store.list_sessions
    )
    assert sessions == []


async def test_zero_energy_session_not_recorded(
    hass: HomeAssistant, mock_client
) -> None:
    """A run that delivered no measurable energy is not recorded."""
    from datetime import UTC, datetime, timedelta

    hass.states.async_set("sensor.ev_soc", "80")
    hass.states.async_set("number.ev_target", "80")

    coordinator = await _build_coordinator(hass)
    t0 = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)

    await coordinator._update_session(_decision_data(coordinator, False), t0)
    await coordinator._update_session(
        _decision_data(coordinator, True), t0 + timedelta(minutes=5)
    )
    # SOC unchanged; no meter -> zero energy.
    await coordinator._update_session(
        _decision_data(coordinator, False), t0 + timedelta(hours=1)
    )

    sessions = await hass.async_add_executor_job(
        coordinator._session_store.list_sessions
    )
    assert sessions == []


# --- settled-cost backfill (phase 3) ----------------------------------------


def _hourly(central_hour: int, actual):
    """A HourlyPrice for an August (CDT) hour-ending, actual in $/kWh."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from comed_hourly_pricing import HourlyPrice

    central = ZoneInfo("America/Chicago")
    return HourlyPrice(
        hour_ending=datetime(2026, 8, 20, central_hour, 0, tzinfo=central),
        estimated=actual,
        actual=actual,
    )


async def test_settle_costs_prices_a_session(
    hass: HomeAssistant, mock_client
) -> None:
    """A fully-settled session gets a time-weighted cost at settled prices."""
    from datetime import UTC, datetime

    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    coordinator = await _build_coordinator(hass)

    # Session 07:30-08:30 UTC = 02:30-03:30 CDT -> buckets ending 03:00 & 04:00
    # CDT (08:00 & 09:00 UTC), each half the run.
    await hass.async_add_executor_job(
        lambda: coordinator._session_store.insert_session(
            started_utc=datetime(2026, 8, 20, 7, 30, tzinfo=UTC),
            ended_utc=datetime(2026, 8, 20, 8, 30, tzinfo=UTC),
            energy_kwh=20.0,
            energy_source="meter",
        )
    )
    # Settled prices: 3¢ for the 03:00 hour, 5¢ for the 04:00 hour.
    mock_client.get_dual.return_value = (_hourly(3, 0.03), _hourly(4, 0.05))

    await coordinator._async_settle_costs()

    sessions = await hass.async_add_executor_job(
        coordinator._session_store.list_sessions
    )
    assert sessions[0].settled_complete is True
    # 20 kWh * (0.5*3¢ + 0.5*5¢) = 80¢.
    assert sessions[0].settled_cost_cents == pytest.approx(80.0)


async def test_settle_costs_partial_stays_incomplete(
    hass: HomeAssistant, mock_client
) -> None:
    """A session with an unsettled hour keeps settled_complete False."""
    from datetime import UTC, datetime

    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    coordinator = await _build_coordinator(hass)

    await hass.async_add_executor_job(
        lambda: coordinator._session_store.insert_session(
            started_utc=datetime(2026, 8, 20, 7, 30, tzinfo=UTC),
            ended_utc=datetime(2026, 8, 20, 8, 30, tzinfo=UTC),
            energy_kwh=20.0,
            energy_source="meter",
        )
    )
    # Only the first hour has settled; the 04:00 hour is still n/a.
    mock_client.get_dual.return_value = (_hourly(3, 0.03), _hourly(4, None))

    await coordinator._async_settle_costs()

    sessions = await hass.async_add_executor_job(
        coordinator._session_store.list_sessions
    )
    assert sessions[0].settled_complete is False
    assert sessions[0].settled_cost_cents is None


# --- exposure: sensors and service (phase 4) --------------------------------


async def test_last_session_sensors_with_savings(
    hass: HomeAssistant, mock_client
) -> None:
    """Settling a session publishes cost and (with a flat rate) savings."""
    from datetime import UTC, datetime

    from custom_components.comed_ev.const import CONF_FLAT_RATE

    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    # Flat baseline 10¢/kWh so savings sensor is created and computable.
    coordinator = await _build_coordinator(hass, extra_options={CONF_FLAT_RATE: 10.0})

    await hass.async_add_executor_job(
        lambda: coordinator._session_store.insert_session(
            started_utc=datetime(2026, 8, 20, 7, 30, tzinfo=UTC),
            ended_utc=datetime(2026, 8, 20, 8, 30, tzinfo=UTC),
            energy_kwh=20.0,
            energy_source="meter",
        )
    )
    mock_client.get_dual.return_value = (_hourly(3, 0.03), _hourly(4, 0.05))
    await coordinator._async_settle_costs()
    await hass.async_block_till_done()

    cost = hass.states.get("sensor.comed_ev_charging_last_session_cost")
    assert cost is not None
    assert float(cost.state) == pytest.approx(0.80)  # 80¢
    assert cost.attributes["cents_per_kwh"] == pytest.approx(4.0)
    assert cost.attributes["energy_source"] == "meter"

    # Baseline 10¢ * 20 kWh = $2.00; settled $0.80 -> $1.20 saved.
    savings = hass.states.get("sensor.comed_ev_charging_last_session_savings")
    assert savings is not None
    assert float(savings.state) == pytest.approx(1.20)


async def test_savings_sensor_unknown_without_flat_rate(
    hass: HomeAssistant, mock_client
) -> None:
    """No flat-rate baseline -> the savings sensor exists but reports unknown.

    The flat rate is now a live number entity (0 = disabled), so the sensor is
    always created and simply has no value until a baseline is set.
    """
    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    await _build_coordinator(hass)
    savings = hass.states.get("sensor.comed_ev_charging_last_session_savings")
    assert savings is not None
    assert savings.state == "unknown"


async def test_get_sessions_service(hass: HomeAssistant, mock_client) -> None:
    """The get_sessions service returns recorded rows with derived fields."""
    from datetime import UTC, datetime

    from custom_components.comed_ev.const import CONF_FLAT_RATE

    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    coordinator = await _build_coordinator(hass, extra_options={CONF_FLAT_RATE: 10.0})

    await hass.async_add_executor_job(
        lambda: coordinator._session_store.insert_session(
            started_utc=datetime(2026, 8, 20, 7, 30, tzinfo=UTC),
            ended_utc=datetime(2026, 8, 20, 8, 30, tzinfo=UTC),
            energy_kwh=20.0,
            energy_source="meter",
        )
    )
    mock_client.get_dual.return_value = (_hourly(3, 0.03), _hourly(4, 0.05))
    await coordinator._async_settle_costs()

    response = await hass.services.async_call(
        DOMAIN, "get_sessions", {}, blocking=True, return_response=True
    )
    rows = response["sessions"]
    assert len(rows) == 1
    assert rows[0]["supply_cost"] == pytest.approx(0.80)
    assert rows[0]["total_cost"] == pytest.approx(0.80)  # no distribution set
    assert rows[0]["cents_per_kwh"] == pytest.approx(4.0)
    assert rows[0]["savings"] == pytest.approx(1.20)
    assert rows[0]["settled_complete"] is True
    assert rows[0]["energy_source"] == "meter"


async def test_distribution_rate_added_to_cost(
    hass: HomeAssistant, mock_client
) -> None:
    """Distribution ¢/kWh is added to settled supply for actual cost, but not
    to savings (which stays a supply-only comparison)."""
    from datetime import UTC, datetime

    from custom_components.comed_ev.const import (
        CONF_DISTRIBUTION_RATE,
        CONF_FLAT_RATE,
    )

    hass.states.async_set("sensor.ev_soc", "20")
    hass.states.async_set("number.ev_target", "80")
    coordinator = await _build_coordinator(
        hass,
        extra_options={CONF_FLAT_RATE: 10.0, CONF_DISTRIBUTION_RATE: 6.0},
    )

    await hass.async_add_executor_job(
        lambda: coordinator._session_store.insert_session(
            started_utc=datetime(2026, 8, 20, 7, 30, tzinfo=UTC),
            ended_utc=datetime(2026, 8, 20, 8, 30, tzinfo=UTC),
            energy_kwh=20.0,
            energy_source="meter",
        )
    )
    mock_client.get_dual.return_value = (_hourly(3, 0.03), _hourly(4, 0.05))
    await coordinator._async_settle_costs()
    await hass.async_block_till_done()

    # Supply 80¢ + distribution 6¢ * 20 kWh = 120¢ -> $2.00 total.
    cost = hass.states.get("sensor.comed_ev_charging_last_session_cost")
    assert float(cost.state) == pytest.approx(2.00)
    assert cost.attributes["cents_per_kwh"] == pytest.approx(10.0)
    assert cost.attributes["supply_cost"] == pytest.approx(0.80)
    assert cost.attributes["distribution_cost"] == pytest.approx(1.20)

    # Savings unchanged: baseline 10¢ vs settled supply 4¢ = $1.20.
    savings = hass.states.get("sensor.comed_ev_charging_last_session_savings")
    assert float(savings.state) == pytest.approx(1.20)


async def test_past_departure_treated_as_unset(
    hass: HomeAssistant, mock_client
) -> None:
    """A departure time in the past is ignored (input_datetime cannot clear)."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.comed_ev.const import CONF_DEPARTURE_ENTITY

    coordinator = await _build_coordinator(
        hass, extra_data={CONF_DEPARTURE_ENTITY: "input_datetime.ev_departure"}
    )
    now = dt_util.utcnow()

    # A future departure is used.
    future = now + timedelta(hours=6)
    hass.states.async_set(
        "input_datetime.ev_departure",
        future.isoformat(),
        {"timestamp": future.timestamp()},
    )
    got = coordinator._get_departure()
    assert got is not None
    assert abs(got - future) < timedelta(seconds=1)

    # A past departure falls back to None (the overnight window).
    past = now - timedelta(hours=1)
    hass.states.async_set(
        "input_datetime.ev_departure",
        past.isoformat(),
        {"timestamp": past.timestamp()},
    )
    assert coordinator._get_departure() is None
