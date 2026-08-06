const $ = (sel) => document.querySelector(sel);
let activeTab = "dashboard";

async function api(path, options = {}) {
  const resp = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  if (resp.status === 401) {
    // Session cookie missing/expired - the browser won't auto-prompt for
    // this the way it did for HTTP Basic Auth, so send it to the login
    // page ourselves.
    window.location.href = "/login";
    throw new Error("not authenticated");
  }
  if (!resp.ok) {
    const text = await resp.text();
    alert(`Request failed (${resp.status}): ${text}`);
    throw new Error(text);
  }
  return resp.status === 204 ? null : resp.json();
}

function accountStatusHtml(a) {
  if (a.status === "connected") return `<span class="pill-bull">connected</span>`;
  if (a.status === "error") {
    return `<span class="pill-bear">error</span><div class="status-detail">${a.status_message || "unknown error"}</div>`;
  }
  return `<span class="pill-neutral">disconnected</span>${a.status_message ? `<div class="status-detail">${a.status_message}</div>` : ""}`;
}

function sizingHtml(a) {
  return a.sizing_mode === "fixed_amount" ? "Fixed ₹" : `${a.capital_per_trade_pct}% of funds`;
}

function capitalPerTradeHtml(a) {
  if (a.estimated_capital_per_trade == null) return "-";
  return "₹" + a.estimated_capital_per_trade.toLocaleString("en-IN", { maximumFractionDigits: 2 });
}

