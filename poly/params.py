"""Live-tunable POLY parameters + dashboard layout."""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARAMS_PATH = os.path.join(ROOT, "data", "poly_params.json")

PARAM_SPEC: dict[str, dict[str, Any]] = {
    "trading_enabled": {"default": True, "bool": True},
    "live_alert_mode": {"default": False, "bool": True},  # legacy manual-link mode (off)
    # CLOB exec: paper (default) | dry_run | live — live also needs live_trading_armed
    "exec_mode": {"default": "paper", "choices": ["paper", "dry_run", "live"]},
    "live_trading_armed": {"default": False, "bool": True},  # explicit arm for real spend
    "claim_poll_sec": {"default": 20.0, "min": 8.0, "max": 120.0},
    "short_crypto_only": {"default": True, "bool": True},  # 1m/5m updown focus
    "copy_enabled": {"default": False, "bool": True},  # off in turbo short mode
    "starting_equity": {"default": 10.0, "min": 1.0, "max": 1_000_000.0},
    "max_positions": {"default": 2, "min": 1, "max": 50},
    "risk_frac": {"default": 0.35, "min": 0.05, "max": 0.80},
    "min_confidence": {"default": 0.72, "min": 0.0, "max": 1.0},
    "min_bet_usd": {"default": 3.0, "min": 1.05, "max": 100.0},
    # Smart sizing — grows $ with equity; tapers risk %; heat + DD brakes
    "sizing_mode": {"default": "smart", "choices": ["smart", "flat"]},
    "sizing_grow_above_usd": {"default": 50.0, "min": 0.0, "max": 1_000_000.0},
    "max_bet_usd": {"default": 40.0, "min": 0.0, "max": 50_000.0},  # 0 = no hard $ cap
    "max_bet_frac": {"default": 0.45, "min": 0.05, "max": 0.95},
    "portfolio_heat_frac": {"default": 0.70, "min": 0.20, "max": 1.0},
    "copy_top_n": {"default": 10, "min": 3, "max": 50},
    "copy_min_pnl": {"default": 2000.0, "min": 0.0, "max": 1e9},
    "copy_min_trade_usd": {"default": 100.0, "min": 0.0, "max": 1e6},
    "copy_periods": {"default": "1d", "choices": ["1d", "1w", "1d,1w", "1m", "all"]},
    "edge_min_edge": {"default": 0.03, "min": 0.0, "max": 0.50},
    "edge_max_spread": {"default": 0.08, "min": 0.01, "max": 0.40},
    "edge_min_liquidity": {"default": 500.0, "min": 0.0, "max": 1e9},
    "edge_min_volume_24h": {"default": 1000.0, "min": 0.0, "max": 1e9},
    # Asymmetric — wider TP / tighter SL so scratches can clear fees
    "tp_price_delta": {"default": 0.16, "min": 0.02, "max": 0.80},
    "sl_price_delta": {"default": 0.10, "min": 0.02, "max": 0.80},
    # Mental trailing stop — arms at fee-aware breakeven, then trails peak
    "trail_stop": {"default": True, "bool": True},
    # Give back at most this fraction of peak profit (0.01 = trail 1% behind the win)
    "trail_profit_giveback": {"default": 0.01, "min": 0.001, "max": 0.25},
    "trail_be_cushion": {"default": 0.005, "min": 0.0, "max": 0.05},  # above exact fee BE
    # legacy absolute distance (unused when trail_profit_giveback is set)
    "trail_distance": {"default": 0.05, "min": 0.02, "max": 0.30},
    "trail_arm_delta": {"default": 0.06, "min": 0.02, "max": 0.40},
    "trail_be_delta": {"default": 0.04, "min": 0.01, "max": 0.30},
    "max_hold_hours": {"default": 0.25, "min": 0.01, "max": 720.0},  # fallback; seats use window-aware hold
    "cooldown_sec": {"default": 45.0, "min": 10.0, "max": 86400.0},
    "min_hold_sec": {"default": 45.0, "min": 5.0, "max": 900.0},
    "min_entry_secs_left": {"default": 50.0, "min": 15.0, "max": 900.0},
    "trader_poll_sec": {"default": 45.0, "min": 10.0, "max": 120.0},
    "edge_poll_sec": {"default": 8.0, "min": 4.0, "max": 300.0},
    "engine_poll_sec": {"default": 2.0, "min": 1.0, "max": 60.0},
    "mark_poll_sec": {"default": 2.0, "min": 1.0, "max": 15.0},
    # LIVE+ARMED open seats: sub-second marks so trail FAKs keep up
    "mark_poll_sec_live": {"default": 0.35, "min": 0.15, "max": 2.0},
    "continuous_learning": {"default": True, "bool": True},
    # Stick-and-tighten: only take historically workable setups
    "setup_allowlist": {"default": True, "bool": True},
    "prefer_down": {"default": True, "bool": True},
    "complementary_arb": {"default": True, "bool": True},
    "near_resolution_snipe": {"default": False, "bool": True},
    "arb_sum_max": {"default": 0.985, "min": 0.90, "max": 0.999},
    "fair_odds_model": {"default": True, "bool": True},
    "fair_odds_allow_down": {"default": False, "bool": True},
    # 0.07 starved the board; 0.045 keeps junk out but restores ~15m cadence
    "fair_min_edge": {"default": 0.045, "min": 0.02, "max": 0.30},
    "fair_min_price": {"default": 0.48, "min": 0.10, "max": 0.70},
    "fair_max_price": {"default": 0.74, "min": 0.50, "max": 0.95},
    "cross_timeframe": {"default": True, "bool": True},
    "cross_tf_min_gap": {"default": 0.06, "min": 0.03, "max": 0.40},
    "prefer_limit_orders": {"default": True, "bool": True},
    "paper_fees": {"default": True, "bool": True},
    "paper_fee_rate": {"default": 0.07, "min": 0.0, "max": 0.20},  # crypto taker Θ
    # Binance spot lead → Polymarket lag snipe (fee-aware)
    "lag_snipe": {"default": True, "bool": True},
    "lag_lookback_sec": {"default": 4.0, "min": 1.0, "max": 30.0},
    "lag_min_move": {"default": 0.0007, "min": 0.0003, "max": 0.02},  # 7 bps
    "lag_min_edge": {"default": 0.04, "min": 0.01, "max": 0.30},
    "lag_slip_buffer": {"default": 0.01, "min": 0.0, "max": 0.10},
    "lag_min_price": {"default": 0.42, "min": 0.15, "max": 0.70},
    "lag_max_price": {"default": 0.82, "min": 0.55, "max": 0.95},
    "lag_tfs": {"default": "5m,15m"},
    "lag_endgame": {"default": True, "bool": True},
    "lag_endgame_min_sec": {"default": 25.0, "min": 10.0, "max": 120.0},
    "lag_endgame_max_sec": {"default": 90.0, "min": 30.0, "max": 300.0},
    "lag_endgame_min_price": {"default": 0.72, "min": 0.55, "max": 0.95},
    # Sub-second hot path when Binance impulse hits (live/armed FAK)
    "lag_hot_path": {"default": True, "bool": True},
    "lag_hot_poll_sec": {"default": 0.35, "min": 0.15, "max": 2.0},
    "lag_cache_sec": {"default": 2.5, "min": 0.5, "max": 8.0},
    "engine_poll_sec_lag": {"default": 1.5, "min": 0.5, "max": 8.0},
    # When True: only spot_lag / spot_lag_endgame seats — no fair_odds/momentum/arb
    "lag_only": {"default": True, "bool": True},
}

