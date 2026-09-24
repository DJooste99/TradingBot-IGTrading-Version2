"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 2) => (n === null || n === undefined ? "–" : Number(n).toFixed(d));
const pnlClass = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "");
let state = { status: null, currency: "GBP" };

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    throw new Error(detail);
  }
  return body;
}

function table(rows, cols, opts = {}) {
  if (!rows.length) return `<p class="muted">${opts.empty || "Nothing yet."}</p>`;
  const head = cols.map((c) => `<th>${esc(c.label)}</th>`).join("");
  const body = rows.map((r, i) => {
    const cells = cols.map((c) => `<td class="${c.cls ? c.cls(r) : ""}">${c.html ? c.html(r) : esc(c.get ? c.get(r) : r[c.key])}</td>`).join("");
    const tr = `<tr class="${opts.detail ? "clickable" : ""}" data-i="${i}">${cells}</tr>`;
    return opts.detail ? tr + `<tr class="detail hidden" data-d="${i}"><td colspan="${cols.length}">${opts.detail(r)}</td></tr>` : tr;
  }).join("");
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function wireDetails(container) {
  container.querySelectorAll("tr.clickable").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      container.querySelector(`tr[data-d="${tr.dataset.i}"]`).classList.toggle("hidden");
    });
  });
}

// ---------------------------------------------------------------- tabs
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === `tab-${b.dataset.tab}`));
  loaders[b.dataset.tab]?.();
}));

// ---------------------------------------------------------------- header / status
async function loadStatus() {
  const s = await api("/api/status");
  state.status = s;
  state.currency = s.currency;
  const badge = $("#mode-badge");
  badge.textContent = s.mode.toUpperCase();
  badge.className = `badge ${s.mode}`;
  $("#mode-toggle").textContent = s.mode === "demo" ? "Switch to LIVE" : "Switch to demo";
  const eng = s.engine;
  $("#engine-state").textContent = `engine: ${eng.running ? "running" : "stopped"}${eng.last_cycle ? " · last cycle " + new Date(eng.last_cycle).toLocaleTimeString() : ""}`;
  $("#engine-toggle").textContent = eng.running ? "Stop engine" : "Start engine";
  const banner = $("#alert-banner");
  const msgs = [];
  if (s.unreviewed_bugs) msgs.push(`${s.unreviewed_bugs} unreviewed bug report(s) from the code monitor.`);
  if (eng.last_error) msgs.push(`Last engine error: ${eng.last_error}`);
  banner.textContent = msgs.join(" ");
  banner.classList.toggle("hidden", !msgs.length);
  const count = $("#bug-count");
  count.textContent = s.unreviewed_bugs;
  count.classList.toggle("hidden", !s.unreviewed_bugs);
  return s;
}

$("#engine-toggle").addEventListener("click", async () => {
  await api(`/api/engine/${state.status.engine.running ? "stop" : "start"}`, { method: "POST" });
  loadStatus();
});