async function refreshAccounts() {
  if (activeTab !== "accounts") return;
  const accounts = await api("/api/accounts");
  const tbody = $("#accountsTable tbody");
  tbody.innerHTML = "";
  for (const a of accounts) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${a.label}</td>
      <td>${a.broker}</td>
      <td>${accountStatusHtml(a)}</td>
      <td>${a.available_funds != null ? "₹" + a.available_funds.toLocaleString("en-IN", {maximumFractionDigits: 2}) : "-"}</td>
      <td>${sizingHtml(a)}</td>
      <td>${capitalPerTradeHtml(a)}</td>
      <td>${a.trades_today} / ${a.max_trades_per_day}</td>
      <td>${a.enabled ? "yes" : "no"}</td>
      <td class="actions"></td>
    `;
    const actions = tr.querySelector(".actions");

    const connectBtn = document.createElement("button");
    connectBtn.className = "secondary";
    const needsAliceLogin = a.broker === "alice_blue" && a.status !== "connected";
    connectBtn.textContent = needsAliceLogin ? "Login with Alice Blue" : (a.status === "connected" ? "Reconnect" : "Connect");
    connectBtn.onclick = () => {
      if (needsAliceLogin) {
        openAliceBlueUrlDialog(a.id);
      } else {
        api(`/api/accounts/${a.id}/connect`, { method: "POST" }).then(refreshAccounts);
      }
    };
    actions.appendChild(connectBtn);

    if (a.status === "connected") {
      const fundsBtn = document.createElement("button");
      fundsBtn.className = "secondary";
      fundsBtn.textContent = "Refresh Funds";
      fundsBtn.onclick = () => api(`/api/accounts/${a.id}/funds`, { method: "POST" }).then(refreshAccounts);
      actions.appendChild(fundsBtn);
    }

    const editRiskBtn = document.createElement("button");
    editRiskBtn.className = "secondary";
    editRiskBtn.textContent = "Edit Risk";
    editRiskBtn.onclick = () => openEditRiskDialog(a);
    actions.appendChild(editRiskBtn);

    const removeBtn = document.createElement("button");
    removeBtn.className = "danger";
    removeBtn.textContent = "Remove";
    removeBtn.onclick = () => {
      if (!confirm(`Remove account "${a.label}"? This deletes its stored credentials.`)) return;
      api(`/api/accounts/${a.id}`, { method: "DELETE" }).then(refreshAccounts);
    };
    actions.appendChild(removeBtn);

    tbody.appendChild(tr);
  }
}

function updateSizingModeFields(selectEl, pctFieldEl, fixedFieldEl) {
  const isFixed = selectEl.value === "fixed_amount";
  pctFieldEl.classList.toggle("hidden", isFixed);
  fixedFieldEl.classList.toggle("hidden", !isFixed);
}

$("#addSizingModeSelect").onchange = () =>
  updateSizingModeFields($("#addSizingModeSelect"), $("#addCapitalPctField"), $("#addFixedAmountField"));
$("#editSizingModeSelect").onchange = () =>
  updateSizingModeFields($("#editSizingModeSelect"), $("#editCapitalPctField"), $("#editFixedAmountField"));

const editRiskDialog = $("#editRiskDialog");
let editRiskAccountId = null;
function openEditRiskDialog(a) {
  editRiskAccountId = a.id;
  $("#editRiskAccountLabel").textContent = `${a.label} (${a.broker}) - available funds: ${a.available_funds != null ? "₹" + a.available_funds : "unknown"}`;
  const form = $("#editRiskForm");
  form.elements.sizing_mode.value = a.sizing_mode;
  form.elements.capital_per_trade_pct.value = a.capital_per_trade_pct;
  form.elements.fixed_amount_per_trade.value = a.fixed_amount_per_trade;
  form.elements.max_trades_per_day.value = a.max_trades_per_day;
  updateSizingModeFields($("#editSizingModeSelect"), $("#editCapitalPctField"), $("#editFixedAmountField"));
  $("#editRiskPreview").textContent = a.estimated_capital_per_trade != null
    ? `~₹${a.estimated_capital_per_trade} will be used per trade at current funds.`
    : "";
  editRiskDialog.showModal();
}
$("#cancelEditRisk").onclick = () => editRiskDialog.close();
$("#editRiskForm").onsubmit = async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const payload = Object.fromEntries(form.entries());
  payload.capital_per_trade_pct = parseFloat(payload.capital_per_trade_pct);
  payload.fixed_amount_per_trade = parseFloat(payload.fixed_amount_per_trade);
  payload.max_trades_per_day = parseInt(payload.max_trades_per_day, 10);
  await api(`/api/accounts/${editRiskAccountId}`, { method: "PATCH", body: JSON.stringify(payload) });
  editRiskDialog.close();
  refreshAccounts();
};

function readBadge(value) {
  if (value == null) return `<span class="pill-neutral">-</span>`;
  const v = String(value).toLowerCase();
  const cls = v === "bull" || v === "trending" || v === "confirm" ? "pill-bull"
    : v === "bear" ? "pill-bear" : "pill-neutral";
  return `<span class="${cls}">${value}</span>`;
}

async function refreshEngineState() {
  const state = await api("/api/engine/state");
  $("#biasBadge").textContent = `bias ${state.bias || "-"}`;
  // Max trades/day is per-account now (see Accounts tab), so the header
  // just shows the day's total trade count, not a ratio against one shared cap.
  $("#dayBadge").textContent = `P&L ${state.realized_pnl_today} · ${state.trades_today} trades today`;
  $("#lastScanBadge").textContent = `last scan ${state.last_scan_ts ? new Date(state.last_scan_ts + "Z").toLocaleTimeString() : "-"}`;
  $("#targetBadge").classList.toggle("hidden", !state.target_hit);
  $("#runModeSelect").value = state.run_mode;
  $("#paperModeSelect").value = state.paper_mode ? "paper" : "live";
  const biasPill = $("#overallBiasPill");
  biasPill.textContent = state.bias || "no scan yet";
  biasPill.className = state.bias === "bullish" ? "pill-bull" : state.bias === "bearish" ? "pill-bear" : "pill-neutral";

  // Engine runs itself (auto-start, auto-scan, auto-confirm, auto-execute) -
  // the only manual override is the kill switch below.
  const statusBadge = $("#engineStatusBadge");
  const killBtn = $("#killSwitchBtn");
  if (state.active) {
    statusBadge.textContent = "engine running";
    statusBadge.className = "badge running";
    killBtn.textContent = "Kill Switch";
    killBtn.title = "Stop the engine and square off every open position immediately";
    killBtn.classList.add("danger");
    killBtn.classList.remove("secondary");
  } else {
    statusBadge.textContent = "engine KILLED";
    statusBadge.className = "badge killed";
    killBtn.textContent = "Resume Engine";
    killBtn.title = "Resume scanning / trading";
    killBtn.classList.remove("danger");
    killBtn.classList.add("secondary");
  }

  if (activeTab !== "dashboard") return;

  const indicators = await api("/api/engine/indicators");

  const wtbody = $("#watchlistTable tbody");
  wtbody.innerHTML = "";
  for (const w of state.watchlist || []) {
    const ind = indicators[w.symbol];
    const reads = ind ? ind.reads : {};
    const tr = document.createElement("tr");
    tr.classList.add("clickable");
    tr.innerHTML = `
      <td>${w.symbol}</td><td>${w.sector}</td><td>${w.direction}</td><td>${w.change_pct}%</td>
      <td>${readBadge(reads?.ema)}</td><td>${readBadge(reads?.vwap)}</td><td>${readBadge(reads?.supertrend)}</td>
      <td>${readBadge(reads?.rsi)}</td><td>${readBadge(reads?.adx)}</td><td>${readBadge(reads?.volume)}</td>
      <td>${ind?.aligned ? `<span class="pill-live">${ind.aligned}</span>` : "-"}</td>
    `;
    tr.onclick = () => openCandleDialog(w.symbol);
    wtbody.appendChild(tr);
  }
}

const arrowUp = "▲";
const arrowDown = "▼";

async function refreshScanDetail() {
  if (activeTab !== "dashboard") return;
  const detail = await api("/api/engine/scan-detail");

  const idxCards = $("#indexCards");
  idxCards.innerHTML = "";
  for (const [name, r] of Object.entries(detail.indices || {})) {
    if (r.bias === "unknown") {
      idxCards.insertAdjacentHTML("beforeend", `
        <div class="stat-card" title="${r.error || ''}">
          <span class="card-title">${name}</span>
          <span class="card-value">-</span>
          <span class="card-change">no data</span>
        </div>`);
      continue;
    }
    const bull = r.bias === "bullish";
    idxCards.insertAdjacentHTML("beforeend", `
      <div class="stat-card ${bull ? 'bull' : 'bear'}">
        <span class="card-title">${name}</span>
        <span class="card-value">${r.ltp}</span>
        <span class="card-change">${bull ? arrowUp : arrowDown} ${r.change_pct}%</span>
      </div>`);
  }

  const gList = $("#gainersList");
  gList.innerHTML = "";
  for (const g of detail.gainers || []) {
    const row = document.createElement("div");
    row.className = "mover-row";
    row.innerHTML = `
      <div><div class="mover-symbol">${g.symbol.replace('-EQ', '')}</div><div class="mover-sector">${g.sector}</div></div>
      <div class="mover-right"><div class="mover-change" style="color:var(--green)">${arrowUp} ${g.change_pct}%</div><div class="mover-ltp">₹${g.ltp}</div></div>
    `;
    row.onclick = () => openCandleDialog(g.symbol);
    gList.appendChild(row);
  }
  if (!(detail.gainers || []).length) gList.innerHTML = `<p class="hint">No scan yet - runs automatically at market open, or shortly after startup if started mid-day.</p>`;

  const lList = $("#losersList");
  lList.innerHTML = "";
  for (const l of detail.losers || []) {
    const row = document.createElement("div");
    row.className = "mover-row";
    row.innerHTML = `
      <div><div class="mover-symbol">${l.symbol.replace('-EQ', '')}</div><div class="mover-sector">${l.sector}</div></div>
      <div class="mover-right"><div class="mover-change" style="color:var(--danger)">${arrowDown} ${l.change_pct}%</div><div class="mover-ltp">₹${l.ltp}</div></div>
    `;
    row.onclick = () => openCandleDialog(l.symbol);
    lList.appendChild(row);
  }
  if (!(detail.losers || []).length) lList.innerHTML = `<p class="hint">No scan yet - runs automatically at market open, or shortly after startup if started mid-day.</p>`;

  const sBars = $("#sectorBars");
  sBars.innerHTML = "";
  const sectorEntries = Object.entries(detail.sector_moves || {}).sort((a, b) => b[1] - a[1]);
  const maxAbs = Math.max(1, ...sectorEntries.map(([, v]) => Math.abs(v)));
  for (const [sector, avg] of sectorEntries) {
    const bull = avg >= 0;
    const widthPct = Math.min(50, (Math.abs(avg) / maxAbs) * 50);
    sBars.insertAdjacentHTML("beforeend", `
      <div class="bar-row">
        <span>${sector}</span>
        <div class="bar-track">
          <div class="bar-fill ${bull ? 'bull' : 'bear'}" style="width:${widthPct}%"></div>
        </div>
        <span class="bar-value ${bull ? 'bull' : 'bear'}">${avg}%</span>
      </div>`);
  }
  if (!sectorEntries.length) sBars.innerHTML = `<p class="hint">No scan yet - runs automatically at market open, or shortly after startup if started mid-day.</p>`;
}

async function openCandleDialog(symbol) {
  $("#candleDialogTitle").textContent = `${symbol} - last 5-min candles`;
  const tbody = $("#candleTable tbody");
  tbody.innerHTML = `<tr><td colspan="6">Loading...</td></tr>`;
  $("#candleDialog").showModal();
  try {
    const candles = await api(`/api/engine/candles/${encodeURIComponent(symbol)}`);
    tbody.innerHTML = "";
    for (const c of candles.slice().reverse()) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${new Date(c.ts).toLocaleTimeString()}</td><td>${c.open}</td><td>${c.high}</td><td>${c.low}</td><td>${c.close}</td><td>${c.volume}</td>`;
      tbody.appendChild(tr);
    }
    if (!candles.length) tbody.innerHTML = `<tr><td colspan="6">No candle data yet</td></tr>`;
  } catch {
    tbody.innerHTML = `<tr><td colspan="6">Failed to load candles</td></tr>`;
  }
}
$("#closeCandleDialog").onclick = () => $("#candleDialog").close();

