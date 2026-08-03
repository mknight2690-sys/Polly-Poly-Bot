/* POLY ALERT DECK — front end */
"use strict";

let STATE = null;
let ws = null;
let wsRetry = 1000;
let _spokenAlertKeys = new Set();
let _voicesCache = [];
let _tradeToggleBusy = false;

const $ = (sel) => document.querySelector(sel);
const grid = $("#widget-grid");

const PERSONAS = [
  { id: "chrome_sentinel", name: "Chrome Sentinel", rate: 1.05, pitch: 0.9, sample: "Poly alert deck armed. Callouts on." },
  { id: "velvet_odds", name: "Velvet Odds", rate: 0.95, pitch: 1.1, sample: "Listening for the next edge." },
  { id: "pit_boss", name: "Pit Boss", rate: 1.15, pitch: 0.85, sample: "Markets hot. Stay sharp." },
];

const VOICE_KEY = "poly_voice_prefs_v1";

function loadVoicePrefs() {
  try {
    return JSON.parse(localStorage.getItem(VOICE_KEY) || "{}");
  } catch {
    return {};
  }
}

let _voicePrefs = Object.assign({ enabled: true, persona: "chrome_sentinel" }, loadVoicePrefs());

function saveVoicePrefs() {
  localStorage.setItem(VOICE_KEY, JSON.stringify(_voicePrefs));
  fetch("/api/dashboard", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ voice: _voicePrefs }),
  }).catch(() => {});
}

function currentPersona() {
  return PERSONAS.find((p) => p.id === _voicePrefs.persona) || PERSONAS[0];
}

function connectWS() {
  try {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onmessage = (ev) => {
      STATE = JSON.parse(ev.data);
      wsRetry = 1000;
      render();
      watchAlertsForVoice();
      // Autopilot owns takes — no auto-open Polymarket tabs
    };
    ws.onclose = () => {
      $("#live-dot").classList.add("dead");
      setTimeout(connectWS, wsRetry);
      wsRetry = Math.min(wsRetry * 2, 15000);
    };
    ws.onerror = () => ws.close();
  } catch (e) {
    setTimeout(connectWS, wsRetry);
  }
}

async function pollFallback() {
  try {
    const r = await fetch("/api/state");
    STATE = await r.json();
    render();
    watchAlertsForVoice();
  } catch (e) { /* restarting */ }
}

const fmt$ = (v) => {
  const n = Number(v);
  if (!Number.isFinite(n)) return "$0.00";
  return "$" + n.toFixed(2);
};
const cls$ = (v) => (Number(v) >= 0 ? "pos" : "neg");
const esc = (s) => String(s ?? "")
  .replace(/&/g, "&amp;")
  .replace(/</g, "&lt;")
  .replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

function polyTitle(row, max = 56) {
  return esc(String(row?.title || "").slice(0, max));
}

let _toastKeys = new Set();

function showBetToast(alert) {
  const key = `${alert.kind}:${alert.ts}:${alert.market_slug || alert.title}:${alert.side}`;
  if (_toastKeys.has(key)) return;
  _toastKeys.add(key);
  if (_toastKeys.size > 200) _toastKeys = new Set(Array.from(_toastKeys).slice(-100));

  const stack = $("#bet-toast-stack");
  if (!stack) return;
  while (stack.children.length) stack.removeChild(stack.firstChild);
  const el = document.createElement("div");
  el.className = "bet-toast announce";
  const mode = (alert.data && alert.data.exec_mode) || (STATE?.live?.exec_mode) || "paper";
  const pnl = alert.data && alert.data.pnl != null ? Number(alert.data.pnl) : null;
  const kind = String(alert.kind || "TAKE").toUpperCase();
  el.innerHTML = `
    <div class="bt-kind">${esc(kind)} · ${esc(alert.side || "")} · ${esc(mode)}</div>
    <div class="bt-title">${esc(alert.title || "")}</div>
    <div class="bt-meta">${esc(alert.reason || alert.detail || "")}
      ${alert.price != null ? ` · @ ${Number(alert.price).toFixed(3)}` : ""}
      ${pnl != null && Number.isFinite(pnl) ? ` · ${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}` : ""}</div>`;
  stack.appendChild(el);
  setTimeout(() => dismissToast(el), 8000);
}

function dismissToast(el) {
  if (!el || el.classList.contains("out")) return;
  el.classList.add("out");
  setTimeout(() => el.remove(), 260);
}

function watchAlertsForToasts() {
  if (!STATE) return;
  const tradingOn = !!STATE?.account?.trading_enabled;
  const rows = STATE.alerts || [];
  for (const a of rows.slice(0, 8).reverse()) {
    const kind = String(a.kind || "").toUpperCase();
    if (kind !== "TAKE" && kind !== "CLOSE") continue;
    // STOP: finish announcing open seats (CLOSE only) — no new TAKE noise
    if (!tradingOn && kind === "TAKE") continue;
    if (Date.now() / 1000 - Number(a.ts || 0) > 90 && _toastKeys.size === 0) {
      const key = `${a.kind}:${a.ts}:${a.market_slug || a.title}:${a.side}`;
      _toastKeys.add(key);
      continue;
    }
    showBetToast(a);
  }
}

function fmtAgo(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  return `${(s / 3600).toFixed(1)}h`;
}