// ---------------------------------------------------------------- overview
async function loadOverview() {
  const s = await loadStatus();
  const [sum, open] = await Promise.all([api(`/api/summary?mode=${s.mode}`), api(`/api/trades?mode=${s.mode}&status=open`)]);
  const c = state.currency;
  $("#summary-cards").innerHTML = [
    ["Total P/L", `${fmt(sum.total_pnl)} ${c}`, pnlClass(sum.total_pnl)],
    ["Win rate", sum.win_rate_pct === null ? "–" : `${sum.win_rate_pct}%`, ""],
    ["Closed trades", sum.closed_trades, ""],
    ["Open trades", sum.open_trades, ""],
  ].map(([l, v, cls]) => `<div class="card"><div class="label">${l} (${s.mode})</div><div class="value ${cls}">${esc(v)}</div></div>`).join("");

  const openEl = $("#open-positions");
  openEl.innerHTML = table(open, [
    { label: "Instrument", key: "instrument" }, { label: "Dir", key: "claude_final_direction" },
    { label: "Size", key: "size" }, { label: "Entry", get: (r) => fmt(r.entry_price, 5) },
    { label: "Stop", get: (r) => fmt(r.stop_loss_used?.stop_price, 5) },
    { label: "", html: (r) => `<button class="btn small" data-close="${r.id}">Close</button>` },
  ], { empty: "No open positions." });
  openEl.querySelectorAll("[data-close]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Close trade #${b.dataset.close} at market?`)) return;
    try { await api(`/api/trades/${b.dataset.close}/close`, { method: "POST" }); } catch (e) { alert(e.message); }
    loadOverview();
  }));

  const gate = s.calibration;
  $("#calibration").innerHTML = gate.checks.map((ch) => `<div class="check"><span class="pill ${ch.met ? "ok" : "no"}">${ch.met ? "met" : "not met"}</span>
    ${esc(ch.name)}: ${esc(ch.actual)} / ${esc(ch.required)}</div>`).join("") +
    `<p class="${gate.passed ? "pos" : "muted"}">${gate.passed ? "Gate cleared: live switch allowed." : gate.manual_override_allowed ? "Gate not cleared. Manual override is allowed, with warning." : "Gate not cleared. Live switch blocked."}</p>`;

  $("#by-strategy").innerHTML = table(sum.by_strategy, [
    { label: "Strategy", key: "strategy" }, { label: "Trades", key: "n" }, { label: "Wins", key: "wins" },
    { label: "Win rate", get: (r) => (r.win_rate_pct === null ? "–" : r.win_rate_pct + "%") },
    { label: `P/L (${c})`, get: (r) => fmt(r.pnl), cls: (r) => pnlClass(r.pnl) },
  ]);

  $("#ig-limits").innerHTML = renderLimits(s.engine.ig_limits);

  const inst = Object.entries(s.engine.instruments || {}).map(([epic, v]) => ({ epic, ...v }));
  $("#instruments").innerHTML = table(inst, [
    { label: "Epic", key: "epic" }, { label: "Class", key: "asset_class" }, { label: "Strategy", key: "strategy" },
    { label: "Last closed candle", get: (r) => (r.last_candle ? new Date(r.last_candle).toLocaleString() : "–") },
    { label: "Rules", get: (r) => (r.last_rules || []).map((x) => `${x.condition}${x.met ? "✓" : "✗"}`).join("  ") },
    { label: "Note", key: "note" },
  ], { empty: "Engine hasn't evaluated any instruments yet. Start the engine." });
}

const LIMIT_LABELS = { app_non_trading: "Non-trading requests (app)", account_non_trading: "Non-trading requests (account)", account_trading: "Trading requests (account)" };
function renderLimits(l) {
  if (!l) return `<p class="muted">Shown once the engine has connected to IG.</p>`;
  const rows = Object.entries(l.requests).map(([k, v]) => ({ name: LIMIT_LABELS[k] || k, ...v }));
  const h = l.historical_points;
  const projPct = Math.round((100 * l.projected_weekly_points) / h.total);
  let html = table(rows, [
    { label: "Budget", key: "name" }, { label: "Used (last 60s)", key: "used_last_minute" },
    { label: "Bot cap / min", key: "limit_enforced" }, { label: "IG limit / min", key: "ig_limit" },
    { label: "Paused", get: (r) => (r.blocked_for_s ? `${r.blocked_for_s}s` : "–") },
  ]);
  html += `<p><b>Historical price points:</b> ${h.remaining === null ? "unknown until the first price request" : `${h.remaining} of ${h.total} left`}${h.resets_in_h !== null ? `, resets in ${h.resets_in_h}h` : ""}
    <span class="muted">(${h.reserve} held in reserve for warm-ups)</span></p>
    <p class="${projPct > 80 ? "neg" : "muted"}">Projected routine use with current strategies: ~${l.projected_weekly_points} points/week (${projPct}% of allowance)${projPct > 80 ? " — reduce instruments or use longer timeframes" : ""}.</p>`;
  return html;
}