// --- Audio alerts ---
// Short synthesized tone (no external sound file) via the Web Audio API,
// plus a spoken alert for stop-losses via the Web Speech API. Alerts are ON
// by default (on by default) - the only thing a user gesture is
// needed for is unlocking the browser's audio output, which we grab
// silently on the very first click/keydown anywhere on the page (see
// bottom of this section), not via a dedicated "enable" button. The button
// itself is just a mute/unmute toggle the user can click any time.
let audioCtx = null;
let alertsEnabled = true;
let seenSignalIds = null;   // null until first fetch, so page-load doesn't alert for old signals
let seenClosedOrderIds = null;

function playTone(freq, durationMs) {
  if (!alertsEnabled) return;
  try {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (audioCtx.state === "suspended") audioCtx.resume();
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = "sine";
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + durationMs / 1000);
    osc.connect(gain).connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime + durationMs / 1000);
  } catch { /* audio not available - fail silently, alerts are a nice-to-have */ }
}

function speak(text) {
  if (!alertsEnabled || !window.speechSynthesis) return;
  try {
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
  } catch { /* ignore */ }
}

function checkSignalAlerts(signals) {
  const currentIds = new Set(signals.map(s => s.id));
  if (seenSignalIds === null) { seenSignalIds = currentIds; return; }
  for (const s of signals) {
    if (!seenSignalIds.has(s.id)) playTone(880, 180);
  }
  seenSignalIds = currentIds;
}