function fmtLeft(sec) {
  const s = Number(sec);
  if (!Number.isFinite(s)) return "—";
  if (s < 0) return "ended";
  if (s < 60) return `${Math.round(s)}s left`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s left`;
}

function renderWalletHdr() {
  const el = $("#wallet-hdr");
  const val = $("#hdr-wallet");
  const st = $("#hdr-wallet-state");
  if (!el || !val || !st) return;
  const L = STATE.live || {};
  const bal = L.balance || {};
  const spending = !!L.spending;
  const usd = bal.balance_usd;
  // Only rewrite the number when LIVE+ARMED; otherwise keep last known (frozen)
  if (spending && usd != null && Number.isFinite(Number(usd))) {
    window._walletLiveUsd = Number(usd);
    val.textContent = fmt$(Number(usd));
  } else if (window._walletLiveUsd == null && usd != null && Number.isFinite(Number(usd))) {
    // Seed once from snapshot while disarmed / dry_run
    window._walletLiveUsd = Number(usd);
    val.textContent = fmt$(Number(usd));
  } else if (window._walletLiveUsd != null) {
    val.textContent = fmt$(window._walletLiveUsd);
  } else {
    val.textContent = usd != null ? fmt$(Number(usd)) : "$—";
  }
  el.classList.toggle("live", spending);
  el.classList.toggle("frozen", !spending);
  st.textContent = spending ? "LIVE" : "FROZEN";
}

function render() {
  if (!STATE) return;
  $("#live-dot").classList.remove("dead");
  const dash = STATE.dashboard || {};
  document.documentElement.style.setProperty("--accent", dash.accent || "#ff8a3d");
  $("#deck-title").textContent = dash.title || "POLY // ALERT DECK";
  document.title = dash.title || "POLY";

  const acct = STATE.account || {};
  const start = Number(acct.start_balance || 0) || 1;
  const eq = Number(acct.equity || 0);
  const totalPnl = acct.total_pnl != null
    ? Number(acct.total_pnl)
    : (eq - start);
  const roi = acct.roi_pct != null ? Number(acct.roi_pct) : ((eq - start) / start) * 100;
  $("#hdr-equity").textContent = fmt$(eq);
  $("#hdr-roi").textContent = (roi >= 0 ? "+" : "") + roi.toFixed(2) + "%";
  $("#hdr-roi").className = "hval " + cls$(totalPnl);
  $("#hdr-alerts").textContent = String(STATE.alerts_today || 0);
  const buildEl = $("#build-tag");
  if (buildEl) {
    const build = STATE.build || STATE.version?.build || "—";
    if (buildEl.textContent !== build) {
      buildEl.textContent = build;
      buildEl.classList.add("fresh");
      setTimeout(() => buildEl.classList.remove("fresh"), 1200);
    }
    buildEl.title = `Deck ${build} (#${STATE.build_num || STATE.version?.build_num || "?"}) — match chat tag`;
  }
  renderWalletHdr();
  const clock = STATE.clock || {};
  const syncEl = $("#hdr-sync");
  if (syncEl) {
    const age = clock.edge_age_sec;
    const ok = !!clock.poly_sync;
    const wins = Number(clock.short_windows || 0);
    let label = "…";
    let cls = "hval sync-warn";
    if (ok) {
      label = `LIVE ${age != null ? fmtAgo(age) : "now"} · ${wins}w`;
      cls = "hval sync-ok";
    } else if (age != null) {
      label = `STALE ${fmtAgo(age)}`;
      cls = age > 120 ? "hval sync-bad" : "hval sync-warn";
    } else {
      label = "WARMING";
      cls = "hval sync-warn";
    }
    syncEl.textContent = label;
    syncEl.className = cls;
  }
  const bankInput = $("#bankroll-input");
  if (bankInput && document.activeElement !== bankInput) {
    const startBal = Number(acct.start_balance || STATE.params?.starting_equity || 10);
    if (Number.isFinite(startBal) && startBal > 0) bankInput.value = String(Math.round(startBal * 100) / 100);
  }

  if (!_tradeToggleBusy) {
    renderTradeControl(!!acct.trading_enabled);
  }

  renderAutopilotControl();
  watchAlertsForToasts();
  syncVoiceUi();
  renderWidgets();
}

function ensureWidget(id, label) {
  let el = grid.querySelector(`.widget[data-id="${id}"]`);
  if (!el) {
    const tpl = document.getElementById("tpl-widget");
    el = tpl.content.firstElementChild.cloneNode(true);
    el.dataset.id = id;
    el.querySelector(".widget-title").textContent = label;
    grid.appendChild(el);
  }
  return el;
}

function renderWidgets() {
  const widgets = (STATE.dashboard && STATE.dashboard.widgets) || [];
  const spending = !!(STATE.account?.live || STATE.live?.spending);
  for (const w of widgets) {
    if (!w.visible) continue;
    const el = ensureWidget(w.id, w.label);
    if (w.id === "equity") {
      el.classList.toggle("live-spend", spending);
    }
    const body = el.querySelector(".widget-body");
    const extra = el.querySelector(".widget-extra");
    const fn = WIDGET_RENDERERS[w.id];
    if (fn) fn(body, extra);
  }
}

function renderAlerts(body, extra) {
  const rows = STATE.alerts || [];
  extra.textContent = `${rows.length} recent`;
  if (!rows.length) {
    body.innerHTML = `<div class="muted">Waiting for TAKE / SKIP / CLOSE alerts…</div>`;
    return;
  }
  body.innerHTML = `<table class="table"><thead><tr>
    <th>KIND</th><th>MARKET</th><th>SIDE</th><th>PX</th><th>REASON</th><th>WHEN</th>
  </tr></thead><tbody>
  ${rows.slice(0, 24).map((a) => {
    const kind = String(a.kind || "").toLowerCase();
    const ago = Math.max(0, (Date.now() / 1000) - Number(a.ts || 0));
    let reason = String(a.reason || a.detail || "");
    if (String(a.kind || "").toUpperCase() === "CLOSE") {
      const pnl = a.data && a.data.pnl != null ? Number(a.data.pnl) : NaN;
      if (Number.isFinite(pnl)) {
        const sign = pnl >= 0 ? "+" : "";
        reason = `pnl=${sign}${pnl.toFixed(2)} · ${a.reason || ""}`.trim();
      }
    }
    return `<tr>
      <td><span class="pill ${esc(kind)}">${esc(a.kind)}</span></td>
      <td>${polyTitle(a, 52)}</td>
      <td>${esc(a.side || "—")}</td>
      <td>${a.price != null ? Number(a.price).toFixed(3) : "—"}</td>
      <td class="muted">${esc(reason.slice(0, 48))}</td>
      <td class="muted">${fmtAgo(ago)}</td>
    </tr>`;
  }).join("")}
  </tbody></table>`;
}

function renderTraders(body, extra) {
  const t = STATE.traders || {};
  const rows = t.leaderboard || t.watchlist || [];
  extra.textContent = t.ok ? `stream ok · ${t.refresh_count || 0} scans` : (t.last_error || "warming");
  if (!rows.length) {
    body.innerHTML = `<div class="muted">Streaming Polymarket leaderboard…</div>`;
    return;
  }
  body.innerHTML = `<table class="table"><thead><tr>
    <th>RANK</th><th>TRADER</th><th>PNL</th><th>VOL</th><th>WIN</th>
  </tr></thead><tbody>
  ${rows.slice(0, 15).map((r) => `<tr>
    <td>${esc(r.rank || "—")}</td>
    <td>${esc(r.userName || r.wallet || "")} <span class="pill copy">WATCH</span></td>
    <td class="${cls$(r.pnl)}">${fmt$(r.pnl)}</td>
    <td class="muted">${fmt$(r.vol)}</td>
    <td class="muted">${esc(r.period || "")}</td>
  </tr>`).join("")}
  </tbody></table>
  <div style="margin-top:10px" class="muted">Recent copy fills: ${(t.recent_fills || []).length}</div>`;
}

