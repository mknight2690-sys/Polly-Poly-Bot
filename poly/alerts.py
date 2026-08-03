"""Append-only alert archive for TAKE / SKIP / CLOSE calls."""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from typing import Any, Callable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALERTS_PATH = os.path.join(ROOT, "data", "poly_alerts.jsonl")

_lock = threading.Lock()
_recent: deque[dict] = deque(maxlen=400)
_subs: list[Callable[[dict], None]] = []
_hydrated = False


def _hydrate(limit: int = 200):
    global _hydrated
    if _hydrated:
        return
    _hydrated = True
    try:
        if not os.path.exists(ALERTS_PATH):
            return
        with open(ALERTS_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                _recent.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass


def subscribe(cb: Callable[[dict], None]):
    _subs.append(cb)


def emit(
    kind: str,
    title: str,
    *,
    detail: str = "",
    side: str = "",
    price: float | None = None,
    size_usd: float | None = None,
    reason: str = "",
    confidence: float | None = None,
    market_slug: str = "",
    event_slug: str = "",
    url: str = "",
    copy_trader: str = "",
    speak: str = "",
    data: dict | None = None,
) -> dict[str, Any]:
    _hydrate()
    if not url and (event_slug or market_slug):
        try:
            from .client import poly_url
            url = poly_url(event_slug, market_slug)
        except Exception:
            url = ""
    row: dict[str, Any] = {
        "ts": time.time(),
        "kind": str(kind).upper(),
        "title": title,
        "detail": detail,
        "side": side,
        "price": price,
        "size_usd": size_usd,
        "reason": reason,
        "confidence": confidence,
        "market_slug": market_slug,
        "event_slug": event_slug,
        "url": url,
        "copy_trader": copy_trader,
        "speak": speak or "",
        "data": data or {},
    }
    with _lock:
        _recent.append(row)
        os.makedirs(os.path.dirname(ALERTS_PATH), exist_ok=True)
        with open(ALERTS_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    for cb in list(_subs):
        try:
            cb(row)
        except Exception:
            pass
    return row


def recent(limit: int = 50, max_age_sec: float | None = None) -> list[dict]:
    _hydrate()
    with _lock:
        rows = list(_recent)[-limit:][::-1]
    if max_age_sec is not None and max_age_sec > 0:
        cutoff = time.time() - float(max_age_sec)
        rows = [a for a in rows if float(a.get("ts") or 0) >= cutoff]
    return rows


def alerts_today_count() -> int:
    _hydrate()
    day_ago = time.time() - 86400
    with _lock:
        return sum(1 for a in _recent if float(a.get("ts") or 0) >= day_ago and a.get("kind") == "TAKE")
