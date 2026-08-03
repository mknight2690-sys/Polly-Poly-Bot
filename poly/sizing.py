"""Bankroll-aware position sizing for POLY seats.

Grows stake with equity, tapers risk % as the book compounds, brakes on
drawdown, and respects portfolio heat / min-max bet floors.
"""
from __future__ import annotations

import math
from typing import Any


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return float(default)


def compute_stake(
    *,
    equity: float,
    balance: float,
    start_equity: float | None = None,
    peak_equity: float | None = None,
    open_cost: float = 0.0,
    risk_frac: float = 0.35,
    min_bet: float = 2.5,
    max_bet_usd: float = 0.0,
    max_bet_frac: float = 0.45,
    heat_frac: float = 0.70,
    size_mult: float = 1.0,
    confidence: float = 0.5,
    is_lag: bool = False,
    live_clob_usd: float | None = None,
    sizing_mode: str = "smart",
    grow_above_usd: float = 50.0,
) -> dict[str, Any]:
    """Return stake dollars + diagnostics. stake=0 means skip (no room)."""
    eq = max(1.0, _f(equity, 1.0))
    bal = max(0.0, _f(balance, 0.0))
    start = max(1.0, _f(start_equity, eq))
    peak = max(eq, _f(peak_equity, eq))
    open_c = max(0.0, _f(open_cost, 0.0))
    rf = max(0.02, min(0.80, _f(risk_frac, 0.35)))
    min_b = max(1.0, _f(min_bet, 2.5))
    max_b = max(0.0, _f(max_bet_usd, 0.0))
    max_frac = max(0.05, min(0.95, _f(max_bet_frac, 0.45)))
    heat = max(0.10, min(1.0, _f(heat_frac, 0.70)))
    mult = max(0.15, min(2.5, _f(size_mult, 1.0)))
    conf = max(0.0, min(1.0, _f(confidence, 0.5)))
    mode = str(sizing_mode or "smart").lower().strip()
    grow_above = max(0.0, _f(grow_above_usd, 50.0))

    reasons: list[str] = []
    conf_tilt = 1.0
    base_frac = rf
    room = max(0.0, heat * eq - open_c)

    # Until equity clears grow_above: always min bet (compound first, size later)
    if mode != "flat" and grow_above > 0 and eq + 1e-9 < grow_above:
        reasons.append(f"min_until_${grow_above:.0f}")
        raw = float(min_b)
        cash_cap = bal * 0.95
        raw = min(raw, cash_cap, room if room > 0 else raw)
        if live_clob_usd is not None:
            try:
                raw = min(raw, float(live_clob_usd) * 0.95)
            except Exception:
                pass
        skip = raw + 1e-9 < min_b
        if skip:
            return {
                "stake": 0.0,
                "ok": False,
                "skip": True,
                "why": "below min bet / no room",
                "equity": eq,
                "base_frac": min_b / eq,
                "size_mult": 1.0,
                "conf_tilt": 1.0,
                "heat_room": room,
                "peak_equity": peak,
                "growth": eq / start,
                "grow_above_usd": grow_above,
                "min_bet_phase": True,
                "reasons": reasons,
            }
        return {
            "stake": round(max(0.0, raw), 4),
            "ok": True,
            "skip": False,
            "why": "",
            "equity": eq,
            "base_frac": round(raw / eq, 4),
            "size_mult": 1.0,
            "conf_tilt": 1.0,
            "heat_room": room,
            "peak_equity": peak,
            "growth": eq / start,
            "grow_above_usd": grow_above,
            "min_bet_phase": True,
            "reasons": reasons,
        }

    if mode == "flat":
        # Legacy: equity * risk * mult only
        base_frac = rf
        conf_tilt = 1.0
        taper = 1.0
        dd_mult = 1.0
    else:
        # Growth: $ stake rises with equity; risk % gently tapers as bank compounds
        growth = eq / start
        # log10(1)=0 → taper 1.0; 10x → ~0.87; 100x → ~0.77
        taper = 1.0 / (1.0 + 0.15 * max(0.0, math.log10(max(1.0, growth))))
        base_frac = rf * taper
        reasons.append(f"taper={taper:.2f}")
        reasons.append(f"grow_phase_above_${grow_above:.0f}")

        # Drawdown brake vs peak mark-to-market equity
        dd = eq / peak if peak > 0 else 1.0
        if dd < 0.88:
            dd_mult = 0.55
            reasons.append(f"dd_hard={dd:.0%}")
        elif dd < 0.94:
            dd_mult = 0.75
            reasons.append(f"dd_soft={dd:.0%}")
        elif dd >= 1.0 and growth >= 1.15:
            dd_mult = 1.08  # mild press when at highs + grown
            reasons.append("press_highs")
        else:
            dd_mult = 1.0

        # Confidence tilt (same shape as prior engine)
        conf_tilt = 0.85 + 0.35 * min(1.0, max(0.0, (conf - 0.5) / 0.45))

        # Lag snipes: slightly smaller than full risk — edge is fast but noisy
        if is_lag:
            mult *= 0.92
            reasons.append("lag_haircut")

    base_frac = max(0.02, min(0.80, base_frac * dd_mult))
    raw = eq * base_frac * mult * conf_tilt
    reasons.append(f"base_frac={base_frac:.3f}")

    # Per-seat caps
    frac_cap = eq * max_frac
    raw = min(raw, frac_cap)
    if max_b > 0:
        raw = min(raw, max_b)
        reasons.append(f"max_bet={max_b:.2f}")

    # Portfolio heat — leave dry powder across open seats
    if raw > room + 1e-9:
        raw = room
        reasons.append(f"heat_room={room:.2f}")

    # Cash + optional live CLOB wallet
    cash_cap = bal * 0.95
    raw = min(raw, cash_cap)
    if live_clob_usd is not None:
        try:
            clob_cap = float(live_clob_usd) * 0.95
            if raw > clob_cap + 1e-9:
                raw = max(0.0, clob_cap)
                reasons.append(f"clob_cap={clob_cap:.2f}")
        except Exception:
            pass

    skip = False
    skip_why = ""
    if raw + 1e-9 < min_b:
        if bal + 1e-9 >= min_b and room + 1e-9 >= min_b:
            # Only bump to floor if heat + cash allow
            if (live_clob_usd is None) or (float(live_clob_usd) * 0.95 + 1e-9 >= min_b):
                raw = min_b
                reasons.append("min_bet_floor")
            else:
                skip = True
                skip_why = "below min bet / CLOB"
        else:
            skip = True
            skip_why = "below min bet / no room"

    if skip:
        return {
            "stake": 0.0,
            "ok": False,
            "skip": True,
            "why": skip_why,
            "equity": eq,
            "base_frac": base_frac,
            "size_mult": mult,
            "conf_tilt": conf_tilt,
            "heat_room": room,
            "grow_above_usd": grow_above,
            "min_bet_phase": False,
            "reasons": reasons,
        }

    stake = round(max(0.0, raw), 4)
    return {
        "stake": stake,
        "ok": stake >= min_b - 1e-9,
        "skip": False,
        "why": "",
        "equity": eq,
        "base_frac": base_frac,
        "size_mult": mult,
        "conf_tilt": conf_tilt,
        "heat_room": room,
        "peak_equity": peak,
        "growth": eq / start,
        "grow_above_usd": grow_above,
        "min_bet_phase": False,
        "reasons": reasons,
    }


def open_invested(positions: list[dict] | None) -> float:
    return sum(float(p.get("cost") or 0.0) for p in (positions or []))
