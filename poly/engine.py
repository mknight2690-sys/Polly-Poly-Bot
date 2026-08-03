"""Paper trading engine: copy + edge candidates → seats, learning, alerts."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Any

from . import alerts
from . import client as poly
from . import fees as poly_fees
from .client import poly_url
from .edges import edge_scanner
from .live_exec import live_exec
from .memory import PolyMemory
from .params import LiveParams
from .sizing import compute_stake, open_invested
from .skills import skillbook
from .tracker import bet_tracker
from .traders import trader_streamer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNT_PATH = os.path.join(ROOT, "data", "poly_account.json")


class PaperEngine:
    def __init__(self, params: LiveParams, memory: PolyMemory):
        self.params = params
        self.memory = memory
        self._lock = threading.Lock()
        self.equity = float(params.values.get("starting_equity") or 1000.0)
        self.balance = self.equity
        self.start_balance = self.equity
        self.peak_equity = self.equity
        self.positions: list[dict] = []
        self.closed: list[dict] = []
        self.signals: list[dict] = []
        self.cooldowns: dict[str, float] = {}
        self.fees_paid: float = 0.0
        self.last_loop_ts = 0.0
        self.loop_count = 0
        self.last_error = ""
        self.last_sizing: dict[str, Any] = {}
        self.live_peak_equity = 0.0
        self.live_start_equity = 0.0
        self.load()

    def load(self):
        want = float(self.params.values.get("starting_equity") or 10.0)
        try:
            with open(ACCOUNT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.equity = float(data.get("equity") or self.equity)
            self.balance = float(data.get("balance") or self.balance)
            self.start_balance = float(data.get("start_balance") or self.start_balance)
            self.peak_equity = float(
                data.get("peak_equity") or max(self.equity, self.start_balance)
            )
            self.positions = list(data.get("positions") or [])
            self.closed = list(data.get("closed") or [])[-200:]
            self.cooldowns = dict(data.get("cooldowns") or {})
            self.fees_paid = float(data.get("fees_paid") or 0.0)
        except FileNotFoundError:
            self.equity = want
            self.balance = want
            self.start_balance = want
            self.peak_equity = want
            self.fees_paid = 0.0
            self.save()
            return
        except Exception:
            pass
        # Bankroll change (e.g. $1000 -> $10 turbo) resets the paper book
        if abs(float(self.start_balance or 0) - want) > 0.05:
            self.equity = want
            self.balance = want
            self.start_balance = want
            self.peak_equity = want
            self.positions = []
            self.closed = []
            self.cooldowns = {}
            self.fees_paid = 0.0
            self.save()

    def reset_bankroll(self, amount: float, *, keep_lessons: bool = True) -> dict[str, Any]:
        """Wipe paper PnL slate and restart at `amount`. Learning memory is always kept."""
        amt = float(amount)
        if not (amt > 0):
            raise ValueError("amount must be > 0")
        amt = max(1.0, min(1_000_000.0, amt))
        with self._lock:
            self.params.set_param("starting_equity", amt, who="reset")
            self.equity = amt
            self.balance = amt
            self.start_balance = amt
            self.peak_equity = amt
            self.positions = []
            self.closed = []
            self.cooldowns = {}
            self.signals = []
            self.fees_paid = 0.0
            self.last_error = ""
            self.last_sizing = {}
            self.live_peak_equity = 0.0
            self.live_start_equity = 0.0
            self.save()
        # Clear live tracker seats only (open marks) — never wipe skillbook / trades
        try:
            bet_tracker.lives.clear()
            bet_tracker.recent_closed_lives = []
        except Exception:
            pass
        # Soft equity-curve wipe for a clean chart; KEEP trades / stats / bet_lives / lessons
        try:
            self.memory.data["equity_history"] = []
            if not keep_lessons:
                # Explicit opt-out only — default keeps everything learned
                self.memory.data["lessons"] = []
            self.memory.save()
            kept = {
                "trades": len(self.memory.data.get("trades") or []),
                "bet_lives": len(self.memory.data.get("bet_lives") or []),
                "lessons": len(self.memory.data.get("lessons") or []),
                "setups": len(getattr(skillbook, "data", {}).get("setups") or {}),
            }
            self.memory.add_lesson(
                f"Paper bankroll reset to ${amt:.2f}. Session PnL wiped; "
                f"learning kept (trades={kept['trades']}, lives={kept['bet_lives']}, "
                f"lessons={kept['lessons']}, setups={kept['setups']}).",
                source="reset",
            )
        except Exception:
            kept = {}
        return {
            "ok": True,
            "equity": self.equity,
            "start_balance": self.start_balance,
            "lessons_kept": True,
            "learning_kept": True,
            "learning": kept,
        }

    def save(self):
        os.makedirs(os.path.dirname(ACCOUNT_PATH), exist_ok=True)
        payload = {
            "equity": self.equity,
            "balance": self.balance,
            "start_balance": self.start_balance,
            "peak_equity": float(self.peak_equity),
            "positions": self.positions,
            "closed": self.closed[-200:],
            "cooldowns": self.cooldowns,
            "fees_paid": float(self.fees_paid),
            "updated_at": time.time(),
        }
        tmp = ACCOUNT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, ACCOUNT_PATH)

    def _mark_price(self, pos: dict) -> float:
        token = str(pos.get("token_id") or "")
        if token:
            try:
                mid = poly.midpoint(token)
                if mid is not None:
                    return max(0.0, min(1.0, float(mid)))
            except Exception:
                pass
        return max(0.0, min(1.0, float(pos.get("mark") or pos.get("entry") or 0.0)))

    def _position_upnl(self, pos: dict) -> float:
        """Canonical paper uPNL: (mark - entry) * shares − estimated exit fee."""
        entry = float(pos.get("entry") or 0.0)
        shares = float(pos.get("shares") or 0.0)
        cost = float(pos.get("cost") or 0.0)
        if shares <= 0 and entry > 0 and cost > 0:
            shares = cost / entry
            pos["shares"] = shares
        mark = max(0.0, min(1.0, float(pos.get("mark") or entry)))
        upnl = (mark - entry) * shares
        # Accrue expected taker fee on exit so open equity isn't optimistic
        exit_fee = self._taker_fee(
            shares, mark, category=str(pos.get("category") or "crypto")
        )
        return upnl - exit_fee

    def _rollback_failed_live_open(self, pos: dict) -> None:
        """Undo a seat if LIVE+ARMED CLOB buy did not post — no phantom exposure."""
        try:
            cost = float(pos.get("cost") or 0)
            fee = float(pos.get("entry_fee") or 0)
            self.balance += cost + fee
            self.fees_paid = max(0.0, self.fees_paid - fee)
            self.positions = [p for p in self.positions if p.get("id") != pos.get("id")]
            bet_tracker.prune_missing({str(p.get("id")) for p in self.positions})
            self._sync_equity()
            self.memory.add_lesson(
                f"LIVE BUY failed — rolled back {pos.get('side')} "
                f"{str(pos.get('title') or '')[:50]} ({(pos.get('clob') or {}).get('msg')})",
                source="live_exec",
            )
        except Exception:
            pass

    def _sync_equity(self) -> float:
        """Reconcile equity = cash + invested cost + mark uPNL (no network)."""
        upnl = 0.0
        invested = 0.0
        for pos in self.positions:
            u = self._position_upnl(pos)
            cost = float(pos.get("cost") or 0.0)
            pos["upnl"] = u
            pos["roi"] = u / (cost or 1.0)
            upnl += u
            invested += cost
        self.equity = float(self.balance) + invested + upnl
        if self.equity > float(self.peak_equity or 0):
            self.peak_equity = float(self.equity)
        return upnl

    def _record_equity_curves(self) -> None:
        """Paper curve always; live curve only while LIVE+ARMED (CLOB book)."""
        try:
            self.memory.record_equity(float(self.equity), source="paper")
        except Exception:
            pass
        mode = str(self.params.values.get("exec_mode") or "paper").lower().strip()
        armed = bool(self.params.values.get("live_trading_armed"))
        if mode != "live" or not armed:
            return
        try:
            clob = self._clob_balance_usd(max_age_sec=8.0)
            if clob is None:
                return
            live_eq = float(clob) + self._live_open_cost()
            self.memory.record_equity(live_eq, source="live")
        except Exception:
            pass

    def _live_clob_budget(self) -> float | None:
        """Real CLOB collateral when LIVE+ARMED (None if not spending)."""
        mode = str(self.params.values.get("exec_mode") or "paper").lower().strip()
        armed = bool(self.params.values.get("live_trading_armed"))
        if mode != "live" or not armed:
            return None
        return self._clob_balance_usd(max_age_sec=5.0)

    def _clob_balance_usd(self, *, max_age_sec: float = 5.0) -> float | None:
        try:
            bal = live_exec.fetch_balance(max_age_sec=max_age_sec)
            usd = bal.get("balance_usd")
            if usd is None:
                return None
            return float(usd)
        except Exception:
            return None

    def mirror_paper_to_live(
        self,
        *,
        set_start: bool = False,
        max_age_sec: float = 2.0,
        force: bool = False,
    ) -> dict[str, Any]:
        """Keep paper ledger cash aligned with CLOB while LIVE+ARMED.

        Free cash := CLOB collateral. Equity := cash + live seat mark value.
        Call on arm (set_start=True) and after every live open/close.
        """
        mode = str(self.params.values.get("exec_mode") or "paper").lower().strip()
        armed = bool(self.params.values.get("live_trading_armed"))
        if not force and (mode != "live" or not armed):
            return {"ok": False, "msg": "not live+armed"}
        clob = self._clob_balance_usd(max_age_sec=max_age_sec)
        if clob is None:
            return {"ok": False, "msg": "clob balance unknown"}
        self.balance = float(clob)
        upnl = self._sync_equity()
        live_eq = float(self.equity)
        if set_start:
            self.start_balance = live_eq
            self.live_start_equity = live_eq
            self.peak_equity = live_eq
            self.live_peak_equity = live_eq
        else:
            if not float(self.live_start_equity or 0):
                self.live_start_equity = live_eq
            self.live_peak_equity = max(float(self.live_peak_equity or 0), live_eq)
            self.peak_equity = max(float(self.peak_equity or 0), live_eq)
        return {
            "ok": True,
            "clob": float(clob),
            "balance": float(self.balance),
            "equity": live_eq,
            "upnl": float(upnl),
            "start": float(self.start_balance),
            "peak": float(self.peak_equity),
            "set_start": bool(set_start),
        }

    def _live_open_cost(self) -> float:
        """Cost of seats that actually posted to CLOB (not paper-only)."""
        total = 0.0
        for p in self.positions:
            clob = p.get("clob") or {}
            if clob.get("posted") and str(clob.get("mode") or "").lower() == "live":
                total += float(p.get("cost") or 0.0)
        return total

    def _size_seat(
        self,
        *,
        advice: dict,
        confidence: float,
        reason: str = "",
        need_seats: int = 1,
    ) -> dict[str, Any]:
        """Bankroll-aware stake. LIVE+ARMED sizes off CLOB wallet — never paper equity."""
        p = self.params.values
        is_lag = "spot_lag" in str(reason or "")
        min_bet = float(p.get("min_bet_usd") or 2.5) * max(1, int(need_seats))
        mode = str(p.get("exec_mode") or "paper").lower().strip()
        armed = bool(p.get("live_trading_armed"))
        spending = mode == "live" and armed

        if spending:
            clob = self._clob_balance_usd(max_age_sec=3.0)
            if clob is None:
                out = {
                    "stake": 0.0,
                    "ok": False,
                    "skip": True,
                    "why": "live+armed but CLOB balance unknown — PREP LAG / fund wallet",
                    "bankroll_source": "live_clob",
                    "live_clob_usd": None,
                    "min_bet_phase": True,
                    "reasons": ["clob_balance_unknown"],
                }
                self.last_sizing = dict(out)
                return out
            invested_live = self._live_open_cost()
            # Live book = free CLOB cash + already-posted live seats
            live_eq = float(clob) + invested_live
            if not hasattr(self, "live_peak_equity"):
                self.live_peak_equity = live_eq
            self.live_peak_equity = max(float(self.live_peak_equity or 0), live_eq)
            # Start reference: first live peak snapshot or current book (for taper)
            live_start = float(
                getattr(self, "live_start_equity", 0) or 0
            ) or live_eq
            if not getattr(self, "live_start_equity", 0):
                self.live_start_equity = live_eq
                live_start = live_eq
            out = compute_stake(
                equity=live_eq,
                balance=float(clob),
                start_equity=live_start,
                peak_equity=float(self.live_peak_equity),
                open_cost=invested_live,
                risk_frac=float(p.get("risk_frac") or 0.35),
                min_bet=min_bet,
                max_bet_usd=float(p.get("max_bet_usd") or 0.0),
                max_bet_frac=float(p.get("max_bet_frac") or 0.45),
                heat_frac=float(p.get("portfolio_heat_frac") or 0.70),
                size_mult=float(advice.get("size_mult") or 1.0),
                confidence=float(confidence),
                is_lag=is_lag,
                live_clob_usd=float(clob),
                sizing_mode=str(p.get("sizing_mode") or "smart"),
                grow_above_usd=float(p.get("sizing_grow_above_usd") or 50.0),
            )
            out = dict(out)
            out["bankroll_source"] = "live_clob"
            out["live_clob_usd"] = float(clob)
            out["live_equity"] = live_eq
            # Hard safety: never risk more than 95% of free CLOB (already in compute)
            # and never more than half a sub-$50 live book in one seat
            if live_eq + 1e-9 < float(p.get("sizing_grow_above_usd") or 50.0):
                out["stake"] = round(min(float(out.get("stake") or 0), min_bet), 4)
                out["min_bet_phase"] = True
                reasons = list(out.get("reasons") or [])
                if "live_min_until_grow" not in reasons:
                    reasons.append("live_min_until_grow")
                out["reasons"] = reasons
            if float(out.get("stake") or 0) + 1e-9 > float(clob) * 0.95:
                out["stake"] = round(max(0.0, float(clob) * 0.95), 4)
            if float(out.get("stake") or 0) + 1e-9 < min_bet:
                out["ok"] = False
                out["skip"] = True
                out["why"] = f"CLOB ${clob:.2f} cannot fund min bet ${min_bet:.2f}"
                out["stake"] = 0.0
        else:
            out = compute_stake(
                equity=float(self.equity),
                balance=float(self.balance),
                start_equity=float(self.start_balance or self.equity),
                peak_equity=float(self.peak_equity or self.equity),
                open_cost=open_invested(self.positions),
                risk_frac=float(p.get("risk_frac") or 0.35),
                min_bet=min_bet,
                max_bet_usd=float(p.get("max_bet_usd") or 0.0),
                max_bet_frac=float(p.get("max_bet_frac") or 0.45),
                heat_frac=float(p.get("portfolio_heat_frac") or 0.70),
                size_mult=float(advice.get("size_mult") or 1.0),
                confidence=float(confidence),
                is_lag=is_lag,
                live_clob_usd=None,
                sizing_mode=str(p.get("sizing_mode") or "smart"),
                grow_above_usd=float(p.get("sizing_grow_above_usd") or 50.0),
            )
            out = dict(out)
            out["bankroll_source"] = "paper"
            # Arb pair: allow a bit more room than a single seat (paper only)
            if need_seats > 1 and out.get("ok") and out.get("stake"):
                boost = min(
                    float(out["stake"]) * 1.15,
                    float(self.balance) * 0.90,
                    float(self.equity) * float(p.get("portfolio_heat_frac") or 0.70)
                    - open_invested(self.positions),
                )
                out["stake"] = round(max(float(out["stake"]), boost), 4)

        self.last_sizing = dict(out)
        return out

    def _mark_all(self, *, learn: bool = True):
        learn_on = learn and bool(self.params.values.get("continuous_learning", True))
        for pos in self.positions:
            if not pos.get("url"):
                pos["url"] = poly_url(
                    str(pos.get("event_slug") or ""),
                    str(pos.get("market_slug") or ""),
                )
            mark = self._mark_price(pos)
            pos["mark"] = mark
            upnl = self._position_upnl(pos)
            cost = float(pos.get("cost") or 0.0) or 1.0
            pos["upnl"] = upnl
            pos["roi"] = upnl / cost
            life = bet_tracker.update_mark(
                pos,
                mark,
                learn=learn_on,
                skillbook=skillbook if learn_on else None,
                memory=self.memory if learn_on else None,
            )
            pos["grade"] = life.get("grade")
            pos["mfe"] = life.get("mfe")
            pos["mae"] = life.get("mae")
            pos["mfe_roi"] = life.get("mfe_roi")
            pos["mae_roi"] = life.get("mae_roi")
            pos["path_len"] = len(life.get("path") or [])
            self._update_trailing_stop(pos)
        upnl = self._sync_equity()
        bet_tracker.prune_missing({str(p.get("id")) for p in self.positions})
        return upnl

    def _fee_px_at(self, mark: float) -> float:
        if not bool(self.params.values.get("paper_fees", True)):
            return 0.0
        fee_rate = float(self.params.values.get("paper_fee_rate") or 0.07)
        return float(
            poly_fees.taker_fee_usdc(1.0, mark, fee_rate=fee_rate)
        )

    def _tf_step_sec(self, tf: str) -> float:
        return {"1m": 60.0, "5m": 300.0, "15m": 900.0}.get(str(tf or "").lower(), 300.0)

    def _maybe_auto_claim_close(self, pos: dict) -> str | None:
        """Return exit reason if position should be auto-claimed / force-exited."""
        mark = float(pos.get("mark") or self._mark_price(pos))
        pos["mark"] = mark
        secs_left = pos.get("secs_left")
        try:
            secs_left = float(secs_left) if secs_left is not None else None
        except Exception:
            secs_left = None
        tf = str(pos.get("timeframe") or "")
        step = self._tf_step_sec(tf)
        # Prefer absolute end from window_ts
        if pos.get("window_ts"):
            try:
                secs_left = float(pos.get("window_ts")) + step - time.time()
                pos["secs_left"] = secs_left
            except Exception:
                pass
        elif secs_left is None and pos.get("window_ts"):
            try:
                secs_left = float(pos.get("window_ts")) + step - time.time()
                pos["secs_left"] = secs_left
            except Exception:
                pass
        if mark >= float(pos.get("tp") or 0.99):
            return "tp"
        if mark <= float(pos.get("sl") or 0.01):
            return "trail_sl" if pos.get("trail_armed") or pos.get("trail_mode") else "sl"
        # Pure arb pairs: hold to resolution (don't scalp out the locked EV)
        if pos.get("hold_to_resolve") or pos.get("arb_pair"):
            if mark >= 0.985:
                return "claim_win"
            if mark <= 0.02:
                return "claim_loss"
            if secs_left is not None and secs_left <= 5:
                return "claim_time"
            return None

        entry = float(pos.get("entry") or pos.get("entry_price") or 0)
        age = time.time() - float(pos.get("opened_at") or time.time())
        min_hold = float(self.params.values.get("min_hold_sec") or 60.0)

        # Big winners / dead losers only — no fee-churn scalps
        if mark >= 0.92 and (entry <= 0 or mark >= entry + 0.05):
            return "claim_win"
        if mark <= 0.04:
            return "claim_loss"

        # Anti-churn: only bank when green *after* expected exit taker fee
        fee_rate = float(self.params.values.get("paper_fee_rate") or 0.07)
        fee_px = (
            poly_fees.taker_fee_usdc(1.0, mark, fee_rate=fee_rate)
            if bool(self.params.values.get("paper_fees", True))
            else 0.0
        )
        if age >= min_hold and secs_left is not None and secs_left <= 25:
            if entry > 0 and mark >= entry + 0.04 + fee_px:
                return "claim_early"
        if age >= min_hold and secs_left is not None and secs_left <= 12:
            if entry > 0 and mark >= max(0.60, entry + 0.03 + fee_px):
                return "claim_bank"
        # Last seconds: only flatten if tiny edge left or already won
        if secs_left is not None and secs_left <= 4:
            if entry <= 0 or mark >= entry or mark <= 0.08 or mark >= 0.92:
                return "claim_time"
        return None

    def _hold_limit_sec(self, pos: dict) -> float:
        """Per-seat hold limit — don't cut 15m windows with a 7m global max_hold."""
        tf = str(pos.get("timeframe") or "").lower()
        step = {"1m": 60.0, "5m": 300.0, "15m": 900.0}.get(tf)
        opened = float(pos.get("opened_at") or time.time())
        if step and pos.get("window_ts"):
            try:
                # Hold until ~20s before window end so claim_* can bank
                until = float(pos.get("window_ts")) + step - 20.0 - opened
                if until > 30:
                    return until
            except Exception:
                pass
        by_tf = {"1m": 75.0, "5m": 240.0, "15m": 780.0}
        if tf in by_tf:
            return by_tf[tf]
        return float(self.params.values.get("max_hold_hours") or 0.12) * 3600

    def mark_live(self, *, fast: bool = False) -> dict[str, Any]:
        """Fast real-time mark pass (no new entries) for trail exits + UI.

        fast=True (LIVE+ARMED): skip path-learning I/O so trail FAKs fire sooner.
        """
        with self._lock:
            try:
                for pos in self.positions:
                    bet_tracker.ensure(pos)
                # Trail/BE must update BEFORE exit checks (paper + live)
                self._mark_all(learn=not fast)
                closed = 0
                for pos in list(self.positions):
                    reason = self._maybe_auto_claim_close(pos)
                    if reason:
                        self._close(pos, reason)
                        closed += 1
                        continue
                    age = time.time() - float(pos.get("opened_at") or time.time())
                    if age >= self._hold_limit_sec(pos):
                        self._close(pos, "max_hold")
                        closed += 1
                self.save()
                if not fast or closed:
                    self._record_equity_curves()
                self.last_error = ""
                return {"ok": True, "closed": closed, "open": len(self.positions)}
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                return {"ok": False, "error": self.last_error}

    def _fill_price(self, mid: float, side: str) -> float:
        # Conservative: pay a bit worse than mid
        slip = 0.01
        px = mid + slip
        return max(0.01, min(0.99, px))

    def _paper_fees_on(self) -> bool:
        """Apply protocol fees in paper + dry_run (live fees settle on-chain)."""
        if not bool(self.params.values.get("paper_fees", True)):
            return False
        mode = str(self.params.values.get("exec_mode") or "paper").lower().strip()
        return mode in ("paper", "dry_run")

    def _taker_fee(self, shares: float, price: float, *, category: str = "crypto") -> float:
        if not self._paper_fees_on():
            return 0.0
        return self._estimate_taker_fee(shares, price, category=category)

    def _estimate_taker_fee(
        self, shares: float, price: float, *, category: str = "crypto"
    ) -> float:
        """Protocol fee estimate for trail/BE — paper, dry_run, AND live."""
        rate = float(
            self.params.values.get("paper_fee_rate")
            or poly_fees.fee_rate_for_category(category or "crypto")
        )
        return float(poly_fees.taker_fee_usdc(shares, price, fee_rate=rate))

    def _fee_breakeven_mark(self, pos: dict, *, at_mark: float | None = None) -> float:
        """Lowest mark where net PnL ≥ 0 after entry+exit taker fees (all modes)."""
        entry = float(pos.get("entry") or 0.0)
        shares = float(pos.get("shares") or 0.0)
        if shares <= 0 and entry > 0:
            cost = float(pos.get("cost") or 0.0)
            shares = cost / entry if entry else 0.0
        shares = max(shares, 1e-9)
        entry_fee = float(pos.get("entry_fee") or 0.0)
        # Live seats often have entry_fee=0 in the paper book — still estimate
        if entry_fee <= 0:
            entry_fee = self._estimate_taker_fee(
                shares, entry, category=str(pos.get("category") or "crypto")
            )
        ref = float(at_mark if at_mark is not None else pos.get("mark") or entry)
        exit_fee = self._estimate_taker_fee(
            shares, max(0.01, ref), category=str(pos.get("category") or "crypto")
        )
        cushion = float(self.params.values.get("trail_be_cushion") or 0.005)
        be = entry + (entry_fee + exit_fee) / shares + cushion
        return max(0.01, min(0.97, be))

    def _update_trailing_stop(self, pos: dict) -> None:
        """Past fee-aware BE: trail SL 1% behind peak profit (lock ~99% of the win).

        Same path for paper, dry_run, and live+armed.
        """
        if not bool(self.params.values.get("trail_stop", True)):
            return
        if pos.get("hold_to_resolve") or pos.get("arb_pair"):
            return
        mark = float(pos.get("mark") or 0.0)
        entry = float(pos.get("entry") or 0.0)
        if mark <= 0.01 or entry <= 0.01:
            return
        if pos.get("initial_sl") is None:
            pos["initial_sl"] = float(pos.get("sl") or max(0.01, entry - 0.10))
        best = max(float(pos.get("best_mark") or entry), mark, entry)
        pos["best_mark"] = best

        be_mark = self._fee_breakeven_mark(pos, at_mark=best)
        pos["be_mark"] = round(be_mark, 4)

        # Not yet fee-breakeven at peak — leave original SL alone
        if best + 1e-9 < be_mark:
            pos["trail_armed"] = False
            if not pos.get("trail_mode"):
                pos["trail_mode"] = ""
            return

        peak_profit = max(0.0, best - entry)
        giveback = float(self.params.values.get("trail_profit_giveback") or 0.01)
        giveback = max(0.001, min(0.25, giveback))
        # SL = entry + 99% of peak profit  (1% behind the winning profit)
        trail_sl = entry + peak_profit * (1.0 - giveback)
        initial_sl = float(pos.get("initial_sl") or 0.01)
        cur_sl = float(pos.get("sl") or initial_sl)
        floor = be_mark
        new_sl = max(floor, trail_sl, initial_sl)
        new_sl = max(0.01, min(0.97, new_sl))
        # Don't park SL above live mark (instant stop)
        new_sl = min(new_sl, max(floor, mark - 0.005))
        new_sl = max(0.01, min(0.97, new_sl))

        mode = "breakeven" if new_sl <= floor + 1e-4 else "trail"
        if new_sl > cur_sl + 1e-9:
            pos["sl"] = round(new_sl, 4)
        elif pos.get("trail_armed") and float(pos.get("sl") or 0) + 1e-9 < floor:
            pos["sl"] = round(floor, 4)
        pos["trail_armed"] = True
        pos["trail_mode"] = mode
        pos["trail_giveback"] = giveback

    def _open(self, cand: dict, size_usd: float, advice: dict) -> dict | None:
        quoted = float(cand.get("price") or 0.0)
        mid = quoted
        token = str(cand.get("token_id") or "")
        reason = str(cand.get("reason") or "")
        is_lag = "spot_lag" in reason or bool(cand.get("urgent_fak"))
        # Lag snipes: skip REST midpoint (adds 100-400ms). Cross book slightly for FAK.
        if is_lag:
            entry = min(0.99, max(0.01, quoted + 0.02))
            mid = entry
        else:
            if token:
                try:
                    live = poly.midpoint(token)
                    if live is not None:
                        mid = float(live)
                except Exception:
                    pass
            if mid <= 0.01 or mid >= 0.99:
                return None
            # Reject if live mid diverges from scored quote — caused 0.09 fills on 0.50+ edges
            if quoted > 0 and abs(mid - quoted) > 0.08:
                return None
            entry = self._fill_price(mid, str(cand.get("side") or "YES"))
        side_u = str(cand.get("side") or "").upper()
        if "fair_odds" in reason:
            lo = float(self.params.values.get("fair_min_price") or 0.48)
            hi = float(self.params.values.get("fair_max_price") or 0.74)
            if entry < lo or entry > hi:
                return None
        elif reason.startswith("edge:short_") or "momentum" in reason:
            if entry < 0.48 or entry > 0.78:
                return None
            if side_u == "UP" and entry < 0.58:
                return None
        # Polymarket: min ~5 shares AND >=$1 notional — size seats accordingly
        from .live_exec import MIN_SHARE_SIZE, _ensure_buy_notional

        min_bet = float(self.params.values.get("min_bet_usd") or 3.0)
        size_usd = max(float(size_usd), min_bet, MIN_SHARE_SIZE * entry)
        shares = size_usd / entry if entry else 0.0
        entry, shares = _ensure_buy_notional(entry, shares)
        size_usd = shares * entry
        # Re-check band after notional bump (price unchanged, but keep guard)
        if "fair_odds" in reason:
            lo = float(self.params.values.get("fair_min_price") or 0.48)
            hi = float(self.params.values.get("fair_max_price") or 0.74)
            if entry < lo or entry > hi:
                return None
        if shares <= 0 or size_usd > self.balance:
            return None
        entry_fee = self._taker_fee(
            shares, entry, category=str(advice.get("category") or "crypto")
        )
        if size_usd + entry_fee > self.balance:
            return None
        market_slug = str(cand.get("market_slug") or "")
        event_slug = str(cand.get("event_slug") or "")
        url = str(cand.get("url") or "") or poly_url(event_slug, market_slug)
        pos = {
            "id": uuid.uuid4().hex[:12],
            "title": cand.get("title") or "",
            "market_slug": market_slug,
            "event_slug": event_slug,
            "url": url,
            "condition_id": cand.get("condition_id") or "",
            "token_id": token,
            "side": str(cand.get("side") or "YES").upper(),
            "entry": entry,
            "mark": entry,
            "shares": shares,
            "cost": size_usd,
            "entry_fee": entry_fee,
            "fees": entry_fee,
            "upnl": 0.0,
            "opened_at": time.time(),
            "source": cand.get("source"),
            "reason": cand.get("reason"),
            "copy_trader": cand.get("copy_trader") or "",
            "confidence": float(cand.get("confidence") or advice.get("confidence") or 0.5),
            "secs_left": cand.get("secs_left"),
            "end_date": cand.get("end_date") or "",
            "window_ts": cand.get("window_ts"),
            "timeframe": cand.get("timeframe") or "",
            "tp": min(
                0.99,
                entry + float(
                    cand.get("_tp_delta")
                    or self.params.values.get("tp_price_delta")
                    or 0.12
                ),
            ),
            "sl": max(
                0.01,
                entry - float(
                    cand.get("_sl_delta")
                    or self.params.values.get("sl_price_delta")
                    or 0.08
                ),
            ),
            "initial_sl": max(
                0.01,
                entry - float(
                    cand.get("_sl_delta")
                    or self.params.values.get("sl_price_delta")
                    or 0.08
                ),
            ),
            "best_mark": entry,
            "trail_armed": False,
            "trail_mode": "",
            "category": advice.get("category") or "",
            "setup": advice.get("setup") or "",
            "timeframe": cand.get("timeframe") or "",
            "asset": str(cand.get("asset") or skillbook.extract_asset(
                str(cand.get("title") or ""),
                market_slug,
                str(cand.get("asset") or ""),
            )),
            "grade": "C",
            "mfe": 0.0,
            "mae": 0.0,
            "roi": 0.0,
            "path_len": 1,
            "prefer_limit": False if is_lag else bool(cand.get("prefer_limit")),
            "urgent_fak": bool(is_lag or cand.get("urgent_fak")),
            "sizing": cand.get("sizing") or {"stake": size_usd},
            "hold_to_resolve": bool(cand.get("hold_to_resolve")),
            "arb_pair": bool(cand.get("arb_pair")),
            "arb_id": cand.get("arb_id") or "",
        }
        self.balance -= size_usd + entry_fee
        self.fees_paid += entry_fee
        self.positions.append(pos)
        self._sync_equity()
        bet_tracker.ensure(pos)
        slug = pos["market_slug"] or pos["title"]
        self.cooldowns[slug] = time.time()
        self._maybe_clob_order(pos, size_usd=size_usd, entry=entry)
        exec_mode = str(self.params.values.get("exec_mode") or "paper").lower().strip()
        armed = bool(self.params.values.get("live_trading_armed"))
        # LIVE+ARMED: no seat without a posted CLOB buy
        if exec_mode == "live" and armed:
            clob = pos.get("clob") or {}
            if not clob.get("posted"):
                self._rollback_failed_live_open(pos)
                return None
            # Paper cash must track wallet after every live fill
            try:
                self.mirror_paper_to_live(set_start=False, max_age_sec=0.0)
            except Exception:
                pass
        tf = str(pos.get("timeframe") or "").lower().strip()
        if tf.endswith("m") and tf[:-1].isdigit():
            tf_bit = f"{tf[:-1]} minute "
        elif tf:
            tf_bit = f"{tf} "
        else:
            tf_bit = ""
        if exec_mode == "dry_run":
            speak = (
                f"Took {tf_bit}{pos['side']} on {pos['title'][:70]} at {entry:.2f}. Dry run."
            )
        elif exec_mode == "live":
            speak = (
                f"Took {tf_bit}{pos['side']} on {pos['title'][:70]} at {entry:.2f}."
            )
        else:
            speak = (
                f"Took {tf_bit}{pos['side']} on {pos['title'][:70]} at {entry:.2f}."
            )
        alerts.emit(
            "TAKE",
            pos["title"],
            detail=f"{pos['side']} @ {entry:.3f} size=${float(pos.get('cost') or size_usd):.2f} | {pos['reason']}",
            side=pos["side"],
            price=entry,
            size_usd=float(pos.get("cost") or size_usd),
            reason=str(pos.get("reason") or ""),
            confidence=pos["confidence"],
            market_slug=pos["market_slug"],
            event_slug=pos.get("event_slug") or "",
            url="",
            copy_trader=pos.get("copy_trader") or "",
            speak=speak,
            data={
                "position_id": pos["id"],
                "exec_mode": exec_mode,
                "clob": pos.get("clob") or {},
            },
        )
        return pos

    def _open_arb_pair(self, cand: dict, size_usd: float, advice: dict) -> list[dict]:
        """Buy BOTH Up and Down with equal shares when pair_sum < $1 — locked EV."""
        legs = list(cand.get("legs") or [])
        if len(legs) < 2:
            return []
        try:
            up = next(l for l in legs if str(l.get("side")).upper() == "UP")
            down = next(l for l in legs if str(l.get("side")).upper() == "DOWN")
        except StopIteration:
            return []
        up_px = max(0.01, min(0.99, float(up.get("price") or 0)))
        down_px = max(0.01, min(0.99, float(down.get("price") or 0)))
        pair = up_px + down_px
        if pair <= 0 or pair >= 1.0:
            return []
        # Equal share count so one side redeems $1/share
        budget = float(size_usd)
        shares = budget / pair
        # Polymarket min ~5 shares per leg when live
        if shares < 5.0:
            shares = 5.0
            budget = shares * pair
        if budget > self.balance * 0.95:
            shares = (self.balance * 0.95) / pair
            budget = shares * pair
        if shares < 5.0 or budget > self.balance:
            return []
        arb_id = uuid.uuid4().hex[:10]
        opened: list[dict] = []
        tilt = str(cand.get("tilt") or "").upper()
        # Optional small tilt: +10% shares on preferred side, -10% other (still both legs)
        up_sh, down_sh = shares, shares
        if tilt == "UP":
            up_sh *= 1.1
            down_sh *= 0.9
        elif tilt == "DOWN":
            down_sh *= 1.1
            up_sh *= 0.9
        for leg, sh in ((up, up_sh), (down, down_sh)):
            leg_cand = dict(cand)
            leg_cand["side"] = leg["side"]
            leg_cand["token_id"] = leg["token_id"]
            leg_cand["price"] = leg["price"]
            leg_cand["both_legs"] = False
            leg_cand["legs"] = None
            leg_cand["reason"] = "edge:complementary_arb_pair"
            leg_cand["hold_to_resolve"] = True
            leg_cand["prefer_limit"] = True
            leg_cand["arb_pair"] = True
            leg_cand["arb_id"] = arb_id
            cost = float(sh) * float(leg["price"])
            # Bypass normal fill slip for arb — use book price
            mid = float(leg["price"])
            if mid <= 0.01 or mid >= 0.99:
                continue
            entry = mid
            pos = {
                "id": uuid.uuid4().hex[:12],
                "title": cand.get("title") or "",
                "market_slug": cand.get("market_slug") or "",
                "event_slug": cand.get("event_slug") or "",
                "url": cand.get("url") or "",
                "condition_id": cand.get("condition_id") or "",
                "token_id": str(leg["token_id"]),
                "side": str(leg["side"]).upper(),
                "entry": entry,
                "mark": entry,
                "shares": float(sh),
                "cost": cost,
                "upnl": 0.0,
                "opened_at": time.time(),
                "source": "edge",
                "reason": "edge:complementary_arb_pair",
                "copy_trader": "",
                "confidence": float(cand.get("confidence") or advice.get("confidence") or 0.8),
                "secs_left": cand.get("secs_left"),
                "end_date": cand.get("end_date") or "",
                "window_ts": cand.get("window_ts"),
                "timeframe": cand.get("timeframe") or "",
                "tp": 0.99,
                "sl": 0.01,
                "category": advice.get("category") or "",
                "setup": advice.get("setup") or "",
                "asset": str(cand.get("asset") or ""),
                "hold_to_resolve": True,
                "arb_pair": True,
                "arb_id": arb_id,
                "prefer_limit": True,
                "pair_sum": pair,
                "grade": "C",
                "mfe": 0.0,
                "mae": 0.0,
                "roi": 0.0,
                "path_len": 1,
            }
            if cost > self.balance:
                for p in list(opened):
                    self.balance += float(p.get("cost") or 0) + float(p.get("entry_fee") or 0)
                    self.fees_paid = max(0.0, self.fees_paid - float(p.get("entry_fee") or 0))
                    self.positions = [x for x in self.positions if x.get("id") != p.get("id")]
                return []
            fee = self._taker_fee(float(sh), entry, category="crypto")
            if cost + fee > self.balance:
                for p in list(opened):
                    self.balance += float(p.get("cost") or 0) + float(p.get("entry_fee") or 0)
                    self.fees_paid = max(0.0, self.fees_paid - float(p.get("entry_fee") or 0))
                    self.positions = [x for x in self.positions if x.get("id") != p.get("id")]
                return []
            pos["entry_fee"] = fee
            pos["fees"] = fee
            self.balance -= cost + fee
            self.fees_paid += fee
            self.positions.append(pos)
            bet_tracker.ensure(pos)
            self._maybe_clob_order(pos, size_usd=cost, entry=entry)
            exec_mode = str(self.params.values.get("exec_mode") or "paper").lower().strip()
            armed = bool(self.params.values.get("live_trading_armed"))
            if exec_mode == "live" and armed:
                clob = pos.get("clob") or {}
                if not clob.get("posted"):
                    self._rollback_failed_live_open(pos)
                    for p in list(opened):
                        self._rollback_failed_live_open(p)
                    return []
            opened.append(pos)
            alerts.emit(
                "TAKE",
                pos["title"],
                detail=f"ARB {pos['side']} @ {entry:.3f} size=${cost:.2f} pair={pair:.3f}",
                side=pos["side"],
                price=entry,
                size_usd=cost,
                reason="complementary_arb_pair",
                confidence=pos["confidence"],
                market_slug=pos["market_slug"],
                event_slug=pos.get("event_slug") or "",
                url="",
                speak=f"Arb pair {pos['side']} on {str(pos['title'])[:50]}",
                data={"arb_id": arb_id, "pair_sum": pair, "exec_mode": exec_mode},
            )
        self.cooldowns[str(cand.get("market_slug") or cand.get("title") or "")] = time.time()
        self._sync_equity()
        if opened:
            self.memory.add_lesson(
                f"ARB PAIR opened id={arb_id} sum={pair:.3f} edge={1-pair:.3f} legs={len(opened)}",
                source="arb",
            )
        return opened

    def _paper_stats_for_gate(self) -> dict:
        trades = list(self.memory.data.get("trades") or [])
        lag_only = bool(self.params.values.get("lag_only", True))
        lag_trades = [
            t
            for t in trades
            if "spot_lag" in str(t.get("reason") or "")
            or "spot_lag" in str(t.get("edge") or "")
        ]
        # When lag-only seating, gate on lag book so arm UX matches what we trade
        use = lag_trades if (lag_only and lag_trades) else trades
        n = len(use)
        wins = sum(1 for t in use if float(t.get("pnl") or 0) > 0)
        pnl = sum(float(t.get("pnl") or 0) for t in use[-80:])
        regime = skillbook.status().get("regime") or {}
        return {
            "trade_count": n,
            "n": n,
            "win_rate": (wins / n) if n else None,
            "pnl": pnl,
            "recent_pnl": pnl,
            "regime_n": int(regime.get("n") or 0),
            "lag_only_gate": bool(lag_only and lag_trades),
            "lag_n": len(lag_trades),
            "all_n": len(trades),
        }

    def _maybe_clob_order(self, pos: dict, *, size_usd: float, entry: float) -> None:
        """Optional CLOB path. Default paper — never spends unless armed + live."""
        mode = str(self.params.values.get("exec_mode") or "paper").lower().strip()
        if mode in ("", "paper", "off"):
            return
        token = str(pos.get("token_id") or "")
        if not token or entry <= 0:
            return
        shares = float(pos.get("shares") or 0) or (size_usd / entry if entry else 0)
        armed = bool(self.params.values.get("live_trading_armed"))
        urgent = bool(pos.get("urgent_fak")) or "spot_lag" in str(
            pos.get("reason") or ""
        )
        prefer_limit = (
            False
            if urgent
            else (
                bool(pos.get("prefer_limit"))
                and bool(self.params.values.get("prefer_limit_orders", True))
            )
        )
        # Cap live size to real CLOB collateral so we never overspend the wallet
        if mode == "live" and armed:
            try:
                # Refresh allowance before lag FAK — cold allowance rejects buys
                if urgent:
                    live_exec.prepare_collateral(force=False)
                bal = live_exec.fetch_balance(max_age_sec=3.0 if urgent else 0.0)
                usd = bal.get("balance_usd")
                if usd is not None and float(usd) >= 0:
                    max_usd = float(usd) * 0.95
                    if size_usd > max_usd + 1e-9:
                        if max_usd < float(self.params.values.get("min_bet_usd") or 1.0):
                            pos["clob"] = {
                                "ok": False,
                                "posted": False,
                                "mode": "live",
                                "msg": f"CLOB balance ${float(usd):.2f} below min bet — fund funder",
                                "side": "BUY",
                                "urgent": urgent,
                            }
                            return
                        size_usd = max_usd
                        shares = size_usd / entry
                        pos["shares"] = shares
                        # Adjust paper cost to match what we will post
                        delta = float(pos.get("cost") or 0) - size_usd
                        if delta > 0:
                            self.balance += delta
                            pos["cost"] = size_usd
            except Exception as e:
                pos["clob"] = {
                    "ok": False,
                    "posted": False,
                    "mode": "live",
                    "msg": f"balance check failed: {e}"[:160],
                    "side": "BUY",
                    "urgent": urgent,
                }
                return
        try:
            result = live_exec.place_order(
                token_id=token,
                side="BUY",
                price=float(entry),
                size=float(shares),
                mode=mode,
                armed=armed,
                paper_stats=self._paper_stats_for_gate(),
                prefer_limit=prefer_limit,
                urgent=urgent,
            )
            pos["clob"] = {
                "mode": result.get("mode"),
                "posted": bool(result.get("posted")),
                "ok": bool(result.get("ok")),
                "msg": str(result.get("msg") or "")[:160],
                "side": "BUY",
                "built": result.get("built"),
                "build_kind": result.get("build_kind"),
                "notional": result.get("notional"),
                "urgent": urgent,
                "order_path": result.get("used") or ("FAK" if urgent else "limit"),
            }
            # Sync seat to what was actually posted (size may bump for $1 min)
            if result.get("posted") and result.get("size"):
                new_shares = float(result["size"])
                new_px = float(result.get("price") or entry)
                old_cost = float(pos.get("cost") or size_usd)
                new_cost = new_shares * new_px
                delta = new_cost - old_cost
                if delta > 0 and delta <= self.balance:
                    self.balance -= delta
                    pos["cost"] = new_cost
                    pos["shares"] = new_shares
                    pos["entry"] = new_px
                elif delta <= 0:
                    self.balance += (-delta)
                    pos["cost"] = new_cost
                    pos["shares"] = new_shares
                    pos["entry"] = new_px
            if mode == "live" and result.get("posted"):
                self.memory.add_lesson(
                    f"CLOB LIVE BUY {pos.get('side')} {pos.get('title','')[:50]} "
                    f"@ {float(result.get('price') or entry):.3f} size={float(result.get('size') or shares):.4f}",
                    source="live_exec",
                )
            elif mode == "dry_run":
                self.memory.add_lesson(
                    f"CLOB dry_run BUY {pos.get('side')} {pos.get('title','')[:50]} "
                    f"@ {entry:.3f} ({result.get('msg')})",
                    source="live_exec",
                )
        except Exception as e:
            pos["clob"] = {"ok": False, "msg": str(e)[:160], "mode": mode, "side": "BUY"}

    def _maybe_clob_exit(
        self, pos: dict, *, mark: float, shares: float, reason: str = ""
    ) -> dict:
        """CLOB sell on close when dry_run/live. Paper skips. Claims cross the book."""
        mode = str(self.params.values.get("exec_mode") or "paper").lower().strip()
        if mode in ("", "paper", "off"):
            return {}
        token = str(pos.get("token_id") or "")
        if not token or shares <= 0 or mark <= 0:
            return {"ok": False, "msg": "missing token/size for exit", "mode": mode}
        armed = bool(self.params.values.get("live_trading_armed"))
        # Always allow CLOB exit for seats that actually posted a live buy
        live_posted = bool((pos.get("clob") or {}).get("posted")) and str(
            (pos.get("clob") or {}).get("mode") or ""
        ) == "live"
        if live_posted:
            mode = "live"
            armed = True
        r = str(reason or "")
        # LIVE trail/TP/SL/claim: always FAK — never rest a GTC while the book moves
        urgent = bool(pos.get("urgent_fak")) or "spot_lag" in str(
            pos.get("reason") or ""
        )
        if mode == "live" and armed:
            urgent = True
        elif r in ("trail_sl", "sl", "tp", "open_abort") or r.startswith("claim"):
            urgent = True
        # Aggressive sell for trail/force exits so FAK fills before stop runs
        px = float(mark)
        if r.startswith("claim") or r in (
            "tp",
            "max_hold",
            "settle_win",
            "trail_sl",
            "sl",
            "open_abort",
        ):
            if r in ("trail_sl", "sl", "open_abort"):
                # Cross hard for stop fills — speed > price improvement
                px = max(0.01, mark - 0.06)
            elif mark >= 0.90:
                px = 0.88
            elif mark >= 0.70:
                px = max(0.40, mark - 0.08)
            elif mark >= 0.40:
                px = max(0.10, mark - 0.06)
            else:
                px = max(0.01, mark - 0.04)
        px = max(0.01, min(0.99, px))
        # Venue sell FAK makerAmount (=shares) max 2 decimals
        sell_shares = float(int(float(shares) * 100) / 100.0)
        if sell_shares < 0.01:
            sell_shares = float(shares)
        try:
            result = live_exec.exit_order(
                token_id=token,
                price=px,
                size=float(sell_shares),
                mode=mode,
                armed=armed,
                paper_stats=self._paper_stats_for_gate(),
                urgent=urgent,
            )
            out = {
                "mode": result.get("mode"),
                "posted": bool(result.get("posted")),
                "ok": bool(result.get("ok")),
                "msg": str(result.get("msg") or "")[:160],
                "side": "SELL",
                "built": result.get("built"),
                "px": px,
                "price": result.get("price") or px,
                "size": result.get("size") or sell_shares,
                "order_path": result.get("used"),
                "reason": r,
            }
            if mode == "live" and result.get("posted"):
                self.memory.add_lesson(
                    f"CLOB LIVE SELL {pos.get('side')} {pos.get('title','')[:50]} "
                    f"@ {px:.3f} size={float(result.get('size') or sell_shares):.2f} ({r or 'exit'})",
                    source="live_exec",
                )
            elif mode == "dry_run":
                self.memory.add_lesson(
                    f"CLOB dry_run SELL {pos.get('side')} {pos.get('title','')[:50]} "
                    f"@ {px:.3f} ({result.get('msg')}) ({r or 'exit'})",
                    source="live_exec",
                )
            return out
        except Exception as e:
            return {"ok": False, "msg": str(e)[:160], "mode": mode, "side": "SELL"}

    def _close(self, pos: dict, reason: str) -> dict:
        # Prefer mark just refreshed this loop — skip another REST midpoint on trail exits
        mark = float(pos.get("mark") or 0.0)
        if mark <= 0.01 or str(reason or "") not in (
            "trail_sl",
            "sl",
            "tp",
            "open_abort",
            "claim_win",
            "claim_loss",
            "claim_early",
            "claim_bank",
            "claim_time",
        ):
            mark = self._mark_price(pos)
        pos["mark"] = mark
        entry = float(pos.get("entry") or 0.0)
        shares = float(pos.get("shares") or 0.0)
        cost = float(pos.get("cost") or 0.0)
        if shares <= 0 and entry > 0 and cost > 0:
            shares = cost / entry
            pos["shares"] = shares
        # CLOB exit BEFORE bookkeeping so deck CLOSE/TP/SL/claim hit the exchange path
        clob_exit = self._maybe_clob_exit(pos, mark=mark, shares=shares, reason=reason)
        if clob_exit:
            pos["clob_exit"] = clob_exit
        clob_entry = pos.get("clob") or {}
        live_seat = (
            bool(clob_entry.get("posted"))
            and str(clob_entry.get("mode") or "") == "live"
            and not str(reason or "").startswith("claim")
        )
        # LIVE seat: never book paper PnL if the CLOB sell did not post —
        # unless CLOB is already flat (dust / no conditional balance) or mark wiped.
        if live_seat and clob_exit and not clob_exit.get("posted"):
            fail_msg = str(clob_exit.get("msg") or "").lower()
            already_flat = any(
                x in fail_msg
                for x in (
                    "dust",
                    "no conditional balance",
                    "sell size 0",
                    "not enough balance",
                    "not enough balance / allowance",
                )
            ) or mark <= 0.02
            if already_flat:
                # Tokens gone (sold/resolved/dust) — free the seat so lag can snipe
                clob_exit = {
                    **clob_exit,
                    "posted": True,
                    "ok": True,
                    "flat_reconcile": True,
                    "price": mark,
                    "size": 0.0,
                    "msg": f"reconcile flat: {clob_exit.get('msg') or 'no balance'}"[:160],
                }
                pos["clob_exit"] = clob_exit
                # Book wipeout / dust exit at mark (usually ~0)
            else:
                fails = int(pos.get("exit_fail_count") or 0) + 1
                pos["exit_fail_count"] = fails
                pos["exit_failed"] = True
                pos["exit_fail_ts"] = time.time()
                pos["exit_fail_reason"] = reason
                pos["exit_fail_msg"] = str(clob_exit.get("msg") or "")[:160]
                # After repeated hard fails, stop blocking seats forever
                if fails >= 8:
                    clob_exit = {
                        **clob_exit,
                        "posted": True,
                        "ok": True,
                        "flat_reconcile": True,
                        "price": mark,
                        "size": 0.0,
                        "msg": f"reconcile after {fails} exit fails: {pos['exit_fail_msg']}"[:160],
                    }
                    pos["clob_exit"] = clob_exit
                else:
                    # Throttle voice/alerts — was spamming and filling max_positions
                    last_alert = float(pos.get("exit_fail_alert_ts") or 0)
                    if time.time() - last_alert >= 20.0:
                        pos["exit_fail_alert_ts"] = time.time()
                        self.memory.add_lesson(
                            f"EXIT FAIL kept open {pos.get('side')} "
                            f"{str(pos.get('title') or '')[:50]} ({reason}): "
                            f"{pos['exit_fail_msg']}",
                            source="live_exec",
                        )
                        alerts.emit(
                            "EXIT_FAIL",
                            str(pos.get("title") or ""),
                            detail=(
                                f"CLOB sell failed — seat kept open ({reason}): "
                                f"{pos['exit_fail_msg']}"
                            ),
                            side=str(pos.get("side") or ""),
                            price=mark,
                            size_usd=cost,
                            reason=reason,
                            confidence=float(pos.get("confidence") or 0),
                            market_slug=str(pos.get("market_slug") or ""),
                            event_slug=str(pos.get("event_slug") or ""),
                            speak=(
                                f"Exit failed on {str(pos.get('title') or '')[:50]}. "
                                f"Position still open."
                            ),
                            data={"position_id": pos.get("id"), "clob_exit": clob_exit},
                        )
                    self._sync_equity()
                    self.save()
                    return {"ok": False, "kept_open": True, "clob_exit": clob_exit}
        # If live sell filled a different size, book that fill
        if (
            live_seat
            and clob_exit.get("posted")
            and clob_exit.get("size")
            and not clob_exit.get("flat_reconcile")
        ):
            try:
                fill_shares = float(clob_exit["size"])
                if fill_shares > 0:
                    shares = fill_shares
                    pos["shares"] = shares
            except Exception:
                pass
        entry_fee = float(pos.get("entry_fee") or 0.0)
        # Prefer CLOB fill price when live exit posted
        exit_px = mark
        if live_seat and clob_exit.get("posted") and clob_exit.get("price"):
            try:
                exit_px = float(clob_exit["price"]) or mark
            except Exception:
                exit_px = mark
        if clob_exit.get("flat_reconcile"):
            # Inventory already gone on CLOB — don't invent sell proceeds
            exit_fee = 0.0
            proceeds_gross = 0.0
            proceeds = 0.0
            pnl = float(0.0 - cost - entry_fee)
            reason = f"{reason}_flat" if reason and not str(reason).endswith("_flat") else reason
        else:
            exit_fee = self._taker_fee(
                shares, mark, category=str(pos.get("category") or "crypto")
            )
            # Resolution / claim_win near $1: selling may be redeem-like; still charge
            # taker fee on book exits (paper assumes sell). Dust claim_loss same.
            proceeds_gross = exit_px * shares
            proceeds = proceeds_gross - exit_fee
            pnl = proceeds_gross - cost - entry_fee - exit_fee
            pnl = float(pnl)
        self.balance += proceeds
        self.fees_paid += exit_fee
        trade = {
            **pos,
            "exit": exit_px,
            "proceeds": proceeds,
            "proceeds_gross": proceeds_gross,
            "entry_fee": entry_fee,
            "exit_fee": exit_fee,
            "fees": entry_fee + exit_fee,
            "pnl": pnl,
            "realized_pnl": pnl,
            "pnl_gross": proceeds_gross - cost,
            "exit_reason": reason,
            "ts_closed": time.time(),
            "held_sec": time.time() - float(pos.get("opened_at") or time.time()),
            "clob_exit": clob_exit or pos.get("clob_exit") or {},
        }
        self.positions = [p for p in self.positions if p.get("id") != pos.get("id")]
        self.closed.append(trade)
        # LIVE seats: re-key paper cash to CLOB so phantom closes can't drift the book
        if live_seat or (
            str(self.params.values.get("exec_mode") or "").lower() == "live"
            and bool(self.params.values.get("live_trading_armed"))
        ):
            try:
                mirrored = self.mirror_paper_to_live(set_start=False, max_age_sec=0.0)
                if mirrored.get("ok") and clob_exit.get("flat_reconcile"):
                    # Replace invented wipeout PnL with wallet-delta estimate
                    try:
                        before = float(cost)
                        after_eq = float(mirrored.get("equity") or self.equity)
                        # keep trade audit, but don't leave paper stranded
                        trade["pnl_ledger"] = pnl
                        trade["mirrored_equity"] = after_eq
                        trade["flat_reconcile"] = True
                    except Exception:
                        pass
            except Exception:
                self._sync_equity()
        else:
            self._sync_equity()
        self.memory.record_trade(trade)
        life = bet_tracker.close_life(
            pos,
            trade,
            skillbook=skillbook if self.params.values.get("continuous_learning", True) else None,
            memory=self.memory,
        )
        trade["grade"] = life.get("grade")
        trade["mfe_roi"] = life.get("mfe_roi")
        trade["mae_roi"] = life.get("mae_roi")
        trade["path"] = (life.get("path") or [])[-40:]
        # Mirror final pnl onto life for live-bets widget
        try:
            life["pnl"] = pnl
            life["final_roi"] = pnl / (cost or 1.0)
        except Exception:
            pass
        if self.params.values.get("continuous_learning", True):
            self.memory.add_lesson(
                f"Closed {pos.get('side')} on {str(pos.get('title') or '')[:70]} "
                f"pnl={pnl:+.2f} grade={life.get('grade')} "
                f"MFE={float(life.get('mfe_roi') or 0):+.0%} "
                f"MAE={float(life.get('mae_roi') or 0):+.0%} ({reason})",
                source="engine",
                market=str(pos.get("market_slug") or ""),
                trader=str(pos.get("copy_trader") or ""),
            )
        speak = (
            f"Closed {pos.get('side')} on {str(pos.get('title') or '')[:60]}. "
            f"{'Profit' if pnl >= 0 else 'Loss'} {abs(pnl):.2f} dollars."
        )
        alerts.emit(
            "CLOSE",
            str(pos.get("title") or ""),
            detail=f"pnl={pnl:+.2f} via {reason}",
            side=str(pos.get("side") or ""),
            price=mark,
            size_usd=cost,
            reason=reason,
            confidence=float(pos.get("confidence") or 0),
            market_slug=str(pos.get("market_slug") or ""),
            event_slug=str(pos.get("event_slug") or ""),
            url="",
            copy_trader=str(pos.get("copy_trader") or ""),
            speak=speak,
            data={
                "pnl": pnl,
                "position_id": pos.get("id"),
                "exec_mode": exec_mode,
                "clob_exit": clob_exit or {},
            },
        )
        return trade

    def close_all(self, reason: str = "user") -> int:
        with self._lock:
            n = 0
            for pos in list(self.positions):
                self._close(pos, reason)
                n += 1
            self._mark_all(learn=False)
            self.save()
            return n

    def close_id(self, pid: str, reason: str = "user") -> bool:
        with self._lock:
            for pos in list(self.positions):
                if pos.get("id") == pid:
                    self._close(pos, reason)
                    self._mark_all(learn=False)
                    self.save()
                    return True
            return False

    def tick(self) -> dict[str, Any]:
        with self._lock:
            return self._tick_unlocked()

    def tick_lag_fast(self, cands: list[dict] | None = None) -> dict[str, Any]:
        """Immediate open path for spot_lag hits — no full board ranking delay."""
        with self._lock:
            p = self.params.values
            if not p.get("trading_enabled", True) or not p.get("lag_snipe", True):
                return {"opened": 0, "skipped": 0, "hot": True}
            opened = 0
            skipped = 0
            rows = list(cands or [])
            if not rows:
                return {"opened": 0, "skipped": 0, "hot": True}
            max_pos = int(p.get("max_positions") or 2)
            min_bet = float(p.get("min_bet_usd") or 2.5)
            open_slugs = {
                (x.get("market_slug") or x.get("title")) for x in self.positions
            }
            for cand in rows:
                if len(self.positions) >= max_pos:
                    break
                slug = str(cand.get("market_slug") or cand.get("title") or "")
                if not slug or slug in open_slugs:
                    skipped += 1
                    continue
                if time.time() - float(self.cooldowns.get(slug) or 0) < float(
                    p.get("cooldown_sec") or 45
                ):
                    skipped += 1
                    continue
                conf = float(cand.get("confidence") or 0.5)
                advice = skillbook.advice(
                    trader="",
                    title=str(cand.get("title") or ""),
                    reason=str(cand.get("reason") or ""),
                    confidence=conf,
                    side=str(cand.get("side") or ""),
                    timeframe=str(cand.get("timeframe") or ""),
                    asset=str(cand.get("asset") or ""),
                    market_slug=slug,
                    price=float(cand.get("price") or 0) or None,
                )
                setup_n = int(advice.get("setup_n") or 0)
                setup_exp = float(advice.get("setup_expectancy") or 0.0)
                if setup_n >= 6 and setup_exp < 0:
                    skipped += 1
                    continue
                advice = dict(advice)
                if setup_n >= 4 and setup_exp > 0:
                    advice["size_mult"] = max(
                        float(advice.get("size_mult") or 1.0), 1.15
                    )
                sized = self._size_seat(
                    advice=advice,
                    confidence=conf,
                    reason=str(cand.get("reason") or ""),
                )
                size = float(sized.get("stake") or 0.0)
                if not sized.get("ok") or size < min_bet:
                    skipped += 1
                    continue
                cand = dict(cand)
                cand["urgent_fak"] = True
                cand["prefer_limit"] = False
                cand["_tp_delta"] = float(p.get("tp_price_delta") or 0.16)
                cand["_sl_delta"] = 0.14
                cand["sizing"] = {
                    "stake": size,
                    "base_frac": sized.get("base_frac"),
                    "growth": sized.get("growth"),
                    "reasons": sized.get("reasons"),
                }
                pos = self._open(cand, size, advice)
                if pos:
                    opened += 1
                    open_slugs.add(slug)
                else:
                    skipped += 1
            if opened:
                self.save()
                self._record_equity_curves()
            return {"opened": opened, "skipped": skipped, "hot": True}

    def _tick_unlocked(self) -> dict[str, Any]:
        p = self.params.values
        opened = 0
        closed = 0
        skipped = 0
        try:
            # Manage open seats (marks + path learning happen in _mark_all / mark_live)
            self._mark_all(learn=True)
            for pos in list(self.positions):
                age = time.time() - float(pos.get("opened_at") or time.time())
                reason = self._maybe_auto_claim_close(pos)
                if reason:
                    self._close(pos, reason)
                    closed += 1
                elif age >= self._hold_limit_sec(pos):
                    self._close(pos, "max_hold")
                    closed += 1

            self._mark_all(learn=True)

            if not p.get("trading_enabled", True):
                self.last_loop_ts = time.time()
                self.loop_count += 1
                self.save()
                self._record_equity_curves()
                return {"opened": 0, "closed": closed, "skipped": 0, "armed": False}

            # High-prob short crypto first; copy optional
            cands = edge_scanner.pop_candidates(12)
            if (
                p.get("copy_enabled", False)
                and not p.get("short_crypto_only", True)
                and not p.get("lag_only", True)
            ):
                cands = trader_streamer.pop_candidates(8) + cands
            # Lag-only mode: refuse every non-spot_lag candidate
            if bool(p.get("lag_only", True)):
                cands = [
                    c
                    for c in cands
                    if "spot_lag" in str(c.get("reason") or "")
                ]
            # Only notify / paper-take high probability
            cands = [
                c for c in cands
                if float(c.get("confidence") or 0) >= float(p.get("min_confidence") or 0.72)
                or c.get("high_prob")
            ]
            # Score + sort by learned expectancy; prefer fair_odds_up / spot_lag
            ranked: list[tuple[float, dict, dict]] = []
            for cand in cands:
                conf = float(cand.get("confidence") or 0.5)
                reason0 = str(cand.get("reason") or "")
                side0 = str(cand.get("side") or "").upper()
                advice = skillbook.advice(
                    trader=str(cand.get("copy_trader") or ""),
                    title=str(cand.get("title") or ""),
                    reason=reason0,
                    confidence=conf,
                    side=side0,
                    timeframe=str(cand.get("timeframe") or ""),
                    asset=str(cand.get("asset") or ""),
                    market_slug=str(cand.get("market_slug") or ""),
                    price=float(cand.get("price") or 0) or None,
                )
                score = float(advice.get("score") or conf)
                if "fair_odds_up" in reason0 or (
                    "fair_odds" in reason0 and side0 == "UP"
                ):
                    score += 0.15
                if "spot_lag" in reason0:
                    score += 0.12
                if "short_5m" in reason0 or (
                    "momentum" in reason0 and str(cand.get("timeframe") or "") == "5m"
                ):
                    score -= 0.05
                ranked.append((score, cand, advice))
            ranked.sort(key=lambda x: -x[0])
            self.signals = [
                {
                    **c,
                    "learn_score": sc,
                    "setup": adv.get("setup"),
                    "setup_expectancy": adv.get("setup_expectancy"),
                    "size_mult": adv.get("size_mult"),
                }
                for sc, c, adv in ranked[:20]
            ]
            max_pos = int(p.get("max_positions") or 2)
            min_conf = float(p.get("min_confidence") or 0.72)
            min_bet = float(p.get("min_bet_usd") or 1.0)
            cooldown = float(p.get("cooldown_sec") or 45)

            open_slugs = {
                (x.get("market_slug") or x.get("title")) for x in self.positions
            }

            for _score, cand, advice in ranked:
                need_seats = 2 if cand.get("both_legs") else 1
                if len(self.positions) + need_seats > max(int(p.get("max_positions") or 2), need_seats):
                    break
                slug = str(cand.get("market_slug") or cand.get("title") or "")
                if not slug or slug in open_slugs:
                    continue
                if time.time() - float(self.cooldowns.get(slug) or 0) < cooldown:
                    skipped += 1
                    continue
                # Anti-churn: skip near-expiry entries (claim_time fee burn)
                min_left = float(p.get("min_entry_secs_left") or 120.0)
                try:
                    left = float(cand.get("secs_left") or 0)
                except Exception:
                    left = 0.0
                # Endgame lag snipes intentionally enter inside the floor
                _reason = str(cand.get("reason") or "")
                if (
                    left > 0
                    and left < min_left
                    and "complementary_arb" not in _reason
                    and "spot_lag_endgame" not in _reason
                ):
                    skipped += 1
                    continue
                conf = float(cand.get("confidence") or 0.5)
                reason = str(cand.get("reason") or "")
                # Structure edges use their own edge thresholds; don't let
                # auto-tuned min_confidence (often > honest conf caps) starve them.
                _struct = (
                    "complementary_arb",
                    "near_resolution",
                    "fair_odds",
                    "cross_timeframe",
                    "spot_lag",
                    "lag_snipe",
                )
                is_struct = any(x in reason for x in _struct)
                if (
                    conf < min_conf
                    and not is_struct
                    and not cand.get("high_prob")
                ):
                    skipped += 1
                    continue  # quiet skip — only notify high-prob TAKEs
                # Hard-gate short_5m momentum to proven cells (skillbook + reason)
                tf0 = str(cand.get("timeframe") or "").lower()
                side_u = str(cand.get("side") or "").upper()
                asset_u = str(cand.get("asset") or "").upper()
                if "short_5m" in reason or (
                    "momentum" in reason and tf0 == "5m"
                ):
                    px_band = skillbook.entry_band(float(cand.get("price") or 0) or None)
                    ok_m = (
                        asset_u == "BTC"
                        and side_u == "DOWN"
                        and px_band in ("cheap", "lean", "rich", "mid")
                    ) or (side_u == "DOWN" and px_band in ("lean", "rich"))
                    if not ok_m:
                        skipped += 1
                        continue
                # Allowlist + hard veto (no soft explore on proven cold)
                if bool(p.get("setup_allowlist", True)):
                    setup_key = str(advice.get("setup") or "")
                    ok_al, why = skillbook.setup_allowed(
                        setup_key, allowlist=True
                    )
                    if not ok_al and not is_struct:
                        skipped += 1
                        continue
                # Skip proven-negative expectancy unless structure/lag
                setup_n = int(advice.get("setup_n") or 0)
                setup_exp = float(advice.get("setup_expectancy") or 0.0)
                if (
                    setup_n >= 6
                    and setup_exp < 0
                    and not is_struct
                ):
                    skipped += 1
                    continue
                advice = dict(advice)
                if setup_n >= 4 and setup_exp > 0:
                    advice["size_mult"] = max(
                        float(advice.get("size_mult") or 1.0), 1.15
                    )
                if advice.get("veto") and not is_struct:
                    if cand.get("high_prob"):
                        # Board already marked high-prob — shrink, don't hard-block
                        advice["size_mult"] = min(
                            float(advice.get("size_mult") or 1.0), 0.55
                        )
                    else:
                        skipped += 1
                        continue
                # Dual-leg arb pair takes priority seats
                if cand.get("both_legs") and cand.get("legs"):
                    sized = self._size_seat(
                        advice=advice,
                        confidence=conf,
                        reason=reason,
                        need_seats=2,
                    )
                    budget = float(sized.get("stake") or 0.0)
                    if not sized.get("ok") or budget < min_bet * 2:
                        skipped += 1
                        continue
                    pair_pos = self._open_arb_pair(cand, budget, advice)
                    if pair_pos:
                        opened += len(pair_pos)
                        open_slugs.add(slug)
                    else:
                        skipped += 1
                    continue
                sized = self._size_seat(
                    advice=advice,
                    confidence=conf,
                    reason=reason,
                )
                size = float(sized.get("stake") or 0.0)
                if not sized.get("ok") or size < min_bet or size > self.balance:
                    skipped += 1
                    continue
                # Hard price band — edge scanner can race; cheap UP lottery bled the 12m curve
                px = float(cand.get("price") or 0)
                if "spot_lag" in reason:
                    lo = float(p.get("lag_min_price") or 0.42)
                    hi = float(p.get("lag_max_price") or 0.82)
                    if "endgame" in reason:
                        lo = float(p.get("lag_endgame_min_price") or 0.72)
                        hi = 0.94
                    if px < lo or px > hi:
                        skipped += 1
                        continue
                elif "fair_odds" in reason:
                    lo = float(p.get("fair_min_price") or 0.48)
                    hi = float(p.get("fair_max_price") or 0.74)
                    if px < lo or px > hi:
                        skipped += 1
                        continue
                elif reason.startswith("edge:short_") or "momentum" in reason:
                    # Momentum: lean/rich only — no cheap lottery
                    if px < 0.48 or px > 0.78:
                        skipped += 1
                        continue
                    if str(cand.get("side") or "").upper() == "UP" and px < 0.58:
                        skipped += 1
                        continue
                # Asymmetric brackets — TP wider than SL so winners clear fees
                tf = str(cand.get("timeframe") or "")
                if tf in ("1m", "5m", "15m"):
                    cand = dict(cand)
                    if "spot_lag" in reason:
                        cand["_tp_delta"] = float(p.get("tp_price_delta") or 0.16)
                        cand["_sl_delta"] = 0.14  # noise buffer for lag thesis
                        cand["prefer_limit"] = False
                    else:
                        cand["_tp_delta"] = (
                            float(p.get("tp_price_delta") or 0.16)
                            if tf in ("5m", "15m")
                            else 0.10
                        )
                        cand["_sl_delta"] = (
                            float(p.get("sl_price_delta") or 0.10)
                            if tf in ("5m", "15m")
                            else 0.08
                        )
                        # Non-lag: prefer maker path
                        if bool(p.get("prefer_limit_orders", True)):
                            cand["prefer_limit"] = True
                cand = dict(cand)
                cand["sizing"] = {
                    "stake": size,
                    "base_frac": sized.get("base_frac"),
                    "growth": sized.get("growth"),
                    "reasons": sized.get("reasons"),
                }
                pos = self._open(cand, size, advice)
                if pos:
                    opened += 1
                    open_slugs.add(slug)

            # After closes/opens, gently auto-tune risk/confidence from regime
            if closed or opened:
                try:
                    if bool(p.get("continuous_learning", True)):
                        msgs = skillbook.auto_tune_params(self.params)
                        for m in msgs:
                            self.memory.add_lesson(m, source="auto_tune")
                except Exception:
                    pass

            self._mark_all()
            self.last_error = ""
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
        self.last_loop_ts = time.time()
        self.loop_count += 1
        self.save()
        self._record_equity_curves()
        return {"opened": opened, "closed": closed, "skipped": skipped, "armed": True}

    def _preview_sizing(self) -> dict[str, Any]:
        """Next-seat preview — LIVE+ARMED shows CLOB sizing (what lag actually uses)."""
        try:
            mode = str(self.params.values.get("exec_mode") or "paper").lower().strip()
            armed = bool(self.params.values.get("live_trading_armed"))
            spending = mode == "live" and armed
            # Prefer last live seat size when spending so UI matches hot path
            if spending and self.last_sizing.get("bankroll_source") == "live_clob":
                out = dict(self.last_sizing)
                out["mode"] = str(self.params.values.get("sizing_mode") or "smart")
                out["risk_frac"] = float(self.params.values.get("risk_frac") or 0.35)
                out["grow_above_usd"] = float(
                    self.params.values.get("sizing_grow_above_usd") or 50.0
                )
                out["last"] = dict(self.last_sizing)
            else:
                out = compute_stake(
                    equity=float(self.equity),
                    balance=float(self.balance),
                    start_equity=float(self.start_balance or self.equity),
                    peak_equity=float(self.peak_equity or self.equity),
                    open_cost=open_invested(self.positions),
                    risk_frac=float(self.params.values.get("risk_frac") or 0.35),
                    min_bet=float(self.params.values.get("min_bet_usd") or 2.5),
                    max_bet_usd=float(self.params.values.get("max_bet_usd") or 0.0),
                    max_bet_frac=float(self.params.values.get("max_bet_frac") or 0.45),
                    heat_frac=float(
                        self.params.values.get("portfolio_heat_frac") or 0.70
                    ),
                    size_mult=1.0,
                    confidence=0.75,
                    is_lag=bool(self.params.values.get("lag_only", True)),
                    live_clob_usd=None,
                    sizing_mode=str(self.params.values.get("sizing_mode") or "smart"),
                    grow_above_usd=float(
                        self.params.values.get("sizing_grow_above_usd") or 50.0
                    ),
                )
                out["mode"] = str(self.params.values.get("sizing_mode") or "smart")
                out["risk_frac"] = float(self.params.values.get("risk_frac") or 0.35)
                out["grow_above_usd"] = float(
                    self.params.values.get("sizing_grow_above_usd") or 50.0
                )
                out["bankroll_source"] = "paper"
                out["last"] = dict(self.last_sizing) if self.last_sizing else {}
            # Cached CLOB only — never block /api/state on RPC
            live_prev = None
            try:
                cache = getattr(live_exec, "_bal_cache", None) or {}
                clob = cache.get("balance_usd")
                if clob is not None and float(clob) >= 0:
                    clob_f = float(clob)
                    min_b = float(self.params.values.get("min_bet_usd") or 2.5)
                    grow = float(
                        self.params.values.get("sizing_grow_above_usd") or 50.0
                    )
                    invested = self._live_open_cost() if spending else 0.0
                    live_eq = clob_f + invested
                    live_prev = compute_stake(
                        equity=live_eq,
                        balance=clob_f,
                        start_equity=float(
                            getattr(self, "live_start_equity", 0) or live_eq
                        ),
                        peak_equity=float(
                            getattr(self, "live_peak_equity", 0) or live_eq
                        ),
                        open_cost=invested,
                        risk_frac=float(self.params.values.get("risk_frac") or 0.35),
                        min_bet=min_b,
                        max_bet_usd=float(self.params.values.get("max_bet_usd") or 0.0),
                        max_bet_frac=float(
                            self.params.values.get("max_bet_frac") or 0.45
                        ),
                        heat_frac=float(
                            self.params.values.get("portfolio_heat_frac") or 0.70
                        ),
                        size_mult=1.0,
                        confidence=0.75,
                        is_lag=True,
                        live_clob_usd=clob_f,
                        sizing_mode=str(
                            self.params.values.get("sizing_mode") or "smart"
                        ),
                        grow_above_usd=grow,
                    )
                    if live_eq + 1e-9 < grow:
                        live_prev["stake"] = round(
                            min(float(live_prev.get("stake") or 0), min_b), 4
                        )
                        live_prev["min_bet_phase"] = True
                    live_prev["live_clob_usd"] = clob_f
                    live_prev["bankroll_source"] = "live_clob"
            except Exception:
                live_prev = None
            out["live_if_armed"] = live_prev
            # LIVE+ARMED: surface the stake lag will actually use
            if spending and live_prev:
                for k, v in live_prev.items():
                    if k != "live_if_armed":
                        out[k] = v
                out["bankroll_source"] = "live_clob"
            return out
        except Exception as e:
            return {"ok": False, "stake": 0.0, "why": str(e)[:80]}

    def status(self) -> dict[str, Any]:
        # Never hit the network here — broadcaster /api/state call this on the
        # event loop. Marks are refreshed by mark_live / tick workers.
        mode = str(self.params.values.get("exec_mode") or "paper").lower().strip()
        armed = bool(self.params.values.get("live_trading_armed"))
        spending = mode == "live" and armed
        # LIVE+ARMED: overlay free cash from cached CLOB so header can't drift
        if spending:
            try:
                cache = getattr(live_exec, "_bal_cache", None) or {}
                clob = cache.get("balance_usd")
                if clob is not None and float(clob) >= 0:
                    self.balance = float(clob)
            except Exception:
                pass
        upnl = self._sync_equity()
        if spending:
            live_eq = float(self.equity)
            self.live_peak_equity = max(float(self.live_peak_equity or 0), live_eq)
            start = float(self.live_start_equity or self.start_balance or live_eq) or 1.0
            peak = float(self.live_peak_equity or self.peak_equity or live_eq)
        else:
            start = float(self.start_balance or 0.0) or 1.0
            peak = float(self.peak_equity or self.equity)
        total_pnl = float(self.equity) - start
        session_realized = sum(float(t.get("pnl") or 0.0) for t in self.closed)
        live = bet_tracker.status()
        # Re-attach tracker seats after restart (positions persist; in-memory lives don't)
        for p in self.positions:
            try:
                bet_tracker.ensure(p)
            except Exception:
                pass
        live = bet_tracker.status()
        # Keep Live Tracker $ in lockstep with Paper Positions (same seat id)
        by_id = {str(p.get("id")): p for p in self.positions}
        for life in live.get("open") or []:
            p = by_id.get(str(life.get("id") or ""))
            if not p:
                continue
            life["last_mark"] = float(p.get("mark") or life.get("last_mark") or 0)
            life["last_upnl"] = float(p.get("upnl") or 0)
            life["last_roi"] = float(p.get("roi") or 0)
            if p.get("grade"):
                life["grade"] = p.get("grade")
            if p.get("mfe_roi") is not None:
                life["mfe_roi"] = p.get("mfe_roi")
            if p.get("mae_roi") is not None:
                life["mae_roi"] = p.get("mae_roi")
            # seed a 2-point path so sparkline isn't empty after rehydrate
            if len(life.get("path") or []) < 2:
                now = time.time()
                entry = float(p.get("entry") or 0)
                mark = float(p.get("mark") or entry)
                life["path"] = [
                    [float(p.get("opened_at") or now), entry, 0.0],
                    [now, mark, float(p.get("upnl") or 0)],
                ]
        # After process restart, in-memory recent_closed is empty — hydrate from
        # persisted bet_lives / session closed so path PnL still streams.
        if not (live.get("recent_closed") or []):
            persisted = list(self.memory.data.get("bet_lives") or [])[-12:][::-1]
            if persisted:
                live["recent_closed"] = persisted
            else:
                live["recent_closed"] = [
                    {
                        "id": t.get("id"),
                        "title": t.get("title"),
                        "market_slug": t.get("market_slug"),
                        "event_slug": t.get("event_slug"),
                        "url": t.get("url"),
                        "side": t.get("side"),
                        "pnl": t.get("pnl"),
                        "grade": t.get("grade") or "C",
                        "mfe_roi": t.get("mfe_roi"),
                        "mae_roi": t.get("mae_roi"),
                        "exit_reason": t.get("exit_reason"),
                    }
                    for t in list(self.closed)[-12:][::-1]
                ]
        return {
            "equity": float(self.equity),
            "balance": float(self.balance),
            "start_balance": float(start if spending else self.start_balance),
            "peak_equity": float(peak if spending else (self.peak_equity or self.equity)),
            "sizing": self._preview_sizing(),
            "upnl": float(upnl),
            "unrealized_pnl": float(upnl),
            "realized_pnl": float(session_realized),
            "total_pnl": float(total_pnl),
            "roi_pct": (total_pnl / start) * 100.0,
            "bankroll_source": "live_clob" if spending else "paper",
            "fees_paid": float(self.fees_paid),
            "paper_fees": bool(self.params.values.get("paper_fees", True)),
            "paper_fee_rate": float(
                self.params.values.get("paper_fee_rate")
                or poly_fees.DEFAULT_CRYPTO_RATE
            ),
            "invested": sum(float(p.get("cost") or 0.0) for p in self.positions),
            "positions": list(self.positions),
            "position_count": len(self.positions),
            "recent_closed": list(self.closed)[-20:][::-1],
            "closed_count": len(self.closed),
            "signals": list(self.signals)[:15],
            "last_loop_ts": self.last_loop_ts,
            "loop_count": self.loop_count,
            "last_error": self.last_error,
            "trading_enabled": bool(self.params.values.get("trading_enabled")),
            "live_alert_mode": bool(self.params.values.get("live_alert_mode")),
            "live_bets": live,
        }