function checkOrderAlerts(orders) {
  const closedWithSL = orders.filter(o => o.status === "closed" && (o.note || "").toLowerCase().includes("stop loss"));
  const currentIds = new Set(closedWithSL.map(o => o.id));
  if (seenClosedOrderIds === null) { seenClosedOrderIds = currentIds; return; }
  for (const o of closedWithSL) {
    if (!seenClosedOrderIds.has(o.id)) {
      playTone(220, 350);
      speak(`Stop loss hit on ${o.symbol.replace("-EQ", "")}`);
    }
  }
  seenClosedOrderIds = currentIds;
}

// Silently unlock the browser's audio output on the very first user
// interaction with the page, whatever it is (click, keypress, tab switch) -
// alerts are on by default, so there should be no dedicated "turn sound on"
// step for the user to remember to do.
function unlockAudioOnce() {
  if (!audioCtx) {
    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    } catch { /* ignore */ }
  }
  if (audioCtx && audioCtx.state === "suspended") audioCtx.resume().catch(() => {});
  document.removeEventListener("click", unlockAudioOnce);
  document.removeEventListener("keydown", unlockAudioOnce);
}
document.addEventListener("click", unlockAudioOnce);
document.addEventListener("keydown", unlockAudioOnce);

$("#enableAlertsBtn").onclick = () => {
  alertsEnabled = !alertsEnabled;
  $("#enableAlertsBtn").textContent = alertsEnabled ? "Sound Alerts: On" : "Sound Alerts: Off (muted)";
  if (alertsEnabled) playTone(880, 120); // audible confirmation it's back on
};

async function refreshSignals() {
  const signals = await api("/api/engine/signals");
  checkSignalAlerts(signals);
  if (activeTab !== "dashboard") return;
  const tbody = $("#signalsTable tbody");
  tbody.innerHTML = "";
  for (const s of signals) {
    const tr = document.createElement("tr");
    const outcomeCls = s.outcome === "target_hit" ? "pill-bull" : s.outcome === "sl_hit" ? "pill-bear" : "pill-neutral";
    const pnlStr = s.outcome_pnl_pct != null ? `${s.outcome_pnl_pct > 0 ? "+" : ""}${s.outcome_pnl_pct}%` : "-";
    tr.innerHTML = `
      <td>${s.symbol}</td><td>${s.direction}</td>
      <td>${s.entry_price ?? "-"}</td><td>${s.target_price ?? "-"}</td><td>${s.stop_loss_price ?? "-"}</td>
      <td>${s.status}${s.executed ? "" : " (not executed)"}</td>
      <td><span class="${outcomeCls}">${s.outcome}</span></td>
      <td>${pnlStr}</td>
      <td class="actions"></td>
    `;
    const actions = tr.querySelector(".actions");
    if (s.status === "pending") {
      const approveBtn = document.createElement("button");
      approveBtn.textContent = "Approve";
      approveBtn.onclick = () => api(`/api/engine/signals/${s.id}/approve`, { method: "POST" }).then(() => { refreshSignals(); refreshOrders(); });
      const rejectBtn = document.createElement("button");
      rejectBtn.className = "secondary";
      rejectBtn.textContent = "Reject";
      rejectBtn.onclick = () => api(`/api/engine/signals/${s.id}/reject`, { method: "POST" }).then(refreshSignals);
      actions.append(approveBtn, rejectBtn);
    }
    tbody.appendChild(tr);
  }
}

