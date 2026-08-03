"""Binance spot lead feed for Polymarket lag snipes.

Keeps a short tick history for BTC/ETH/SOL so the edge scanner can detect
spot moves that Polymarket Up/Down books have not yet priced in.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.request
from collections import deque
from typing import Any

# Combined aggTrade stream — low latency, per-fill prices
_BINANCE_WS = (
    "wss://stream.binance.com:9443/stream?streams="
    "btcusdt@aggTrade/ethusdt@aggTrade/solusdt@aggTrade"
)
_BINANCE_REST = (
    "https://api.binance.com/api/v3/ticker/price"
    '?symbols=["BTCUSDT","ETHUSDT","SOLUSDT"]'
)
_SYM_TO_ASSET = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
}
_ASSETS = ("BTC", "ETH", "SOL")


class SpotLead:
    def __init__(self, *, maxlen: int = 600):
        self._lock = threading.Lock()
        self._ticks: dict[str, deque[tuple[float, float]]] = {
            a: deque(maxlen=maxlen) for a in _ASSETS
        }
        self._last: dict[str, float] = {}
        self._last_ts: dict[str, float] = {}
        self._started = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.mode = "idle"  # ws | rest | idle
        self.last_error = ""
        self.msg_count = 0
        self.connect_count = 0

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="poly-spot-lead", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _push(self, asset: str, price: float, ts: float | None = None) -> None:
        if asset not in self._ticks or price <= 0:
            return
        t = float(ts if ts is not None else time.time())
        with self._lock:
            self._ticks[asset].append((t, float(price)))
            self._last[asset] = float(price)
            self._last_ts[asset] = t
            self.msg_count += 1

    def last(self, asset: str) -> float:
        with self._lock:
            return float(self._last.get(str(asset).upper()) or 0.0)

    def age_sec(self, asset: str = "BTC") -> float | None:
        with self._lock:
            ts = self._last_ts.get(str(asset).upper())
        if not ts:
            return None
        return time.time() - float(ts)

    def ret(self, asset: str, lookback_sec: float = 4.0) -> dict[str, float]:
        """Return over lookback: (last - old) / old using tick history."""
        a = str(asset).upper()
        now = time.time()
        lb = max(0.25, float(lookback_sec))
        with self._lock:
            ticks = list(self._ticks.get(a) or [])
            last = float(self._last.get(a) or 0.0)
        if last <= 0 or len(ticks) < 2:
            return {"ret": 0.0, "dir": 0.0, "spot": last, "n": float(len(ticks)), "age": 0.0}
        cutoff = now - lb
        # oldest tick still inside window (or earliest available after cutoff)
        old_px = None
        old_ts = None
        for t, px in ticks:
            if t >= cutoff:
                old_px = px
                old_ts = t
                break
        if old_px is None:
            # all ticks older than window — use oldest retained
            old_ts, old_px = ticks[0]
        if not old_px:
            return {"ret": 0.0, "dir": 0.0, "spot": last, "n": float(len(ticks)), "age": 0.0}
        r = (last - old_px) / old_px
        return {
            "ret": float(r),
            "dir": 1.0 if r > 0 else (-1.0 if r < 0 else 0.0),
            "spot": last,
            "old": float(old_px),
            "n": float(len(ticks)),
            "age": now - float(self._last_ts.get(a) or now),
            "span": now - float(old_ts or now),
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            ages = {a: (time.time() - self._last_ts[a]) if a in self._last_ts else None for a in _ASSETS}
            lasts = dict(self._last)
        rets = {a: self.ret(a, 4.0).get("ret") for a in _ASSETS}
        ok = self.mode in ("ws", "rest") and any(
            ages.get(a) is not None and ages[a] < 15 for a in _ASSETS
        )
        return {
            "ok": ok,
            "mode": self.mode,
            "last_error": self.last_error,
            "msg_count": self.msg_count,
            "connect_count": self.connect_count,
            "last": lasts,
            "age_sec": ages,
            "ret_4s": rets,
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self._ws_loop())
                finally:
                    try:
                        loop.close()
                    except Exception:
                        pass
            except Exception as e:
                self.last_error = str(e)[:160]
                self.mode = "rest"
            # REST fallback bursts while WS is down
            for _ in range(8):
                if self._stop.is_set():
                    return
                try:
                    self._rest_poll()
                    self.mode = "rest" if self.mode != "ws" else self.mode
                except Exception as e:
                    self.last_error = str(e)[:160]
                time.sleep(1.0)

    async def _ws_loop(self) -> None:
        try:
            import websockets
        except Exception as e:
            self.last_error = f"websockets:{e}"
            raise

        self.connect_count += 1
        async with websockets.connect(
            _BINANCE_WS,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_queue=1024,
        ) as ws:
            self.mode = "ws"
            self.last_error = ""
            while not self._stop.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                data = msg.get("data") if isinstance(msg, dict) else None
                if not isinstance(data, dict):
                    data = msg if isinstance(msg, dict) else None
                if not isinstance(data, dict):
                    continue
                sym = str(data.get("s") or "").upper()
                asset = _SYM_TO_ASSET.get(sym)
                if not asset:
                    continue
                try:
                    px = float(data.get("p") or 0)
                except (TypeError, ValueError):
                    continue
                ts_ms = data.get("T") or data.get("E")
                try:
                    ts = float(ts_ms) / 1000.0 if ts_ms else time.time()
                except Exception:
                    ts = time.time()
                self._push(asset, px, ts)

    def _rest_poll(self) -> None:
        req = urllib.request.Request(
            _BINANCE_REST,
            headers={"User-Agent": "poly-alert-deck/spot-lead"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
        if not isinstance(rows, list):
            return
        now = time.time()
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = str(row.get("symbol") or "").upper()
            asset = _SYM_TO_ASSET.get(sym)
            if not asset:
                continue
            try:
                px = float(row.get("price") or 0)
            except (TypeError, ValueError):
                continue
            self._push(asset, px, now)


spot_lead = SpotLead()
