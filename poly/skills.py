"""Skill book: learn fine-grained setups and size/veto for profitability."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS_PATH = os.path.join(ROOT, "data", "poly_skills.json")

_ASSET_RE = re.compile(r"\b(btc|bitcoin|eth|ethereum|sol|solana)\b", re.I)


class SkillBook:
    def __init__(self, path: str = SKILLS_PATH):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {
            "traders": {},  # wallet/name -> stats
            "categories": {},  # crypto|sports|politics|other
            "setups": {},  # asset|tf|side|family -> stats (primary learning grain)
            "live": {},  # soft in-trade learning by setup/category
            "path_grades": {},  # A/B/C/D/F counts
            "recent": [],  # last N closed outcomes for regime auto-tune
            "auto_tune": {},  # last applied param nudges
            "updated_at": 0.0,
        }
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.data.update(loaded)
            self.data.setdefault("setups", {})
            self.data.setdefault("recent", [])
            self.data.setdefault("auto_tune", {})
        except FileNotFoundError:
            self.save()
        except Exception:
            pass

    def save(self):
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.data["updated_at"] = time.time()
            self.data["recent"] = list(self.data.get("recent") or [])[-120:]
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)

    @staticmethod
    def categorize(title: str, reason: str = "") -> str:
        t = f"{title} {reason}".lower()
        if any(k in t for k in ("bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "sol ")):
            return "crypto"
        if any(k in t for k in ("nba", "nfl", "mlb", "nhl", "soccer", "ufc", "match", "vs.")):
            return "sports"
        if any(k in t for k in ("president", "election", "senate", "trump", "vote", "prime minister")):
            return "politics"
        return "other"

    @staticmethod
    def extract_asset(title: str = "", market_slug: str = "", asset: str = "") -> str:
        if asset:
            a = str(asset).upper().strip()
            if a in ("BTC", "ETH", "SOL"):
                return a
        blob = f"{market_slug} {title}".lower()
        m = _ASSET_RE.search(blob)
        if not m:
            return "UNK"
        tok = m.group(1).lower()
        if tok in ("btc", "bitcoin"):
            return "BTC"
        if tok in ("eth", "ethereum"):
            return "ETH"
        if tok in ("sol", "solana"):
            return "SOL"
        return "UNK"

    @staticmethod
    def extract_timeframe(title: str = "", reason: str = "", timeframe: str = "") -> str:
        if timeframe in ("1m", "5m", "15m", "1h"):
            return str(timeframe)
        blob = f"{reason} {title}".lower()
        for tf in ("1m", "5m", "15m", "1h"):
            if tf in blob or f"short_{tf}" in blob:
                return tf
        if "updown-5m" in blob or "5 minute" in blob:
            return "5m"
        if "updown-1m" in blob:
            return "1m"
        return "na"

    @staticmethod
    def reason_family(reason: str = "") -> str:
        r = str(reason or "").lower()
        if "complementary_arb" in r or "pair_sum" in r or "arb_pair" in r:
            return "complementary_arb"
        if "near_resolution" in r or "snipe" in r:
            return "near_resolution"
        if "fair_odds" in r:
            return "fair_odds"
        if "cross_timeframe" in r or "xtf" in r:
            return "cross_timeframe"
        if "spot_lag" in r or "lag_snipe" in r or "latency" in r:
            return "spot_lag"
        if "short_1m" in r:
            return "short_1m"
        if "short_5m" in r:
            return "short_5m"
        if "momentum" in r:
            return "momentum"
        if "spot_gap" in r or "fair" in r:
            return "fair_gap"
        if "copy" in r:
            return "copy"
        if r.startswith("edge:"):
            return r.split(":", 1)[-1][:24] or "edge"
        return (r or "misc")[:24]

    @staticmethod
    def entry_band(price: float | None) -> str:
        try:
            px = float(price or 0)
        except (TypeError, ValueError):
            return "na"
        if px <= 0:
            return "na"
        if px < 0.35:
            return "cheap"
        if px < 0.55:
            return "mid"
        if px < 0.72:
            return "lean"
        return "rich"

    def setup_key(
        self,
        *,
        title: str = "",
        reason: str = "",
        side: str = "",
        timeframe: str = "",
        asset: str = "",
        market_slug: str = "",
        price: float | None = None,
    ) -> str:
        a = self.extract_asset(title, market_slug, asset)
        tf = self.extract_timeframe(title, reason, timeframe)
        sd = (side or "?").upper()
        fam = self.reason_family(reason)
        band = self.entry_band(price)
        return f"{a}|{tf}|{sd}|{fam}|{band}"

    def _bucket(self, kind: str, key: str) -> dict:
        store = self.data.setdefault(kind, {})
        return store.setdefault(
            key,
            {
                "n": 0,
                "wins": 0,
                "losses": 0,
                "pnl": 0.0,
                "sum_roi": 0.0,
                "size_bias": 1.0,
                "expectancy": 0.0,
            },
        )

    def _apply_outcome(self, b: dict, pnl: float, roi: float | None = None):
        b["n"] = int(b.get("n") or 0) + 1
        b["pnl"] = float(b.get("pnl") or 0) + float(pnl)
        if roi is not None:
            b["sum_roi"] = float(b.get("sum_roi") or 0) + float(roi)
        if pnl >= 0:
            b["wins"] = int(b.get("wins") or 0) + 1
            # Winners: push size up harder as sample grows
            bump = 1.045 if int(b["n"]) >= 8 else 1.03
            b["size_bias"] = min(2.0, float(b.get("size_bias") or 1.0) * bump)
        else:
            b["losses"] = int(b.get("losses") or 0) + 1
            # Losers: cut size faster — protect bankroll
            cut = 0.88 if int(b["n"]) >= 6 else 0.93
            b["size_bias"] = max(0.15, float(b.get("size_bias") or 1.0) * cut)
        n = max(1, int(b["n"]))
        b["expectancy"] = float(b.get("pnl") or 0) / n
        if roi is not None:
            b["avg_roi"] = float(b.get("sum_roi") or 0) / n

    def record_outcome(
        self,
        *,
        trader: str = "",
        title: str = "",
        reason: str = "",
        pnl: float = 0.0,
        side: str = "",
        timeframe: str = "",
        asset: str = "",
        market_slug: str = "",
        price: float | None = None,
        roi: float | None = None,
    ):
        cat = self.categorize(title, reason)
        key = self.setup_key(
            title=title,
            reason=reason,
            side=side,
            timeframe=timeframe,
            asset=asset,
            market_slug=market_slug,
            price=price,
        )
        for kind, k in (
            ("categories", cat),
            ("traders", trader or "edge"),
            ("setups", key),
        ):
            if not k:
                continue
            self._apply_outcome(self._bucket(kind, k), pnl, roi)
        self.save()

    def record_live_tick(
        self,
        *,
        trader: str = "",
        title: str = "",
        reason: str = "",
        roi: float = 0.0,
        mfe_roi: float = 0.0,
        mae_roi: float = 0.0,
        grade: str = "C",
        side: str = "",
        timeframe: str = "",
        asset: str = "",
        market_slug: str = "",
        price: float | None = None,
    ):
        """Soft in-trade learning: nudge size_bias from how the path is playing out."""
        cat = self.categorize(title, reason)
        setup = self.setup_key(
            title=title,
            reason=reason,
            side=side,
            timeframe=timeframe,
            asset=asset,
            market_slug=market_slug,
            price=price,
        )
        live = self.data.setdefault("live", {})
        for key in (setup, cat, trader or "edge"):
            if not key:
                continue
            b = live.setdefault(
                key,
                {
                    "ticks": 0,
                    "sum_roi": 0.0,
                    "sum_mfe": 0.0,
                    "sum_mae": 0.0,
                    "size_bias": 1.0,
                    "last_grade": "C",
                },
            )
            b["ticks"] = int(b.get("ticks") or 0) + 1
            b["sum_roi"] = float(b.get("sum_roi") or 0) + float(roi)
            b["sum_mfe"] = float(b.get("sum_mfe") or 0) + float(mfe_roi)
            b["sum_mae"] = float(b.get("sum_mae") or 0) + float(mae_roi)
            b["last_grade"] = grade
            if roi >= 0.08:
                b["size_bias"] = min(1.6, float(b.get("size_bias") or 1.0) * 1.006)
            elif roi <= -0.10:
                b["size_bias"] = max(0.35, float(b.get("size_bias") or 1.0) * 0.994)
        if int(time.time()) % 5 == 0:
            self.save()

    def record_path_close(
        self,
        *,
        trader: str = "",
        title: str = "",
        reason: str = "",
        pnl: float = 0.0,
        mfe_roi: float = 0.0,
        mae_roi: float = 0.0,
        grade: str = "C",
        held_sec: float = 0.0,
        side: str = "",
        timeframe: str = "",
        asset: str = "",
        market_slug: str = "",
        price: float | None = None,
        cost: float | None = None,
    ):
        """Learn from full path quality at close (beyond raw pnl)."""
        roi = None
        try:
            if cost and float(cost) > 0:
                roi = float(pnl) / float(cost)
        except (TypeError, ValueError):
            roi = None
        self.record_outcome(
            trader=trader,
            title=title,
            reason=reason,
            pnl=pnl,
            side=side,
            timeframe=timeframe,
            asset=asset,
            market_slug=market_slug,
            price=price,
            roi=roi,
        )
        grades = self.data.setdefault("path_grades", {})
        g = grades.setdefault(grade or "C", {"n": 0, "pnl": 0.0})
        g["n"] += 1
        g["pnl"] += float(pnl)

        key = self.setup_key(
            title=title,
            reason=reason,
            side=side,
            timeframe=timeframe,
            asset=asset,
            market_slug=market_slug,
            price=price,
        )
        for kind, k in (
            ("categories", self.categorize(title, reason)),
            ("traders", trader or "edge"),
            ("setups", key),
        ):
            b = self._bucket(kind, k)
            n = max(1, int(b.get("n") or 1))
            b["avg_mfe"] = (
                (float(b.get("avg_mfe") or 0) * (n - 1) + mfe_roi) / n
            )
            b["avg_mae"] = (
                (float(b.get("avg_mae") or 0) * (n - 1) + mae_roi) / n
            )
            b["last_grade"] = grade
            if grade in ("A", "B") and mfe_roi >= 0.10 and mae_roi > -0.12:
                b["size_bias"] = min(2.0, float(b.get("size_bias") or 1.0) * 1.06)
            elif grade == "F" or mae_roi <= -0.25:
                b["size_bias"] = max(0.12, float(b.get("size_bias") or 1.0) * 0.86)
            if held_sec and held_sec < 120 and pnl < 0:
                b["fast_loss"] = int(b.get("fast_loss") or 0) + 1

        recent = self.data.setdefault("recent", [])
        recent.append(
            {
                "ts": time.time(),
                "setup": key,
                "pnl": float(pnl),
                "roi": roi,
                "grade": grade,
                "side": (side or "").upper(),
                "asset": self.extract_asset(title, market_slug, asset),
                "tf": self.extract_timeframe(title, reason, timeframe),
            }
        )
        self.data["recent"] = recent[-120:]
        self.save()

    def _expectancy_mult(self, b: dict) -> tuple[float, list[str], bool]:
        """Map bucket stats → size multiplier + optional hard veto."""
        reasons: list[str] = []
        n = int(b.get("n") or 0)
        if n < 4:
            return 1.0, reasons, False
        wins = int(b.get("wins") or 0)
        win_rate = wins / max(1, n)
        exp = float(b.get("expectancy") or 0.0)
        if exp == 0.0 and n:
            exp = float(b.get("pnl") or 0.0) / n
        bias = float(b.get("size_bias") or 1.0)
        mult = bias

        # Expectancy-first sizing (dollar PnL per trade on this setup)
        if n >= 6 and exp > 0.05:
            mult *= min(1.45, 1.0 + exp * 2.5)
            reasons.append(f"pos_exp={exp:+.2f}")
        elif n >= 5 and exp < -0.08:
            mult *= max(0.25, 0.55 + exp)  # deeper cut when worse
            reasons.append(f"neg_exp={exp:+.2f}")

        if win_rate < 0.38 and n >= 8:
            mult *= 0.55
            reasons.append(f"cold_wr={win_rate:.0%}")
        elif win_rate > 0.58 and n >= 6:
            mult *= 1.12
            reasons.append(f"hot_wr={win_rate:.0%}")

        if int(b.get("fast_loss") or 0) >= 3 and n >= 5:
            mult *= 0.75
            reasons.append("fast_losers")

        # Hard veto earlier — stick-and-tighten (was n>=8 / n>=12)
        veto = False
        if n >= 6 and exp < -0.05 and win_rate < 0.45:
            veto = True
            reasons.append("setup_veto")
        elif n >= 8 and win_rate < 0.38:
            veto = True
            reasons.append("wr_veto")
        elif n >= 10 and exp < 0:
            veto = True
            reasons.append("neg_exp_veto")
        return mult, reasons, veto

    def setup_allowed(self, setup: str, *, allowlist: bool = True) -> tuple[bool, str]:
        """Allowlist from skillbook evidence + RetroValix-style structure filters."""
        if not allowlist:
            return True, "allowlist_off"
        parts = str(setup or "").split("|")
        if len(parts) < 5:
            return False, "bad_setup_key"
        asset, tf, side, fam, band = parts[0], parts[1], parts[2], parts[3], parts[4]
        side_u = side.upper()
        band_l = band.lower()
        fam_l = fam.lower()

        # Toxic cells — even inside otherwise-allowed microstructure families
        if (
            asset.upper() == "SOL"
            and side_u == "UP"
            and "fair_odds" in fam_l
            and band_l == "lean"
        ):
            return False, "ban_sol_fair_up_lean"

        # short_5m momentum sink (-$15 lifetime): only proven DOWN lean/rich + BTC DOWN
        if fam_l == "short_5m" or (fam_l == "momentum" and tf == "5m"):
            if asset.upper() == "BTC" and side_u == "DOWN" and band_l in (
                "cheap",
                "lean",
                "rich",
                "mid",
            ):
                return True, "btc_down_5m"
            if side_u == "DOWN" and band_l in ("lean", "rich"):
                return True, "down_lean_rich_5m"
            return False, "ban_short_5m_cell"

        # Always allow microstructure / near-resolution families
        if fam_l in (
            "complementary_arb",
            "complementary_arb_pair",
            "near_resolution",
            "short_arb",
            "fair_odds",
            "fair_odds_up",
            "fair_odds_down",
            "cross_timeframe",
            "spot_lag",
            "lag_snipe",
            "latency",
        ):
            return True, "microstructure"

        # Ban historically toxic cells
        if side_u == "UP" and band_l in ("cheap", "mid"):
            return False, "ban_up_cheap_mid"
        if asset.upper() == "SOL" and band_l == "cheap":
            return False, "ban_sol_cheap"
        if band_l == "mid" and side_u == "UP":
            return False, "ban_up_mid"

        b = self.data.get("setups", {}).get(setup) or {}
        n = int(b.get("n") or 0)
        wins = int(b.get("wins") or 0)
        wr = wins / n if n else None
        exp = float(b.get("expectancy") or 0.0)
        if exp == 0.0 and n:
            exp = float(b.get("pnl") or 0.0) / n

        # Proven cold → deny
        if n >= 8 and wr is not None and (wr < 0.42 or exp < 0):
            return False, "proven_cold"

        # Prefer DOWN lean/rich (and BTC DOWN cheap/lean which printed)
        if side_u == "DOWN" and band_l in ("lean", "rich"):
            return True, "down_lean_rich"
        if asset.upper() == "BTC" and side_u == "DOWN" and band_l in ("cheap", "lean", "rich"):
            return True, "btc_down"
        if side_u == "DOWN" and band_l == "mid" and (n < 8 or (wr is not None and wr >= 0.48)):
            return True, "down_mid_ok"

        # UP only if rich + not proven cold
        if side_u == "UP" and band_l == "rich" and (n < 8 or (wr is not None and wr >= 0.48 and exp >= 0)):
            return True, "up_rich_ok"

        # Thin unknown: only lean/rich DOWN explore
        if n < 8 and side_u == "DOWN" and band_l in ("lean", "rich", "mid"):
            return True, "thin_down_explore"

        return False, "not_allowlisted"

    def advice(
        self,
        *,
        trader: str = "",
        title: str = "",
        reason: str = "",
        confidence: float = 0.5,
        side: str = "",
        timeframe: str = "",
        asset: str = "",
        market_slug: str = "",
        price: float | None = None,
    ) -> dict[str, Any]:
        cat = self.categorize(title, reason)
        setup = self.setup_key(
            title=title,
            reason=reason,
            side=side,
            timeframe=timeframe,
            asset=asset,
            market_slug=market_slug,
            price=price,
        )
        trader_b = self.data.get("traders", {}).get(trader or "edge") or {}
        cat_b = self.data.get("categories", {}).get(cat) or {}
        setup_b = self.data.get("setups", {}).get(setup) or {}
        live = self.data.get("live", {})
        live_b = live.get(setup) or live.get(trader or "edge") or live.get(cat) or {}

        veto = False
        reasons: list[str] = []
        size_mult = 1.0
        score = float(confidence)

        # Setup grain dominates; category/trader are softer secondary signals
        for label, bucket, weight in (
            ("setup", setup_b, 1.0),
            ("trader", trader_b, 0.55),
            ("category", cat_b, 0.35),
        ):
            if not bucket:
                continue
            mult, rs, hard = self._expectancy_mult(bucket)
            # Blend toward 1.0 by weight so coarse buckets don't dominate early setups
            blended = 1.0 + (mult - 1.0) * weight
            size_mult *= blended
            for r in rs:
                reasons.append(f"{label}:{r}")
            if hard and label == "setup":
                veto = True
            elif hard and label != "setup":
                # Coarse scars shrink size but don't hard-block fine setups alone
                size_mult *= 0.7
                reasons.append(f"{label}:soft_veto")

        if live_b:
            size_mult *= float(live_b.get("size_bias") or 1.0)
            ticks = int(live_b.get("ticks") or 0)
            if ticks >= 20:
                avg_roi = float(live_b.get("sum_roi") or 0) / ticks
                if avg_roi < -0.08:
                    size_mult *= 0.8
                    reasons.append("live_path_cold")
                elif avg_roi > 0.06:
                    size_mult *= 1.08
                    reasons.append("live_path_hot")

        # Recent regime (last 20 closes) — if bleeding, demand more confidence / less size
        recent = list(self.data.get("recent") or [])[-20:]
        if len(recent) >= 8:
            r_pnl = sum(float(x.get("pnl") or 0) for x in recent)
            r_wins = sum(1 for x in recent if float(x.get("pnl") or 0) >= 0)
            r_wr = r_wins / len(recent)
            if r_pnl < -1.5 or r_wr < 0.35:
                size_mult *= 0.65
                score -= 0.04
                reasons.append(f"regime_cold wr={r_wr:.0%}")
            elif r_pnl > 1.5 and r_wr >= 0.55:
                size_mult *= 1.15
                score += 0.03
                reasons.append(f"regime_hot wr={r_wr:.0%}")

        if confidence < 0.35:
            veto = True
            reasons.append("low_confidence")

        # Learned score for ranking candidates (higher = take first)
        setup_n = int(setup_b.get("n") or 0)
        setup_exp = float(setup_b.get("expectancy") or 0)
        if setup_n >= 4:
            score += max(-0.2, min(0.2, setup_exp * 0.8))
        score += (float(size_mult) - 1.0) * 0.08

        return {
            "take": not veto,
            "veto": veto,
            "size_mult": max(0.12, min(2.0, size_mult)),
            "category": cat,
            "setup": setup,
            "reasons": reasons,
            "confidence": confidence,
            "score": score,
            "setup_n": setup_n,
            "setup_expectancy": setup_exp,
        }

    def auto_tune_params(self, params) -> list[str]:
        """Nudge live params from recent closed-trade regime to maximize growth."""
        recent = list(self.data.get("recent") or [])[-30:]
        if len(recent) < 10:
            return []
        msgs: list[str] = []
        r_pnl = sum(float(x.get("pnl") or 0) for x in recent)
        r_wins = sum(1 for x in recent if float(x.get("pnl") or 0) >= 0)
        r_wr = r_wins / len(recent)
        tune = self.data.setdefault("auto_tune", {})
        now = time.time()
        # Don't thrash params more than once per 3 minutes
        if now - float(tune.get("ts") or 0) < 180:
            return []

        cur_conf = float(params.values.get("min_confidence") or 0.72)
        cur_risk = float(params.values.get("risk_frac") or 0.35)

        if r_pnl < -2.0 or r_wr < 0.38:
            # Cap at 0.76 — 0.82 starved TAKEs (honest fair-odds conf tops ~0.84)
            new_conf = min(0.76, cur_conf + 0.02)
            new_risk = max(0.14, cur_risk * 0.90)
            if abs(new_conf - cur_conf) > 1e-6:
                params.set_param("min_confidence", new_conf, who="auto_tune")
                msgs.append(f"min_confidence {cur_conf:.2f}->{new_conf:.2f} (cold regime)")
            if abs(new_risk - cur_risk) > 1e-6:
                params.set_param("risk_frac", new_risk, who="auto_tune")
                msgs.append(f"risk_frac {cur_risk:.2f}->{new_risk:.2f} (cold regime)")
        elif r_pnl > 2.0 and r_wr >= 0.55:
            new_conf = max(0.68, cur_conf - 0.02)
            new_risk = min(0.55, cur_risk * 1.08)
            if abs(new_conf - cur_conf) > 1e-6:
                params.set_param("min_confidence", new_conf, who="auto_tune")
                msgs.append(f"min_confidence {cur_conf:.2f}->{new_conf:.2f} (hot regime)")
            if abs(new_risk - cur_risk) > 1e-6:
                params.set_param("risk_frac", new_risk, who="auto_tune")
                msgs.append(f"risk_frac {cur_risk:.2f}->{new_risk:.2f} (hot regime)")

        if msgs:
            tune["ts"] = now
            tune["last"] = msgs
            tune["recent_pnl"] = r_pnl
            tune["recent_wr"] = r_wr
            self.save()
        return msgs

    def status(self) -> dict[str, Any]:
        traders = sorted(
            self.data.get("traders", {}).items(),
            key=lambda kv: -float(kv[1].get("pnl") or 0),
        )
        cats = sorted(
            self.data.get("categories", {}).items(),
            key=lambda kv: -float(kv[1].get("pnl") or 0),
        )
        setups = sorted(
            self.data.get("setups", {}).items(),
            key=lambda kv: -float(kv[1].get("expectancy") or kv[1].get("pnl") or 0),
        )
        recent = list(self.data.get("recent") or [])[-20:]
        r_pnl = sum(float(x.get("pnl") or 0) for x in recent)
        r_wins = sum(1 for x in recent if float(x.get("pnl") or 0) >= 0)
        return {
            "updated_at": self.data.get("updated_at"),
            "top_traders": [{"key": k, **v} for k, v in traders[:8]],
            "categories": [{"key": k, **v} for k, v in cats],
            "top_setups": [{"key": k, **v} for k, v in setups[:8]],
            "cold_setups": [
                {"key": k, **v}
                for k, v in setups
                if int(v.get("n") or 0) >= 5 and float(v.get("expectancy") or 0) < 0
            ][:8],
            "cold_traders": [
                {"key": k, **v}
                for k, v in traders
                if int(v.get("n") or 0) >= 6 and float(v.get("pnl") or 0) < 0
            ][:6],
            "path_grades": self.data.get("path_grades") or {},
            "live": self.data.get("live") or {},
            "regime": {
                "n": len(recent),
                "pnl": r_pnl,
                "win_rate": (r_wins / len(recent)) if recent else None,
            },
            "auto_tune": self.data.get("auto_tune") or {},
        }


skillbook = SkillBook()
