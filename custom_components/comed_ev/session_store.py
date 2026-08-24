"""SQLite store for settled charge-session cost records.

No Home Assistant imports; pure ``sqlite3`` so it is offline-testable. Every
method is synchronous and opens a short-lived connection per call — the
coordinator runs them via ``hass.async_add_executor_job``, and per-call
connections sidestep the thread-affinity constraint of a single ``sqlite3``
connection shared across executor threads.

Units: energy in kWh, prices and costs in ¢/kWh and cents. Timestamps are
stored as ISO-8601 UTC strings and surfaced as timezone-aware ``datetime``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import os
import sqlite3

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id                 INTEGER PRIMARY KEY,
    started_utc        TEXT NOT NULL,
    ended_utc          TEXT NOT NULL,
    start_soc          REAL,
    end_soc            REAL,
    energy_kwh         REAL NOT NULL,
    energy_source      TEXT NOT NULL,
    settled_cost_cents REAL,
    settled_complete   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settled_price (
    hour_ending_utc TEXT PRIMARY KEY,
    price_cents     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS transition (
    id             INTEGER PRIMARY KEY,
    ts_utc         TEXT NOT NULL,
    charging       INTEGER NOT NULL,
    reason         TEXT NOT NULL,
    mode           TEXT,
    decision_price REAL,
    threshold      REAL,
    on_threshold   REAL,
    deadband       REAL,
    volatility     REAL,
    lockout_held   INTEGER NOT NULL DEFAULT 0,
    context_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_transition_ts ON transition(ts_utc);

CREATE TABLE IF NOT EXISTS deferral (
    id             INTEGER PRIMARY KEY,
    started_utc    TEXT NOT NULL,
    ended_utc      TEXT NOT NULL,
    reason         TEXT NOT NULL,
    mode           TEXT,
    decision_price REAL,
    min_ahead      REAL,
    ended_reason   TEXT
);

CREATE INDEX IF NOT EXISTS idx_deferral_started ON deferral(started_utc);
"""


@dataclass(frozen=True)
class Session:
    """One charge session and its settled cost (once ComEd settles the hours)."""

    id: int
    started_utc: datetime
    ended_utc: datetime
    start_soc: float | None
    end_soc: float | None
    energy_kwh: float
    energy_source: str  # 'meter' or 'soc'
    settled_cost_cents: float | None
    settled_complete: bool


@dataclass(frozen=True)
class Transition:
    """One charge_now edge with the decision context that produced it.

    The durable troubleshooting record: it captures the operands compared at the
    instant the switch flipped, so a start or stop can be explained after the
    fact without the HA recorder. `context_json` carries the full decision dump
    for anything not promoted to its own column.
    """

    id: int
    ts_utc: datetime
    charging: bool
    reason: str
    mode: str | None
    decision_price: float | None
    threshold: float | None
    on_threshold: float | None
    deadband: float | None
    volatility: float | None
    lockout_held: bool
    context_json: str | None


@dataclass(frozen=True)
class Deferral:
    """One reserve-gate deferral episode: a contiguous hold, not an edge.

    Records a span the optimizer would have charged on price but the reserve gate
    (`cheaper_later`) held off to reach a cheaper hour. One row per hold, written
    when it settles — never per poll. `decision_price` is what charging would have
    cost at the hold's start, `min_ahead` the cheaper price it waited for, and
    `ended_reason` why the hold released (charging started, price rose, target
    reached).
    """

    id: int
    started_utc: datetime
    ended_utc: datetime
    reason: str
    mode: str | None
    decision_price: float | None
    min_ahead: float | None
    ended_reason: str | None


def _iso(value: datetime) -> str:
    """Serialize a datetime to an ISO-8601 UTC string."""
    return value.astimezone(UTC).isoformat()


