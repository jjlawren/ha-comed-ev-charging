"""Offline unit tests for the SQLite session store (no HA harness)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from custom_components.comed_ev.session_store import (
    SCHEMA_VERSION,
    SessionStore,
    _parse,
)

CENTRAL_OFFSET = timezone(timedelta(hours=-5))


@pytest.fixture
def store(tmp_path):
    s = SessionStore(str(tmp_path / "sessions.db"))
    s.setup()
    return s


def _dt(hour: int) -> datetime:
    return datetime(2026, 8, 20, hour, 0, tzinfo=UTC)


def test_setup_is_idempotent_and_stamps_version(tmp_path):
    path = str(tmp_path / "s.db")
    store = SessionStore(path)
    store.setup()
    store.setup()  # second call must not raise
    import sqlite3

    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_insert_and_list_roundtrip(store):
    sid = store.insert_session(
        started_utc=_dt(2),
        ended_utc=_dt(5),
        energy_kwh=18.5,
        energy_source="meter",
        start_soc=40.0,
        end_soc=80.0,
    )
    assert sid == 1
    sessions = store.list_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.energy_kwh == 18.5
    assert s.energy_source == "meter"
    assert s.start_soc == 40.0
    assert s.settled_cost_cents is None
    assert s.settled_complete is False
    assert s.started_utc == _dt(2)


def test_timestamps_normalize_to_utc(store):
    # A non-UTC input datetime must come back as the same instant in UTC.
    local_start = datetime(2026, 8, 19, 21, 0, tzinfo=CENTRAL_OFFSET)  # 08-20 02:00 UTC
    store.insert_session(
        started_utc=local_start,
        ended_utc=local_start + timedelta(hours=2),
        energy_kwh=10.0,
        energy_source="soc",
    )
    s = store.list_sessions()[0]
    assert s.started_utc == _dt(2)
    assert s.started_utc.tzinfo == UTC


def test_update_cost_and_last_settled(store):
    a = store.insert_session(
        started_utc=_dt(1), ended_utc=_dt(2), energy_kwh=5.0, energy_source="meter"
    )
    b = store.insert_session(
        started_utc=_dt(3), ended_utc=_dt(4), energy_kwh=6.0, energy_source="meter"
    )
    # Nothing settled yet.
    assert store.get_last_settled_session() is None
    assert [s.id for s in store.sessions_incomplete()] == [a, b]

    store.update_session_cost(a, settled_cost_cents=120.0, settled_complete=True)
    last = store.get_last_settled_session()
    assert last is not None and last.id == a
    assert last.settled_cost_cents == 120.0
    assert last.settled_complete is True
    # b still incomplete.
    assert [s.id for s in store.sessions_incomplete()] == [b]

    # A later fully-settled session wins "last settled".
    store.update_session_cost(b, settled_cost_cents=90.0, settled_complete=True)
    assert store.get_last_settled_session().id == b


def test_list_sessions_time_range(store):
    for h in (1, 3, 5):
        store.insert_session(
            started_utc=_dt(h),
            ended_utc=_dt(h) + timedelta(hours=1),
            energy_kwh=1.0,
            energy_source="soc",
        )
    # Range overlapping only the middle session.
    got = store.list_sessions(start_utc=_dt(3), end_utc=_dt(4))
    assert [s.started_utc for s in got] == [_dt(3)]


def test_settled_prices_upsert_and_lookup(store):
    store.upsert_settled_prices({_dt(2): 3.1, _dt(3): 4.2})
    # Overwrite one, add one.
    store.upsert_settled_prices({_dt(3): 4.9, _dt(4): 5.0})
    got = store.get_settled_prices([_dt(2), _dt(3), _dt(4), _dt(9)])
    assert got == {_dt(2): 3.1, _dt(3): 4.9, _dt(4): 5.0}  # _dt(9) absent, not raised


def test_settled_prices_empty_inputs(store):
    store.upsert_settled_prices({})  # no-op, must not raise
    assert store.get_settled_prices([]) == {}


def test_parse_naive_string_assumes_utc():
    assert _parse("2026-08-20T02:00:00") == _dt(2)
