# Settled session-cost reporting — implementation plan

Status: **implemented.** Built across `session_store.py` (SQLite store),
`coordinator.py` (session boundary detection + daily settled-cost backfill),
`sensor.py` (`last_session_cost` / `last_session_savings`), and `services.py`
(`comed_ev.get_sessions`). The `flat_rate` option drives savings. See the README
"Session cost reporting" section for usage. The design below records the rationale.

## Goal

After a charge session ends, report its **actual cost** using ComEd's *settled*
prices — the final hourly prices ComEd publishes after the fact, which differ from
the live 5-minute and current-hour-average values the component decides on in real
time. Surface, per session:

- Energy delivered (kWh) and duration.
- Cost at settled prices (¢ and $).
- Effective ¢/kWh, and a comparison vs. a flat-rate baseline (savings).

**Supply vs. distribution.** The ComEd feed (live and settled) is the **supply**
rate only. The real bill also carries a fixed **distribution** rate per kWh. So the
DB stores supply-only settled cost (the part that needs the settled backfill), and a
configured distribution rate (`CONF_DISTRIBUTION_RATE`, ¢/kWh) is added at read time
to give actual cost = settled supply + distribution × energy. Distribution is a live
constant, so changing it retroactively corrects reported cost with no re-backfill.
Charging **decisions** stay on supply only — it is the sole variable rate — and
**savings** compares supply only too, since distribution is billed the same on any
plan and cancels out.

## Why the rolling JSON history is not enough

The component already keeps a rolling ~30-day price window in HA `Store`
(`.storage/comed_ev.history`) for percentile threshold suggestions. That store is a
dict wrapping the **pruned** `history` list plus an `energy` block (vehicle/EVSE
lifetime meter totals used for measured efficiency); the prices in it are **live**
values only. Session-cost reporting needs:

- **Durable** rows that outlive the rolling window (months of session records).
- **Settled** prices, joined to sessions after ComEd publishes them (a later pass).
- Relational-ish queries (per session, per month, per season).

These are different needs, so keep the JSON store as-is and add a dedicated store.

## Proposed storage: dedicated SQLite

Use an integration-owned SQLite file (e.g. `.storage/comed_ev_sessions.db`), not the
HA Recorder DB — keeps schema migrations and retention under the component's control
and avoids coupling to Recorder purge settings.

Suggested tables:

```
session(
  id INTEGER PRIMARY KEY,
  started_utc TEXT, ended_utc TEXT,
  start_soc REAL, end_soc REAL,
  energy_kwh REAL,
  energy_source TEXT,           -- 'meter' or 'soc' (provenance of energy_kwh)
  settled_cost_cents REAL,      -- NULL until settled prices fill in
  settled_complete INTEGER      -- 0/1, all hours have a settled price
)

settled_price(
  hour_ending_utc TEXT PRIMARY KEY,
  price_cents REAL
)
```

Retention: **keep all rows** (no pruning). A year of nightly sessions is ~365 rows /
well under 1 MB; SQLite handles years of it without a cap.

Session energy is attributed to hour buckets; cost = Σ (bucket_kwh × settled_price).
The optimizer's `estimate_charge_cost` (upcoming-charge estimate at *forecast* prices)
already buckets energy into hours and prices it; reuse that hour-bucketing here rather
than reinventing it — the only difference is the settled price source.
A background pass backfills `settled_price` from the ComEd settled feed and recomputes
`settled_cost_cents` for sessions whose hours are now fully settled.

All DB I/O must run off the event loop (`hass.async_add_executor_job`).

## Session boundary detection

A "session" is a contiguous run of `charge_now == on`. Detect edges from the existing
decision stream (or the binary_sensor state), recording start/end SOC from the SOC
entity at each edge.

Prefer a **meter delta** for energy: the coordinator's `_meter_delta()` already yields
positive wall-kWh advance per vehicle/EVSE meter with reset handling — reuse it. A meter
delta is wall energy directly, so **no efficiency factor** is needed for actual cost.