def _parse(value: str) -> datetime:
    """Parse an ISO-8601 string back to a timezone-aware UTC datetime."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class SessionStore:
    """Durable per-session cost store backed by an integration-owned SQLite file."""

    def __init__(self, path: str) -> None:
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def setup(self) -> None:
        """Create the schema and apply migrations. Idempotent."""
        parent = os.path.dirname(self._path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            # Every schema object uses IF NOT EXISTS, so replaying the full
            # script additively upgrades an older DB (v1 -> v2 gains the
            # `transition` table; v2 -> v3 gains the `deferral` table). Only
            # stamp the version afterwards.
            conn.executescript(_SCHEMA)
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # --- sessions ------------------------------------------------------------
    def insert_session(
        self,
        *,
        started_utc: datetime,
        ended_utc: datetime,
        energy_kwh: float,
        energy_source: str,
        start_soc: float | None = None,
        end_soc: float | None = None,
    ) -> int:
        """Insert a completed session (cost still unsettled); return its id."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO session ("
                " started_utc, ended_utc, start_soc, end_soc,"
                " energy_kwh, energy_source, settled_cost_cents, settled_complete"
                ") VALUES (?, ?, ?, ?, ?, ?, NULL, 0)",
                (
                    _iso(started_utc),
                    _iso(ended_utc),
                    start_soc,
                    end_soc,
                    energy_kwh,
                    energy_source,
                ),
            )
            assert cur.lastrowid is not None  # guaranteed after a successful INSERT
            return cur.lastrowid

    def update_session_cost(
        self, session_id: int, settled_cost_cents: float, settled_complete: bool
    ) -> None:
        """Write a recomputed settled cost onto a session."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE session"
                " SET settled_cost_cents = ?, settled_complete = ?"
                " WHERE id = ?",
                (settled_cost_cents, 1 if settled_complete else 0, session_id),
            )

    def list_sessions(
        self,
        *,
        start_utc: datetime | None = None,
        end_utc: datetime | None = None,
    ) -> list[Session]:
        """Return sessions ordered oldest-first, optionally within a time range."""
        clauses: list[str] = []
        params: list[str] = []
        if start_utc is not None:
            clauses.append("ended_utc >= ?")
            params.append(_iso(start_utc))
        if end_utc is not None:
            clauses.append("started_utc <= ?")
            params.append(_iso(end_utc))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM session{where} ORDER BY started_utc", params
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    def get_last_settled_session(self) -> Session | None:
        """Return the most recent fully-settled session, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM session"
                " WHERE settled_complete = 1"
                " ORDER BY ended_utc DESC LIMIT 1"
            ).fetchone()
        return _row_to_session(row) if row is not None else None

    def sessions_incomplete(self) -> list[Session]:
        """Return sessions whose settled cost is not yet complete (oldest-first)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM session"
                " WHERE settled_complete = 0"
                " ORDER BY started_utc"
            ).fetchall()
        return [_row_to_session(row) for row in rows]

    # --- transitions ---------------------------------------------------------
    def insert_transition(
        self,
        *,
        ts_utc: datetime,
        charging: bool,
        reason: str,
        mode: str | None = None,
        decision_price: float | None = None,
        threshold: float | None = None,
        on_threshold: float | None = None,
        deadband: float | None = None,
        volatility: float | None = None,
        lockout_held: bool = False,
        context_json: str | None = None,
        retention: int | None = None,
    ) -> int:
        """Record a charge_now edge; return its id.

        With `retention` set, prune all but the newest `retention` rows after
        the insert so the table stays bounded (transitions are rare, but this
        caps a pathological chatter run).
        """
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO transition ("
                " ts_utc, charging, reason, mode, decision_price, threshold,"
                " on_threshold, deadband, volatility, lockout_held, context_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _iso(ts_utc),
                    1 if charging else 0,
                    reason,
                    mode,
                    decision_price,
                    threshold,
                    on_threshold,
                    deadband,
                    volatility,
                    1 if lockout_held else 0,
                    context_json,
                ),
            )
            if retention is not None:
                # Keep the newest `retention` rows; the OFFSET boundary is the
                # newest row to drop (NULL when fewer exist -> deletes nothing).
                conn.execute(
                    "DELETE FROM transition WHERE id <= ("
                    " SELECT id FROM transition ORDER BY id DESC LIMIT 1 OFFSET ?)",
                    (retention,),
                )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def list_transitions(self, limit: int = 20) -> list[Transition]:
        """Return the most recent transitions, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transition ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_transition(row) for row in rows]

    # --- deferrals -----------------------------------------------------------
    def insert_deferral(
        self,
        *,
        started_utc: datetime,
        ended_utc: datetime,
        reason: str,
        mode: str | None = None,
        decision_price: float | None = None,
        min_ahead: float | None = None,
        ended_reason: str | None = None,
        retention: int | None = None,
    ) -> int:
        """Record a settled deferral episode; return its id.

        With `retention` set, prune all but the newest `retention` rows after the
        insert so the table stays bounded, mirroring `insert_transition`.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO deferral ("
                " started_utc, ended_utc, reason, mode, decision_price,"
                " min_ahead, ended_reason"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _iso(started_utc),
                    _iso(ended_utc),
                    reason,
                    mode,
                    decision_price,
                    min_ahead,
                    ended_reason,
                ),
            )
            if retention is not None:
                conn.execute(
                    "DELETE FROM deferral WHERE id <= ("
                    " SELECT id FROM deferral ORDER BY id DESC LIMIT 1 OFFSET ?)",
                    (retention,),
                )
            assert cur.lastrowid is not None
            return cur.lastrowid

    def list_deferrals(self, limit: int = 20) -> list[Deferral]:
        """Return the most recent deferral episodes, newest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM deferral ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_deferral(row) for row in rows]

    # --- settled prices ------------------------------------------------------
    def upsert_settled_prices(self, prices: Mapping[datetime, float]) -> None:
        """Insert or replace settled hour-ending prices (¢/kWh)."""
        rows = [(_iso(hour), cents) for hour, cents in prices.items()]
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO settled_price (hour_ending_utc, price_cents)"
                " VALUES (?, ?)"
                " ON CONFLICT(hour_ending_utc) DO UPDATE SET price_cents = excluded.price_cents",
                rows,
            )

    def get_settled_prices(self, hours: Iterable[datetime]) -> dict[datetime, float]:
        """Return the settled ¢/kWh price for each known hour-ending in `hours`."""
        keys = [_iso(hour) for hour in hours]
        if not keys:
            return {}
        placeholders = ",".join("?" * len(keys))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT hour_ending_utc, price_cents FROM settled_price"
                f" WHERE hour_ending_utc IN ({placeholders})",
                keys,
            ).fetchall()
        return {_parse(row["hour_ending_utc"]): row["price_cents"] for row in rows}


