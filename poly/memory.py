"""Persistent POLY learning memory."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_PATH = os.path.join(ROOT, "data", "poly_memory.json")


class PolyMemory:
    def __init__(self, path: str = MEMORY_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {
            "trades": [],
            "lessons": [],
            "trader_stats": {},
            "market_stats": {},
            "equity_history": [],
            "live_equity_history": [],
            "incidents": [],
            "chat": [],
            "bet_lives": [],  # closed path summaries for learning review
        }
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            for k in self.data:
                if k in loaded:
                    self.data[k] = loaded[k]
            if "live_equity_history" not in self.data:
                self.data["live_equity_history"] = []
        except FileNotFoundError:
            self.save()
        except Exception:
            pass

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            trimmed = dict(self.data)
            trimmed["trades"] = self.data["trades"][-5000:]
            trimmed["lessons"] = self.data["lessons"][-500:]
            trimmed["chat"] = self.data["chat"][-200:]
            trimmed["equity_history"] = self.data["equity_history"][-5000:]
            trimmed["live_equity_history"] = list(
                self.data.get("live_equity_history") or []
            )[-5000:]
            trimmed["incidents"] = self.data["incidents"][-200:]
            trimmed["bet_lives"] = self.data.get("bet_lives", [])[-500:]
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(trimmed, f)
            os.replace(tmp, self.path)

    def record_equity(self, equity: float, *, source: str = "paper"):
        """Append equity point. source=paper|live — live only while LIVE+ARMED spending."""
        row = [time.time(), float(equity)]
        key = "live_equity_history" if str(source).lower() == "live" else "equity_history"
        bucket = self.data.setdefault(key, [])
        bucket.append(row)
        # Throttle disk: paper every 8, live every 4 (more important while spending)
        n = len(bucket)
        every = 4 if key == "live_equity_history" else 8
        if n % every == 0:
            self.save()

    def record_incident(self, inc: dict):
        row = dict(inc)
        row.setdefault("ts", time.time())
        self.data["incidents"].append(row)
        self.save()

    def add_lesson(self, text: str, source: str = "poly", market: str = "", trader: str = ""):
        self.data["lessons"].append(
            {
                "ts": time.time(),
                "source": source,
                "text": str(text)[:500],
                "market": market,
                "trader": trader,
            }
        )
        self.save()

    def _bump_stats(self, bucket: str, key: str, pnl: float):
        stats = self.data[bucket].setdefault(
            key,
            {"wins": 0, "losses": 0, "pnl": 0.0, "n": 0, "avg_win": 0.0, "avg_loss": 0.0},
        )
        stats["n"] += 1
        stats["pnl"] += pnl
        if pnl >= 0:
            stats["avg_win"] = (stats["avg_win"] * stats["wins"] + pnl) / (stats["wins"] + 1)
            stats["wins"] += 1
        else:
            stats["avg_loss"] = (stats["avg_loss"] * stats["losses"] + pnl) / (stats["losses"] + 1)
            stats["losses"] += 1

    def record_trade(self, trade: dict):
        trade = dict(trade)
        trade.setdefault("ts_closed", time.time())
        self.data["trades"].append(trade)
        pnl = float(trade.get("pnl") or 0.0)
        market = str(trade.get("market_slug") or trade.get("title") or "?")
        self._bump_stats("market_stats", market, pnl)
        trader = str(trade.get("copy_trader") or "")
        if trader:
            self._bump_stats("trader_stats", trader, pnl)
        self.save()

    def record_bet_life(self, life: dict):
        """Persist a closed bet's mark path + grade for ongoing learning review."""
        row = {
            "id": life.get("id"),
            "title": life.get("title"),
            "market_slug": life.get("market_slug"),
            "side": life.get("side"),
            "reason": life.get("reason"),
            "copy_trader": life.get("copy_trader"),
            "entry": life.get("entry"),
            "exit": life.get("exit"),
            "pnl": life.get("pnl"),
            "final_roi": life.get("final_roi"),
            "mfe_roi": life.get("mfe_roi"),
            "mae_roi": life.get("mae_roi"),
            "grade": life.get("grade"),
            "milestones": life.get("milestones") or [],
            "path": (life.get("path") or [])[-80:],
            "exit_reason": life.get("exit_reason"),
            "held_sec": life.get("held_sec"),
            "ts_closed": life.get("ts_closed") or time.time(),
        }
        self.data.setdefault("bet_lives", []).append(row)
        self.save()

    def add_chat(self, role: str, text: str):
        self.data["chat"].append({"ts": time.time(), "role": role, "text": str(text)[:2000]})
        self.save()

    def recent_chat(self, n: int = 60) -> list[dict]:
        return list(self.data["chat"][-n:])

    def summary_for_prompt(self) -> str:
        lessons = self.data["lessons"][-8:]
        traders = sorted(
            self.data["trader_stats"].items(),
            key=lambda kv: -float(kv[1].get("pnl") or 0),
        )[:5]
        lines = [f"closed_trades={len(self.data['trades'])}"]
        for t in traders:
            s = t[1]
            lines.append(
                f"trader {t[0]}: n={s.get('n')} pnl={s.get('pnl'):.2f} "
                f"W/L={s.get('wins')}/{s.get('losses')}"
            )
        for L in lessons:
            lines.append(f"lesson: {L.get('text')}")
        return "\n".join(lines)
