"""Auto-claim / capital recycle for short windows.

Primary recycle path (engine): CLOB sell before resolution via claim_win /
claim_early / claim_time. This module is the belt-and-suspenders pass:

1. Sell ANY wallet position that still has value (curPrice > dust).
2. Skip worthless resolved losers (curPrice ~ 0) — they don't free capital.
3. Prefer aggressive prices so fills recycle pUSD back to the wallet.
4. True on-chain redeem for winning redeemables needs Relayer API keys
   (optional RELAYER_API_KEY in poly_clob.txt) — CLOB sell is attempted first.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Below this mark, tokens are dust — don't spam CLOB
DUST_PRICE = 0.03
# Sell anything above this to free capital
VALUE_PRICE = 0.05


def _http_get_json(url: str, timeout: float = 20.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "poly-alert-deck/1.0", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class Claimer:
    def __init__(self):
        self._lock = threading.Lock()
        self.last_ok_ts = 0.0
        self.last_error = ""
        self.last_claim: dict[str, Any] = {}
        self.redeemable_count = 0
        self.valuable_count = 0
        self.dust_skipped = 0
        self.claimed_count = 0
        self.dry_run_count = 0
        self._recent_keys: dict[str, float] = {}

    def fetch_positions(self, funder: str, *, redeemable_only: bool = False) -> list[dict]:
        if not funder:
            return []
        q = f"user={funder}&sizeThreshold=0&limit=80"
        if redeemable_only:
            q += "&redeemable=true"
        url = f"https://data-api.polymarket.com/positions?{q}"
        data = _http_get_json(url)
        if not isinstance(data, list):
            return []
        return [p for p in data if float(p.get("size") or 0) > 0]

    def _aggressive_sell_px(self, cur: float) -> float:
        """Cross the book — prioritize fill over a few cents of edge."""
        if cur >= 0.95:
            return 0.90
        if cur >= 0.80:
            return max(0.50, cur - 0.08)
        if cur >= 0.40:
            return max(0.10, cur - 0.06)
        return max(0.01, cur - 0.04)

    def _cooldown_ok(self, key: str, sec: float = 45.0) -> bool:
        now = time.time()
        last = float(self._recent_keys.get(key) or 0)
        if now - last < sec:
            return False
        self._recent_keys[key] = now
        # prune
        if len(self._recent_keys) > 200:
            cutoff = now - 600
            self._recent_keys = {k: t for k, t in self._recent_keys.items() if t >= cutoff}
        return True

    def tick(
        self,
        *,
        funder: str,
        mode: str,
        armed: bool,
        live_exec,
        memory=None,
        engine_positions: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Recycle capital: CLOB-sell valuable wallet seats; dry_run logs only."""
        mode = (mode or "paper").lower().strip()
        with self._lock:
            try:
                rows = self.fetch_positions(funder, redeemable_only=False)
                redeemable = [p for p in rows if p.get("redeemable")]
                self.redeemable_count = len(redeemable)
                valuable: list[dict] = []
                dust = 0
                for p in rows:
                    cur = float(p.get("curPrice") if p.get("curPrice") is not None else 0)
                    if cur <= DUST_PRICE:
                        dust += 1
                        continue
                    if cur >= VALUE_PRICE or p.get("redeemable"):
                        valuable.append(p)
                self.dust_skipped = dust
                self.valuable_count = len(valuable)
                self.last_ok_ts = time.time()
                self.last_error = ""

                # Also harvest engine open seats that are deep ITM / near expiry
                # (token already known — force CLOB exit even if data-api lags)
                engine_force: list[dict] = []
                for pos in engine_positions or []:
                    token = str(pos.get("token_id") or "")
                    mark = float(pos.get("mark") or 0)
                    shares = float(pos.get("shares") or 0)
                    secs = pos.get("secs_left")
                    try:
                        secs = float(secs) if secs is not None else None
                    except Exception:
                        secs = None
                    if not token or shares <= 0:
                        continue
                    force = False
                    if mark >= 0.90:
                        force = True
                    elif secs is not None and secs <= 30 and mark >= VALUE_PRICE:
                        force = True
                    elif secs is not None and secs <= 15:
                        force = True
                    if force:
                        engine_force.append(
                            {
                                "asset": token,
                                "size": shares,
                                "curPrice": mark,
                                "title": pos.get("title") or "",
                                "redeemable": False,
                                "_from_engine": True,
                            }
                        )

                # Dedupe by token
                seen: set[str] = set()
                queue: list[dict] = []
                for p in engine_force + valuable:
                    tok = str(p.get("asset") or p.get("token_id") or "")
                    if not tok or tok in seen:
                        continue
                    seen.add(tok)
                    queue.append(p)

                actions: list[dict] = []
                if mode in ("", "paper", "off"):
                    return {
                        "ok": True,
                        "redeemable": self.redeemable_count,
                        "valuable": self.valuable_count,
                        "dust_skipped": self.dust_skipped,
                        "actions": [],
                        "claimed_count": self.claimed_count,
                        "dry_run_count": self.dry_run_count,
                        "msg": "paper — claimer idle",
                    }

                for pos in queue[:16]:
                    token = str(pos.get("asset") or pos.get("token_id") or "")
                    size = float(pos.get("size") or 0)
                    title = str(pos.get("title") or "")[:70]
                    cur = float(pos.get("curPrice") if pos.get("curPrice") is not None else 0)
                    if not token or size <= 0:
                        continue
                    if cur <= DUST_PRICE and not pos.get("_from_engine"):
                        continue
                    key = f"{token}:{round(size, 4)}"
                    if not self._cooldown_ok(key, 40.0):
                        continue

                    px = self._aggressive_sell_px(cur if cur > 0 else 0.50)
                    try:
                        from . import client as poly

                        mid = poly.midpoint(token)
                        if mid is not None:
                            mid_f = float(mid)
                            # Still cross — never sit on the bid hoping
                            px = min(px, self._aggressive_sell_px(mid_f))
                    except Exception:
                        pass
                    px = max(0.01, min(0.99, px))

                    result = live_exec.exit_order(
                        token_id=token,
                        price=px,
                        size=size,
                        mode=mode,
                        armed=armed and mode == "live",
                    )
                    row = {
                        "title": title,
                        "size": size,
                        "price": px,
                        "cur": cur,
                        "mode": result.get("mode"),
                        "ok": result.get("ok"),
                        "posted": result.get("posted"),
                        "msg": str(result.get("msg") or "")[:120],
                        "from_engine": bool(pos.get("_from_engine")),
                        "redeemable": bool(pos.get("redeemable")),
                    }
                    actions.append(row)
                    self.last_claim = row
                    if mode == "dry_run" and result.get("ok"):
                        self.dry_run_count += 1
                        if memory and (pos.get("_from_engine") or cur >= 0.5):
                            memory.add_lesson(
                                f"CLAIM dry_run {title} size={size:.2f} @ {px:.3f} (cur={cur:.2f})",
                                source="claimer",
                            )
                    elif mode == "live" and result.get("posted"):
                        self.claimed_count += 1
                        if memory:
                            memory.add_lesson(
                                f"CLAIM LIVE {title} size={size:.2f} @ {px:.3f} (cur={cur:.2f})",
                                source="claimer",
                            )
                    elif mode == "live" and armed and not result.get("posted") and memory:
                        memory.add_lesson(
                            f"CLAIM LIVE miss {title}: {row['msg']}",
                            source="claimer",
                        )

                return {
                    "ok": True,
                    "redeemable": self.redeemable_count,
                    "valuable": self.valuable_count,
                    "dust_skipped": self.dust_skipped,
                    "actions": actions,
                    "claimed_count": self.claimed_count,
                    "dry_run_count": self.dry_run_count,
                }
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                return {"ok": False, "msg": self.last_error}

    def status(self) -> dict[str, Any]:
        return {
            "ok": bool(self.last_ok_ts) and not self.last_error,
            "last_ok_ts": self.last_ok_ts,
            "last_error": self.last_error,
            "redeemable": self.redeemable_count,
            "valuable": self.valuable_count,
            "dust_skipped": self.dust_skipped,
            "claimed_count": self.claimed_count,
            "dry_run_count": self.dry_run_count,
            "last_claim": dict(self.last_claim) if self.last_claim else {},
        }


claimer = Claimer()