// ---------------------------------------------------------------- trades
function tradeDetail(r) {
  return `<b>Claude (${esc(r.claude_confidence || "n/a")}):</b> ${esc(r.claude_reasoning || "–")}<br>
    <b>News:</b> ${esc(r.news_summary || "–")}<br>
    <b>Status note:</b> ${esc(r.status_note || "–")}<br>
    <b>Post-trade review:</b> ${esc(r.claude_post_trade_note || "–")}<br>
    <b>Stop-loss used:</b><pre>${esc(JSON.stringify(r.stop_loss_used, null, 1))}</pre>
    <b>Indicators at decision time:</b><pre>${esc(JSON.stringify(r.indicators_used, null, 1))}</pre>`;
}

const tradeCols = () => [
  { label: "#", key: "id" }, { label: "Time", get: (r) => new Date(r.timestamp).toLocaleString() },
  { label: "Mode", key: "mode" }, { label: "Instrument", key: "instrument" }, { label: "Strategy", key: "strategy" },
  { label: "Signal", key: "indicator_suggested_direction" }, { label: "Claude", key: "claude_final_direction" },
  { label: "Conf.", key: "claude_confidence" }, { label: "Size", key: "size" },
  { label: "Entry", get: (r) => fmt(r.entry_price, 5) }, { label: "Exit", get: (r) => fmt(r.exit_price, 5) },
  { label: `P/L (${state.currency})`, get: (r) => fmt(r.profit_loss), cls: (r) => pnlClass(r.profit_loss) },
  { label: "Status", key: "status" },
];

async function loadTrades() {
  const q = new URLSearchParams({ limit: 500 });
  if ($("#f-mode").value) q.set("mode", $("#f-mode").value);
  if ($("#f-status").value) q.set("status", $("#f-status").value);
  const rows = await api(`/api/trades?${q}`);
  const el = $("#trades-table");
  el.innerHTML = table(rows, tradeCols(), { detail: tradeDetail, empty: "No trades logged yet." });
  wireDetails(el);
}
$("#f-refresh").addEventListener("click", loadTrades);
$("#f-mode").addEventListener("change", loadTrades);
$("#f-status").addEventListener("change", loadTrades);

// ---------------------------------------------------------------- decisions
async function loadDecisions() {
  const s = state.status || (await loadStatus());
  const [sum, skipped] = await Promise.all([api(`/api/summary?mode=${s.mode}`), api(`/api/decisions/skipped?mode=${s.mode}`)]);
  $("#decision-matrix").innerHTML = table(sum.decision_matrix, [
    { label: "Strategy suggested", get: (r) => r.s ?? "–" }, { label: "Claude final", key: "c" }, { label: "Count", key: "n" },
  ]);
  const el = $("#skipped-table");
  el.innerHTML = table(skipped, [
    { label: "#", key: "id" }, { label: "Time", get: (r) => new Date(r.timestamp).toLocaleString() },
    { label: "Instrument", key: "instrument" }, { label: "Strategy", key: "strategy" },
    { label: "Suggested", key: "indicator_suggested_direction" }, { label: "Conf.", key: "claude_confidence" },
    { label: "Price then", get: (r) => fmt(r.indicators_used?.last_close, 5) },
  ], { detail: tradeDetail, empty: "Claude hasn't skipped any signals yet." });
  wireDetails(el);
}

