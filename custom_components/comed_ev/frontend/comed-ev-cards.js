/**
 * ComEd EV Charging — bundled Lovelace cards.
 *
 *   type: custom:comed-ev-schedule-card   — forward charge schedule
 *   type: custom:comed-ev-history-card     — settled charge history
 *   type: custom:comed-ev-activity-card    — charge on/off transitions and why
 *
 * Plain web components, no build step. The schedule card reads the
 * `charge_schedule` sensor's `hours[]` attribute; the history card calls the
 * `comed_ev.get_sessions` service; the activity card calls
 * `comed_ev.get_transitions`, and renders the response.
 */

const DOMAIN = "comed_ev";
const CHARGE = "#0b93a6"; // electric teal — charging
const WARM = "#cf7a1c"; // amber — price / skipped
const GOOD = "#2b9968"; // green — savings

const BASE_CSS = `
  :host { display: block; }
  ha-card { overflow: hidden; }
  .head {
    display: flex; align-items: center; gap: 10px;
    padding: 16px 18px 12px;
  }
  .head .ttl {
    font-size: 16px; font-weight: 600; letter-spacing: -.01em;
    display: flex; align-items: center; gap: 8px;
    color: var(--primary-text-color);
  }
  .head .sp { flex: 1; }
  .bolt { width: 18px; height: 18px; color: ${CHARGE}; flex: none; }
  .chip {
    font-size: 11px; font-weight: 500; padding: 3px 9px; border-radius: 999px;
    background: var(--secondary-background-color); color: var(--secondary-text-color);
    white-space: nowrap; font-variant-numeric: tabular-nums;
  }
  .chip.accent { background: color-mix(in srgb, ${CHARGE} 16%, transparent); color: ${CHARGE}; }
  .foot {
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    padding: 12px 18px; border-top: 1px solid var(--divider-color);
    background: var(--secondary-background-color);
  }
  .stat { display: flex; flex-direction: column; gap: 1px; }
  .stat .k {
    font-size: 10px; letter-spacing: .05em; text-transform: uppercase;
    color: var(--secondary-text-color);
  }
  .stat .v {
    font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums;
    color: var(--primary-text-color);
  }
  .stat .v.good { color: ${GOOD}; }
  .stat .v.accent { color: ${CHARGE}; }
  .fsp { flex: 1; }
  .empty { padding: 24px 18px; color: var(--secondary-text-color); font-size: 14px; }
`;

const boltSvg = (cls) =>
  `<svg class="${cls}" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M13 2 4.5 13.2c-.4.5 0 1.3.7 1.3H11l-1 7.5c-.1.7.8 1.1 1.2.5L20 11.3c.4-.5 0-1.3-.7-1.3H13l1-8c.1-.7-.8-1.1-1.2-.5z"/></svg>`;