def _row_to_transition(row: sqlite3.Row) -> Transition:
    return Transition(
        id=row["id"],
        ts_utc=_parse(row["ts_utc"]),
        charging=bool(row["charging"]),
        reason=row["reason"],
        mode=row["mode"],
        decision_price=row["decision_price"],
        threshold=row["threshold"],
        on_threshold=row["on_threshold"],
        deadband=row["deadband"],
        volatility=row["volatility"],
        lockout_held=bool(row["lockout_held"]),
        context_json=row["context_json"],
    )


def _row_to_deferral(row: sqlite3.Row) -> Deferral:
    return Deferral(
        id=row["id"],
        started_utc=_parse(row["started_utc"]),
        ended_utc=_parse(row["ended_utc"]),
        reason=row["reason"],
        mode=row["mode"],
        decision_price=row["decision_price"],
        min_ahead=row["min_ahead"],
        ended_reason=row["ended_reason"],
    )


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        started_utc=_parse(row["started_utc"]),
        ended_utc=_parse(row["ended_utc"]),
        start_soc=row["start_soc"],
        end_soc=row["end_soc"],
        energy_kwh=row["energy_kwh"],
        energy_source=row["energy_source"],
        settled_cost_cents=row["settled_cost_cents"],
        settled_complete=bool(row["settled_complete"]),
    )