// ---------------------------------------------------------------- reviews
async function loadReviews() {
  const rows = await api("/api/reviews");
  $("#reviews-list").innerHTML = rows.length ? rows.map((r) => `<div class="panel">
      <h2>${new Date(r.timestamp).toLocaleString()} · ${esc(r.mode || "")} · ${r.trades_reviewed} trades</h2>
      <p class="muted">${esc(r.period_start)} → ${esc(r.period_end)}</p>
      <p>${esc(r.summary)}</p>
      ${(r.flagged_issues || []).length ? `<b>Flagged issues</b><ul>${r.flagged_issues.map((i) => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}
    </div>`).join("") : `<p class="muted">No periodic reviews yet.</p>`;
}
$("#run-review").addEventListener("click", async (e) => {
  e.target.disabled = true;
  e.target.textContent = "Reviewing…";
  try { await api("/api/reviews/run", { method: "POST" }); } catch (err) { alert(err.message); }
  e.target.disabled = false;
  e.target.textContent = "Run periodic review now";
  loadReviews();
});

// ---------------------------------------------------------------- bugs
async function loadBugs() {
  const st = $("#b-status").value;
  const rows = await api(`/api/bugs${st ? "?status=" + st : ""}`);
  const el = $("#bugs-list");
  el.innerHTML = rows.length ? rows.map((r) => `<div class="panel">
      <h2>#${r.id} · ${esc(r.trigger_type)} · ${new Date(r.timestamp).toLocaleString()}</h2>
      <p>${esc(r.error_summary)}</p>
      <b>Claude's diagnosis</b><p>${esc(r.claude_diagnosis || "pending…")}</p>
      <b>Suggested fix</b><pre>${esc(r.claude_suggested_fix || "pending…")}</pre>
      ${r.stack_trace ? `<details><summary>Stack trace</summary><pre>${esc(r.stack_trace)}</pre></details>` : ""}
      <div class="filters">Status:
        <select data-bug="${r.id}">${["unreviewed", "acknowledged", "fixed", "dismissed"].map((s) => `<option ${s === r.status ? "selected" : ""}>${s}</option>`).join("")}</select>
      </div>
    </div>`).join("") : `<p class="muted">No bug reports.</p>`;
  el.querySelectorAll("[data-bug]").forEach((sel) => sel.addEventListener("change", async () => {
    await api(`/api/bugs/${sel.dataset.bug}`, { method: "PATCH", body: JSON.stringify({ status: sel.value }) });
    loadStatus();
  }));
}
$("#b-status").addEventListener("change", loadBugs);

// ---------------------------------------------------------------- config
const CLASSES = ["shares", "indices", "forex", "commodities"];
async function loadConfig() {
  const assets = await api("/api/config/asset");
  $("#asset-toggles").innerHTML = CLASSES.map((c) => `<label><input type="checkbox" value="${c}" ${assets.enabled_classes.includes(c) ? "checked" : ""}>
    ${c} <span class="muted">(${(assets.instruments[c] || []).length} instruments)</span></label>`).join("");
  $("#asset-toggles").querySelectorAll("input").forEach((cb) => cb.addEventListener("change", async () => {
    const enabled = [...$("#asset-toggles").querySelectorAll("input:checked")].map((x) => x.value);
    try { await api("/api/assets/enabled", { method: "PUT", body: JSON.stringify({ enabled_classes: enabled }) }); }
    catch (e) { alert(e.message); }
  }));
  loadConfigFile();
}
async function loadConfigFile() {
  const name = $("#cfg-name").value;
  $("#cfg-editor").value = JSON.stringify(await api(`/api/config/${name}`), null, 2);
  $("#cfg-editor").readOnly = name === "connection";
  $("#cfg-save").disabled = name === "connection";
  $("#cfg-msg").textContent = name === "connection" ? "Change mode via the header switch; credentials are env: references." : "";
}
$("#cfg-name").addEventListener("change", loadConfigFile);
$("#cfg-save").addEventListener("click", async () => {
  const msg = $("#cfg-msg");
  let data;
  try { data = JSON.parse($("#cfg-editor").value); } catch (e) { msg.textContent = "Invalid JSON: " + e.message; msg.className = "neg"; return; }
  try {
    await api(`/api/config/${$("#cfg-name").value}`, { method: "PUT", body: JSON.stringify(data) });
    msg.textContent = "Saved. The engine picks it up on its next cycle."; msg.className = "pos";
  } catch (e) { msg.textContent = e.message; msg.className = "neg"; }
});

// ---------------------------------------------------------------- mode switch modal
$("#mode-toggle").addEventListener("click", openModeModal);
$("#modal-cancel").addEventListener("click", () => $("#modal").classList.add("hidden"));

async function openModeModal() {
  const p = await api("/api/mode/preview");
  const target = p.current_mode === "demo" ? "live" : "demo";
  const gate = p.calibration;
  const r = p.risk;
  const mapping = Object.entries(p.strategies.asset_class_defaults).map(([cls, st]) => `${cls}: ${st ?? "none"}`).join(", ");
  const overrides = Object.entries(p.strategies.instrument_overrides || {}).map(([e, st]) => `${e} → ${st}`).join(", ") || "none";
  let body = `<p>Switching <b>${p.current_mode.toUpperCase()} → ${target.toUpperCase()}</b>. These settings carry over unchanged:</p>
    <table>
      <tr><th>Max total exposure</th><td>${fmt(r.max_total_exposure)} ${esc(r.currency)}</td></tr>
      <tr><th>Max per trade</th><td>${fmt(r.max_per_trade)} ${esc(r.currency)}</td></tr>
      <tr><th>Default stop-loss</th><td>${esc(JSON.stringify(r.default_stop_loss))}</td></tr>
      <tr><th>Take profit</th><td>${r.take_profit_pct ? r.take_profit_pct + "%" : "disabled"}</td></tr>
      <tr><th>Enabled classes</th><td>${esc(p.assets.enabled_classes.join(", "))}</td></tr>
      <tr><th>Class strategies</th><td>${esc(mapping)}</td></tr>
      <tr><th>Instrument overrides</th><td>${esc(overrides)}</td></tr>
      <tr><th>Strategies</th><td>${esc(Object.entries(p.strategies.strategies).map(([n, s]) => `${n} (${s.timeframe}, stop ${s.stop_loss ? s.stop_loss.mode : "default"})`).join("; "))}</td></tr>
      <tr><th>Claude decisions</th><td>${p.decision_engine.enabled ? `on for ${esc(p.decision_engine.run_on.join("/"))}, min confidence ${esc(p.decision_engine.min_confidence_to_trade)}` : "off"}</td></tr>
    </table>`;
  let canConfirm = true;
  if (target === "live") {
    body += `<h3>Calibration gate</h3>` + gate.checks.map((ch) => `<div class="check"><span class="pill ${ch.met ? "ok" : "no"}">${ch.met ? "met" : "not met"}</span> ${esc(ch.name)}: ${esc(ch.actual)} / ${esc(ch.required)}</div>`).join("");
    if (!gate.passed) {
      if (gate.manual_override_allowed) {
        body += `<div class="warning"><b>The demo track record has not cleared the calibration gate.</b> Forcing live now means real money
          is traded on an unproven configuration.<br><label><input type="checkbox" id="override-cb"> I understand and want to force the switch.</label></div>`;
        canConfirm = false;
      } else {
        body += `<div class="warning">The calibration gate is not cleared and manual override is disabled. Live switch is blocked.</div>`;
        canConfirm = false;
      }
    }
    body += `<p><b>Real money will be traded with the settings above.</b></p>`;
  }
  $("#modal-title").textContent = `Switch to ${target.toUpperCase()}`;
  $("#modal-body").innerHTML = body;
  const btn = $("#modal-confirm");
  btn.disabled = !canConfirm;
  btn.className = `btn ${target === "live" ? "danger" : ""}`;
  $("#override-cb")?.addEventListener("change", (e) => { btn.disabled = !e.target.checked; });
  btn.onclick = async () => {
    try {
      await api("/api/mode", { method: "POST", body: JSON.stringify({ mode: target, confirm: true, override: !!$("#override-cb")?.checked }) });
      $("#modal").classList.add("hidden");
      loadOverview();
    } catch (e) { alert(e.message); }
  };
  $("#modal").classList.remove("hidden");
}

// ---------------------------------------------------------------- boot
const loaders = { overview: loadOverview, trades: loadTrades, decisions: loadDecisions, reviews: loadReviews, bugs: loadBugs, config: loadConfig };
loadOverview().catch((e) => { $("#alert-banner").textContent = e.message; $("#alert-banner").classList.remove("hidden"); });
setInterval(() => {
  const active = document.querySelector(".tabs button.active").dataset.tab;
  if (active === "overview") loadOverview().catch(() => {});
  else loadStatus().catch(() => {});
}, 15000);