async function refreshOrders() {
  const orders = await api("/api/engine/orders");
  checkOrderAlerts(orders);
  if (activeTab !== "orders") return;
  const tbody = $("#ordersTable tbody");
  tbody.innerHTML = "";
  for (const o of orders) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${o.account_id}</td><td>${o.symbol}</td><td>${o.side}</td><td>${o.quantity}</td>
      <td>${o.price}</td><td>${o.target_price ?? "-"}</td><td>${o.stop_loss ?? "-"}</td>
      <td><span class="${o.mode === 'live' ? 'pill-live' : 'pill-sandbox'}">${o.mode}</span></td>
      <td>${o.status}</td><td>${o.pnl}</td><td>${o.note || "-"}</td>
    `;
    tbody.appendChild(tr);
  }
}

// --- Error alerts: sound + visible banner, independent of which tab is open ---
// Every ERROR-level log line (broker disconnects, rate-limited/failed candle
// fetches, orders that couldn't be placed, unaffordable position sizing,
// square-off failures, ...) shows up here with its reason, not just in the
// Logs tab where it'd go unnoticed.
let lastErrorLineCount = null; // null until first fetch, so page-load doesn't alarm on old errors
let recentErrors = []; // most-recent-first, capped

function shortenErrorLine(line) {
  // Log lines are "TIMESTAMP LEVEL logger: message" - keep the readable tail
  // and cap broker JSON payloads so the banner stays scannable.
  const msg = line.replace(/^\S+ \S+ ERROR \S+:\s*/, "");
  return msg.length > 260 ? msg.slice(0, 260) + "…" : msg;
}

function renderErrorBanner() {
  const banner = $("#errorBanner");
  if (!recentErrors.length) {
    banner.classList.add("hidden");
    banner.innerHTML = "";
    return;
  }
  banner.classList.remove("hidden");
  banner.innerHTML = `
    <div class="error-banner-head">
      <strong>${recentErrors.length} issue${recentErrors.length > 1 ? "s" : ""} need attention</strong>
      <button id="dismissErrorsBtn" class="secondary">Dismiss</button>
    </div>
    <ul>${recentErrors.map(e => `<li>${e}</li>`).join("")}</ul>
  `;
  $("#dismissErrorsBtn").onclick = () => { recentErrors = []; renderErrorBanner(); };
}

async function pollErrorAlerts() {
  try {
    const lines = await api("/api/engine/logs?level=error&limit=500");
    if (lastErrorLineCount === null) {
      lastErrorLineCount = lines.length;
      return;
    }
    const freshLines = lines.length > lastErrorLineCount ? lines.slice(lastErrorLineCount - lines.length) : [];
    lastErrorLineCount = lines.length;
    if (!freshLines.length) return;
    for (const line of freshLines) {
      playTone(220, 250);
      recentErrors.unshift(shortenErrorLine(line));
    }
    recentErrors = recentErrors.slice(0, 8);
    renderErrorBanner();
    speak(freshLines.length === 1 ? "Strategy error - check the dashboard" : `${freshLines.length} strategy errors - check the dashboard`);
  } catch { /* transient poll failure - try again next tick */ }
}

// Shows scan progress (a full scan takes 2-5 minutes) regardless of what
// triggered it - the automatic startup catch-up scan, the daily scheduled
// scan, or a manual Scan Now click - since this polls the engine's own
// progress state rather than being tied to a button's click handler.
const SCAN_RING_CIRCUMFERENCE = 113.1;
async function pollScanProgress() {
  try {
    const p = await api("/api/engine/scan-progress");
    const banner = $("#scanProgressBanner");
    if (p.total > 0 && p.label !== "idle") {
      const pct = Math.round((p.current / p.total) * 100);
      $("#scanProgressRingFg").style.strokeDashoffset = SCAN_RING_CIRCUMFERENCE * (1 - pct / 100);
      $("#scanProgressLabel").textContent = p.label;
      $("#scanProgressPct").textContent = `${pct}% (${p.current}/${p.total})`;
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }
  } catch { /* transient poll failure - try again next tick */ }
}

async function refreshLogs() {
  if (activeTab !== "logs") return;
  const level = $("#logLevelSelect").value;
  const lines = await api(`/api/engine/logs?level=${level}&limit=300`);
  const view = $("#logView");
  const wasAtBottom = view.scrollTop + view.clientHeight >= view.scrollHeight - 10;
  view.innerHTML = lines.map(line => {
    const cls = / ERROR /.test(line) ? "log-line-error" : / WARNING /.test(line) ? "log-line-warning" : "";
    const escaped = line.replace(/&/g, "&amp;").replace(/</g, "&lt;");
    return cls ? `<span class="${cls}">${escaped}</span>` : escaped;
  }).join("\n") || "No log entries yet.";
  if (wasAtBottom) view.scrollTop = view.scrollHeight;
}
$("#logLevelSelect").onchange = refreshLogs;

async function loadDayDetail(day, container) {
  // History re-renders every ~2s while a row is open (see refreshHistory) -
  // only show the "Loading..." placeholder the first time, not on every
  // silent background refresh, or the content flickers blank repeatedly.
  if (!container.dataset.loadedOnce) container.innerHTML = `<p class="hint">Loading...</p>`;
  try {
    const [orders, signals] = await Promise.all([
      api(`/api/engine/orders?day=${day}`),
      api(`/api/engine/signals?day=${day}`),
    ]);
    container.innerHTML = "";
    container.dataset.loadedOnce = "1";

    const tradesHead = document.createElement("h4");
    tradesHead.textContent = `Trades (${orders.length})`;
    container.appendChild(tradesHead);
    if (!orders.length) {
      container.insertAdjacentHTML("beforeend", `<p class="hint">No real trades executed this day (paper or live).</p>`);
    } else {
      const tradesTable = document.createElement("table");
      tradesTable.innerHTML = `
        <thead><tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Exit Time</th><th>Mode</th><th>Status</th><th>P&amp;L</th><th>Note</th></tr></thead>
        <tbody>${orders.map(o => `
          <tr>
            <td>${new Date(o.ts + "Z").toLocaleTimeString()}</td>
            <td>${o.symbol}</td><td>${o.side}</td><td>${o.quantity}</td>
            <td>${o.price}</td><td>${o.exit_price ?? "-"}</td>
            <td>${o.exit_ts ? new Date(o.exit_ts + "Z").toLocaleTimeString() : "-"}</td>
            <td><span class="${o.mode === 'live' ? 'pill-live' : 'pill-sandbox'}">${o.mode}</span></td>
            <td>${o.status}</td>
            <td class="${o.pnl > 0 ? 'pill-bull' : o.pnl < 0 ? 'pill-bear' : ''}">${o.pnl}</td>
            <td>${o.note || "-"}</td>
          </tr>`).join("")}</tbody>
      `;
      container.appendChild(tradesTable);
    }

    const signalsHead = document.createElement("h4");
    signalsHead.textContent = `Signals fired (${signals.length}) - strategy review, including ones never executed`;
    signalsHead.style.marginTop = "1rem";
    container.appendChild(signalsHead);
    if (!signals.length) {
      container.insertAdjacentHTML("beforeend", `<p class="hint">No signals fired this day.</p>`);
      return;
    }
    const signalsTable = document.createElement("table");
    signalsTable.innerHTML = `
      <thead><tr><th>Symbol</th><th>Direction</th><th>Entry</th><th>Target</th><th>Stop Loss</th><th>Status</th><th>Outcome</th><th>Result %</th></tr></thead>
      <tbody>${signals.map(s => {
        const outcomeCls = s.outcome === "target_hit" ? "pill-bull" : s.outcome === "sl_hit" ? "pill-bear" : "pill-neutral";
        const pnlStr = s.outcome_pnl_pct != null ? `${s.outcome_pnl_pct > 0 ? "+" : ""}${s.outcome_pnl_pct}%` : "-";
        return `<tr>
          <td>${s.symbol}</td><td>${s.direction}</td>
          <td>${s.entry_price ?? "-"}</td><td>${s.target_price ?? "-"}</td><td>${s.stop_loss_price ?? "-"}</td>
          <td>${s.status}${s.executed ? "" : " (not executed)"}</td>
          <td><span class="${outcomeCls}">${s.outcome}</span></td>
          <td>${pnlStr}</td>
        </tr>`;
      }).join("")}</tbody>
    `;
    container.appendChild(signalsTable);
  } catch {
    container.innerHTML = `<p class="hint">Failed to load this day's detail.</p>`;
  }
}