function renderEdges(body, extra) {
  const e = STATE.edges || {};
  const rows = e.edges || [];
  const age = e.age_sec;
  extra.textContent = e.ok
    ? `${rows.length} live · ${e.short_windows || 0} windows · ${age != null ? fmtAgo(age) : "fresh"}`
    : (e.last_error || "scanning");
  if (!rows.length) {
    body.innerHTML = `<div class="muted">Scanning live Polymarket 5m crypto Up/Down windows…</div>`;
    return;
  }
  body.innerHTML = `<table class="table"><thead><tr>
    <th>MARKET</th><th>SIDE</th><th>EDGE</th><th>MID</th><th>LEFT</th><th>WHY</th>
  </tr></thead><tbody>
  ${rows.slice(0, 16).map((r) => `<tr>
    <td>${polyTitle(r, 48)}</td>
    <td><span class="pill edge">${esc(r.side || "")}</span></td>
    <td class="pos">${(Number(r.edge || 0) * 100).toFixed(1)}%</td>
    <td>${Number(r.price || 0).toFixed(3)}</td>
    <td class="muted">${fmtLeft(r.secs_left)}</td>
    <td class="muted">${esc((r.reason || "").replace("edge:", ""))}</td>
  </tr>`).join("")}
  </tbody></table>`;
}

