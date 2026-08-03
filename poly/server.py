"""POLY alert deck server: dashboard, websocket, paper engine, stream workers."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import alerts
from .claimer import claimer
from .edges import edge_scanner
from .engine import PaperEngine
from .live_exec import live_exec
from .memory import PolyMemory
from .params import LiveParams
from .selfheal import Supervisor
from .skills import skillbook
from .spot_lead import spot_lead
from .tracker import bet_tracker
from .traders import trader_streamer
from .version import BUILD, BUILD_NUM, as_dict as version_dict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

HOST = "127.0.0.1"
PORT = 18112

app = FastAPI(title="POLY Alert Deck")

params = LiveParams()
memory = PolyMemory()
engine = PaperEngine(params, memory)
supervisor = Supervisor(memory)

_ws_clients: set[WebSocket] = set()


def _downsample_series(rows: list, *, max_pts: int = 900) -> list:
    """Keep first/last + evenly spaced samples so long ranges still zoom."""
    if not rows:
        return []
    n = len(rows)
    if n <= max_pts:
        return list(rows)
    out: list = []
    step = (n - 1) / float(max_pts - 1)
    seen: set[int] = set()
    for i in range(max_pts):
        idx = int(round(i * step))
        if idx in seen:
            continue
        seen.add(idx)
        out.append(rows[idx])
    if n - 1 not in seen:
        out.append(rows[-1])
    return out


def paper_stats_for_gate() -> dict[str, Any]:
    """Aggregate paper performance for the live profitability gate."""
    trades = list(memory.data.get("trades") or [])
    lag_only = bool(params.values.get("lag_only", True))
    lag_trades = [
        t
        for t in trades
        if "spot_lag" in str(t.get("reason") or "")
        or "spot_lag" in str(t.get("edge") or "")
    ]
    use = lag_trades if (lag_only and lag_trades) else trades
    n = len(use)
    wins = sum(1 for t in use if float(t.get("pnl") or 0) > 0)
    pnl = sum(float(t.get("pnl") or 0) for t in use[-80:])
    wr = (wins / n) if n else None
    regime = skillbook.status().get("regime") or {}
    return {
        "trade_count": n,
        "n": n,
        "win_rate": wr,
        "pnl": pnl,
        "recent_pnl": pnl,
        "regime_n": int(regime.get("n") or 0),
        "lag_only_gate": bool(lag_only and lag_trades),
        "lag_n": len(lag_trades),
        "all_n": len(trades),
    }


_balance_cache: dict[str, Any] = {"ts": 0.0, "data": {}, "live_ts": 0.0}


def live_status_payload(*, refresh_balance: bool = False, reload_creds: bool = False) -> dict[str, Any]:
    if reload_creds:
        live_exec.reload_creds()
    stats = paper_stats_for_gate()
    ready = live_exec.status(paper_stats=stats)
    mode = str(params.values.get("exec_mode") or ready.get("mode") or "paper").lower()
    armed = bool(params.values.get("live_trading_armed"))
    spending = mode == "live" and armed and bool(ready.get("creds_ok"))
    balance = _balance_cache.get("data") or {}
    now = time.time()
    # Header wallet $ only auto-adjusts when LIVE+ARMED. Otherwise freeze last snapshot
    # (still allow one-shot seed / explicit refresh from Live Ready widget).
    need_seed = not balance and bool(ready.get("creds_ok"))
    should_fetch = bool(ready.get("creds_ok")) and (
        refresh_balance
        or need_seed
        or (spending and now - float(_balance_cache.get("live_ts") or 0) > 12)
    )
    if should_fetch:
        try:
            balance = live_exec.fetch_balance()
            _balance_cache["ts"] = now
            _balance_cache["data"] = balance
            if spending:
                _balance_cache["live_ts"] = now
        except Exception as e:
            balance = {"ok": False, "msg": str(e)[:120]}
            _balance_cache["ts"] = now
            _balance_cache["data"] = balance
    bal_out = dict(balance) if isinstance(balance, dict) else {"raw": balance}
    bal_out["frozen"] = not spending
    bal_out["asof_ts"] = float(_balance_cache.get("ts") or 0)
    min_bet = float(params.values.get("min_bet_usd") or 2.5)
    lag_ready = live_exec.lag_ready(
        paper_stats=stats,
        exec_mode=mode,
        armed=armed,
        min_bet=min_bet,
    )
    # Prefer prepare_collateral balance when fresher / when lag checklist ran
    if lag_ready.get("balance_usd") is not None and bal_out.get("balance_usd") is None:
        bal_out["balance_usd"] = lag_ready.get("balance_usd")
        bal_out["allowance_ok"] = lag_ready.get("allowance_ok")
    payload = {
        **ready,
        "exec_mode": mode,
        "live_trading_armed": armed,
        "paper_stats": stats,
        "spending": spending,
        "arm_ready": bool(ready.get("creds_ok")),
        "lag_ready": lag_ready,
        "balance": bal_out,
        "claimer": claimer.status(),
        "deposit": {
            "chain": "Polygon",
            "asset": "USDC / pUSD",
            "funder": (ready.get("meta") or {}).get("funder") or "",
            "note": "Send USDC on Polygon to this funder address. Site Deposit may be geo-blocked.",
        },
    }
    live_exec.save_status(
        {
            "ts": time.time(),
            "creds_ok": payload.get("creds_ok"),
            "missing": payload.get("missing"),
            "exec_mode": mode,
            "armed": armed,
            "spending": payload.get("spending"),
            "balance_usd": (balance or {}).get("balance_usd"),
            "gate": payload.get("gate"),
        }
    )
    return payload


def full_state() -> dict[str, Any]:
    snap = params.snapshot()
    paper = engine.status()
    traders = trader_streamer.status()
    edges = edge_scanner.status()
    skills = skillbook.status()
    live = live_status_payload()
    now = time.time()
    eq_hist = _downsample_series(
        list(memory.data.get("equity_history") or []), max_pts=1000
    )
    live_eq_hist = _downsample_series(
        list(memory.data.get("live_equity_history") or []), max_pts=1000
    )
    trader_stats = sorted(
        memory.data.get("trader_stats", {}).items(),
        key=lambda kv: -float(kv[1].get("pnl") or 0),
    )
    market_stats = sorted(
        memory.data.get("market_stats", {}).items(),
        key=lambda kv: -float(kv[1].get("pnl") or 0),
    )
    edge_age = edges.get("age_sec")
    return {
        "ts": now,
        "clock": {
            "server_ts": now,
            "edge_age_sec": edge_age,
            "edge_ok": bool(edges.get("ok")),
            "short_windows": edges.get("short_windows") or 0,
            "engine_age_sec": (
                now - float(paper.get("last_loop_ts") or 0)
                if paper.get("last_loop_ts")
                else None
            ),
            "poly_sync": bool(edges.get("ok")) and (edge_age is None or float(edge_age) < 45),
            "spot_lead": spot_lead.status(),
            "lag_hot": {
                "enabled": bool(params.values.get("lag_hot_path", True))
                and bool(params.values.get("lag_snipe", True)),
                "poll_sec": params.values.get("lag_hot_poll_sec"),
                "lag_hot_count": edges.get("lag_hot_count"),
                "shorts_cache_age": edges.get("shorts_cache_age"),
            },
        },
        "account": {
            "source": (
                "live"
                if live.get("spending")
                else str(params.values.get("exec_mode") or "paper")
            ),
            "live": bool(live.get("spending")),
            "exec_mode": str(params.values.get("exec_mode") or "paper"),
            "live_trading_armed": bool(params.values.get("live_trading_armed")),
            "equity": paper.get("equity"),
            "balance": paper.get("balance"),
            "start_balance": paper.get("start_balance"),
            "peak_equity": paper.get("peak_equity"),
            "live_clob_usd": (live.get("balance") or {}).get("balance_usd"),
            "paper_equity": paper.get("equity"),
            "sizing": paper.get("sizing") or {},
            "positions": paper.get("positions") or [],
            "position_count": paper.get("position_count") or 0,
            "upnl": paper.get("upnl") if paper.get("upnl") is not None else sum(
                float(p.get("upnl") or 0) for p in (paper.get("positions") or [])
            ),
            "unrealized_pnl": paper.get("unrealized_pnl"),
            "realized_pnl": paper.get("realized_pnl"),
            "total_pnl": paper.get("total_pnl"),
            "roi_pct": paper.get("roi_pct"),
            "fees_paid": paper.get("fees_paid"),
            "paper_fees": paper.get("paper_fees"),
            "paper_fee_rate": paper.get("paper_fee_rate"),
            "invested": paper.get("invested"),
            "recent_closed": paper.get("recent_closed") or [],
            "closed_count": paper.get("closed_count") or 0,
            "signals": paper.get("signals") or [],
            "last_loop_ts": paper.get("last_loop_ts"),
            "loop_count": paper.get("loop_count"),
            "last_error": paper.get("last_error"),
            "trading_enabled": paper.get("trading_enabled"),
            "live_alert_mode": paper.get("live_alert_mode"),
        },
        "traders": traders,
        "edges": edges,
        "live_bets": paper.get("live_bets") or bet_tracker.status(),
        # Keep the alert rail live — hide multi-hour stale noise from hydrate
        "alerts": alerts.recent(40, max_age_sec=45 * 60),
        "alerts_today": alerts.alerts_today_count(),
        "params": snap["values"],
        "dashboard": snap["dashboard"],
        "health": supervisor.status(),
        "skills": skills,
        "live": live,
        "build": BUILD,
        "build_num": BUILD_NUM,
        "version": version_dict(),
        "memory": {
            "lessons": memory.data["lessons"][-12:][::-1],
            "equity_history": eq_hist,
            "live_equity_history": live_eq_hist,
            "trade_count": len(memory.data["trades"]),
            "bet_lives": list(memory.data.get("bet_lives") or [])[-8:][::-1],
            "best_traders": [{"key": k, **v} for k, v in trader_stats[:6]],
            "worst_traders": [
                {"key": k, **v}
                for k, v in trader_stats
                if float(v.get("pnl") or 0) < 0
            ][:6],
            "best_markets": [{"key": k, **v} for k, v in market_stats[:6]],
        },
        "stream_refresh_ms": 500,
    }


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/version")
async def api_version():
    return version_dict()


@app.get("/api/state")
async def api_state():
    return JSONResponse(full_state())


@app.get("/api/alerts")
async def api_alerts():
    return {"alerts": alerts.recent(80)}


@app.post("/api/trading")
async def api_trading(payload: dict = Body(...)):
    raw = payload.get("enabled", payload.get("trading_enabled"))
    if raw is None:
        return {
            "ok": False,
            "msg": "missing enabled",
            "trading_enabled": bool(params.values.get("trading_enabled")),
        }
    enabled = (
        bool(raw)
        if not isinstance(raw, str)
        else str(raw).strip().lower() in ("1", "true", "yes", "on", "start")
    )
    ok, msg = params.set_param("trading_enabled", enabled, who="dashboard")
    if ok:
        memory.add_lesson(
            f"Dashboard {'STARTED' if enabled else 'STOPPED'} entries "
            f"(trading_enabled={enabled}). Open seats keep marking until flat.",
            source="dashboard",
        )
    open_n = len(engine.positions)
    return {
        "ok": ok,
        "msg": msg if not ok else (
            "HOT — new entries on" if enabled
            else (
                f"COLD — finishing {open_n} open seat(s), then idle"
                if open_n else "COLD — stopped until START"
            )
        ),
        "trading_enabled": bool(params.values.get("trading_enabled")),
        "open_positions": open_n,
    }


@app.post("/api/live_alert_mode")
async def api_live_alert_mode(payload: dict = Body(...)):
    raw = payload.get("enabled", payload.get("live_alert_mode"))
    enabled = (
        bool(raw)
        if not isinstance(raw, str)
        else str(raw).strip().lower() in ("1", "true", "yes", "on")
    )
    ok, msg = params.set_param("live_alert_mode", enabled, who="dashboard")
    return {
        "ok": ok,
        "msg": msg,
        "live_alert_mode": bool(params.values.get("live_alert_mode")),
    }


@app.get("/api/live")
async def api_live_get():
    return JSONResponse(live_status_payload())


@app.post("/api/live/reload")
async def api_live_reload():
    meta = live_exec.reload_creds()
    return {**live_status_payload(reload_creds=False, refresh_balance=True), "ok": True, "meta": meta}


@app.post("/api/live/connect")
async def api_live_connect():
    """Derive/verify CLOB API creds — does not place orders."""
    result = await asyncio.to_thread(live_exec.connect)
    # Prefer connect outcome over status.ok overwrite
    payload = live_status_payload()
    return {**payload, **result, "ok": bool(result.get("ok"))}


@app.post("/api/live/mode")
async def api_live_mode(payload: dict = Body(...)):
    """Set exec_mode: paper | dry_run | live. Live still needs arm + gate."""
    mode = str(payload.get("mode") or payload.get("exec_mode") or "").strip().lower()
    ok, msg = params.set_param("exec_mode", mode, who="dashboard")
    return {**live_status_payload(), "ok": ok, "msg": msg}


@app.post("/api/live/arm")
async def api_live_arm(payload: dict = Body(...)):
    """Explicit arm switch for real spend. Requires LIVE mode + creds. Gate is advisory."""
    raw = payload.get("armed", payload.get("live_trading_armed"))
    armed = (
        bool(raw)
        if not isinstance(raw, str)
        else str(raw).strip().lower() in ("1", "true", "yes", "on", "arm")
    )
    if armed:
        status = live_status_payload()
        mode = str(params.values.get("exec_mode") or "paper").lower()
        if mode != "live":
            return {
                **status,
                "ok": False,
                "msg": "set exec_mode=live before arming",
            }
        if not status.get("creds_ok"):
            return {**status, "ok": False, "msg": "credentials incomplete"}
        # Warm CLOB + collateral before first lag FAK can fire
        try:
            await asyncio.to_thread(live_exec.prewarm_lag)
        except Exception:
            pass
        # Size live seats off CLOB wallet AND mirror paper bankroll to that wallet
        try:
            bal = await asyncio.to_thread(live_exec.fetch_balance)
            clob = float((bal or {}).get("balance_usd") or 0)
            engine.live_start_equity = clob
            engine.live_peak_equity = clob
            mirrored = engine.mirror_paper_to_live(
                set_start=True, max_age_sec=0.0, force=True
            )
            memory.add_lesson(
                f"Live ARMED — paper bankroll synced to CLOB "
                f"${float((mirrored or {}).get('equity') or clob):.2f}.",
                source="live_exec",
            )
        except Exception:
            pass
        # Paper gate is advisory — user may arm after funding even if paper is cold
    ok, msg = params.set_param("live_trading_armed", armed, who="dashboard")
    if ok and not armed:
        try:
            engine.live_start_equity = 0.0
            engine.live_peak_equity = 0.0
        except Exception:
            pass
        memory.add_lesson(
            f"Live trading {'ARMED' if armed else 'DISARMED'} "
            f"(exec_mode={params.values.get('exec_mode')}).",
            source="dashboard",
        )
    elif ok:
        memory.add_lesson(
            f"Live trading ARMED (exec_mode={params.values.get('exec_mode')}). "
            f"Seats size from CLOB wallet only.",
            source="dashboard",
        )
    status = live_status_payload(refresh_balance=True) if armed else live_status_payload()
    warn = ""
    if armed and not (status.get("gate") or {}).get("pass"):
        warn = " armed with paper gate HOLD — skillbook still vetoes cold setups"
    lag = status.get("lag_ready") or {}
    if armed and not lag.get("ok"):
        warn += " · lag not funded/ready — " + "; ".join((lag.get("reasons") or [])[:2])
    return {**status, "ok": ok, "msg": (msg or "") + warn}


@app.get("/api/live/balance")
async def api_live_balance():
    result = await asyncio.to_thread(live_exec.fetch_balance)
    return {**live_status_payload(refresh_balance=True), **result}


@app.post("/api/live/lag_prep")
async def api_live_lag_prep():
    """Warm CLOB client + refresh collateral allowance for lag FAKs."""
    warm = await asyncio.to_thread(live_exec.prewarm_lag)
    stats = paper_stats_for_gate()
    mode = str(params.values.get("exec_mode") or "dry_run").lower()
    armed = bool(params.values.get("live_trading_armed"))
    min_bet = float(params.values.get("min_bet_usd") or 2.5)
    lag = await asyncio.to_thread(
        lambda: live_exec.lag_ready(
            paper_stats=stats,
            exec_mode=mode,
            armed=armed,
            min_bet=min_bet,
            prepare=True,
        )
    )
    payload = live_status_payload(refresh_balance=True)
    payload["lag_ready"] = lag
    payload["prewarm"] = warm
    payload["ok"] = bool(lag.get("ok"))
    return payload


@app.post("/api/param")
async def api_param(payload: dict):
    ok, msg = params.set_param(payload.get("key"), payload.get("value"), who="user")
    return {"ok": ok, "msg": msg}


@app.post("/api/dashboard")
async def api_dashboard(payload: dict):
    ok, msg = params.set_dashboard(payload, who="user")
    return {"ok": ok, "msg": msg}


@app.post("/api/position/close")
async def api_close(payload: dict):
    pid = str(payload.get("id") or payload.get("position_id") or "").strip()
    if str(payload.get("all") or "").lower() in ("1", "true", "yes") or pid.lower() == "all":
        n = engine.close_all("user_dashboard")
        return {"ok": True, "msg": f"closed {n} positions"}
    ok = engine.close_id(pid, "user_dashboard")
    return {"ok": ok, "msg": f"closed {pid}" if ok else f"no position {pid}"}


@app.post("/api/reset_bankroll")
async def api_reset_bankroll(payload: dict = Body(...)):
    """Set paper bankroll and wipe PnL slate; lessons are kept."""
    raw = payload.get("amount", payload.get("equity", payload.get("starting_equity")))
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        return {"ok": False, "msg": "invalid amount"}
    if amount <= 0:
        return {"ok": False, "msg": "amount must be > 0"}
    keep = payload.get("keep_lessons", True)
    keep_lessons = bool(keep) if not isinstance(keep, str) else keep.strip().lower() in (
        "1", "true", "yes", "on",
    )
    try:
        result = await asyncio.to_thread(
            engine.reset_bankroll, amount, keep_lessons=keep_lessons
        )
        return {
            "ok": True,
            "msg": f"reset to ${float(result['equity']):.2f} — session PnL wiped, learning kept",
            **result,
        }
    except Exception as e:
        return {"ok": False, "msg": str(e)[:200]}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_clients.add(ws)
    try:
        while True:
            await asyncio.sleep(3600)
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(ws)


async def trader_worker(beat):
    await asyncio.sleep(1)
    while True:
        beat()
        try:
            await asyncio.to_thread(trader_streamer.refresh, dict(params.values))
        except Exception as e:
            memory.record_incident(
                {"ts": time.time(), "task": "trader_stream", "error": str(e)[:200]}
            )
        await asyncio.sleep(float(params.values.get("trader_poll_sec") or 20))


async def edge_worker(beat):
    await asyncio.sleep(2)
    while True:
        beat()
        try:
            await asyncio.to_thread(edge_scanner.refresh, dict(params.values))
        except Exception as e:
            memory.record_incident(
                {"ts": time.time(), "task": "edge_scan", "error": str(e)[:200]}
            )
        await asyncio.sleep(float(params.values.get("edge_poll_sec") or 45))


async def engine_worker(beat):
    await asyncio.sleep(2)
    while True:
        beat()
        try:
            await asyncio.to_thread(engine.tick)
        except Exception as e:
            memory.record_incident(
                {"ts": time.time(), "task": "paper_engine", "error": str(e)[:200]}
            )
        # Faster loop when lag hot-path is on (still backs the full board)
        if bool(params.values.get("lag_hot_path", True)) and bool(
            params.values.get("lag_snipe", True)
        ):
            sleep_for = float(params.values.get("engine_poll_sec_lag") or 1.5)
        else:
            sleep_for = float(params.values.get("engine_poll_sec") or 2.0)
        await asyncio.sleep(max(0.5, sleep_for))


async def lag_hot_worker(beat):
    """Sub-second loop: Binance impulse → lag score → immediate FAK-ready open."""
    await asyncio.sleep(3)
    last_fire = 0.0
    while True:
        beat()
        try:
            if not (
                bool(params.values.get("lag_snipe", True))
                and bool(params.values.get("lag_hot_path", True))
                and bool(params.values.get("trading_enabled", True))
            ):
                await asyncio.sleep(1.0)
                continue
            min_move = float(params.values.get("lag_min_move") or 0.0007)
            lookback = float(params.values.get("lag_lookback_sec") or 4.0)
            hot = False
            for asset in ("BTC", "ETH", "SOL"):
                ret = abs(float(spot_lead.ret(asset, lookback).get("ret") or 0.0))
                if ret >= min_move:
                    hot = True
                    break
            now = time.time()
            # Debounce: at most one hot burst every ~0.8s
            if hot and (now - last_fire) >= 0.8:
                last_fire = now
                p = dict(params.values)

                def _burst():
                    cands = edge_scanner.refresh_lag_only(p)
                    if cands:
                        return engine.tick_lag_fast(cands)
                    return {"opened": 0, "skipped": 0, "hot": True, "cands": 0}

                await asyncio.to_thread(_burst)
            sleep_for = float(params.values.get("lag_hot_poll_sec") or 0.35)
        except Exception as e:
            memory.record_incident(
                {"ts": time.time(), "task": "lag_hot", "error": str(e)[:200]}
            )
            sleep_for = 1.0
        await asyncio.sleep(max(0.15, sleep_for))


async def mark_worker(beat):
    """Real-time mark-to-market + trail exits. Sub-second when LIVE+ARMED with seats."""
    await asyncio.sleep(2)
    _learn_tick = 0
    while True:
        beat()
        try:
            mode = str(params.values.get("exec_mode") or "paper").lower()
            armed = bool(params.values.get("live_trading_armed"))
            spending = mode == "live" and armed
            open_n = 0
            try:
                open_n = len(engine.positions)
            except Exception:
                open_n = 0
            fast = spending and open_n > 0
            # Full path-learning every ~8 fast ticks so skillbook still updates
            _learn_tick += 1
            if fast and _learn_tick % 8 == 0:
                fast = False
            await asyncio.to_thread(engine.mark_live, fast=fast)
            if spending and open_n > 0:
                sleep_for = float(params.values.get("mark_poll_sec_live") or 0.35)
            else:
                sleep_for = float(params.values.get("mark_poll_sec") or 2.0)
        except Exception as e:
            memory.record_incident(
                {"ts": time.time(), "task": "live_mark", "error": str(e)[:200]}
            )
            sleep_for = float(params.values.get("mark_poll_sec") or 2.0)
        await asyncio.sleep(max(0.15, sleep_for))


async def claimer_worker(beat):
    """Auto-claim / recycle so capital keeps rolling — runs even when entries STOPPED."""
    await asyncio.sleep(6)
    while True:
        beat()
        try:
            mode = str(params.values.get("exec_mode") or "paper").lower()
            armed = bool(params.values.get("live_trading_armed"))
            spending = mode == "live" and armed
            funder = str((live_exec.status().get("meta") or {}).get("funder") or "")
            if mode in ("dry_run", "live") and funder:
                # Snapshot open seats for near-expiry force sells
                try:
                    eng_pos = list(engine.positions)
                except Exception:
                    eng_pos = []
                await asyncio.to_thread(
                    claimer.tick,
                    funder=funder,
                    mode=mode,
                    armed=armed,
                    live_exec=live_exec,
                    memory=memory,
                    engine_positions=eng_pos,
                )
            # Faster when live spending so dice keep rolling
            base = float(params.values.get("claim_poll_sec") or 20)
            sleep_for = 8.0 if spending else max(10.0, base)
        except Exception as e:
            memory.record_incident(
                {"ts": time.time(), "task": "claimer", "error": str(e)[:200]}
            )
            sleep_for = 15.0
        await asyncio.sleep(sleep_for)


async def broadcaster(beat):
    while True:
        if _ws_clients:
            state = json.dumps(full_state())
            dead = []
            for ws in _ws_clients:
                try:
                    await ws.send_text(state)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                _ws_clients.discard(ws)
        beat()
        await asyncio.sleep(0.5)


@app.on_event("startup")
async def on_startup():
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    # SAFETY: never boot into a spending state — user must arm from the dashboard
    if bool(params.values.get("live_trading_armed")):
        params.set_param("live_trading_armed", False, who="startup_safety")
    mode = str(params.values.get("exec_mode") or "paper").lower()
    if mode == "live":
        params.set_param("exec_mode", "dry_run", who="startup_safety")
    # Keep continuous learning on — lag snipes write into the same skillbook
    if not bool(params.values.get("continuous_learning", True)):
        params.set_param("continuous_learning", True, who="startup_learning")
    # Seat queue is lag-only unless user explicitly turns lag_only off
    if "lag_only" not in params.values or params.values.get("lag_only") is None:
        params.set_param("lag_only", True, who="startup_lag_only")
    if not bool(params.values.get("lag_snipe", True)):
        params.set_param("lag_snipe", True, who="startup_lag_only")
    try:
        spot_lead.start()
    except Exception as e:
        memory.record_incident(
            {"ts": time.time(), "task": "spot_lead", "error": str(e)[:200]}
        )
    # Prewarm CLOB client early (non-blocking thread) so first live/armed lag FAK isn't cold
    def _bg_prewarm():
        try:
            live_exec.prewarm_lag()
        except Exception:
            try:
                live_exec.prewarm()
            except Exception:
                pass

    try:
        import threading

        threading.Thread(target=_bg_prewarm, name="poly-clob-prewarm", daemon=True).start()
    except Exception:
        pass
    supervisor.spawn("trader_stream", trader_worker, heartbeat_timeout=180)
    supervisor.spawn("edge_scan", edge_worker, heartbeat_timeout=240)
    supervisor.spawn("paper_engine", engine_worker, heartbeat_timeout=120)
    supervisor.spawn("lag_hot", lag_hot_worker, heartbeat_timeout=15)
    supervisor.spawn("live_mark", mark_worker, heartbeat_timeout=30)
    supervisor.spawn("claimer", claimer_worker, heartbeat_timeout=90)
    supervisor.spawn("ws_broadcaster", broadcaster, heartbeat_timeout=10)
    supervisor.spawn("watchdog", supervisor.watchdog, heartbeat_timeout=0)
    memory.add_lesson(
        "POLY alert deck online — disarmed on boot; LIVE spend only when you click ARM. "
        "Lag hot-path is FAK-ready when you arm.",
        source="system",
    )


# Mount static after routes so `/` stays the index
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main():
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