// Track which day's detail row is expanded independent of the table's DOM,
// since the table rebuilds on every data refresh.
let expandedHistoryDay = null;
let _lastHistoryJson = null;

async function refreshHistory() {
  if (activeTab !== "history") return;
  const rows = await api("/api/engine/history");
  // Skip the rebuild entirely when the data hasn't changed, so an expanded
  // row isn't torn down and recreated (collapsed) on every poll.
  const json = JSON.stringify(rows);
  if (json === _lastHistoryJson) return;
  _lastHistoryJson = json;
  const tbody = $("#historyTable tbody");
  tbody.innerHTML = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.classList.add("clickable");
    tr.innerHTML = `
      <td>${r.trading_day}${r.is_today ? ' <span class="pill-live">today</span>' : ''}</td><td>${r.bias || "-"}</td>
      <td><span class="${r.paper_mode ? 'pill-sandbox' : 'pill-live'}">${r.paper_mode ? "paper" : "live"}</span></td>
      <td>${r.trades_taken}</td>
      <td class="${r.realized_pnl > 0 ? 'pill-bull' : r.realized_pnl < 0 ? 'pill-bear' : ''}">${r.realized_pnl}</td>
      <td>${r.signals_fired}</td><td>${r.signals_target_hit}</td><td>${r.signals_sl_hit}</td>
      <td>${r.target_hit ? "yes" : "no"}</td>
    `;
    const detailRow = document.createElement("tr");
    const detailCell = document.createElement("td");
    detailCell.colSpan = 9;
    const isOpen = expandedHistoryDay === r.trading_day;
    detailCell.className = "history-detail" + (isOpen ? "" : " hidden");
    detailRow.appendChild(detailCell);
    tr.onclick = () => {
      expandedHistoryDay = isOpen ? null : r.trading_day;
      refreshHistory();
    };
    tbody.appendChild(tr);
    tbody.appendChild(detailRow);
    if (isOpen) loadDayDetail(r.trading_day, detailCell);
  }
  if (!rows.length) tbody.innerHTML = `<tr><td colspan="9">No completed trading days yet.</td></tr>`;
}

