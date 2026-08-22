# Design note: settled session-cost reporting (deferred)

Status: **design only — not implemented.** This records the intended shape so the
work can start cleanly later. Do not build it until explicitly scheduled.

## Goal

After a charge session ends, report its **actual cost** using ComEd's *settled*
prices — the final hourly prices ComEd publishes after the fact, which differ from
the live 5-minute and current-hour-average values the component decides on in real
time. Surface, per session:

- Energy delivered (kWh) and duration.
- Cost at settled prices (¢ and $).
- Effective ¢/kWh, and a comparison vs. a flat-rate baseline (savings).

## Why the rolling JSON history is not enough

The component already keeps a rolling ~30-day price window in HA `Store`
(`.storage/comed_ev.history`) for percentile threshold suggestions. That store is
deliberately **pruned** and holds **live** prices only. Session-cost reporting needs:

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
  settled_cost_cents REAL,      -- NULL until settled prices fill in
  settled_complete INTEGER      -- 0/1, all hours have a settled price
)

settled_price(
  hour_ending_utc TEXT PRIMARY KEY,
  price_cents REAL
)
```

Session energy is attributed to hour buckets; cost = Σ (bucket_kwh × settled_price).
A background pass backfills `settled_price` from the ComEd settled feed and recomputes
`settled_cost_cents` for sessions whose hours are now fully settled.

All DB I/O must run off the event loop (`hass.async_add_executor_job`).

## Session boundary detection

A "session" is a contiguous run of `charge_now == on`. Detect edges from the existing
decision stream (or the binary_sensor state), recording start/end SOC from the SOC
entity at each edge. Energy ≈ (end_soc − start_soc)/100 × capacity_kwh. If a real
energy meter entity is available, prefer its delta.

## Seasonal analysis (available now, no new code)

For season-over-season *price* trends the price sensors already carry
`state_class=measurement`, so HA **long-term statistics** aggregate them for free —
usable today via the Statistics card. Session-cost reporting above is the only piece
that needs the dedicated SQLite store.

## Open questions to resolve before building

- Settled-price feed endpoint/shape in `comed-hourly-pricing` (may need a new client
  method) and its publish latency.
- Retention/cap on `session` rows.
- Whether to expose sessions as HA entities/attributes or only via a service/report.
