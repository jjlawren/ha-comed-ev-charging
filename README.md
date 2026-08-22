# ComEd EV Charging

A Home Assistant custom component that shifts EV charging into the cheapest
[ComEd hourly-pricing](https://hourlypricing.comed.com/) hours while still reaching
a required State-of-Charge (SOC) by an optional departure time.

It is **hardware-agnostic**: the component *reads* SOC / target / rate from entities
you already have (e.g. a Rivian, Tesla, or Wallbox integration) and *publishes* a
`binary_sensor.comed_ev_charge_now` signal plus price and plan sensors. You wire the
charger to that signal with your own automation — the component never commands the
charger directly.

Built on the [`comed-hourly-pricing`](https://pypi.org/project/comed-hourly-pricing/) async library.

## How it decides

The primary trigger is a **SOC-driven price threshold**. Willingness-to-pay rises as
SOC falls: define a price floor (always charge below) and a realistic ceiling (max
worth paying near empty), and the component charges when the **live 5-minute price**
sits under a curve `T(SOC)` between them. A gentle `gamma > 1` curve keeps the
threshold near the floor across the high-SOC band and only ramps toward the ceiling
when the battery is genuinely low.

- **No departure needed** for the common case — SOC pressure vs. the live price decides.
- **Optional departure** adds an hourly feasibility check: if there is no longer enough
  time to reach the target, `charge_now` forces on (`must_charge`) regardless of price.
- A **live price spike** naturally suppresses charging, because the 5-minute price
  jumps above the threshold unless SOC pressure (or a deadline) justifies paying it.

### Auto vs. Manual thresholds

The component observes the ComEd price distribution and **suggests** the floor/ceiling
(25th / 90th percentile by default). The **Automatic thresholds** switch (on by default)
tracks that rolling suggestion, so no tuning is required and it adapts as prices shift.
Turn the switch off to pin your own values with the **Price floor** / **Price ceiling**
number entities.

## Entities

| Entity | Description |
| --- | --- |
| `binary_sensor.comed_ev_charge_now` | The automation trigger. Attributes: `reason`, `live_price`, `threshold`, and (deadline mode) `slack_hours`. |
| `sensor.comed_ev_current_price` | Live 5-minute price (¢/kWh). |
| `sensor.comed_ev_hourly_price` | Current-hour average price. |
| `sensor.comed_ev_charge_threshold` | Current `T(SOC)`; attributes: floor/ceiling, mode. |
| `sensor.comed_ev_projected_end_soc` | Deadline mode only: projected SOC at departure. |
| `sensor.comed_ev_energy_needed_to_target` | Wall energy (kWh) to reach target SOC (target−current, divided by efficiency). |
| `sensor.comed_ev_estimated_charge_cost` | Estimated cost ($) of the upcoming charge, priced over the cheapest forecast hours plus the fixed distribution rate. Window is now→departure (deadline mode) or now→next 6 AM Central otherwise. Attributes: `energy_kwh`, `average_price` (supply-only $/kWh), `supply_cost`, `distribution_cost`, `hours_used`. |
| `sensor.comed_ev_estimated_charge_average_price` | Estimated supply-only average $/kWh for that charge (excludes the distribution rate). |
| `sensor.comed_ev_suggested_floor` / `_ceiling` | Analytics recommendations (disabled by default). |
| `sensor.comed_ev_measured_efficiency` | Measured vehicle/wall ratio when energy meters are set (diagnostic). |
| `sensor.comed_ev_last_session_cost` | Actual cost ($) of the last charge session: ComEd settled *supply* prices plus the fixed distribution rate. Attributes: `energy_kwh`, `cents_per_kwh` (effective), `supply_cost`, `distribution_cost`, `energy_source` (`meter`/`soc`), `started`, `ended`. |
| `sensor.comed_ev_last_session_savings` | Savings ($) of that session vs. the flat-rate baseline. Only present when a flat rate is set in options. |

`reason` is one of `below_threshold`, `above_threshold`, `target_reached`, `must_charge`.

## Session cost reporting

Each charge session (a contiguous `charge_now == on` run) is recorded to an
integration-owned SQLite store. Energy comes from the EVSE (wall) meter when set,
otherwise from the SOC rise. A daily pass prices each session at ComEd's *settled*
hourly prices once they publish. The ComEd feed is **supply-only**, so set the fixed
**distribution rate** (¢/kWh) in the options to have it added to each session's actual
cost. Set a flat-rate baseline (¢/kWh) to also get per-session savings — savings
compares supply only, since distribution is billed the same on any plan.

The `comed_ev.get_sessions` service returns the recorded sessions (optional `start`/
`end` bounds) with energy, settled cost, ¢/kWh, and savings — use it for reports or
automations.

## Installation

### HACS (recommended)
1. Add this repository as a custom repository (category: Integration).
2. Install **ComEd EV Charging**, restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → ComEd EV Charging**.

### Manual
Copy `custom_components/comed_ev` into your `config/custom_components/` directory and
restart Home Assistant.

## Setup

You are asked for:
- Battery capacity — a constant kWh or an entity — and charging efficiency.
- **Current-SOC** entity (required) and **Target-SOC** entity (required).
- Charge rate — a constant kW or an entity.
- **Departure** entity (optional): `input_datetime`, `schedule`, or a datetime `sensor`.
  A departure **in the past counts as unset** — the integration reverts to the
  no-departure overnight window. Because an `input_datetime` cannot be cleared,
  this is how you turn a deadline off: set it to any past time. A time-only
  `input_datetime` (`has_date: false`) self-clears each day once its time passes.
- **Energy meters** (optional): cumulative kWh sensors for energy delivered to the
  vehicle and energy drawn by the charger. When both are set, the ratio of their
  measured advance replaces the efficiency constant (it falls back to the constant
  until enough energy has accumulated). Older sessions decay at ~0.98/day
  (a ~34-day half-life) so the ratio tracks recent behavior and slow drift.

Tuning knobs are not asked for at setup — they are live entities you adjust anytime
(see **Controls** below).

## Example automation

```yaml
automation:
  - alias: "EV: charge on ComEd cheap signal"
    trigger:
      - platform: state
        entity_id: binary_sensor.comed_ev_charge_now
    action:
      - choose:
          - conditions: "{{ is_state('binary_sensor.comed_ev_charge_now', 'on') }}"
            sequence:
              - service: switch.turn_on
                target: { entity_id: switch.ev_charger }
        default:
          - service: switch.turn_off
            target: { entity_id: switch.ev_charger }
```

## Controls

The tuning knobs are live entities under the device — adjust them from a dashboard,
automation, or voice; changes apply immediately (no reload) and survive restarts:

- **Automatic thresholds** (switch) — on tracks the analytics suggestion; off pins the
  manual floor/ceiling below.
- **Price floor** / **Price ceiling** (¢/kWh) — the pinned band used when the switch is
  off.
- **Urgency-full SOC** and **Curve steepness (gamma)** — shape the willingness-to-pay
  curve `T(SOC)`.
- **Floor percentile** / **Ceiling percentile** / **History window** — tune the analytics
  suggestion (recomputed on change).
- **Flat-rate baseline** (¢/kWh, 0 = disabled) and **Distribution charge** (¢/kWh) — used
  for session-cost reporting.

Only the **poll interval** remains under the integration's **Configure** button, since
changing it needs a reload.

## History & analysis

- **Rolling window** — the component keeps ~30 days of live prices in HA storage to
  drive the percentile threshold suggestions. On first setup it **backfills** this
  window from the ComEd 5-minute API in chunked date ranges, so Auto mode has a
  data-driven floor/ceiling immediately instead of waiting a month.
- **Seasonal price trends** — the price sensors carry `state_class=measurement`, so
  Home Assistant's **long-term statistics** aggregate them automatically (view them
  with the Statistics card). No extra setup.
- **Per-session settled cost** is planned but not yet built. See
  [`docs/session-cost-design.md`](docs/session-cost-design.md) for the design.

## Development

```bash
uv sync --group dev
uv run pytest        # pure optimizer/analytics tests + HA-harness tests
uv run ruff check custom_components tests
```

The pure decision logic lives in `optimizer.py` and `analytics.py` (no Home Assistant
imports) and carries the bulk of the test coverage.

## License

MIT