function refreshAll() {
  refreshAccounts();
  refreshEngineState();
  refreshScanDetail();
  refreshSignals();
  refreshOrders();
  refreshLogs();
  refreshHistory();
}

$("#runModeSelect").onchange = (e) => api("/api/engine/mode", { method: "POST", body: JSON.stringify({ run_mode: e.target.value }) });
$("#paperModeSelect").onchange = (e) => {
  const goingLive = e.target.value === "live";
  if (goingLive && !confirm("Switch to LIVE trading? Every signal that fires from now on will place a REAL order with real money.")) {
    e.target.value = "paper";
    return;
  }
  api("/api/engine/paper-mode", { method: "POST", body: JSON.stringify({ paper_mode: !goingLive }) });
};
function withBusyState(btn, busyLabel, fn) {
  return async () => {
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = busyLabel;
    try {
      await fn();
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  };
}

// Single kill switch: engine otherwise runs itself end to end (auto-start,
// auto-scan, auto-confirm, auto-execute) with no other manual controls.
// Killing it stops the scheduler AND squares off every open position - a
// live-money emergency stop should not leave positions dangling open.
$("#killSwitchBtn").onclick = withBusyState($("#killSwitchBtn"), "Working...", async () => {
  const state = await api("/api/engine/state");
  if (state.active) {
    if (!confirm("KILL SWITCH: stop the engine and square off every open position immediately?")) return;
    await api("/api/engine/square-off", { method: "POST" });
    await api("/api/engine/stop", { method: "POST" });
  } else {
    if (!confirm("Resume the engine? Scanning / confirmation / auto-execution will restart.")) return;
    await api("/api/engine/start", { method: "POST" });
  }
  await refreshAll();
});

function updateAddAccountFormForBroker() {
  const isAlice = $("#brokerSelect").value === "alice_blue";
  $("#passwordField").style.display = isAlice ? "none" : "";
  $("#totpField").style.display = isAlice ? "none" : "";
  $("#secretField").style.display = isAlice ? "" : "none";
  $("#aliceBlueNote").style.display = isAlice ? "" : "none";
  $("#passwordField input").required = !isAlice;
}
$("#brokerSelect").onchange = updateAddAccountFormForBroker;

const dialog = $("#addAccountDialog");
$("#addAccountBtn").onclick = () => {
  updateAddAccountFormForBroker();
  updateSizingModeFields($("#addSizingModeSelect"), $("#addCapitalPctField"), $("#addFixedAmountField"));
  dialog.showModal();
};
$("#cancelAddAccount").onclick = () => dialog.close();
$("#addAccountForm").onsubmit = async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const payload = Object.fromEntries(form.entries());
  payload.capital_per_trade_pct = parseFloat(payload.capital_per_trade_pct);
  payload.fixed_amount_per_trade = parseFloat(payload.fixed_amount_per_trade);
  payload.max_trades_per_day = parseInt(payload.max_trades_per_day, 10);
  const account = await api("/api/accounts", { method: "POST", body: JSON.stringify(payload) });
  dialog.close();
  e.target.reset();
  if (payload.broker === "alice_blue") {
    // Alice Blue has no direct login - show the redirect URL rather than
    // silently navigating away, so the user can see/copy it (e.g. to open
    // on a different device/browser) before committing to leave this page.
    await openAliceBlueUrlDialog(account.id);
    refreshAccounts();
  } else {
    await api(`/api/accounts/${account.id}/connect`, { method: "POST" });
    refreshAccounts();
  }
};

async function openAliceBlueUrlDialog(accountId) {
  const { url } = await api(`/api/accounts/${accountId}/alice_blue/login-url`);
  $("#aliceBlueUrlInput").value = url;
  $("#openAliceBlueUrl").href = url;
  $("#aliceBlueUrlDialog").showModal();
}
$("#copyAliceBlueUrl").onclick = async () => {
  const input = $("#aliceBlueUrlInput");
  input.select();
  try {
    await navigator.clipboard.writeText(input.value);
    $("#copyAliceBlueUrl").textContent = "Copied!";
    setTimeout(() => { $("#copyAliceBlueUrl").textContent = "Copy Link"; }, 1500);
  } catch { /* clipboard permission denied - the text is still selected for manual copy */ }
};
$("#cancelAliceBlueUrl").onclick = () => $("#aliceBlueUrlDialog").close();

