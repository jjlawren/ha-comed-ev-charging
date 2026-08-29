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
 * `comed_ev.get_transitions`, and renders the response. That response is one
 * interleaved timeline of `kind: "edge"` charge on/off transitions and
 * `kind: "deferral"` reserve-gate hold spans.
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
function fmtTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
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
    this._transitions = null;
    this._error = null;
    this._loading = false;
    // Session ids the viewer has pinned open, kept across re-renders (hass
    // pushes rebuild the table but leave the expanded set alone).
    this._open = new Set();
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
      // Best-effort: pull the transition feed so each session row can expand to
      // its start/stop detail. A failure here leaves the sessions table intact;
      // the rows just won't have expandable detail.
      try {
        const tlimit = Math.min(500, Math.max(60, sessions.length * 4 + 30));
        const tres = await this._hass.callWS({
          type: "execute_script",
          sequence: [
            {
              service: "comed_ev.get_transitions",
              data: { limit: tlimit },
              response_variable: "_r",
            },
            { stop: "done", response_variable: "_r" },
          ],
        });
        this._transitions = tres?.response?.transitions ?? [];
      } catch (_e) {
        this._transitions = [];
      }
    } catch (err) {
      this._error = err?.message || String(err);
    } finally {
      this._loading = false;
      this._render();
    }
  }

  // Pin/unpin a session row's detail. Toggling the DOM directly (rather than a
  // full re-render) keeps the click responsive, and _open carries the state
  // through the next hass-driven re-render.
  _toggle(row) {
    const id = row.getAttribute("data-id");
    if (!id) return;
    const willOpen = !this._open.has(id);
    if (willOpen) this._open.add(id);
    else this._open.delete(id);
    row.classList.toggle("open", willOpen);
    const detail = this.shadowRoot.querySelector(
      `tr.detail[data-detail="${CSS.escape(id)}"]`,
    );
    if (detail) detail.classList.toggle("open", willOpen);
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._built) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `<style>${BASE_CSS}${HISTORY_CSS}${ACTIVITY_CSS}${DETAIL_CSS}</style><ha-card></ha-card>`;
      this._card = this.shadowRoot.querySelector("ha-card");
      this._built = true;
      this._card.addEventListener("click", (e) => {
        if (e.target.closest(".refresh")) {
          this._fetch();
          return;
        }
        const row = e.target.closest("tr.session");
        if (row) this._toggle(row);
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
      // Link each session to its start/stop edges in the transition feed so a
      // row can expand to the Activity detail. Split the feed by polarity once;
      // a shared price axis is scaled across every matched edge so gauges line
      // up between sessions the way they do inside the Activity card.
      const edges = (this._transitions || []).filter((t) => t.kind !== "deferral");
      const starts = edges.filter((e) => e.charging);
      const stops = edges.filter((e) => !e.charging);
      const matched = rows.map((s) => ({
        start: nearestEdge(starts, s.started),
        stop: nearestEdge(stops, s.ended),
      }));
      const gScale = gaugeScale(
        matched.flatMap((m) => [m.start, m.stop].filter(Boolean)),
      );

      const trs = rows
        .map((s, i) => {
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
          const m = matched[i];
          const hasDetail = !!(m.start || m.stop);
          const id = String(s.id);
          const open = this._open.has(id);
          const chev = hasDetail ? `<span class="chev"></span>` : "";
          const detailRow = hasDetail
            ? `
            <tr class="detail${open ? " open" : ""}" data-detail="${id}">
              <td colspan="6"><div class="detailwrap">${sessionDetail(m.start, m.stop, gScale)}</div></td>
            </tr>`
            : "";
          return `
            <tr class="${hasDetail ? "session" : ""}${open ? " open" : ""}" data-id="${id}">
              <td class="day">${chev}<span class="d">${fmtDay(s.started)}</span><span class="t">${fmtTime(s.started)}–${fmtTime(s.ended)}${status}</span></td>
              <td>${Number(s.energy_kwh).toFixed(1)}</td>
              <td>${cost}</td>
              <td>${rate}</td>
              ${save}
              <td class="soc">${soc}</td>
            </tr>${detailRow}`;
        })
        .join("");
      body = `
        <div class="twrap">
          <table>
            <thead><tr>
              <th>Session</th><th>kWh</th><th>Total<br>Cost</th><th>Supply<br>¢/kWh</th><th>Saved</th><th>SOC</th>
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

function gaugeReason(t) {
  return t.reason === "below_threshold" || t.reason === "above_threshold";
}

// Shared price axis for every gauge in a card, so a price sits at the same x on
// every row and landmarks (ON, T) line up. Scaled to the priciest drawn value
// across the gauge-bearing rows; min_ahead is not drawn, so it is left out of
// the scale. The axis adapts to the data (no fixed ceiling), so a card of
// near-threshold prices spreads across the width instead of bunching left; the
// floor drops below zero only when a price actually goes negative.
function gaugeScale(rows) {
  const vals = [];
  for (const t of rows) {
    if (!gaugeReason(t)) continue;
    for (const v of [t.decision_price, t.on_threshold, t.threshold]) {
      if (v != null) vals.push(v);
    }
    // The settled dot is drawn only on charge starts; keep it inside the axis.
    if (t.charging && t.settled_price != null) vals.push(t.settled_price);
  }
  return { lo: Math.min(0, ...vals) * 1.15, hi: Math.max(1, ...vals) * 1.15 };
}

// A price-vs-threshold gauge, only where the threshold governs (opportunistic
// on/off). Deadline edges omit it — the threshold is not consulted there.
// `scale` is the card-wide {lo, hi} axis from gaugeScale().
function txGauge(t, scale) {
  if (!gaugeReason(t)) return "";
  const span = scale.hi - scale.lo || 1;
  const pct = (v) => Math.max(0, Math.min(100, ((v - scale.lo) / span) * 100));
  const price = pct(t.decision_price);
  // A sub-zero price is a paid-to-charge hour — flag the dot and its value green.
  const neg = t.decision_price != null && t.decision_price < 0;
  const dotCls = neg ? "g-price neg" : "g-price";
  const zeroLine = scale.lo < 0 ? `<div class="g-zero" style="left:${pct(0)}%"></div>` : "";

  // On a charge start, once the session settles, show where the price ended up:
  // a second dot at the session's energy-weighted settled ¢/kWh (spanning every
  // hour it charged, not just the start hour), the triggering `decision_price`
  // dot greyed, and an arrow along the bar from the read to the settled value.
  const settled = t.charging && t.settled_price != null ? t.settled_price : null;
  const sPct = settled != null ? pct(settled) : null;
  const sNeg = settled != null && settled < 0;

  let visuals;
  if (t.reason === "below_threshold") {
    const bar = pct(t.on_threshold);
    const top = pct(t.threshold);
    visuals =
      `<div class="g-fill" style="left:0;width:${bar}%"></div>` +
      `<div class="g-band" style="left:${bar}%;width:${Math.max(0, top - bar)}%"></div>`;
  } else {
    const thr = t.threshold != null ? `<div class="g-thr" style="left:${pct(t.threshold)}%"></div>` : "";
    visuals = `${thr}<div class="g-fill" style="left:${price}%;right:0"></div>`;
  }

  // Label candidates, most important first. A start fires when price ≈ ON and a
  // stop when price ≈ T, so labels routinely coincide; keep the higher-priority
  // one and drop any lower-priority label within MIN_GAP% of it. Dropped values
  // still show in the pills below and their landmark stays drawn on the bar.
  const cand = [];
  const add = (v, text, cls, prio, opts) => {
    if (v != null) cand.push({ pos: pct(v), text, cls, prio, ...opts });
  };
  if (settled != null) {
    // The settled session value leads; the greyed read follows, dropped if they collide.
    add(settled, cents(settled), sNeg ? "g-tick val settled neg" : "g-tick val settled", 0);
    add(t.decision_price, cents(t.decision_price), "g-tick val muted", 0.5);
  } else {
    add(t.decision_price, cents(t.decision_price), neg ? "g-tick val neg" : "g-tick val", 0);
  }
  add(t.threshold, "T", "g-tick", 1);
  if (t.reason === "below_threshold") add(t.on_threshold, "ON", "g-tick", 2);
  if (scale.lo < 0) add(0, "0", "g-tick", 3);
  // Axis min/max, flush to the ends (positioned by CSS, not `left`).
  cand.push({ pos: 0, text: cents(scale.lo), cls: "g-tick g-end lo", prio: 4, flush: true });
  cand.push({ pos: 100, text: cents(scale.hi), cls: "g-tick g-end hi", prio: 4, flush: true });

  const MIN_GAP = 9;
  const kept = [];
  for (const c of [...cand].sort((a, b) => a.prio - b.prio)) {
    if (kept.every((k) => Math.abs(k.pos - c.pos) >= MIN_GAP)) kept.push(c);
  }
  const ticks = kept
    .map((c) =>
      c.flush
        ? `<span class="${c.cls}">${c.text}</span>`
        : `<span class="${c.cls}" style="left:${c.pos}%">${c.text}</span>`,
    )
    .join("");

  // Dots (and the arrow between them, when settled). The read dot greys out once
  // settled; the settled dot carries the price sign for its colour.
  let overlay = `<span class="${dotCls}${settled != null ? " muted" : ""}" style="left:${price}%"></span>`;
  if (settled != null) {
    const dir = sPct >= price ? "right" : "left";
    const lo = Math.min(price, sPct);
    const wide = Math.abs(sPct - price);
    overlay =
      `<div class="g-track" style="left:${lo}%;width:${wide}%"></div>` +
      `<div class="g-arrow ${dir}" style="left:${sPct}%"></div>` +
      overlay +
      `<span class="${sNeg ? "g-price neg" : "g-price"} settled" style="left:${sPct}%"></span>`;
  }

  return `<div class="gauge labeled">${visuals}${zeroLine}${overlay}</div>
    <div class="g-ticks">${ticks}</div>`;
}

// A deferral is a reserve-gate hold span (kind: "deferral"), not an edge — the
// optimizer would charge on price but held the start off for a cheaper hour.
function dfDuration(d) {
  const mins = Math.max(1, Math.round((new Date(d.ended) - new Date(d.ts)) / 60000));
  return mins >= 90 ? `${(mins / 60).toFixed(1)} h` : `${mins} min`;
}
function dfDescribe(d) {
  const tail =
    {
      below_threshold: "then charging began",
      above_threshold: "then the price rose past the threshold",
      target_reached: "then the target was reached",
    }[d.ended_reason] || "then the hold released";
  return `Reserved cheaper hours — held ${dfDuration(d)}, ${tail}.`;
}
function dfPills(d) {
  const out = [];
  const add = (k, v, cls) =>
    out.push(`<span class="op ${cls || ""}">${k} <b>${v}</b></span>`);
  add("price", cents(d.decision_price), "hot");
  if (d.min_ahead != null) add("waited for", cents(d.min_ahead), "cool");
  return out.join("");
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
      const gScale = gaugeScale(rows);
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
        if (t.kind === "deferral") {
          parts.push(`
            <div class="tx defer${last ? " last" : ""}">
              <div class="rail"><span class="marker"></span></div>
              <div class="body">
                <div class="l1">
                  <span class="time">${fmtClock(t.ts)}–${fmtClock(t.ended)}</span>
                  <span class="label">Charging deferred</span>
                  <span class="sp"></span>${mode}
                </div>
                <div class="why">${dfDescribe(t)}</div>
                <div class="ops">${dfPills(t)}</div>
              </div>
            </div>`);
          return;
        }
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
              ${txGauge(t, gScale)}
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
    const edgeCount = rows.filter((r) => r.kind !== "deferral").length;

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
              <div class="stat"><span class="k">Transitions</span><span class="v">${edgeCount}</span></div>
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
  .tx.defer .marker {
    background: var(--card-background-color, var(--ha-card-background, #fff));
    border: 2px dashed ${CHARGE};
  }
  .tx.defer .label { color: ${CHARGE}; }
  .tx.defer .body { opacity: .92; }
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
  .gauge.labeled { margin: 2px 0 0; }
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
  .g-thr {
    position: absolute; top: 0; bottom: 0; width: 1px;
    background: color-mix(in srgb, var(--secondary-text-color) 55%, transparent);
  }
  .g-zero {
    position: absolute; top: 0; bottom: 0; width: 1px;
    background: color-mix(in srgb, var(--secondary-text-color) 40%, transparent);
  }
  .g-ticks {
    position: relative; height: 13px; margin: 1px 0 7px;
    font-size: 9.5px; color: var(--secondary-text-color);
  }
  .g-tick {
    position: absolute; top: 3px; transform: translateX(-50%); white-space: nowrap;
    letter-spacing: .04em; text-transform: uppercase; font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .g-tick::before {
    content: ""; position: absolute; left: 50%; top: -3px; width: 1px; height: 3px;
    background: var(--divider-color); transform: translateX(-50%);
  }
  .g-tick.val { color: var(--primary-text-color); text-transform: none; letter-spacing: 0; }
  .tx.start .g-tick.val::before { background: ${CHARGE}; }
  .tx.stop .g-tick.val::before { background: ${WARM}; }
  .tx .g-price.neg { background: ${GOOD}; }
  .tx .g-tick.val.neg { color: ${GOOD}; }
  .tx .g-tick.val.neg::before { background: ${GOOD}; }
  /* Settled overlay: the greyed triggering read, and the arrow to the settled dot. */
  .tx .g-price.muted { background: var(--secondary-text-color); opacity: .55; z-index: 2; }
  .g-track {
    position: absolute; top: 50%; height: 1.5px; transform: translateY(-50%);
    background: var(--secondary-text-color); opacity: .5; z-index: 1;
  }
  .g-arrow {
    position: absolute; top: 50%; width: 0; height: 0; z-index: 3;
    border-top: 3px solid transparent; border-bottom: 3px solid transparent;
  }
  .g-arrow.right { transform: translate(calc(-100% - 5px), -50%); border-left: 5px solid var(--secondary-text-color); }
  .g-arrow.left { transform: translate(5px, -50%); border-right: 5px solid var(--secondary-text-color); }
  .g-tick.val.muted { color: var(--secondary-text-color); opacity: .7; }
  .tx .g-tick.val.muted::before { background: var(--divider-color); }
  .g-end {
    text-transform: none; letter-spacing: 0; font-weight: 500; opacity: .7;
  }
  .g-end::before { display: none; }
  .g-end.lo { left: 0; transform: none; }
  .g-end.hi { left: auto; right: 0; transform: none; }
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

/* ==================== History ⇄ Activity linking ==================== */

// Sessions record their bounds on the poll tick, while an edge can also fire on
// a settings republish, so a session boundary can lag its edge by up to a poll
// interval. Match the nearest edge of the right polarity within a tolerance
// rather than assume the timestamps are identical.
const EDGE_MATCH_TOL_MS = 15 * 60 * 1000;

function nearestEdge(edges, iso) {
  const target = new Date(iso).getTime();
  let best = null;
  let bestDelta = EDGE_MATCH_TOL_MS + 1;
  for (const e of edges) {
    const d = Math.abs(new Date(e.ts).getTime() - target);
    if (d < bestDelta) {
      bestDelta = d;
      best = e;
    }
  }
  return bestDelta <= EDGE_MATCH_TOL_MS ? best : null;
}

// One transition row rendered as a compact Activity block. Reuses the Activity
// card's describe/gauge/pill helpers and its `.tx.start`/`.tx.stop` styling, so
// the detail reads the same as the standalone feed — minus the timeline rail.
function edgeBlock(t, scale) {
  const mode =
    t.mode === "deadline"
      ? `<span class="mode deadline">Deadline</span>`
      : `<span class="mode">Opportunistic</span>`;
  return `
    <div class="edge tx ${t.charging ? "start" : "stop"}">
      <div class="body">
        <div class="l1">
          <span class="time">${fmtClock(t.ts)}</span>
          <span class="label">${t.charging ? "Charging started" : "Charging stopped"}</span>
          <span class="sp"></span>${mode}
        </div>
        <div class="why">${txDescribe(t)}</div>
        ${txGauge(t, scale)}
        <div class="ops">${txPills(t)}</div>
      </div>
    </div>`;
}

function sessionDetail(start, stop, scale) {
  const edges = [start, stop].filter(Boolean);
  if (!edges.length) {
    return `<div class="nodetail">No transition detail retained for this session.</div>`;
  }
  const held =
    start && start.lockout_held
      ? `<div class="held">${lockSvg} start delayed by a minimum-off lockout</div>`
      : "";
  return edges.map((e) => edgeBlock(e, scale)).join("") + held;
}

const DETAIL_CSS = `
  tr.session { cursor: pointer; }
  tr.session .chev {
    display: inline-block; width: 6px; height: 6px; margin-right: 7px;
    border-right: 1.5px solid var(--secondary-text-color);
    border-bottom: 1.5px solid var(--secondary-text-color);
    transform: rotate(-45deg); transition: transform .15s ease; vertical-align: middle;
  }
  tr.session.open .chev { transform: rotate(45deg); }
  @media (hover: hover) {
    tr.session:hover { background: var(--secondary-background-color); }
    tr.session:hover + tr.detail { display: table-row; }
  }
  tr.detail { display: none; }
  tr.detail.open { display: table-row; }
  tr.detail > td {
    text-align: left; white-space: normal; padding: 0 14px 10px;
    background: var(--secondary-background-color); border-bottom: 1px solid var(--divider-color);
  }
  .detailwrap { padding: 2px 0; }
  .detail .edge.tx { display: block; padding: 0; }
  .detail .edge .body { border-bottom: none; padding: 8px 0; }
  .detail .edge + .edge .body { border-top: 1px dashed var(--divider-color); }
  .detail .held { padding: 2px 0 4px; }
  .detail .nodetail {
    padding: 10px 2px; font-size: 12.5px; color: var(--secondary-text-color);
  }
`;

/* ============================ Registration ============================ */

// Guard each define: if the module ever evaluates twice in one document, a
// bare define() throws on the first duplicate and aborts the rest of the file,
// leaving later cards unregistered ("Custom element not found").
const define = (tag, cls) => {
  if (!customElements.get(tag)) customElements.define(tag, cls);
};
define("comed-ev-schedule-card", ComEdScheduleCard);
define("comed-ev-history-card", ComEdHistoryCard);
define("comed-ev-activity-card", ComEdActivityCard);

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