function fmtHour(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "numeric" });
}
function fmtDay(iso) {
  return new Date(iso).toLocaleDateString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
  });
}
function money(v) {
  return v == null ? "—" : `$${Number(v).toFixed(2)}`;
}
function cents(v) {
  return v == null ? "—" : `${Number(v).toFixed(1)}¢`;
}
function fmtClock(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function dayLabel(iso) {
  const d = new Date(iso);
  const now = new Date();
  const same = (a, b) => a.toDateString() === b.toDateString();
  if (same(d, now)) return "Today";
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (same(d, y)) return "Yesterday";
  return fmtDay(iso);
}
function relTime(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

// Find this integration's entity by its unique_id suffix rather than trusting a
// fixed entity_id. A renamed entity, a slugified device name, or a second
// config entry all shift the entity_id; the unique_id suffix (see entity.py)
// and the translation_key stay stable. Falls back to the conventional id.
function findEntity(hass, suffix) {
  const registry = hass.entities || {};
  for (const [entityId, entry] of Object.entries(registry)) {
    if (entry.platform !== DOMAIN) continue;
    if (
      (entry.unique_id && entry.unique_id.endsWith(`_${suffix}`)) ||
      entry.translation_key === suffix ||
      entityId.endsWith(`_${suffix}`)
    ) {
      return entityId;
    }
  }
  const guess = `sensor.${DOMAIN}_charging_${suffix}`;
  return hass.states[guess] ? guess : null;
}

/* ============================ Schedule card ============================ */

class ComEdScheduleCard extends HTMLElement {
  setConfig(config) {
    // Leave `entity` unset unless the user pins one; it is auto-resolved from
    // the entity registry at render time (hass is not available here yet).
    this._config = { title: "Charge Schedule", ...config };
    this._built = false;
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    const hass = this._hass;
    if (!hass || !this._config) return;

    // Prefer a pinned entity; otherwise resolve once and cache only on success
    // so the card recovers if the entity registers after the first render.
    let entity = this._config.entity;
    if (!entity) {
      this._resolved = this._resolved || findEntity(hass, "charge_schedule");
      entity = this._resolved;
    }
    const st = entity ? hass.states[entity] : undefined;

    if (!this._built) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${SCHEDULE_CSS}</style><ha-card></ha-card>`;
      this._card = this.shadowRoot.querySelector("ha-card");
      this._built = true;
    }
    if (!st) {
      this._card.innerHTML = entity
        ? `<div class="empty">Entity <code>${entity}</code> not found.</div>`
        : `<div class="empty">No ComEd charge-schedule sensor found.</div>`;
      return;
    }

    const a = st.attributes || {};
    const hours = Array.isArray(a.hours) ? a.hours : [];
    const maxPrice = Math.max(0.01, ...hours.map((h) => h.price || 0));
    const deadline = a.mode === "deadline";

    const rowsHtml = hours
      .map((h) => {
        const w = Math.max(6, Math.round((h.price / maxPrice) * 100));
        const cls = h.charging ? "row charge" : "row skip";
        const bolt = h.charging ? boltSvg("mini-bolt") : "";
        const soc = h.projected_soc == null ? "" : `${Math.round(h.projected_soc)}%`;
        return `
          <div class="${cls}">
            <span class="time">${fmtHour(h.hour_ending)}</span>
            <div class="track"><div class="fill" style="width:${w}%"></div>${bolt}</div>
            <span class="price">${Number(h.price).toFixed(1)}</span>
            <span class="soc">${soc}</span>
          </div>`;
      })
      .join("");

    const modeChip = deadline
      ? `<span class="chip accent">Deadline</span>`
      : `<span class="chip">Opportunistic</span>`;
    const readyChip = a.ready_time
      ? `<span class="chip">ready ${fmtHour(a.ready_time)}</span>`
      : "";

    const energy = a.charging_energy_kwh;
    const cost = a.estimated_cost;

    this._card.innerHTML = `
      <div class="head">
        <span class="ttl">${boltSvg("bolt")}${this._config.title}</span>
        <span class="sp"></span>
        ${modeChip}${readyChip}
      </div>
      <div class="sched">
        ${rowsHtml || `<div class="empty">No forecast hours yet.</div>`}
      </div>
      <div class="foot">
        <div class="stat"><span class="k">Charging</span><span class="v accent">${st.state} hrs</span></div>
        ${energy != null ? `<div class="stat"><span class="k">Energy</span><span class="v">${Number(energy).toFixed(1)} kWh</span></div>` : ""}
        ${cost != null ? `<div class="stat"><span class="k">Est. cost</span><span class="v">${money(cost)}</span></div>` : ""}
        <div class="fsp"></div>
        ${a.ready_time ? `<div class="stat" style="text-align:right"><span class="k">Ready by</span><span class="v">${fmtHour(a.ready_time)}</span></div>` : ""}
      </div>`;
  }
}

const SCHEDULE_CSS = `
  .sched { padding: 6px 10px 8px; }
  .row {
    display: grid; grid-template-columns: 54px 1fr 42px 44px;
    align-items: center; gap: 10px; padding: 6px 8px; border-radius: 9px;
  }
  .row .time {
    font-size: 12.5px; font-weight: 500; color: var(--secondary-text-color);
    font-variant-numeric: tabular-nums;
  }
  .track {
    height: 20px; border-radius: 5px; position: relative; overflow: hidden;
    background: var(--secondary-background-color);
  }
  .fill { position: absolute; inset: 0 auto 0 0; border-radius: 4px; }
  .row .price {
    font-size: 13px; font-weight: 600; text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .row .soc {
    font-size: 12px; color: var(--secondary-text-color); text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .row.charge .fill { background: ${CHARGE}; }
  .row.charge .price { color: ${CHARGE}; }
  .row.skip .fill { background: color-mix(in srgb, ${WARM} 30%, transparent); }
  .row.skip .price { color: ${WARM}; }
  .mini-bolt {
    position: absolute; right: 5px; top: 50%; transform: translateY(-50%);
    width: 11px; height: 11px; color: #fff;
  }
`;

/* ============================ History card ============================ */

class ComEdHistoryCard extends HTMLElement {
  setConfig(config) {
    this._config = { title: config.title || "Recent Charges", days: config.days ?? 14, ...config };
    this._built = false;
    this._sessions = null;
    this._error = null;
    this._loading = false;
  }

  getCardSize() {
    return 6;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._fetch();
    else this._render();
  }

  async _fetch() {
    if (!this._hass || this._loading) return;
    this._loading = true;
    this._error = null;
    this._render();
    try {
      const data = {};
      if (this._config.days) {
        const since = new Date(Date.now() - this._config.days * 864e5);
        data.start = since.toISOString();
      }
      const res = await this._hass.callWS({
        type: "execute_script",
        sequence: [
          { service: "comed_ev.get_sessions", data, response_variable: "_r" },
          { stop: "done", response_variable: "_r" },
        ],
      });
      const sessions = res?.response?.sessions ?? [];
      // Newest first for display.
      this._sessions = [...sessions].reverse();
    } catch (err) {
      this._error = err?.message || String(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._built) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${HISTORY_CSS}</style><ha-card></ha-card>`;
      this._card = this.shadowRoot.querySelector("ha-card");
      this._built = true;
      this._card.addEventListener("click", (e) => {
        if (e.target.closest(".refresh")) this._fetch();
      });
    }

    const rows = this._sessions || [];
    let spent = 0;
    let saved = 0;
    for (const s of rows) {
      if (s.total_cost != null) spent += s.total_cost;
      if (s.savings != null) saved += s.savings;
    }

    let body;
    if (this._error) {
      body = `<div class="empty">Could not load sessions: ${this._error}</div>`;
    } else if (this._loading && !rows.length) {
      body = `<div class="empty">Loading…</div>`;
    } else if (!rows.length) {
      body = `<div class="empty">No charge sessions yet.</div>`;
    } else {
      const trs = rows
        .map((s) => {
          const soc =
            s.start_soc != null && s.end_soc != null
              ? `${Math.round(s.start_soc)}→${Math.round(s.end_soc)}`
              : "—";
          // Supply-only rate: distribution is billed the same on any plan, so
          // exclude it from ¢/kWh (it still lands in the Cost column). Derived
          // from supply_cost rather than the service's all-in cents_per_kwh.
          const rate =
            s.supply_cost != null && s.energy_kwh
              ? ((s.supply_cost * 100) / s.energy_kwh).toFixed(1)
              : "—";
          const cost = s.settled_complete ? money(s.total_cost) : `≈${money(s.total_cost)}`;
          const status = s.settled_complete
            ? ""
            : ` · <span class="pending"><span class="d"></span>settling</span>`;
          const save =
            s.savings != null
              ? `<td class="save">+${money(s.savings)}</td>`
              : `<td class="soc">—</td>`;
          return `
            <tr>
              <td class="day"><span class="d">${fmtDay(s.started)}</span><span class="t">${fmtHour(s.started)}–${fmtHour(s.ended)}${status}</span></td>
              <td>${Number(s.energy_kwh).toFixed(1)}</td>
              <td>${cost}</td>
              <td>${rate}</td>
              ${save}
              <td class="soc">${soc}</td>
            </tr>`;
        })
        .join("");
      body = `
        <div class="twrap">
          <table>
            <thead><tr>
              <th>Session</th><th>kWh</th><th>Cost</th><th>Supply<br>¢/kWh</th><th>Saved</th><th>SOC</th>
            </tr></thead>
            <tbody>${trs}</tbody>
          </table>
        </div>`;
    }

    this._card.innerHTML = `
      <div class="head">
        <span class="ttl">
          <svg class="bolt" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M7 14l4-4 3 3 5-6"/></svg>
          ${this._config.title}
        </span>
        <span class="sp"></span>
        <span class="chip">last ${this._config.days} days</span>
        <button class="refresh" title="Refresh" aria-label="Refresh">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v5h-5"/></svg>
        </button>
      </div>
      ${body}
      ${
        rows.length
          ? `<div class="foot">
              <div class="stat"><span class="k">Sessions</span><span class="v">${rows.length}</span></div>
              <div class="stat"><span class="k">Spent</span><span class="v">${money(spent)}</span></div>
              <div class="fsp"></div>
              ${saved ? `<div class="stat" style="text-align:right"><span class="k">Saved vs flat</span><span class="v good">+${money(saved)}</span></div>` : ""}
            </div>`
          : ""
      }`;
  }
}

const HISTORY_CSS = `
  .refresh {
    width: 30px; height: 30px; border-radius: 8px; cursor: pointer;
    border: 1px solid var(--divider-color); background: var(--secondary-background-color);
    color: var(--secondary-text-color); display: grid; place-items: center;
  }
  .refresh svg { width: 15px; height: 15px; }
  .twrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; min-width: 420px; }
  thead th {
    font-size: 10px; letter-spacing: .05em; text-transform: uppercase;
    color: var(--secondary-text-color); font-weight: 600; text-align: right;
    padding: 8px 14px; border-bottom: 1px solid var(--divider-color); white-space: nowrap;
  }
  thead th:first-child { text-align: left; }
  tbody td {
    padding: 10px 14px; border-bottom: 1px solid var(--divider-color);
    font-size: 13px; text-align: right; font-variant-numeric: tabular-nums;
    white-space: nowrap; color: var(--primary-text-color);
  }
  tbody tr:last-child td { border-bottom: none; }
  td.day { text-align: left; }
  td.day .d { font-weight: 600; }
  td.day .t { display: block; font-size: 11px; color: var(--secondary-text-color); }
  td.save { color: ${GOOD}; font-weight: 600; }
  td.soc { color: var(--secondary-text-color); }
  .pending { color: ${WARM}; }
  .pending .d {
    display: inline-block; width: 6px; height: 6px; border-radius: 50%;
    background: ${WARM}; margin-right: 3px;
  }
`;

/* ============================ Activity card ============================ */

function txDescribe(t) {
  switch (t.reason) {
    case "below_threshold":
      return "Price is under the ON bar.";
    case "above_threshold":
      return "Price rose over the threshold — released at once.";
    case "cheaper_later":
      return t.mode === "deadline"
        ? "Cheaper hours ahead — deferred."
        : "A cheaper hour was still ahead.";
    case "target_reached":
      return "Target charge reached.";
    case "must_charge":
      return "Deadline forced charging.";
    case "cheapest_hours":
      return "Reserved a cheapest hour.";
    default:
      return t.reason || "";
  }
}

function txPills(t) {
  const out = [];
  const add = (k, v, cls) =>
    out.push(`<span class="op ${cls || ""}">${k} <b>${v}</b></span>`);
  const price = cents(t.decision_price);
  const sig = (v) => Number(v).toFixed(1);
  switch (t.reason) {
    case "below_threshold":
      add("price", price, "cool");
      add("bar", cents(t.on_threshold));
      add("T", cents(t.threshold));
      if (t.volatility != null) add("σ", sig(t.volatility));
      if (t.deadband != null) add("δ", sig(t.deadband));
      break;
    case "above_threshold":
      add("price", price, "hot");
      add("T", cents(t.threshold));
      if (t.volatility != null) add("σ", sig(t.volatility));
      break;
    case "cheaper_later":
      add("price", price, t.mode === "deadline" ? "hot" : "");
      if (t.min_ahead != null) add("cheapest ahead", cents(t.min_ahead), "cool");
      if (t.slack_hours != null) add("slack", `${t.slack_hours} h`);
      break;
    case "cheapest_hours":
      add("price", price, "cool");
      if (t.hours_needed != null) add("need", `${t.hours_needed} h`);
      if (t.slack_hours != null) add("slack", `${t.slack_hours} h`);
      break;
    case "must_charge":
      add("price", price, "hot");
      if (t.slack_hours != null) add("slack", `${t.slack_hours} h`);
      break;
    case "target_reached":
      add("price", price, "cool");
      break;
    default:
      add("price", price);
  }
  return out.join("");
}

// A price-vs-threshold gauge, only where the threshold governs (opportunistic
// on/off). Deadline edges omit it — the threshold is not consulted there.
function txGauge(t) {
  if (t.reason !== "below_threshold" && t.reason !== "above_threshold") return "";
  const vals = [t.decision_price, t.threshold, t.on_threshold, t.min_ahead].filter(
    (v) => v != null,
  );
  const hi = Math.max(6, ...vals) * 1.15;
  const pct = (v) => Math.max(0, Math.min(100, (v / hi) * 100));
  const price = pct(t.decision_price);
  if (t.reason === "below_threshold") {
    const bar = pct(t.on_threshold);
    const top = pct(t.threshold);
    return `<div class="gauge">
        <div class="g-fill" style="left:0;width:${bar}%"></div>
        <div class="g-band" style="left:${bar}%;width:${Math.max(0, top - bar)}%"></div>
        <span class="g-price" style="left:${price}%"></span>
      </div>`;
  }
  return `<div class="gauge">
      <div class="g-fill" style="left:${price}%;right:0"></div>
      <span class="g-price" style="left:${price}%"></span>
    </div>`;
}

const lockSvg = `<svg class="lockicon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="10" width="16" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>`;

class ComEdActivityCard extends HTMLElement {
  setConfig(config) {
    this._config = {
      title: config.title || "Charge Activity",
      limit: config.limit ?? 25,
      ...config,
    };
    this._built = false;
    this._rows = null;
    this._error = null;
    this._loading = false;
  }

  getCardSize() {
    return 7;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._fetch();
    else this._render();
  }

  async _fetch() {
    if (!this._hass || this._loading) return;
    this._loading = true;
    this._error = null;
    this._render();
    try {
      const res = await this._hass.callWS({
        type: "execute_script",
        sequence: [
          {
            service: "comed_ev.get_transitions",
            data: { limit: this._config.limit },
            response_variable: "_r",
          },
          { stop: "done", response_variable: "_r" },
        ],
      });
      // The service already returns newest-first.
      this._rows = res?.response?.transitions ?? [];
    } catch (err) {
      this._error = err?.message || String(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._built) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${ACTIVITY_CSS}</style><ha-card></ha-card>`;
      this._card = this.shadowRoot.querySelector("ha-card");
      this._built = true;
      this._card.addEventListener("click", (e) => {
        if (e.target.closest(".refresh")) this._fetch();
      });
    }

    const rows = this._rows || [];

    let body;
    if (this._error) {
      body = `<div class="empty">Could not load transitions: ${this._error}</div>`;
    } else if (this._loading && !rows.length) {
      body = `<div class="empty">Loading…</div>`;
    } else if (!rows.length) {
      body = `<div class="empty">No charge transitions yet.</div>`;
    } else {
      const parts = [];
      let lastDay = null;
      rows.forEach((t, i) => {
        const dl = dayLabel(t.ts);
        if (dl !== lastDay) {
          parts.push(`<div class="daysep">${dl}</div>`);
          lastDay = dl;
        }
        const last = i === rows.length - 1;
        const mode = t.mode === "deadline"
          ? `<span class="mode deadline">Deadline</span>`
          : `<span class="mode">Opportunistic</span>`;
        parts.push(`
          <div class="tx ${t.charging ? "start" : "stop"}${last ? " last" : ""}">
            <div class="rail"><span class="marker"></span></div>
            <div class="body">
              <div class="l1">
                <span class="time">${fmtClock(t.ts)}</span>
                <span class="label">${t.charging ? "Charging started" : "Charging stopped"}</span>
                <span class="sp"></span>${mode}
              </div>
              <div class="why">${txDescribe(t)}</div>
              ${txGauge(t)}
              <div class="ops">${txPills(t)}</div>
            </div>
          </div>`);
        // A start the lockout held: annotate the gap back to its stop.
        if (t.charging && t.lockout_held) {
          const prev = rows[i + 1];
          if (prev && !prev.charging) {
            const mins = Math.max(
              1,
              Math.round((new Date(t.ts) - new Date(prev.ts)) / 60000),
            );
            parts.push(
              `<div class="held">${lockSvg} held ${mins} min — minimum-off lockout</div>`,
            );
          }
        }
      });
      body = `<div class="feed">${parts.join("")}</div>`;
    }

    const cyclesToday = rows.filter(
      (r) => r.charging && dayLabel(r.ts) === "Today",
    ).length;

    this._card.innerHTML = `
      <div class="head">
        <span class="ttl">${boltSvg("bolt")}${this._config.title}</span>
        <span class="sp"></span>
        <span class="chip">last ${this._config.limit}</span>
        <button class="refresh" title="Refresh" aria-label="Refresh">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v5h-5"/></svg>
        </button>
      </div>
      ${body}
      ${
        rows.length
          ? `<div class="foot">
              <div class="stat"><span class="k">Transitions</span><span class="v">${rows.length}</span></div>
              <div class="stat"><span class="k">Cycles today</span><span class="v accent">${cyclesToday}</span></div>
              <div class="fsp"></div>
              <div class="stat" style="text-align:right"><span class="k">Last change</span><span class="v">${relTime(rows[0].ts)}</span></div>
            </div>`
          : ""
      }`;
  }
}

const ACTIVITY_CSS = `
  .refresh {
    width: 30px; height: 30px; border-radius: 8px; cursor: pointer;
    border: 1px solid var(--divider-color); background: var(--secondary-background-color);
    color: var(--secondary-text-color); display: grid; place-items: center; flex: none;
  }
  .refresh svg { width: 15px; height: 15px; }
  .feed { padding: 2px 4px 6px; }
  .daysep {
    font-size: 10px; letter-spacing: .06em; text-transform: uppercase; font-weight: 600;
    color: var(--secondary-text-color); padding: 10px 14px 2px 46px;
  }
  .tx { display: grid; grid-template-columns: 44px 1fr; align-items: start; padding: 0 14px 0 0; }
  .rail { position: relative; align-self: stretch; }
  .rail::before {
    content: ""; position: absolute; left: 21px; top: 0; bottom: 0; width: 2px;
    background: var(--divider-color); transform: translateX(-50%);
  }
  .tx:first-of-type .rail::before { top: 16px; }
  .tx.last .rail::before { bottom: auto; height: 16px; }
  .marker {
    position: absolute; left: 21px; top: 13px; transform: translateX(-50%);
    width: 13px; height: 13px; border-radius: 50%; z-index: 1;
    box-shadow: 0 0 0 4px var(--card-background-color, var(--ha-card-background, #fff));
  }
  .tx.start .marker { background: ${CHARGE}; }
  .tx.stop .marker {
    background: var(--card-background-color, var(--ha-card-background, #fff));
    border: 2.5px solid ${WARM};
  }
  .body { padding: 7px 0 10px; min-width: 0; border-bottom: 1px solid var(--divider-color); }
  .tx.last .body { border-bottom: none; }
  .l1 { display: flex; align-items: center; gap: 8px; }
  .l1 .sp { flex: 1; }
  .time {
    font-size: 12px; color: var(--secondary-text-color); font-weight: 500; flex: none;
    font-variant-numeric: tabular-nums;
  }
  .label { font-size: 13.5px; font-weight: 600; letter-spacing: -.01em; }
  .tx.start .label { color: ${CHARGE}; }
  .tx.stop .label { color: ${WARM}; }
  .mode {
    font-size: 10px; font-weight: 600; letter-spacing: .03em; text-transform: uppercase;
    color: var(--secondary-text-color); padding: 2px 7px; border-radius: 5px;
    background: var(--secondary-background-color); white-space: nowrap;
  }
  .mode.deadline { color: ${CHARGE}; background: color-mix(in srgb, ${CHARGE} 14%, transparent); }
  .why { font-size: 12.5px; color: var(--primary-text-color); margin: 5px 0 7px; }
  .gauge {
    position: relative; height: 15px; border-radius: 5px;
    background: var(--secondary-background-color); margin: 2px 0 6px; overflow: hidden;
  }
  .g-fill { position: absolute; top: 0; bottom: 0; opacity: .16; }
  .tx.start .g-fill { background: ${CHARGE}; }
  .tx.stop .g-fill { background: ${WARM}; }
  .g-band {
    position: absolute; top: 0; bottom: 0;
    background: color-mix(in srgb, ${CHARGE} 14%, transparent);
    border-left: 1px dashed color-mix(in srgb, ${CHARGE} 55%, transparent);
    border-right: 1px solid color-mix(in srgb, var(--secondary-text-color) 55%, transparent);
  }
  .g-price {
    position: absolute; top: 50%; width: 11px; height: 11px; border-radius: 50%;
    transform: translate(-50%, -50%); z-index: 2;
    box-shadow: 0 0 0 2px var(--card-background-color, var(--ha-card-background, #fff));
  }
  .tx.start .g-price { background: ${CHARGE}; }
  .tx.stop .g-price { background: ${WARM}; }
  .ops { display: flex; flex-wrap: wrap; gap: 4px 6px; }
  .op {
    font-size: 10.5px; font-weight: 500; color: var(--secondary-text-color);
    background: var(--secondary-background-color); padding: 2px 6px; border-radius: 5px;
    white-space: nowrap; font-variant-numeric: tabular-nums;
  }
  .op b { color: var(--primary-text-color); font-weight: 600; }
  .op.hot b { color: ${WARM}; }
  .op.cool b { color: ${CHARGE}; }
  .held {
    display: flex; align-items: center; gap: 6px; font-style: italic;
    font-size: 11px; color: var(--secondary-text-color); padding: 0 14px 8px 52px;
  }
  .held .lockicon { width: 12px; height: 12px; flex: none; }
`;

/* ============================ Registration ============================ */

customElements.define("comed-ev-schedule-card", ComEdScheduleCard);
customElements.define("comed-ev-history-card", ComEdHistoryCard);
customElements.define("comed-ev-activity-card", ComEdActivityCard);

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "comed-ev-schedule-card",
    name: "ComEd EV — Charge Schedule",
    description: "Forward-looking charge schedule from the ComEd EV optimizer.",
  },
  {
    type: "comed-ev-history-card",
    name: "ComEd EV — Charge History",
    description: "Recent settled charge sessions and their cost.",
  },
  {
    type: "comed-ev-activity-card",
    name: "ComEd EV — Charge Activity",
    description: "Recent charge on/off transitions and why each fired.",
  },
);

console.info("%c ComEd EV cards loaded", `color:${CHARGE}`);