Fall back to SOC when no meter is set: energy ≈ (end_soc − start_soc)/100 ×
capacity_kwh ÷ efficiency. Note capacity may come from an entity (`_capacity_kwh()`) and
can vary, so sample it at the session rather than assuming a fixed constant; efficiency
should be the measured value when available, else the configured constant.

## Seasonal analysis (available now, no new code)

For season-over-season *price* trends the price sensors already carry
`state_class=measurement`, so HA **long-term statistics** aggregate them for free —
usable today via the Statistics card. Session-cost reporting above is the only piece
that needs the dedicated SQLite store.

## Settled-price feed (resolved — no new client method needed)

`comed-hourly-pricing` already exposes the settled feed via `Client.get_dual(day)`
→ `HourlyDayPrices`:

- Each `HourlyPrice` has `.actual` — the **settled** supply rate in **dollars/kWh**,
  `None` until the hour settles (source shows `n/a`). Convert to cents with ×100 for
  the `settled_price` table.
- `.hour_ending` is a timezone-aware **Central** timestamp marking the **end** of the
  hour; store it as UTC in `settled_price`.
- `.settled()` / `.unsettled()` split the day's hours; a day is fully settled when
  `.unsettled()` is empty.
- `get_dual()` is **already called** each cycle in `_refresh_hourly_feeds()`
  (coordinator.py) for the estimated prices — it currently discards `.actual`. The
  backfill reuses the same call per historical `day` and reads `.actual`.

Publish latency: hours settle after the fact, not all at once. The backfill polls
`get_dual(day)` for any day with unsettled session-hours and stops re-polling a day
once `.unsettled()` is empty. Daily cadence is enough; no live path change.

## Decisions (locked)

- **Retention:** keep all session rows (no pruning).
- **Exposure:** last-session sensors **and** a `get_sessions` service (both). Sensors
  cover the dashboard; the service returns the full history for automations/reports.

## Implementation plan

Phased so each step is testable on its own. All DB I/O via
`hass.async_add_executor_job`.

**1. SQLite store module** (`session_store.py`)
   - Open/create `.storage/comed_ev_sessions.db`; `PRAGMA user_version` schema
     migration; create the two tables above.
   - CRUD: `insert_session`, `update_session_cost`, `list_sessions`,
     `get_last_settled_session`; `upsert_settled_prices`, `unsettled_days`.
   - Unit-test against a temp-file DB (no HA needed).

**2. Session boundary detection** (in the coordinator)
   - Track `charge_now` edges across `_async_update_data` cycles. On rising edge,
     record `started_utc`, `start_soc`, and the meters' current totals. On falling
     edge, insert a `session` row: energy from the **meter delta** over the run
     (`energy_source='meter'`), else the SOC formula (`'soc'`), sampling
     `_capacity_kwh()` and measured/constant efficiency at close.
   - Guard against restarts mid-session (persist the open-session marker; a session
     interrupted by a restart is closed on next start or dropped — decide in review).
   - Test: feed a scripted decision stream, assert one row with expected energy.

**3. Settled-cost backfill pass**
   - Periodic task (daily): for each `unsettled_days()`, call `get_dual(day)`, upsert
     `.actual` hours into `settled_price` (×100 → cents).
   - Recompute `settled_cost_cents` for any session whose hours are now all present in
     `settled_price` (reuse the `estimate_charge_cost` hour-bucketing, settled prices
     as the source); set `settled_complete=1`.
   - Test: seed sessions + a dual-feed fixture, run the pass, assert costs.

**4. Exposure**
   - Sensors `sensor.comed_ev_last_session_cost` and `_last_session_savings` from
     `get_last_settled_session`; attributes `energy_kwh`, `cents_per_kwh`,
     `started`/`ended`, `energy_source`. Savings = flat-rate baseline − settled cost.
   - Service `comed_ev.get_sessions` returning session rows (optional date range).
   - **New config:** a flat-rate ¢/kWh baseline for the savings comparison
     (`CONF_FLAT_RATE`) in the options flow; savings/last-session-savings are omitted
     when it is unset.

**5. Docs**
   - README entities table: add the two session sensors and the service.
   - Flip this note's status to *implemented* and link the README.