// --- Tabs ---
// Tab-gated refresh functions above check `activeTab` themselves and no-op
// when hidden, so switching tabs stops fetching data nobody's looking at
// (in particular the log tail, which is the heaviest read) without needing
// a separate polling loop per tab.
for (const btn of document.querySelectorAll(".tab-btn")) {
  btn.onclick = () => {
    activeTab = btn.dataset.tab;
    for (const b of document.querySelectorAll(".tab-btn")) b.classList.toggle("active", b === btn);
    for (const panel of document.querySelectorAll(".tab-panel")) panel.classList.toggle("active", panel.id === `tab-${activeTab}`);
    if (activeTab === "settings") loadSettings();
    refreshAll();
  };
}

// --- Settings tab ---
function updateTargetModeFields() {
  const fixedPct = $("#targetModeSelect").value === "fixed_pct";
  $("#targetPctField").classList.toggle("hidden", !fixedPct);
  $("#targetRField").classList.toggle("hidden", fixedPct);
}
$("#targetModeSelect").onchange = updateTargetModeFields;

function updateSlModeFields() {
  const mode = $("#slModeSelect").value;
  $("#slPctField").classList.toggle("hidden", mode !== "fixed_pct");
  $("#slPointsField").classList.toggle("hidden", mode !== "fixed_points");
}
$("#slModeSelect").onchange = updateSlModeFields;

const _SETTINGS_BOOL_FIELDS = ["use_ema", "use_vwap", "use_supertrend", "use_rsi", "use_adx", "use_volume", "webhook_enabled"];
async function loadSettings() {
  const settings = await api("/api/engine/settings");
  const form = $("#settingsForm");
  for (const [key, value] of Object.entries(settings)) {
    const field = form.elements.namedItem(key);
    if (!field) continue;
    if (field.type === "checkbox") field.checked = !!value;
    else field.value = value;
  }
  updateTargetModeFields();
  updateSlModeFields();
}
const _SETTINGS_STRING_FIELDS = new Set(["target_mode", "webhook_url", "sl_mode"]);
$("#settingsForm").onsubmit = async (e) => {
  e.preventDefault();
  const form = e.target;
  const data = new FormData(form);
  const payload = {};
  for (const [key, value] of data.entries()) {
    payload[key] = (key.includes("time") || _SETTINGS_STRING_FIELDS.has(key)) ? value : parseFloat(value);
  }
  // Unchecked checkboxes are omitted from FormData entirely - read each
  // directly so turning an indicator OFF actually gets sent as false,
  // not silently dropped from the payload.
  for (const name of _SETTINGS_BOOL_FIELDS) {
    payload[name] = form.elements.namedItem(name).checked;
  }
  if (["use_ema", "use_vwap", "use_supertrend", "use_rsi"].every(name => !payload[name])) {
    alert("At least one of EMA / VWAP / Supertrend / RSI must stay checked - a signal needs a directional indicator to agree on.");
    return;
  }
  await api("/api/engine/settings", { method: "PATCH", body: JSON.stringify(payload) });
  const note = $("#settingsSavedNote");
  note.classList.remove("hidden");
  setTimeout(() => note.classList.add("hidden"), 2500);
};

$("#refreshMasterDataBtn").onclick = withBusyState($("#refreshMasterDataBtn"), "Refreshing...", async () => {
  await api("/api/engine/master-data/refresh", { method: "POST" });
  const note = $("#masterDataSavedNote");
  note.classList.remove("hidden");
  setTimeout(() => note.classList.add("hidden"), 2500);
});

$("#sendTestWebhookBtn").onclick = withBusyState($("#sendTestWebhookBtn"), "Sending...", async () => {
  const result = $("#webhookTestResult");
  const url = $("#webhookUrlInput").value.trim();
  if (!url) { result.textContent = "Enter a webhook URL first."; return; }
  try {
    const resp = await fetch("/api/engine/webhook/test", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url }),
    });
    const data = await resp.json();
    result.textContent = resp.ok ? `Success: ${data.message}` : `Failed: ${data.detail}`;
    result.style.color = resp.ok ? "var(--green)" : "var(--red)";
  } catch (e) {
    result.textContent = `Failed: ${e}`;
    result.style.color = "var(--red)";
  }
});

refreshAll();
pollErrorAlerts();
pollScanProgress();
// These endpoints only read our own DB/in-memory state - they never touch
// the broker API - so polling every few seconds is safe and gives the
// "live" feel the dashboard needs without any extra rate-limit risk. Each
// refresh function checks activeTab itself and skips work for hidden tabs.
setInterval(refreshAll, 2000);
// Error alerts and scan progress poll independent of activeTab/refreshAll -
// both need to show up regardless of which tab is open, and scan progress
// specifically needs to reflect a scan that started on its own (the
// startup catch-up scan, or the daily scheduled one), not just one
// triggered from this page.
setInterval(pollErrorAlerts, 5000);
setInterval(pollScanProgress, 1000);

$("#scanNowBtn").onclick = withBusyState($("#scanNowBtn"), "Starting...", async () => {
  await api("/api/engine/scan-now", { method: "POST" });
  await refreshAll();
});
