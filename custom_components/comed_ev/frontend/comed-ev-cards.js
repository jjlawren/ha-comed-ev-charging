/**
 * ComEd EV Charging — bundled Lovelace cards.
 *
 *   type: custom:comed-ev-schedule-card   — forward charge schedule
 *   type: custom:comed-ev-history-card     — settled charge history
 *
 * Plain web components, no build step. The schedule card reads the
 * `charge_schedule` sensor's `hours[]` attribute; the history card calls the
 * `comed_ev.get_sessions` service and renders the response.
 */

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

/* ============================ Schedule card ============================ */

class ComEdScheduleCard extends HTMLElement {
  setConfig(config) {
    this._config = {
      entity: config.entity || "sensor.comed_ev_charging_charge_schedule",
      title: config.title || "Charge Schedule",
      ...config,
    };
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
    const st = hass.states[this._config.entity];

    if (!this._built) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${SCHEDULE_CSS}</style><ha-card></ha-card>`;
      this._card = this.shadowRoot.querySelector("ha-card");
      this._built = true;
    }
    if (!st) {
      this._card.innerHTML = `<div class="empty">Entity <code>${this._config.entity}</code> not found.</div>`;
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
          const rate = s.cents_per_kwh != null ? Number(s.cents_per_kwh).toFixed(1) : "—";
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
              <th>Session</th><th>kWh</th><th>Cost</th><th>¢/kWh</th><th>Saved</th><th>SOC</th>
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

/* ============================ Registration ============================ */

customElements.define("comed-ev-schedule-card", ComEdScheduleCard);
customElements.define("comed-ev-history-card", ComEdHistoryCard);

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
);

console.info("%c ComEd EV cards loaded", `color:${CHARGE}`);
