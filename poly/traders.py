"""Background stream of top Polymarket traders + fresh copy candidates."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from . import client as poly

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADERS_PATH = os.path.join(ROOT, "data", "poly_traders.json")


class TraderStreamer:
    def __init__(self):
        self._lock = threading.Lock()
        self.leaderboard: list[dict] = []
        self.watchlist: list[dict] = []
        self.candidates: list[dict] = []
        self.recent_fills: list[dict] = []
        self.last_error = ""
        self.last_ok_ts = 0.0
        self.refresh_count = 0
        self._seen_keys: set[str] = set()
        self.load()

    def load(self):
        try:
            with open(TRADERS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.leaderboard = list(data.get("leaderboard") or [])
            self.watchlist = list(data.get("watchlist") or [])
            self.recent_fills = list(data.get("recent_fills") or [])[-80:]
            for f in self.recent_fills:
                k = f.get("dedupe")
                if k:
                    self._seen_keys.add(str(k))
        except Exception:
            pass

    def save(self):
        os.makedirs(os.path.dirname(TRADERS_PATH), exist_ok=True)
        payload = {
            "built_at": time.time(),
            "leaderboard": self.leaderboard[:60],
            "watchlist": self.watchlist[:40],
            "recent_fills": self.recent_fills[-80:],
            "last_ok_ts": self.last_ok_ts,
            "last_error": self.last_error,
        }
        tmp = TRADERS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, TRADERS_PATH)

    def refresh(self, params: dict[str, Any]) -> dict[str, Any]:
        top_n = int(params.get("copy_top_n") or 15)
        min_pnl = float(params.get("copy_min_pnl") or 500.0)
        min_usd = float(params.get("copy_min_trade_usd") or 50.0)
        periods_raw = str(params.get("copy_periods") or "1d,1w")
        periods = [p.strip() for p in periods_raw.split(",") if p.strip()] or ["1d"]

        merged: dict[str, dict] = {}
        errors: list[str] = []
        for period in periods:
            try:
                rows = poly.leaderboard(period=period, order_by="PNL", limit=max(top_n, 25))
                for r in rows:
                    wallet = str(r.get("proxyWallet") or "").lower()
                    if not wallet:
                        continue
                    pnl = float(r.get("pnl") or 0.0)
                    if pnl < min_pnl:
                        continue
                    cur = merged.get(wallet)
                    score = pnl
                    if period == "1w":
                        score *= 1.15
                    entry = {
                        "wallet": wallet,
                        "userName": r.get("userName") or wallet[:10],
                        "rank": r.get("rank"),
                        "pnl": pnl,
                        "vol": float(r.get("vol") or 0.0),
                        "period": period,
                        "profileImage": r.get("profileImage") or "",
                        "xUsername": r.get("xUsername") or "",
                        "verifiedBadge": bool(r.get("verifiedBadge")),
                        "score": score,
                    }
                    if not cur or score > float(cur.get("score") or 0):
                        merged[wallet] = entry
            except Exception as e:
                errors.append(f"{period}:{e}")

        ranked = sorted(merged.values(), key=lambda x: -float(x.get("score") or 0))[:top_n]
        watch = ranked[:top_n]

        new_candidates: list[dict] = []
        fills: list[dict] = []
        for trader in watch:
            wallet = trader["wallet"]
            try:
                trades = poly.user_trades(wallet, limit=12)
            except Exception as e:
                errors.append(f"trades:{wallet[:8]}:{e}")
                continue
            for t in trades:
                if str(t.get("side") or "").upper() != "BUY":
                    continue
                size = float(t.get("size") or 0.0)
                price = float(t.get("price") or 0.0)
                usd = size * price if price > 0 else float(t.get("size") or 0.0)
                if usd < min_usd:
                    continue
                outcome = str(t.get("outcome") or "Yes")
                market_slug = str(t.get("slug") or "")
                event_slug = str(t.get("eventSlug") or "")
                slug = market_slug or event_slug
                url = poly.poly_url(event_slug, market_slug)
                ts = float(t.get("timestamp") or 0)
                # Polymarket timestamps sometimes arrive as ms or inflated; normalize if huge.
                if ts > 1e12:
                    ts = ts / 1000.0
                # Activity API returned future-looking unix in samples; clamp absurd futures.
                if ts > time.time() + 86400 * 365:
                    ts = time.time()
                dedupe = f"{wallet}:{t.get('transactionHash') or ''}:{t.get('asset')}:{ts}"
                if dedupe in self._seen_keys:
                    continue
                # Only treat as fresh if within last 6 hours (or unknown ts)
                if ts and ts < time.time() - 6 * 3600:
                    self._seen_keys.add(dedupe)
                    continue
                self._seen_keys.add(dedupe)
                side = "YES" if outcome.lower() in ("yes", "y") else (
                    "NO" if outcome.lower() in ("no", "n") else outcome.upper()
                )
                cand = {
                    "source": "copy",
                    "reason": f"copy:@{trader.get('userName')}",
                    "copy_trader": trader.get("userName") or wallet[:10],
                    "copy_wallet": wallet,
                    "title": t.get("title") or slug,
                    "market_slug": market_slug or slug,
                    "event_slug": event_slug,
                    "url": url,
                    "condition_id": t.get("conditionId") or "",
                    "token_id": str(t.get("asset") or ""),
                    "side": side,
                    "outcome": outcome,
                    "price": price,
                    "size_usd_source": usd,
                    "confidence": min(0.92, 0.55 + min(0.3, float(trader.get("pnl") or 0) / 500000)),
                    "ts": ts or time.time(),
                    "dedupe": dedupe,
                }
                new_candidates.append(cand)
                fills.append(cand)

        # Cap seen set
        if len(self._seen_keys) > 8000:
            self._seen_keys = set(list(self._seen_keys)[-4000:])

        with self._lock:
            self.leaderboard = ranked
            self.watchlist = watch
            if new_candidates:
                self.candidates = (new_candidates + self.candidates)[:80]
                self.recent_fills = (fills + self.recent_fills)[:80]
            self.refresh_count += 1
            if errors and not ranked:
                self.last_error = "; ".join(errors)[:240]
            else:
                self.last_error = "; ".join(errors)[:240] if errors else ""
                self.last_ok_ts = time.time()
            self.save()

        return {
            "traders": len(ranked),
            "new_candidates": len(new_candidates),
            "errors": errors[:5],
        }

    def pop_candidates(self, limit: int = 20) -> list[dict]:
        with self._lock:
            out = list(self.candidates[:limit])
            self.candidates = self.candidates[limit:]
            return out

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": bool(self.last_ok_ts) and (time.time() - self.last_ok_ts < 180),
                "last_ok_ts": self.last_ok_ts,
                "last_error": self.last_error,
                "refresh_count": self.refresh_count,
                "leaderboard": list(self.leaderboard)[:25],
                "watchlist": list(self.watchlist)[:15],
                "pending_candidates": len(self.candidates),
                "recent_fills": list(self.recent_fills)[:20],
            }


trader_streamer = TraderStreamer()
