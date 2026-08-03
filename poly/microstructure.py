"""Microstructure helpers for Polymarket short crypto Up/Down.

RetroValix-style edges: complementary arb, fair-odds repricing, cross-timeframe lag.
"""
from __future__ import annotations

import math
import time
from typing import Any

from . import client as poly


def ewma_vol_from_closes(closes: list[float], *, lam: float = 0.94) -> float:
    """Annualized-ish vol from newest-first closes; returns per-sqrt-second scale."""
    if len(closes) < 3:
        return 0.0
    rets: list[float] = []
    for i in range(len(closes) - 1):
        a, b = float(closes[i]), float(closes[i + 1])
        if a > 0 and b > 0:
            rets.append(math.log(a / b))
    if not rets:
        return 0.0
    var = float(rets[0]) ** 2
    for r in rets[1:]:
        var = lam * var + (1.0 - lam) * (r ** 2)
    # assume ~60s bars if unknown — caller can scale
    return math.sqrt(max(var, 1e-12))


def spot_ewma(asset: str, bar: str = "1m", limit: int = 40) -> dict[str, float]:
    """Spot + EWMA vol from Blofin candles (newest first)."""
    sym = f"{asset}-USDT"
    closes: list[float] = []
    try:
        from official_source_client import get_candles

        rows = get_candles(sym, bar=bar, limit=str(limit)) or []
        for r in rows:
            try:
                closes.append(float(r[4]))
            except Exception:
                continue
    except Exception:
        pass
    if not closes:
        mom = poly.short_crypto_momentum(asset, bar=bar, lookback=3)
        return {
            "spot": float(mom.get("last") or 0),
            "vol": 0.0,
            "ret": float(mom.get("ret") or 0),
            "dir": float(mom.get("dir") or 0),
        }
    bar_sec = 60.0 if bar == "1m" else (300.0 if bar == "5m" else 60.0)
    sigma_bar = ewma_vol_from_closes(closes)
    # per-second vol
    sigma = sigma_bar / math.sqrt(max(bar_sec, 1.0))
    spot = closes[0]
    older = closes[min(5, len(closes) - 1)]
    ret = (spot - older) / older if older else 0.0
    return {
        "spot": spot,
        "vol": sigma,
        "ret": ret,
        "dir": 1.0 if ret > 0 else (-1.0 if ret < 0 else 0.0),
        "open_proxy": closes[min(len(closes) - 1, int(300 / bar_sec) if bar_sec else 5)],
    }


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def binary_up_prob(spot: float, strike: float, secs_left: float, vol_per_sec: float) -> float:
    """Black–Scholes-style P(S_T > K) for a digital UP (cash-or-nothing approx via N(d2))."""
    if spot <= 0 or strike <= 0:
        return 0.5
    t = max(float(secs_left), 1.0)
    # Floor vol so near-expiry doesn't explode to 0/1 from noise
    sig = max(float(vol_per_sec), 1e-6)
    vol_t = sig * math.sqrt(t)
    if vol_t < 1e-8:
        return 1.0 if spot > strike else (0.0 if spot < strike else 0.5)
    d2 = math.log(spot / strike) / vol_t - 0.5 * vol_t
    return max(0.01, min(0.99, _norm_cdf(d2)))


def _strike_at_window(asset: str, window_ts: float | None) -> float | None:
    """Candle close at/just before window open — critical for Up/Down strike."""
    if not window_ts:
        return None
    try:
        from official_source_client import get_candles

        rows = get_candles(f"{asset}-USDT", bar="1m", limit="120") or []
    except Exception:
        return None
    best = None
    best_dt = 1e18
    wt = float(window_ts)
    for r in rows:
        try:
            ts = float(r[0])
            # Blofin ms vs sec
            if ts > 1e12:
                ts /= 1000.0
            close = float(r[4])
        except Exception:
            continue
        if ts <= wt + 1.0:
            dt = wt - ts
            if 0 <= dt < best_dt:
                best_dt = dt
                best = close
    return best


def fair_updown(
    asset: str,
    *,
    secs_left: float,
    window_ts: float | None = None,
    up_px: float | None = None,
    down_px: float | None = None,
) -> dict[str, Any]:
    """Fair UP probability vs window open (strike) + EWMA vol."""
    stats = spot_ewma(asset, bar="1m", limit=48)
    spot = float(stats.get("spot") or 0)
    strike = _strike_at_window(asset, window_ts)
    if strike is None or strike <= 0:
        strike = float(stats.get("open_proxy") or spot)
    p_up = binary_up_prob(spot, strike, secs_left, float(stats.get("vol") or 0))
    p_down = 1.0 - p_up
    # If market already has a strong favorite, don't invent a contrary "edge"
    # from a bad strike — shrink opposite-side edge when book disagrees hard.
    if up_px is not None and down_px is not None:
        if float(up_px) >= 0.78:
            p_up = max(p_up, 0.72)
            p_down = 1.0 - p_up
        elif float(down_px) >= 0.78:
            p_down = max(p_down, 0.72)
            p_up = 1.0 - p_down
    edge_up = (p_up - float(up_px)) if up_px is not None else None
    edge_down = (p_down - float(down_px)) if down_px is not None else None
    return {
        "spot": spot,
        "strike": strike,
        "vol": stats.get("vol"),
        "p_up": p_up,
        "p_down": p_down,
        "edge_up": edge_up,
        "edge_down": edge_down,
        "ret": stats.get("ret"),
        "dir": stats.get("dir"),
        "ts": time.time(),
    }


def complementary_edge(up_px: float, down_px: float, *, max_sum: float = 0.985) -> dict[str, Any] | None:
    s = float(up_px) + float(down_px)
    if s <= 0 or s > max_sum:
        return None
    return {
        "pair_sum": s,
        "edge": 1.0 - s,
        "cheaper": "DOWN" if down_px <= up_px else "UP",
    }


def cross_tf_lag(
    short_row: dict,
    long_row: dict,
    *,
    min_gap: float = 0.08,
) -> dict[str, Any] | None:
    """If short TF has repriced but long TF lags, buy the lagging side."""
    su, sd = float(short_row.get("yes_price") or 0), float(short_row.get("no_price") or 0)
    lu, ld = float(long_row.get("yes_price") or 0), float(long_row.get("no_price") or 0)
    # Normalize outcome labels if needed — caller should pass Up as yes
    gap_up = su - lu
    gap_down = sd - ld
    if gap_up >= min_gap and lu <= 0.85:
        return {
            "side": "UP",
            "price": lu,
            "lead_price": su,
            "gap": gap_up,
            "lag_tf": long_row.get("timeframe"),
            "lead_tf": short_row.get("timeframe"),
        }
    if gap_down >= min_gap and ld <= 0.85:
        return {
            "side": "DOWN",
            "price": ld,
            "lead_price": sd,
            "gap": gap_down,
            "lag_tf": long_row.get("timeframe"),
            "lead_tf": short_row.get("timeframe"),
        }
    return None
