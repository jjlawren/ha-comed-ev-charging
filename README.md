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
(25th / 90th percentile by default). In **Auto** mode (default) the thresholds track
that rolling suggestion, so no tuning is required and they adapt as prices shift. In
**Manual** mode you pin values — the setup form pre-fills them with the live suggestion
so the starting point is still data-driven.

## Entities

| Entity | Description |
| --- | --- |
| `binary_sensor.comed_ev_charge_now` | The automation trigger. Attributes: `reason`, `live_price`, `threshold`, and (deadline mode) `slack_hours`. |
| `sensor.comed_ev_current_price` | Live 5-minute price (¢/kWh). |
| `sensor.comed_ev_hourly_price` | Current-hour average price. |
| `sensor.comed_ev_charge_threshold` | Current `T(SOC)`; attributes: floor/ceiling, mode. |
| `sensor.comed_ev_projected_end_soc` | Deadline mode only: projected SOC at departure. |
| `sensor.comed_ev_energy_needed_to_target` | Wall energy (kWh) to reach target SOC (target−current, divided by efficiency). |
| `sensor.comed_ev_estimated_charge_cost` | Estimated cost ($) of the upcoming charge, priced over the cheapest forecast hours. Window is now→departure (deadline mode) or now→next 6 AM Central otherwise. Attributes: `energy_kwh`, `average_price`, `hours_used`. |
| `sensor.comed_ev_estimated_charge_average_price` | Estimated average $/kWh for that charge. |
| `sensor.comed_ev_suggested_floor` / `_ceiling` | Analytics recommendations (disabled by default). |
| `sensor.comed_ev_measured_efficiency` | Measured vehicle/wall ratio when energy meters are set (diagnostic). |

`reason` is one of `below_threshold`, `above_threshold`, `target_reached`, `must_charge`.

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
- **Energy meters** (optional): cumulative kWh sensors for energy delivered to the
  vehicle and energy drawn by the charger. When both are set, the ratio of their
  measured advance replaces the efficiency constant (it falls back to the constant
  until enough energy has accumulated). Older sessions decay at ~0.98/day
  (a ~34-day half-life) so the ratio tracks recent behavior and slow drift.
- Threshold mode: Auto or Manual.

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

## Options

Reconfigurable from the integration's **Configure** button: threshold mode and Manual
floor/ceiling, `min_soc` and `gamma` (curve steepness), analytics percentiles and
history window, and the poll interval.

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