DASHBOARD_DEFAULT = {
    "title": "POLY // 1M-5M CRYPTO DECK",
    "accent": "#ff8a3d",
    "voice": {"enabled": True, "persona": "pit_boss"},
    "widgets": [
        {"id": "alerts", "label": "Trade Announcements", "visible": True},
        {"id": "edges", "label": "1m / 5m Crypto Edges", "visible": True},
        {"id": "livebets", "label": "Live Bet Tracker", "visible": True},
        {"id": "positions", "label": "Autopilot Seats", "visible": True},
        {"id": "account", "label": "Paper Account", "visible": True},
        {"id": "equity", "label": "Equity Curve", "visible": True},
        {"id": "learning", "label": "Learning Memory", "visible": True},
        {"id": "live", "label": "Live Trading Ready", "visible": True},
        {"id": "traders", "label": "Top Traders Stream", "visible": False},
        {"id": "health", "label": "Stream Health", "visible": True},
    ],
}


class LiveParams:
    def __init__(self, path: str = PARAMS_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.values: dict[str, Any] = {k: s["default"] for k, s in PARAM_SPEC.items()}
        self.dashboard: dict[str, Any] = json.loads(json.dumps(DASHBOARD_DEFAULT))
        self.history: list[dict] = []
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in (data.get("values") or {}).items():
                if k in PARAM_SPEC:
                    self.values[k] = v
            if data.get("dashboard"):
                self.dashboard = data["dashboard"]
            have = {w["id"] for w in self.dashboard.get("widgets", [])}
            for w in DASHBOARD_DEFAULT["widgets"]:
                if w["id"] not in have:
                    self.dashboard.setdefault("widgets", []).append(dict(w))
            if not isinstance(self.dashboard.get("voice"), dict):
                self.dashboard["voice"] = dict(DASHBOARD_DEFAULT["voice"])
        except FileNotFoundError:
            self.save()
        except Exception:
            pass

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            payload = {
                "values": self.values,
                "dashboard": self.dashboard,
                "history": self.history[-200:],
            }
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, self.path)

    def snapshot(self) -> dict[str, Any]:
        return {
            "values": dict(self.values),
            "dashboard": json.loads(json.dumps(self.dashboard)),
        }

    def set_param(self, key: str, value: Any, who: str = "user") -> tuple[bool, str]:
        if key not in PARAM_SPEC:
            return False, f"unknown param {key}"
        spec = PARAM_SPEC[key]
        try:
            if spec.get("bool"):
                if isinstance(value, str):
                    value = value.strip().lower() in ("1", "true", "yes", "on", "start")
                value = bool(value)
            elif "choices" in spec:
                value = str(value)
                if value not in spec["choices"]:
                    return False, f"{key} must be one of {spec['choices']}"
            else:
                value = float(value)
                if "min" in spec:
                    value = max(float(spec["min"]), value)
                if "max" in spec:
                    value = min(float(spec["max"]), value)
                if isinstance(spec["default"], int) and not isinstance(spec["default"], bool):
                    value = int(round(value))
        except Exception as e:
            return False, f"bad value: {e}"
        old = self.values.get(key)
        self.values[key] = value
        self.history.append(
            {"ts": time.time(), "key": key, "old": old, "new": value, "who": who}
        )
        self.save()
        return True, f"{key}={value}"

    def set_dashboard(self, patch: dict, who: str = "user") -> tuple[bool, str]:
        if not isinstance(patch, dict):
            return False, "dashboard patch must be object"
        dash = self.dashboard
        if "title" in patch and patch["title"]:
            dash["title"] = str(patch["title"])[:80]
        if "accent" in patch and patch["accent"]:
            dash["accent"] = str(patch["accent"])[:20]
        if isinstance(patch.get("voice"), dict):
            voice = dash.setdefault("voice", {})
            if "enabled" in patch["voice"]:
                voice["enabled"] = bool(patch["voice"]["enabled"])
            if "persona" in patch["voice"]:
                voice["persona"] = str(patch["voice"]["persona"])[:40]
        if isinstance(patch.get("widgets"), list):
            dash["widgets"] = patch["widgets"]
        self.history.append({"ts": time.time(), "key": "dashboard", "who": who})
        self.save()
        return True, "dashboard updated"

    def force_defaults(self):
        """Reset values + dashboard to code defaults (used for $10 turbo restart)."""
        self.values = {k: s["default"] for k, s in PARAM_SPEC.items()}
        self.dashboard = json.loads(json.dumps(DASHBOARD_DEFAULT))
        self.save()