function sparkline(path, up) {
  const pts = (path || []).map((p) => Number(p[1]));
  if (pts.length < 2) return `<svg class="spark" viewBox="0 0 120 36"></svg>`;
  const min = Math.min(...pts);
  const max = Math.max(...pts);
  const span = Math.max(1e-6, max - min);
  const d = pts.map((v, i) => {
    const x = (i / (pts.length - 1)) * 118 + 1;
    const y = 34 - ((v - min) / span) * 32;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const stroke = up ? "var(--green)" : "var(--red)";
  return `<svg class="spark" viewBox="0 0 120 36" preserveAspectRatio="none">
    <polyline fill="none" stroke="${stroke}" stroke-width="2" points="${d}" />
  </svg>`;
}

function renderLiveBets(body, extra) {
  const lb = STATE.live_bets || {};
  const rows = lb.open || [];
  const closed = lb.recent_closed || [];
  extra.textContent = lb.ok
    ? `${rows.length} tracking · ${lb.mark_count || 0} marks`
    : "warming marks…";
  if (!rows.length && !closed.length) {
    body.innerHTML = `<div class="muted">Autopilot seats stream here — marks, PnL, MFE/MAE teach the skillbook live.</div>`;
    return;
  }
  const openHtml = rows.map((b) => {
    const roi = Number(b.last_roi || 0);
    const grade = esc(b.grade || "C");
    return `<div class="bet-card">
      <div>
        <div class="title">${polyTitle(b, 68)}</div>
        <div class="meta">${esc(b.side)} · ${esc(b.reason || "")} · grade <span class="grade ${grade}">${grade}</span></div>
      </div>
      <div class="${cls$(b.last_upnl)}">${fmt$(b.last_upnl)}<div class="meta">uPNL</div></div>
      <div class="${cls$(roi)}">${(roi * 100).toFixed(1)}%<div class="meta">ROI</div></div>
      <div><div class="pos">+${(Number(b.mfe_roi || 0) * 100).toFixed(0)}%</div>
           <div class="neg">${(Number(b.mae_roi || 0) * 100).toFixed(0)}%</div>
           <div class="meta">MFE/MAE</div></div>
      ${sparkline(b.path, roi >= 0)}
      <div class="meta">mark ${Number(b.last_mark || 0).toFixed(3)} · ${(b.path || []).length} samples · autopilot</div>
    </div>`;
  }).join("");
  const closedHtml = closed.slice(0, 4).map((b) =>
    `<div class="lesson"><span class="grade ${esc(b.grade || "C")}">${esc(b.grade || "C")}</span>
     <span class="${cls$(b.pnl)}">${fmt$(b.pnl)}</span> · ${polyTitle(b, 46)}
     <span class="muted">MFE ${(Number(b.mfe_roi || 0) * 100).toFixed(0)}% / MAE ${(Number(b.mae_roi || 0) * 100).toFixed(0)}%</span></div>`
  ).join("");
  body.innerHTML = `${openHtml || '<div class="muted">No open lives — autopilot will seat the next edge.</div>'}
    <div class="muted" style="margin:12px 0 6px">Recently learned paths</div>
    ${closedHtml || '<div class="muted">—</div>'}`;
}

function renderPositions(body, extra) {
  const acct = STATE.account || {};
  const rows = acct.positions || [];
  extra.textContent = `${rows.length} open · auto`;
  if (!rows.length) {
    body.innerHTML = `<div class="muted">No seats — engine opens and closes on its own.</div>`;
    return;
  }
  body.innerHTML = `<table class="table"><thead><tr>
    <th>MARKET</th><th>SIDE</th><th>ENTRY</th><th>MARK</th><th>SL</th><th>uPNL</th><th>ROI</th>
  </tr></thead><tbody>
  ${rows.map((p) => {
    const trail = p.trail_armed ? (p.trail_mode || "trail") : "";
    const slTxt = Number(p.sl || 0).toFixed(3);
    return `<tr>
    <td>${polyTitle(p, 42)}<div class="muted">${esc(p.reason || "")}${trail ? ` · ${esc(trail)}` : ""}</div></td>
    <td>${esc(p.side)}</td>
    <td>${Number(p.entry || 0).toFixed(3)}</td>
    <td>${Number(p.mark || 0).toFixed(3)}</td>
    <td class="${trail ? "pos" : "muted"}">${slTxt}${p.be_mark != null ? `<div class="muted">be ${Number(p.be_mark).toFixed(3)}</div>` : ""}</td>
    <td class="${cls$(p.upnl)}">${fmt$(p.upnl)}</td>
    <td class="${cls$(p.roi)}">${(Number(p.roi || 0) * 100).toFixed(1)}%</td>
  </tr>`;
  }).join("")}
  </tbody></table>`;
}

function renderAccount(body, extra) {
  const a = STATE.account || {};
  const total = a.total_pnl != null ? Number(a.total_pnl) : (Number(a.equity || 0) - Number(a.start_balance || 0));
  const realized = a.realized_pnl != null ? Number(a.realized_pnl) : 0;
  const upnl = a.upnl != null ? Number(a.upnl) : 0;
  const fees = a.fees_paid != null ? Number(a.fees_paid) : 0;
  const feeRate = a.paper_fee_rate != null ? Number(a.paper_fee_rate) : 0.07;
  extra.textContent = a.source || "paper";
  const sz = a.sizing || {};
  const liveSz = sz.live_if_armed || {};
  body.innerHTML = `<div class="kv">
    <div class="cell"><div class="k">EQUITY</div><div class="v">${fmt$(a.equity)}</div></div>
    <div class="cell"><div class="k">CASH</div><div class="v">${fmt$(a.balance)}</div></div>
    <div class="cell"><div class="k">TOTAL PnL</div><div class="v ${cls$(total)}">${fmt$(total)}</div></div>
    <div class="cell"><div class="k">REALIZED</div><div class="v ${cls$(realized)}">${fmt$(realized)}</div></div>
    <div class="cell"><div class="k">uPNL</div><div class="v ${cls$(upnl)}">${fmt$(upnl)}</div></div>
    <div class="cell"><div class="k">FEES</div><div class="v neg">${fmt$(fees)} <span class="muted">Θ=${feeRate}</span></div></div>
    <div class="cell"><div class="k">PAPER SEAT</div><div class="v">${fmt$(sz.stake)}</div></div>
    <div class="cell"><div class="k">LIVE SEAT</div><div class="v">${liveSz.stake != null ? fmt$(liveSz.stake) : "—"}</div></div>
  </div>
  <div class="muted" style="margin-top:8px">
    paper sizing ${sz.mode || "smart"}${sz.min_bet_phase ? " · MIN-BET" : ""} ·
    grow&gt;${Number(sz.grow_above_usd || 50).toFixed(0)} · peak ${fmt$(a.peak_equity)}
  </div>
  <div class="lesson ${liveSz.min_bet_phase || (liveSz.live_clob_usd != null && Number(liveSz.live_clob_usd) < 50) ? "pos" : ""}" style="margin-top:6px">
    ${liveSz.stake != null
      ? `LIVE+ARM will size off CLOB $${Number(liveSz.live_clob_usd || 0).toFixed(2)} → seat ${fmt$(liveSz.stake)}${liveSz.min_bet_phase ? " (min-bet phase)" : ""} — not paper equity.`
      : "LIVE+ARM sizes off your CLOB wallet (hit PREP LAG / SNAPSHOT $ to preview). Paper seats stay paper-only."}
  </div>
  <div style="margin-top:12px">
    <div class="muted" style="margin-bottom:6px">Recent closed (net of fees)</div>
    ${(a.recent_closed || []).slice(0, 6).map((t) =>
      `<div class="lesson"><span class="${cls$(t.pnl)}">${fmt$(t.pnl)}</span> · ${esc((t.title || "").slice(0, 48))} <span class="muted">(${esc(t.exit_reason || "")}${t.fees != null ? ` · fee ${fmt$(t.fees)}` : ""})</span></div>`
    ).join("") || '<div class="muted">—</div>'}
  </div>`;
}

function renderEquity(body, extra) {
  const ranges = [
    { id: "1h", label: "1H", sec: 3600 },
    { id: "6h", label: "6H", sec: 21600 },
    { id: "1d", label: "1D", sec: 86400 },
    { id: "7d", label: "7D", sec: 604800 },
    { id: "all", label: "ALL", sec: null },
  ];
  if (!window._eqRange) window._eqRange = "1d";
  const range = ranges.find((r) => r.id === window._eqRange) || ranges[2];
  const spending = !!(STATE.account?.live || STATE.live?.spending);
  const paperHist = [...((STATE.memory && STATE.memory.equity_history) || [])];
  const liveHist = [...((STATE.memory && STATE.memory.live_equity_history) || [])];
  const paperEq = Number(STATE.account?.equity);
  const now = Date.now() / 1000;
  if (Number.isFinite(paperEq)) {
    const last = paperHist.length ? paperHist[paperHist.length - 1] : null;
    if (!last || Math.abs(Number(last[1]) - paperEq) > 1e-9) {
      paperHist.push([now, paperEq]);
    }
  }
  let liveTip = null;
  const livePrev = STATE.account?.sizing?.live_if_armed;
  if (livePrev && livePrev.live_clob_usd != null) {
    liveTip = Number(livePrev.live_clob_usd);
  } else if (STATE.live?.balance?.balance_usd != null) {
    liveTip = Number(STATE.live.balance.balance_usd);
  }
  if (Number.isFinite(liveTip)) {
    const last = liveHist.length ? liveHist[liveHist.length - 1] : null;
    if (!last || Math.abs(Number(last[1]) - liveTip) > 1e-9) {
      liveHist.push([now, liveTip]);
    }
  }

  const cutToWindow = (arr, tLo, tHi) =>
    arr.filter((p) => {
      const t = Number(p[0]);
      return t >= tLo - 1e-9 && t <= tHi + 1e-9;
    });

  // Window domain = selected range (not just data extent) so chips visibly zoom
  const dataSpanLo = (() => {
    const all = spending
      ? [...paperHist, ...liveHist]
      : paperHist;
    if (!all.length) return now - (range.sec || 86400);
    return Math.min(...all.map((p) => Number(p[0])));
  })();
  const tHi = now;
  const tLo = range.sec ? tHi - range.sec : dataSpanLo;
  const winLo = Math.min(tLo, tHi - 60);
  const winHi = tHi;
  const winSpan = Math.max(1e-6, winHi - winLo);

  const paperCut = cutToWindow(paperHist, winLo, winHi);
  const liveCut = cutToWindow(liveHist, winLo, winHi);
  const active = spending
    ? (liveCut.length >= 1 ? liveCut : paperCut)
    : paperCut;
  const stroke = spending ? "var(--eq-live)" : "var(--eq-paper)";
  const modeLabel = spending ? "LIVE" : "PAPER";
  const tipEq = spending
    ? (Number.isFinite(liveTip) ? liveTip : (active.length ? Number(active[active.length - 1][1]) : paperEq))
    : paperEq;
  extra.textContent = `${modeLabel} · ${range.label} · ${active.length} pts · ${fmt$(tipEq)}`;

  const chips = ranges.map((r) =>
    `<button type="button" class="eq-range ${r.id === range.id ? "on" : ""}" data-eq-range="${esc(r.id)}">${r.label}</button>`
  ).join("");

  const mapX = (t) => {
    const x = 8 + ((Number(t) - winLo) / winSpan) * (640 - 16);
    return Math.max(8, Math.min(632, x));
  };

  if (active.length < 2) {
    body.innerHTML = `
      <div class="eq-toolbar">
        <div class="eq-ranges">${chips}</div>
        <div class="eq-legend">
          <span class="eq-swatch paper"></span> paper
          <span class="eq-swatch live"></span> live+armed
        </div>
      </div>
      <div class="muted" style="margin-top:10px">
        ${active.length
          ? `Only ${active.length} point in ${range.label} — wait for more ticks or pick a wider range.`
          : (spending
            ? "No live equity in this window yet."
            : "No paper equity in this window yet.")}
      </div>`;
  } else {
    const vals = active.map((p) => Number(p[1]));
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const w = 640, h = 180, pad = 8;
    const span = Math.max(1e-6, max - min);
    const pts = active.map((p) => {
      const x = mapX(p[0]);
      const y = h - pad - ((Number(p[1]) - min) / span) * (h - pad * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(" ");
    let ghost = "";
    if (spending && paperCut.length >= 2) {
      const gvals = paperCut.map((p) => Number(p[1]));
      const gmin = Math.min(...gvals, min);
      const gmax = Math.max(...gvals, max);
      const gspan = Math.max(1e-6, gmax - gmin);
      const gpts = paperCut.map((p) => {
        const x = mapX(p[0]);
        const y = h - pad - ((Number(p[1]) - gmin) / gspan) * (h - pad * 2);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(" ");
      ghost = `<polyline class="eq-ghost" fill="none" stroke="var(--eq-paper)" stroke-width="1.5" opacity="0.28" points="${gpts}" />`;
    }
    body.innerHTML = `
      <div class="eq-toolbar">
        <div class="eq-ranges">${chips}</div>
        <div class="eq-legend">
          <span class="eq-swatch paper"></span> paper
          <span class="eq-swatch live"></span> live+armed
        </div>
      </div>
      <svg class="equity-svg" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
        ${ghost}
        <polyline fill="none" stroke="${stroke}" stroke-width="2.4" points="${pts}" />
      </svg>
      <div class="muted" style="margin-top:6px">
        window ${range.label} · ${active.length} pts · ${fmt$(min)} → ${fmt$(max)}
      </div>`;
  }

  body.querySelectorAll("[data-eq-range]").forEach((btn) => {
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const next = btn.getAttribute("data-eq-range") || "1d";
      if (window._eqRange === next) return;
      window._eqRange = next;
      // Re-render immediately; WS ticks will keep the same range
      renderEquity(body, extra);
    });
  });
}

function renderLearning(body, extra) {
  const m = STATE.memory || {};
  const skills = STATE.skills || {};
  const grades = skills.path_grades || {};
  const regime = skills.regime || {};
  const top = skills.top_setups || [];
  const cold = skills.cold_setups || [];
  const tune = skills.auto_tune || {};
  const wr = regime.win_rate != null ? `${(Number(regime.win_rate) * 100).toFixed(0)}%` : "—";
  extra.textContent = `${m.trade_count || 0} trades · learn+autotune`;
  body.innerHTML = `
    <div class="muted" style="margin-bottom:6px">Regime (last ${regime.n || 0} closes — not account total)</div>
    <div class="kv">
      <div class="cell"><div class="k">WIN RATE</div><div class="v ${cls$(regime.pnl)}">${esc(wr)}</div></div>
      <div class="cell"><div class="k">LAST ${regime.n || 0}</div><div class="v ${cls$(regime.pnl)}">${fmt$(regime.pnl)}</div></div>
      <div class="cell"><div class="k">CONF</div><div class="v">${Number(STATE.params?.min_confidence || 0).toFixed(2)}</div></div>
      <div class="cell"><div class="k">RISK</div><div class="v">${Number(STATE.params?.risk_frac || 0).toFixed(2)}</div></div>
    </div>
    <div class="muted" style="margin:12px 0 6px">Path grades</div>
    <div class="kv">
      ${["A","B","C","D","F"].map((g) => {
        const row = grades[g] || {};
        return `<div class="cell"><div class="k">GRADE ${g}</div><div class="v">${row.n || 0}</div></div>`;
      }).join("")}
    </div>
    <div class="muted" style="margin:12px 0 6px">Hot setups (size up)</div>
    ${top.slice(0, 5).map((c) =>
      `<div class="lesson">${esc(c.key)} · n=${c.n || 0} · exp <span class="${cls$(c.expectancy)}">${fmt$(c.expectancy)}</span>
       <span class="muted">size×${Number(c.size_bias || 1).toFixed(2)}</span></div>`
    ).join("") || '<div class="muted">Collecting setup samples…</div>'}
    <div class="muted" style="margin:12px 0 6px">Cold setups (cut / veto)</div>
    ${cold.slice(0, 5).map((c) =>
      `<div class="lesson">${esc(c.key)} · n=${c.n || 0} · exp <span class="neg">${fmt$(c.expectancy)}</span>
       <span class="muted">size×${Number(c.size_bias || 1).toFixed(2)}</span></div>`
    ).join("") || '<div class="muted">No cold setups yet.</div>'}
    ${(tune.last || []).length ? `<div class="muted" style="margin:12px 0 6px">Auto-tune</div>
      ${(tune.last || []).map((t) => `<div class="lesson">${esc(t)}</div>`).join("")}` : ""}
    <div class="muted" style="margin:12px 0 6px">Lessons</div>
    ${(m.lessons || []).slice(0, 6).map((L) =>
      `<div class="lesson"><div class="ts">${new Date((L.ts || 0) * 1000).toLocaleTimeString()} · ${esc(L.source || "")}</div>${esc(L.text || "")}</div>`
    ).join("") || '<div class="muted">No lessons yet.</div>'}`;
}

function renderHealth(body, extra) {
  const h = STATE.health || {};
  const traders = STATE.traders || {};
  const edges = STATE.edges || {};
  extra.textContent = h.ok ? "streams healthy" : "degraded";
  const tasks = h.tasks || [];
  const clock = STATE.clock || {};
  body.innerHTML = `<table class="table"><thead><tr>
    <th>TASK</th><th>ALIVE</th><th>RESTARTS</th><th>BEAT</th><th>ERR</th>
  </tr></thead><tbody>
  ${tasks.map((t) => `<tr>
    <td>${esc(t.name)}</td>
    <td class="${t.alive ? "pos" : "neg"}">${t.alive ? "YES" : "NO"}</td>
    <td>${t.restarts || 0}</td>
    <td class="muted">${t.since_beat_sec ?? "—"}s</td>
    <td class="muted">${esc((t.last_error || "").slice(0, 60))}</td>
  </tr>`).join("")}
  </tbody></table>
  <div style="margin-top:10px" class="muted">
    poly sync: ${clock.poly_sync ? "LIVE" : "STALE"} ·
    edge age ${clock.edge_age_sec != null ? fmtAgo(clock.edge_age_sec) : "—"} ·
    windows ${clock.short_windows || 0} ·
    traders: ${traders.ok ? "ok" : "down"} · edges: ${edges.ok ? "ok" : "down"} ·
    pending copy=${traders.pending_candidates || 0} edge=${edges.pending_candidates || 0}
  </div>`;
}

function renderLive(body, extra) {
  const L = STATE.live || {};
  const gate = L.gate || {};
  const meta = L.meta || {};
  const missing = L.missing || [];
  const stats = L.paper_stats || {};
  const dep = L.deposit || {};
  const bal = L.balance || {};
  const mode = L.exec_mode || L.mode || "paper";
  const armed = !!L.live_trading_armed;
  const spending = !!L.spending;
  const armReady = !!L.arm_ready || !!L.creds_ok;
  extra.textContent = spending ? "SPENDING LIVE" : (armReady ? `${mode} · arm-ready` : "creds incomplete");
  const funder = dep.funder || meta.funder || "";
  const funderShort = funder ? `${funder.slice(0, 10)}…${funder.slice(-6)}` : "—";
    const balTxt = bal.balance_usd != null ? fmt$(bal.balance_usd) : (bal.ok === false ? "err" : "—");
  const claim = L.claimer || {};
  const claimLine = claim.last_error
    ? `claimer err: ${claim.last_error}`
    : `auto-claim · valuable=${claim.valuable ?? "—"} · redeemable=${claim.redeemable || 0} · dust-skip=${claim.dust_skipped || 0} · dry=${claim.dry_run_count || 0} · live=${claim.claimed_count || 0}`;
  const lastClaim = claim.last_claim || {};
  const lastClaimLine = lastClaim.title
    ? `last claim: ${lastClaim.title} @ ${Number(lastClaim.price || 0).toFixed(2)} · ${lastClaim.posted ? "POSTED" : (lastClaim.msg || "")}`
    : "";
  const lagR = L.lag_ready || {};
  const lagReasons = (lagR.reasons || []).slice(0, 3).join(" · ");
  const lagOk = !!lagR.ok;
  body.innerHTML = `
    <div class="kv" style="margin-bottom:10px;grid-template-columns:repeat(4,1fr)">
      <div class="cell"><div class="k">MODE</div><div class="v">${esc(mode)}</div></div>
      <div class="cell"><div class="k">ARMED</div><div class="v ${armed ? "neg" : ""}">${armed ? "YES" : "NO"}</div></div>
      <div class="cell"><div class="k">CREDS</div><div class="v ${L.creds_ok ? "pos" : "neg"}">${L.creds_ok ? "OK" : "MISSING"}</div></div>
      <div class="cell"><div class="k">CLOB $</div><div class="v">${esc(balTxt)}${bal.frozen ? " · frozen" : ""}</div></div>
    </div>
    <div class="lesson ${armReady ? "pos" : "neg"}">${armReady
      ? "Arm-ready: LIVE → ARM spends real USDC. Learning vetoes stay on. Autopilot auto-claims."
      : "Still need: " + esc(missing.join(" · "))}</div>
    <div class="lesson ${lagOk ? "pos" : "neg"}">Lag FAK: ${lagOk ? "READY" : "NOT READY"}${lagReasons ? " — " + esc(lagReasons) : ""}</div>
    <div class="muted" style="margin:10px 0 4px">Deposit on Polygon to funder</div>
    <div class="lesson" style="user-select:all;word-break:break-all">${esc(funder || "—")}</div>
    <div class="muted">${esc(dep.note || "")} · shown ${esc(funderShort)}</div>
    <div class="muted" style="margin:10px 0 6px">Paper gate (advisory${stats.lag_only_gate ? " · lag-only" : ""}) · n=${stats.n || 0}${stats.lag_n != null ? ` · lag=${stats.lag_n}` : ""} · wr=${stats.win_rate != null ? (Number(stats.win_rate) * 100).toFixed(1) + "%" : "—"} · recent pnl ${fmt$(stats.pnl)} · ${gate.pass ? "PASS" : "HOLD"}</div>
    <div class="muted">${esc(claimLine)}</div>
    ${lastClaimLine ? `<div class="muted">${esc(lastClaimLine)}</div>` : ""}
    <div class="lesson pos">Auto-claim ON — banks wins ≥0.90, dumps near expiry, CLOB-sells wallet value so capital recycles.</div>
    ${L.last_error ? `<div class="lesson neg">${esc(L.last_error)}</div>` : ""}
    ${L.last_order && L.last_order.msg ? `<div class="muted">Last CLOB: ${esc(L.last_order.side || "")} · ${esc(L.last_order.msg)}${L.last_order.urgent ? " · FAK" : ""}</div>` : ""}
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <button type="button" class="tc-btn" id="btn-live-reload" style="padding:6px 10px">RELOAD CREDS</button>
      <button type="button" class="tc-btn" id="btn-live-connect" style="padding:6px 10px">CONNECT CLOB</button>
      <button type="button" class="tc-btn" id="btn-live-bal" style="padding:6px 10px">SNAPSHOT $</button>
      <button type="button" class="tc-btn" id="btn-lag-prep" style="padding:6px 10px">PREP LAG</button>
    </div>`;
  body.querySelector("#btn-live-reload")?.addEventListener("click", async () => {
    await fetch("/api/live/reload", { method: "POST" });
  });
  body.querySelector("#btn-live-connect")?.addEventListener("click", async () => {
    const r = await fetch("/api/live/connect", { method: "POST" });
    const data = await r.json();
    if (!data.ok) alert(data.msg || "connect failed");
  });
  body.querySelector("#btn-live-bal")?.addEventListener("click", async () => {
    const r = await fetch("/api/live/balance");
    const data = await r.json();
    const usd = data?.balance?.balance_usd ?? data?.balance_usd;
    if (usd != null && Number.isFinite(Number(usd))) {
      window._walletLiveUsd = Number(usd);
      renderWalletHdr();
    }
  });
  body.querySelector("#btn-lag-prep")?.addEventListener("click", async () => {
    const r = await fetch("/api/live/lag_prep", { method: "POST" });
    const data = await r.json();
    if (!data.ok && data.lag_ready && !data.lag_ready.ok) {
      alert((data.lag_ready.reasons || []).join("\n") || "lag not ready");
    }
  });
}

const WIDGET_RENDERERS = {
  alerts: renderAlerts,
  traders: renderTraders,
  edges: renderEdges,
  livebets: renderLiveBets,
  positions: renderPositions,
  account: renderAccount,
  equity: renderEquity,
  learning: renderLearning,
  live: renderLive,
  health: renderHealth,
};

/* ---------- voice ---------- */

function refreshVoices() {
  if (!window.speechSynthesis) return;
  _voicesCache = window.speechSynthesis.getVoices() || [];
}
if (window.speechSynthesis) {
  refreshVoices();
  window.speechSynthesis.onvoiceschanged = refreshVoices;
}

function pickVoice() {
  const voices = _voicesCache.length ? _voicesCache : (window.speechSynthesis?.getVoices() || []);
  const prefer = [/google us/i, /microsoft aria/i, /samantha/i, /english/i];
  for (const re of prefer) {
    const hit = voices.find((v) => re.test(v.name + " " + v.lang));
    if (hit) return hit;
  }
  return voices[0] || null;
}

function speakSanitize(text) {
  // TTS reads "5m" as "5 meters" — force minute wording for crypto windows.
  return String(text || "")
    .replace(/\b(\d+)\s*m\b/gi, "$1 minute")
    .replace(/\b(\d+)m\b/gi, "$1 minute");
}

function speakLine(text, { interrupt = false } = {}) {
  if (!window.speechSynthesis || !text || !_voicePrefs.enabled) return;
  if (interrupt) window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(speakSanitize(text));
  const persona = currentPersona();
  u.rate = persona.rate;
  u.pitch = persona.pitch;
  const v = pickVoice();
  if (v) u.voice = v;
  window.speechSynthesis.speak(u);
}

function syncVoiceUi() {
  const on = !!_voicePrefs.enabled;
  const btn = $("#btn-voice-toggle");
  btn.classList.toggle("on", on);
  btn.classList.toggle("off", !on);
  $("#vc-txt").textContent = on ? "VOICE ON" : "VOICE OFF";
  $("#vc-persona-name").textContent = currentPersona().name;
}

function watchAlertsForVoice() {
  if (!STATE || !_voicePrefs.enabled) return;
  const tradingOn = !!STATE?.account?.trading_enabled;
  const rows = STATE.alerts || [];
  // Newest first in state; speak newest TAKE/CLOSE we haven't spoken
  for (const a of rows.slice(0, 12).reverse()) {
    const kind = String(a.kind || "").toUpperCase();
    if (!["TAKE", "CLOSE", "LIVE"].includes(kind)) continue;
    // STOP: only finish callouts for seats we're already in (CLOSE / path LIVE)
    if (!tradingOn && kind === "TAKE") continue;
    const key = `${a.kind}:${a.ts}:${a.market_slug || a.title}:${a.side}`;
    if (_spokenAlertKeys.has(key)) continue;
    // Skip ancient alerts on first paint
    if (Date.now() / 1000 - Number(a.ts || 0) > 90 && _spokenAlertKeys.size === 0) {
      _spokenAlertKeys.add(key);
      continue;
    }
    _spokenAlertKeys.add(key);
    const line = a.speak || `${a.kind} ${a.side || ""} on ${(a.title || "").slice(0, 80)}`;
    speakLine(line);
  }
  if (_spokenAlertKeys.size > 400) {
    _spokenAlertKeys = new Set(Array.from(_spokenAlertKeys).slice(-200));
  }
}

/* ---------- controls ---------- */

function renderAutopilotControl() {
  const L = STATE?.live || {};
  const mode = String(L.exec_mode || STATE?.params?.exec_mode || "paper").toLowerCase();
  const armed = !!L.live_trading_armed;
  const spending = !!L.spending;
  ["paper", "dry_run", "live"].forEach((m) => {
    const id = m === "dry_run" ? "#btn-ap-dry" : `#btn-ap-${m === "paper" ? "paper" : "live"}`;
    const btn = $(id);
    if (!btn) return;
    btn.classList.toggle("on", mode === m);
  });
  const armBtn = $("#btn-ap-arm");
  if (armBtn) {
    armBtn.classList.toggle("on", armed);
    armBtn.classList.toggle("hot", spending);
    armBtn.textContent = spending ? "ARMED$" : (armed ? "ARMED" : "ARM");
  }
  const badge = $("#hdr-trading");
  if (badge) {
    const hot = !!STATE?.account?.trading_enabled;
    let label = "ENTRIES";
    if (spending) label = "LIVE$";
    else if (mode === "live") label = "LIVE";
    else if (mode === "dry_run") label = "DRY";
    badge.textContent = `${label} ${hot ? "HOT" : "COLD"}`;
    badge.className = "hval badge " + (spending ? "live" : (hot ? "hot" : "cold"));
  }
}

function renderTradeControl(armed) {
  const tc = $("#trade-control");
  const startBtn = $("#btn-trade-start");
  const stopBtn = $("#btn-trade-stop");
  if (!tc) return;
  tc.classList.toggle("hot", !!armed);
  tc.classList.toggle("cold", !armed);
  const st = $("#tc-state");
  const sub = $("#tc-sub");
  const lab = $("#tc-label");
  const L = STATE?.live || {};
  const mode = String(L.exec_mode || STATE?.params?.exec_mode || "dry_run").toLowerCase();
  const spending = !!L.spending;
  const openN = Number(
    STATE?.account?.position_count
    || (STATE?.account?.positions || []).length
    || 0
  );
  if (lab) {
    if (spending) lab.textContent = "LIVE";
    else if (mode === "live") lab.textContent = "LIVE";
    else if (mode === "dry_run") lab.textContent = "DRY";
    else lab.textContent = "ENTRIES";
  }
  if (st) st.textContent = armed ? "HOT" : "COLD";
  if (sub) {
    if (armed) sub.textContent = "new entries on";
    else if (openN > 0) sub.textContent = `finishing ${openN} open · then idle`;
    else sub.textContent = "stopped — click START";
  }
  if (startBtn) startBtn.disabled = !!armed;
  if (stopBtn) stopBtn.disabled = !armed;
}

async function setExecMode(mode) {
  try {
    const r = await fetch("/api/live/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    const data = await r.json();
    if (STATE) {
      if (!STATE.live) STATE.live = {};
      Object.assign(STATE.live, data);
      if (STATE.params) STATE.params.exec_mode = mode;
    }
    if (mode !== "live") {
      await fetch("/api/live/arm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ armed: false }),
      });
    }
    renderAutopilotControl();
  } catch (_) { /* ignore */ }
}

async function toggleArm() {
  const L = STATE?.live || {};
  const mode = String(L.exec_mode || "paper").toLowerCase();
  const next = !L.live_trading_armed;
  if (next && mode !== "live") {
    alert("Set AUTOPILOT to LIVE before arming real spend.");
    return;
  }
  if (next && !confirm("Arm LIVE spending? Real USDC will post on autopilot takes/closes. Skillbook vetoes stay on.")) {
    return;
  }
  try {
    const r = await fetch("/api/live/arm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ armed: next }),
    });
    const data = await r.json();
    if (!data.ok && next) alert(data.msg || "arm blocked");
    if (STATE) {
      if (!STATE.live) STATE.live = {};
      Object.assign(STATE.live, data);
    }
    renderAutopilotControl();
  } catch (_) { /* ignore */ }
}

async function setTradingEnabled(enabled) {
  if (_tradeToggleBusy) return;
  _tradeToggleBusy = true;
  const ctl = $("#trade-control");
  ctl?.classList.add("busy");
  // Optimistic UI so the click feels instant
  renderTradeControl(!!enabled);
  try {
    const r = await fetch("/api/trading", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !!enabled }),
    });
    const data = await r.json();
    if (data && data.ok) {
      if (STATE) {
        if (!STATE.account) STATE.account = {};
        if (!STATE.params) STATE.params = {};
        STATE.account.trading_enabled = !!data.trading_enabled;
        STATE.params.trading_enabled = !!data.trading_enabled;
      }
      renderTradeControl(!!data.trading_enabled);
      ctl?.classList.add("flash-ok");
      setTimeout(() => ctl?.classList.remove("flash-ok"), 500);
    } else {
      // revert from server truth
      renderTradeControl(!!(STATE?.account?.trading_enabled));
    }
  } catch (e) {
    renderTradeControl(!!(STATE?.account?.trading_enabled));
  } finally {
    ctl?.classList.remove("busy");
    _tradeToggleBusy = false;
  }
}

function wireControls() {
  $("#btn-trade-start")?.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    setTradingEnabled(true);
  });
  $("#btn-trade-stop")?.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    setTradingEnabled(false);
  });
  $("#btn-ap-paper")?.addEventListener("click", (ev) => {
    ev.preventDefault();
    setExecMode("paper");
  });
  $("#btn-ap-dry")?.addEventListener("click", (ev) => {
    ev.preventDefault();
    setExecMode("dry_run");
  });
  $("#btn-ap-live")?.addEventListener("click", (ev) => {
    ev.preventDefault();
    setExecMode("live");
  });
  $("#btn-ap-arm")?.addEventListener("click", (ev) => {
    ev.preventDefault();
    toggleArm();
  });
  $("#btn-voice-toggle")?.addEventListener("click", () => {
    _voicePrefs.enabled = !_voicePrefs.enabled;
    saveVoicePrefs();
    syncVoiceUi();
    if (_voicePrefs.enabled) speakLine(`${currentPersona().name} armed. Bet callouts on.`, { interrupt: true });
    else if (window.speechSynthesis) window.speechSynthesis.cancel();
  });
  $("#btn-voice-persona")?.addEventListener("click", () => {
    const idx = PERSONAS.findIndex((p) => p.id === _voicePrefs.persona);
    _voicePrefs.persona = PERSONAS[(idx + 1) % PERSONAS.length].id;
    saveVoicePrefs();
    syncVoiceUi();
    speakLine(currentPersona().sample, { interrupt: true });
  });

  const bankInput = $("#bankroll-input");
  const bankOk = $("#btn-bankroll-ok");
  const doReset = async () => {
    const amt = Number(bankInput?.value);
    if (!Number.isFinite(amt) || amt <= 0) {
      alert("Enter a dollar amount greater than 0");
      return;
    }
    if (bankOk) bankOk.disabled = true;
    try {
      const r = await fetch("/api/reset_bankroll", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount: amt, keep_lessons: true }),
      });
      const data = await r.json();
      if (!data.ok) {
        alert(data.msg || "reset failed");
        return;
      }
      if (STATE) {
        if (!STATE.account) STATE.account = {};
        STATE.account.equity = data.equity;
        STATE.account.balance = data.equity;
        STATE.account.start_balance = data.start_balance;
        STATE.account.positions = [];
        STATE.account.position_count = 0;
        STATE.account.recent_closed = [];
        STATE.account.closed_count = 0;
        STATE.account.upnl = 0;
        STATE.account.unrealized_pnl = 0;
        STATE.account.realized_pnl = 0;
        STATE.account.total_pnl = 0;
        STATE.account.roi_pct = 0;
        if (STATE.params) STATE.params.starting_equity = data.equity;
        // Keep learning memory in local STATE — never zero trade_count / lessons
        if (STATE.memory) {
          STATE.memory.equity_history = [];
        }
        if (STATE.live_bets) {
          STATE.live_bets.open = [];
          STATE.live_bets.recent_closed = [];
          STATE.live_bets.open_count = 0;
        }
      }
      const ctl = $("#bankroll-control");
      ctl?.classList.add("flash-ok");
      setTimeout(() => ctl?.classList.remove("flash-ok"), 600);
      render();
    } catch (e) {
      alert("reset failed — is the server up?");
    } finally {
      if (bankOk) bankOk.disabled = false;
    }
  };
  bankOk?.addEventListener("click", (ev) => {
    ev.preventDefault();
    doReset();
  });
  bankInput?.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      doReset();
    }
  });
}

wireControls();
connectWS();
setInterval(pollFallback, 500);
syncVoiceUi();
