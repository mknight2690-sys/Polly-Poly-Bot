"""Real-time bet lifecycle tracker: mark paths, MFE/MAE, live learning ticks."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from . import alerts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVES_PATH = os.path.join(ROOT, "data", "poly_bet_lives.jsonl")


def _grade(roi: float, mfe: float, mae: float) -> str:
    """Path quality label for UI + skill book."""
    if roi >= 0.25 or mfe >= 0.20:
        return "A"
    if roi >= 0.08 or (mfe >= 0.10 and mae > -0.12):
        return "B"
    if roi >= 0.0:
        return "C"
    if mae <= -0.20:
        return "F"
    if roi >= -0.08:
        return "D"
    return "F"


class LiveBetTracker:
    """Follows every open seat tick-by-tick and teaches the skillbook as it evolves."""

    def __init__(self):
        self._lock = threading.Lock()
        self.lives: dict[str, dict[str, Any]] = {}  # position_id -> live record
        self.recent_closed_lives: list[dict] = []
        self.last_mark_ts = 0.0
        self.mark_count = 0

    def ensure(self, pos: dict) -> dict:
        pid = str(pos.get("id") or "")
        with self._lock:
            if pid in self.lives:
                return self.lives[pid]
            entry = float(pos.get("entry") or 0.0)
            now = time.time()
            life = {
                "id": pid,
                "title": pos.get("title") or "",
                "market_slug": pos.get("market_slug") or "",
                "event_slug": pos.get("event_slug") or "",
                "url": pos.get("url") or "",
                "side": pos.get("side") or "",
                "source": pos.get("source") or "",
                "reason": pos.get("reason") or "",
                "copy_trader": pos.get("copy_trader") or "",
                "category": pos.get("category") or "",
                "entry": entry,
                "cost": float(pos.get("cost") or 0.0),
                "shares": float(pos.get("shares") or 0.0),
                "opened_at": float(pos.get("opened_at") or now),
                "path": [[now, entry, 0.0]],  # [ts, mark, upnl]
                "mfe": 0.0,  # best upnl seen
                "mae": 0.0,  # worst upnl seen
                "mfe_roi": 0.0,
                "mae_roi": 0.0,
                "last_mark": entry,
                "last_upnl": 0.0,
                "last_roi": 0.0,
                "grade": "C",
                "milestones": [],  # fired once: green_10, red_10, settle_win, etc.
                "learn_ticks": 0,
                "status": "open",
                "updated_at": now,
            }
            self.lives[pid] = life
            return life

    def _append_archive(self, life: dict):
        try:
            os.makedirs(os.path.dirname(LIVES_PATH), exist_ok=True)
            with open(LIVES_PATH, "a", encoding="utf-8") as f:
                # trim path for disk (keep denser sample)
                row = dict(life)
                path = list(row.get("path") or [])
                if len(path) > 120:
                    step = max(1, len(path) // 120)
                    row["path"] = path[::step][-120:]
                f.write(json.dumps(row) + "\n")
        except Exception:
            pass

    def update_mark(
        self,
        pos: dict,
        mark: float,
        *,
        learn: bool = True,
        skillbook=None,
        memory=None,
    ) -> dict:
        life = self.ensure(pos)
        now = time.time()
        entry = float(life.get("entry") or pos.get("entry") or 0.0)
        shares = float(life.get("shares") or pos.get("shares") or 0.0)
        cost = float(life.get("cost") or pos.get("cost") or 0.0) or 1.0
        if shares <= 0 and entry > 0 and cost > 0:
            shares = cost / entry
            life["shares"] = shares
        upnl = (float(mark) - entry) * shares
        roi = upnl / cost

        events: list[str] = []
        with self._lock:
            life["last_mark"] = float(mark)
            life["last_upnl"] = upnl
            life["last_roi"] = roi
            life["mfe"] = max(float(life.get("mfe") or 0.0), upnl)
            life["mae"] = min(float(life.get("mae") or 0.0), upnl)
            life["mfe_roi"] = life["mfe"] / cost
            life["mae_roi"] = life["mae"] / cost
            life["grade"] = _grade(roi, life["mfe_roi"], life["mae_roi"])
            life["updated_at"] = now
            path = life.setdefault("path", [])
            # sample at most ~every 2s or on meaningful move
            last = path[-1] if path else None
            significant = (
                not last
                or (now - float(last[0]) >= 2.0)
                or abs(float(mark) - float(last[1])) >= 0.005
            )
            if significant:
                path.append([now, float(mark), upnl])
                if len(path) > 400:
                    life["path"] = path[-400:]

            milestones = set(life.get("milestones") or [])
            if roi >= 0.10 and "green_10" not in milestones:
                milestones.add("green_10")
                events.append("green_10")
            if roi >= 0.25 and "green_25" not in milestones:
                milestones.add("green_25")
                events.append("green_25")
            if roi <= -0.10 and "red_10" not in milestones:
                milestones.add("red_10")
                events.append("red_10")
            if roi <= -0.20 and "red_20" not in milestones:
                milestones.add("red_20")
                events.append("red_20")
            # Settlement / near-resolution
            if mark >= 0.97 and "near_win" not in milestones:
                milestones.add("near_win")
                events.append("near_win")
            if mark <= 0.03 and "near_loss" not in milestones:
                milestones.add("near_loss")
                events.append("near_loss")
            life["milestones"] = sorted(milestones)
            life["learn_ticks"] = int(life.get("learn_ticks") or 0) + 1
            snap = dict(life)
            snap["path"] = list(life["path"][-60:])  # UI-sized

        # Soft live learning (does not count as a closed trade win/loss)
        if learn and skillbook is not None and significant:
            try:
                skillbook.record_live_tick(
                    trader=str(pos.get("copy_trader") or ""),
                    title=str(pos.get("title") or ""),
                    reason=str(pos.get("reason") or ""),
                    roi=roi,
                    mfe_roi=float(snap.get("mfe_roi") or 0),
                    mae_roi=float(snap.get("mae_roi") or 0),
                    grade=str(snap.get("grade") or "C"),
                    side=str(pos.get("side") or ""),
                    timeframe=str(pos.get("timeframe") or ""),
                    asset=str(pos.get("asset") or ""),
                    market_slug=str(pos.get("market_slug") or ""),
                    price=float(pos.get("entry") or 0) or None,
                )
            except Exception:
                pass

        for ev in events:
            title = str(pos.get("title") or "")
            if ev.startswith("green"):
                detail = f"Live +{roi*100:.1f}% ROI (MFE {snap['mfe_roi']*100:.1f}%)"
                kind = "LIVE"
                speak = f"Bet improving. {title[:60]}. Up {abs(roi)*100:.0f} percent."
            elif ev.startswith("red"):
                detail = f"Live {roi*100:.1f}% ROI (MAE {snap['mae_roi']*100:.1f}%)"
                kind = "LIVE"
                speak = f"Bet slipping. {title[:60]}. Down {abs(roi)*100:.0f} percent."
            elif ev == "near_win":
                detail = "Near settlement win"
                kind = "LIVE"
                speak = f"Near win on {title[:60]}."
            else:
                detail = "Near settlement loss"
                kind = "LIVE"
                speak = f"Near loss on {title[:60]}."
            alerts.emit(
                kind,
                title,
                detail=detail,
                side=str(pos.get("side") or ""),
                price=float(mark),
                size_usd=float(pos.get("cost") or 0),
                reason=str(pos.get("reason") or ""),
                confidence=float(pos.get("confidence") or 0),
                market_slug=str(pos.get("market_slug") or ""),
                event_slug=str(pos.get("event_slug") or ""),
                url=str(pos.get("url") or ""),
                copy_trader=str(pos.get("copy_trader") or ""),
                speak=speak,
                data={
                    "event": ev,
                    "position_id": pos.get("id"),
                    "roi": roi,
                    "grade": snap.get("grade"),
                    "mfe_roi": snap.get("mfe_roi"),
                    "mae_roi": snap.get("mae_roi"),
                    "url": pos.get("url") or "",
                },
            )
            if memory is not None:
                try:
                    memory.add_lesson(
                        f"Live {ev}: {title[:70]} roi={roi:+.1%} grade={snap.get('grade')}",
                        source="live_track",
                        market=str(pos.get("market_slug") or ""),
                        trader=str(pos.get("copy_trader") or ""),
                    )
                except Exception:
                    pass

        self.last_mark_ts = now
        self.mark_count += 1
        return snap

    def close_life(self, pos: dict, trade: dict, *, skillbook=None, memory=None) -> dict:
        life = self.ensure(pos)
        with self._lock:
            life["status"] = "closed"
            life["exit"] = float(trade.get("exit") or life.get("last_mark") or 0)
            life["pnl"] = float(trade.get("pnl") or life.get("last_upnl") or 0)
            life["exit_reason"] = trade.get("exit_reason")
            life["ts_closed"] = float(trade.get("ts_closed") or time.time())
            life["held_sec"] = float(trade.get("held_sec") or 0)
            cost = float(life.get("cost") or 1.0)
            life["final_roi"] = float(life["pnl"]) / cost
            life["grade"] = _grade(
                life["final_roi"],
                float(life.get("mfe_roi") or 0),
                float(life.get("mae_roi") or 0),
            )
            closed = dict(life)
            closed["path"] = list(life.get("path") or [])[-120:]
            pid = str(pos.get("id") or "")
            self.lives.pop(pid, None)
            self.recent_closed_lives = ([closed] + self.recent_closed_lives)[:40]

        self._append_archive(closed)

        if skillbook is not None:
            try:
                skillbook.record_path_close(
                    trader=str(pos.get("copy_trader") or ""),
                    title=str(pos.get("title") or ""),
                    reason=str(pos.get("reason") or ""),
                    pnl=float(closed.get("pnl") or 0),
                    mfe_roi=float(closed.get("mfe_roi") or 0),
                    mae_roi=float(closed.get("mae_roi") or 0),
                    grade=str(closed.get("grade") or "C"),
                    held_sec=float(closed.get("held_sec") or 0),
                    side=str(pos.get("side") or ""),
                    timeframe=str(pos.get("timeframe") or ""),
                    asset=str(pos.get("asset") or ""),
                    market_slug=str(pos.get("market_slug") or ""),
                    price=float(pos.get("entry") or 0) or None,
                    cost=float(pos.get("cost") or 0) or None,
                )
            except Exception:
                pass

        if memory is not None:
            try:
                memory.record_bet_life(closed)
            except Exception:
                pass

        return closed

    def prune_missing(self, open_ids: set[str]):
        with self._lock:
            for pid in list(self.lives.keys()):
                if pid not in open_ids:
                    self.lives.pop(pid, None)

    def status(self) -> dict[str, Any]:
        with self._lock:
            open_lives = []
            for life in self.lives.values():
                row = dict(life)
                row["path"] = list(life.get("path") or [])[-48:]
                open_lives.append(row)
            open_lives.sort(key=lambda x: -abs(float(x.get("last_upnl") or 0)))
            return {
                "open": open_lives,
                "recent_closed": list(self.recent_closed_lives)[:12],
                "open_count": len(open_lives),
                "last_mark_ts": self.last_mark_ts,
                "mark_count": self.mark_count,
                "ok": bool(self.last_mark_ts) and (time.time() - self.last_mark_ts < 30),
            }


bet_tracker = LiveBetTracker()
