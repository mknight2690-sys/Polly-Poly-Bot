"""Scan Polymarket for high-edge opportunities — short 1m/5m crypto first."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from . import client as poly
from . import fees as poly_fees
from . import microstructure as micro
from .spot_lead import spot_lead

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EDGES_PATH = os.path.join(ROOT, "data", "poly_edges.json")

_ASSET = r"(bitcoin|btc|ethereum|eth|solana|sol)"
_STRIKE = r"\$?\s*([\d,]+(?:\.\d+)?)"
_REACH = re.compile(
    rf"(?:will\s+)?(?:the\s+price\s+of\s+)?{_ASSET}\s+(?:reach|hit|be\s+above|above|over)\s+{_STRIKE}",
    re.I,
)
_DIP = re.compile(
    rf"(?:will\s+)?(?:the\s+price\s+of\s+)?{_ASSET}\s+(?:dip\s+to|fall\s+to|be\s+below|below|under)\s+{_STRIKE}",
    re.I,
)
_ASSET_MAP = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
}
_SHORT_ASSETS = ("btc", "eth", "sol")
# 5m primary; 15m for cross-TF lag; 1m if published
_SHORT_TFS = (("5m", 300), ("15m", 900), ("1m", 60))
_EDGE_MAX_AGE_SEC = 90.0


def _parse_end_ts(end: str, fallback: float | None = None) -> float | None:
    if end:
        try:
            return datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
        except Exception:
            pass
    return fallback


class EdgeScanner:
    def __init__(self):
        self._lock = threading.Lock()
        self.edges: list[dict] = []
        self.candidates: list[dict] = []
        self.last_error = ""
        self.last_ok_ts = 0.0
        self.refresh_count = 0
        self.last_windows = 0
        self._shorts_cache: list[dict] = []
        self._shorts_cache_ts = 0.0
        self.lag_hot_count = 0
        self.load()

    def load(self):
        try:
            with open(EDGES_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.edges = self._prune_stale(list(data.get("edges") or []))
            self.last_ok_ts = float(data.get("last_ok_ts") or 0.0)
        except Exception:
            pass

    def save(self):
        os.makedirs(os.path.dirname(EDGES_PATH), exist_ok=True)
        payload = {
            "built_at": time.time(),
            "edges": self.edges[:60],
            "last_ok_ts": self.last_ok_ts,
            "last_error": self.last_error,
        }
        tmp = EDGES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, EDGES_PATH)

    @staticmethod
    def _step_for(tf: str) -> int:
        return 60 if str(tf) == "1m" else 300

    def _prune_stale(self, rows: list[dict]) -> list[dict]:
        """Drop expired / ancient edges so the dashboard never shows dead windows."""
        now = time.time()
        out: list[dict] = []
        for e in rows:
            row = dict(e)
            tf = str(row.get("timeframe") or "")
            step = self._step_for(tf)
            win = row.get("window_ts")
            end_ts = _parse_end_ts(
                str(row.get("end_date") or ""),
                float(win) + step if win else None,
            )
            if end_ts is not None:
                secs_left = end_ts - now
                row["secs_left"] = secs_left
                if secs_left < 8:
                    continue
            age = now - float(row.get("ts") or 0)
            if float(row.get("ts") or 0) > 0 and age > max(_EDGE_MAX_AGE_SEC * 3, step + 30):
                continue
            out.append(row)
        return out

    @staticmethod
    def _market_row(m: dict, *, tag: str = "", event_slug: str = "") -> dict | None:
        if m.get("closed") or m.get("archived"):
            return None
        if m.get("active") is False:
            return None
        if str(m.get("umaResolutionStatus") or "").lower() == "resolved":
            return None
        prices = poly.parse_outcome_prices(m.get("outcomePrices"))
        tokens = poly.parse_token_ids(m.get("clobTokenIds"))
        outcomes = poly.parse_outcomes(m.get("outcomes")) or ["Yes", "No"]
        if len(prices) < 2 or len(tokens) < 2:
            return None
        yes_px, no_px = prices[0], prices[1]
        # Settled boards still show active=true briefly — skip lottery/locks.
        if (yes_px <= 0.02 and no_px >= 0.98) or (no_px <= 0.02 and yes_px >= 0.98):
            return None
        vol = float(m.get("volume24hr") or m.get("volume") or 0.0)
        liq = float(m.get("liquidityNum") or m.get("liquidity") or 0.0)
        market_slug = str(m.get("slug") or "")
        ev = event_slug or poly.event_slug_from_market(m)
        return {
            "title": m.get("question") or m.get("title") or "",
            "market_slug": market_slug,
            "event_slug": ev,
            "url": poly.poly_url(ev, market_slug),
            "condition_id": m.get("conditionId") or "",
            "yes_price": yes_px,
            "no_price": no_px,
            "yes_token": tokens[0],
            "no_token": tokens[1],
            "outcomes": outcomes,
            "volume24hr": vol,
            "liquidity": liq,
            "end_date": m.get("endDate") or "",
            "tag": tag,
        }

    def _collect_short_crypto(self) -> list[dict]:
        """Live 1m/5m Up/Down windows via known Polymarket slug schedule (parallel)."""
        now = int(time.time())
        jobs: list[tuple[str, str, int, int, str]] = []
        for asset in _SHORT_ASSETS:
            for tf, step in _SHORT_TFS:
                base = (now // step) * step
                # 5m: prior/current/next/next+1 · 1m: current/next only (often unpublished)
                offs = (-1, 0, 1, 2) if tf in ("5m", "15m") else (0, 1)
                for off in offs:
                    ts = base + off * step
                    if off < 0 and (now - ts) > step + 20:
                        continue  # prior window already past by clock
                    jobs.append((asset, tf, step, ts, f"{asset}-updown-{tf}-{ts}"))

        fetched: list[tuple[str, str, int, int, str, dict | None]] = []
        with ThreadPoolExecutor(max_workers=12) as pool:
            futs = {
                pool.submit(poly.event_by_slug, slug): (asset, tf, step, ts, slug)
                for asset, tf, step, ts, slug in jobs
            }
            for fut in as_completed(futs):
                asset, tf, step, ts, slug = futs[fut]
                try:
                    ev = fut.result()
                except Exception:
                    ev = None
                fetched.append((asset, tf, step, ts, slug, ev))

        rows: list[dict] = []
        seen: set[str] = set()
        now_f = time.time()
        for asset, tf, step, ts, slug, ev in fetched:
            if not ev or ev.get("closed") or ev.get("archived"):
                continue
            ev_slug = str(ev.get("slug") or slug)
            markets = list(ev.get("markets") or [])
            if not markets:
                # Some Gamma payloads only have top-level event fields — synthesize one row.
                markets = [ev]
            for m in markets:
                r = self._market_row(m, tag=f"short_{tf}", event_slug=ev_slug)
                if not r:
                    continue
                # Prefer event title when market question is missing
                if not r.get("title"):
                    r["title"] = ev.get("title") or slug
                if not r.get("end_date"):
                    r["end_date"] = ev.get("endDate") or ""
                key = r["market_slug"] or r["condition_id"] or slug
                if not key or key in seen:
                    continue
                end_ts = _parse_end_ts(str(r.get("end_date") or ""), float(ts + step))
                secs_left = float(end_ts) - now_f if end_ts is not None else float(step)
                if secs_left < 8:
                    continue
                r["asset"] = asset.upper()
                r["timeframe"] = tf
                r["secs_left"] = secs_left
                r["window_ts"] = ts
                r["stream_ts"] = now_f
                seen.add(key)
                rows.append(r)
        # Prefer soonest-expiring (live) windows first
        rows.sort(key=lambda x: float(x.get("secs_left") or 9e9))
        return rows

    def _score_short_updown(self, row: dict, min_conf: float, params: dict | None = None) -> dict | None:
        params = params or {}
        asset = str(row.get("asset") or "")
        tf = str(row.get("timeframe") or "5m")
        if not asset:
            return None

        # Wire previously-dead board filters
        min_edge = float(params.get("edge_min_edge") or 0.03)
        max_spread = float(params.get("edge_max_spread") or 0.08)
        min_liq = float(params.get("edge_min_liquidity") or 500.0)
        min_vol = float(params.get("edge_min_volume_24h") or 1000.0)
        liq = float(row.get("liquidity") or 0)
        vol = float(row.get("volume24hr") or 0)
        if liq and liq < min_liq:
            return None
        if vol and vol < min_vol:
            return None

        bar = "1m" if tf == "1m" else "5m"
        lookback = 2 if tf == "1m" else 3
        mom = poly.short_crypto_momentum(asset, bar=bar, lookback=lookback)
        strength = float(mom.get("strength") or 0.0)
        direction = float(mom.get("dir") or 0.0)

        up_px = float(row["yes_price"])   # outcome 0 = Up
        down_px = float(row["no_price"])  # outcome 1 = Down
        outcomes = [str(x).lower() for x in (row.get("outcomes") or [])]
        if outcomes and "down" in outcomes[0]:
            up_px, down_px = down_px, up_px
            up_token, down_token = row["no_token"], row["yes_token"]
        else:
            up_token, down_token = row["yes_token"], row["no_token"]

        pair_sum = up_px + down_px
        # Spread proxy: deviation of pair from $1 (book friction / mispricing)
        pair_gap = abs(pair_sum - 1.0)
        if pair_gap > max_spread and pair_sum >= 1.0:
            return None

        secs_left = float(row.get("secs_left") or 0)
        prefer_down = bool(params.get("prefer_down", True))
        arb_on = bool(params.get("complementary_arb", True))
        snipe_on = bool(params.get("near_resolution_snipe", True))
        arb_max = float(params.get("arb_sum_max") or 0.985)

        # --- RetroValix #1: BOTH-LEG complementary arb (Up+Down < $1) ---
        if arb_on and pair_sum > 0 and pair_sum <= arb_max:
            edge = 1.0 - pair_sum
            conf = min(0.92, 0.72 + edge * 5.0)
            if conf >= min(min_conf, 0.68):
                # Prefer equal-share both legs; tilt shares later in engine
                tilt = "DOWN" if (prefer_down and direction <= 0) or down_px <= up_px else "UP"
                return {
                    **row,
                    "source": "edge",
                    "reason": "edge:complementary_arb_pair",
                    "side": "PAIR",
                    "token_id": up_token,  # primary; legs carry both
                    "price": pair_sum / 2.0,
                    "fair": 0.5,
                    "edge": edge,
                    "confidence": conf,
                    "momentum_ret": mom.get("ret"),
                    "momentum_strength": strength,
                    "spot": mom.get("last"),
                    "tag": f"arb_pair_{tf}",
                    "pair_sum": pair_sum,
                    "both_legs": True,
                    "prefer_limit": True,
                    "hold_to_resolve": True,
                    "tilt": tilt,
                    "legs": [
                        {
                            "side": "UP",
                            "token_id": up_token,
                            "price": up_px,
                        },
                        {
                            "side": "DOWN",
                            "token_id": down_token,
                            "price": down_px,
                        },
                    ],
                    "high_prob": True,
                    "rank_score": 12.0 + edge * 25 + conf,
                    "ts": time.time(),
                }

        # --- Spot lead lag snipe (Binance → Polymarket book lag) ---
        lag_hit = self._score_lag_snipe(
            row,
            params,
            min_conf=min_conf,
            up_px=up_px,
            down_px=down_px,
            up_token=up_token,
            down_token=down_token,
            secs_left=secs_left,
            asset=asset,
            tf=tf,
        )
        if lag_hit:
            return lag_hit

        # --- RetroValix #6: near-resolution snipe ---
        if snipe_on and secs_left <= 40:
            for side, price, token, want_dir in (
                ("UP", up_px, up_token, 1),
                ("DOWN", down_px, down_token, -1),
            ):
                if 0.92 <= price <= 0.985 and (direction == 0 or direction == want_dir or strength < 0.0004):
                    # Require mom not fighting hard against the near-certain side
                    if direction != 0 and direction != want_dir and strength >= 0.001:
                        continue
                    edge = 1.0 - price
                    conf = min(0.90, 0.78 + edge * 8)
                    return {
                        **row,
                        "source": "edge",
                        "reason": "edge:near_resolution",
                        "side": side,
                        "token_id": token,
                        "price": price,
                        "fair": 0.99,
                        "edge": edge,
                        "confidence": conf,
                        "momentum_ret": mom.get("ret"),
                        "momentum_strength": strength,
                        "spot": mom.get("last"),
                        "tag": f"snipe_{tf}",
                        "prefer_limit": True,
                        "high_prob": True,
                        "rank_score": 8.0 + edge * 15,
                        "ts": time.time(),
                    }

        # --- Fair-odds repricing (BS/EWMA vs window strike) ---
        # Paper curve: cheap fair_odds_down was pure bleed (0% WR). Require
        # lean+ entry, mom agreement, and real edge vs corrected strike.
        if bool(params.get("fair_odds_model", True)) and secs_left >= 90:
            try:
                fo = micro.fair_updown(
                    asset,
                    secs_left=secs_left,
                    window_ts=float(row.get("window_ts") or 0) or None,
                    up_px=up_px,
                    down_px=down_px,
                )
                eu = float(fo.get("edge_up") or 0)
                ed = float(fo.get("edge_down") or 0)
                min_fair = float(params.get("fair_min_edge") or 0.045)
                mom_dir = float(fo.get("dir") or 0)
                # Ban lottery tickets — mid/lean only (no sub-0.48 junk)
                min_px = float(params.get("fair_min_price") or 0.48)
                max_px = float(params.get("fair_max_price") or 0.74)
                if (
                    eu >= min_fair
                    and eu >= ed
                    and min_px <= up_px <= max_px
                    and mom_dir >= 0  # don't fade a down tape with UP fair
                ):
                    conf = min(0.84, 0.55 + eu * 2.2)
                    if conf >= min_conf * 0.90:
                        return {
                            **row,
                            "source": "edge",
                            "reason": "edge:fair_odds_up",
                            "side": "UP",
                            "token_id": up_token,
                            "price": up_px,
                            "fair": fo.get("p_up"),
                            "edge": eu,
                            "confidence": conf,
                            "spot": fo.get("spot"),
                            "strike": fo.get("strike"),
                            "tag": f"fair_{tf}",
                            "prefer_limit": True,
                            "high_prob": True,
                            "rank_score": 6.0 + eu * 12 + conf,
                            "ts": time.time(),
                        }
                # DOWN paused by default — last session 0% WR / -$4.8
                allow_down = bool(params.get("fair_odds_allow_down", False))
                if (
                    allow_down
                    and ed >= max(min_fair, 0.12)
                    and ed > eu
                    and max(min_px, 0.50) <= down_px <= max_px
                    and mom_dir <= 0
                ):
                    conf = min(0.84, 0.55 + ed * 2.2)
                    if conf >= min_conf * 0.90:
                        return {
                            **row,
                            "source": "edge",
                            "reason": "edge:fair_odds_down",
                            "side": "DOWN",
                            "token_id": down_token,
                            "price": down_px,
                            "fair": fo.get("p_down"),
                            "edge": ed,
                            "confidence": conf,
                            "spot": fo.get("spot"),
                            "strike": fo.get("strike"),
                            "tag": f"fair_{tf}",
                            "prefer_limit": True,
                            "high_prob": True,
                            "rank_score": 6.2 + ed * 12 + conf,
                            "ts": time.time(),
                        }
            except Exception:
                pass

        # --- Repricing / momentum (tightened) ---
        if direction == 0 or strength < (0.00055 if tf == "1m" else 0.00085):
            return None

        want_up = direction > 0
        # Prefer DOWN historically unless UP is clearly richer + strong
        if prefer_down:
            if want_up and (up_px < 0.62 or strength < 0.0018):
                if 0.50 <= down_px <= 0.78:
                    want_up = False
            elif not want_up and down_px < 0.50:
                return None  # don't chase cheap DOWN lottery
        side = "UP" if want_up else "DOWN"
        price = up_px if want_up else down_px
        token = up_token if want_up else down_token
        if price <= 0.12 or price >= 0.90:
            return None

        # Ban toxic entry cells early
        if want_up and price < 0.55:
            return None  # UP cheap/mid historically bled
        if str(asset).upper() == "SOL" and price < 0.40:
            return None
        if secs_left < (40 if tf == "1m" else 90):
            return None

        lean = min(0.28, 0.06 + strength * 100)
        fair = 0.5 + lean
        edge = fair - price
        agrees = price >= 0.62
        value = 0.45 <= price <= 0.72
        if not (value or agrees):
            return None
        if edge < min_edge and not (agrees and strength >= (0.001 if tf == "1m" else 0.0012)):
            return None

        # Honest confidence — stop capping everything near 0.94
        conf = 0.50 + min(0.18, strength * 90) + max(0.0, edge) * 0.55
        if agrees:
            conf += 0.04
        if not want_up:
            conf += 0.03  # slight DOWN prior from book
        step = 60.0 if tf == "1m" else 300.0
        if secs_left > step * 1.05:
            conf -= 0.10
            if strength < (0.0014 if tf == "5m" else 0.0010):
                return None
        if secs_left < (30 if tf == "1m" else 70):
            conf -= 0.10
        conf = max(0.0, min(0.82, conf))
        if conf < min_conf:
            return None

        return {
            **row,
            "source": "edge",
            "reason": f"edge:short_{tf}_momentum",
            "side": side,
            "token_id": token,
            "price": price,
            "fair": fair,
            "edge": max(edge, min_edge),
            "confidence": conf,
            "momentum_ret": mom.get("ret"),
            "momentum_strength": strength,
            "spot": mom.get("last"),
            "tag": f"short_{tf}",
            "pair_sum": pair_sum,
            "high_prob": True,
            "rank_score": conf * 2 + max(edge, 0) * 3 + (1.5 if not want_up else 1.0),
            "ts": time.time(),
        }

    def _score_lag_snipe(
        self,
        row: dict,
        params: dict,
        *,
        min_conf: float,
        up_px: float,
        down_px: float,
        up_token: str,
        down_token: str,
        secs_left: float,
        asset: str,
        tf: str,
    ) -> dict | None:
        """Buy the lagging Up/Down side after a hard Binance spot impulse."""
        import math

        if not bool(params.get("lag_snipe", True)):
            return None
        tfs = {
            x.strip().lower()
            for x in str(params.get("lag_tfs") or "5m,15m").split(",")
            if x.strip()
        }
        if str(tf).lower() not in tfs:
            return None
        step = {"1m": 60.0, "5m": 300.0, "15m": 900.0}.get(str(tf).lower(), 300.0)
        age = step - float(secs_left or 0)
        # Skip brand-new windows (noise) and last few seconds (resolution race)
        if age < (20.0 if tf != "15m" else 40.0):
            return None
        if secs_left < 12:
            return None

        lookback = float(params.get("lag_lookback_sec") or 4.0)
        min_move = float(params.get("lag_min_move") or 0.0007)
        lead = spot_lead.ret(asset, lookback)
        ret = float(lead.get("ret") or 0.0)
        if abs(ret) < min_move:
            # End-cycle favorite: spot already decided, book still cheap-ish
            if not bool(params.get("lag_endgame", True)):
                return None
            eg_lo = float(params.get("lag_endgame_min_sec") or 25.0)
            eg_hi = float(params.get("lag_endgame_max_sec") or 90.0)
            eg_px = float(params.get("lag_endgame_min_price") or 0.72)
            if not (eg_lo <= secs_left <= eg_hi):
                return None
            # softer move OK near resolve if favorite agrees with lead dir
            soft = spot_lead.ret(asset, max(lookback, 8.0))
            sret = float(soft.get("ret") or 0.0)
            if abs(sret) < min_move * 0.35:
                return None
            want_up = sret > 0
            price = up_px if want_up else down_px
            token = up_token if want_up else down_token
            side = "UP" if want_up else "DOWN"
            if price < eg_px or price > 0.94:
                return None
            fair = min(0.96, max(0.78, price + 0.08 + abs(sret) * 8))
            edge = fair - price
            fee_rate = float(params.get("paper_fee_rate") or 0.07)
            fee_px = poly_fees.taker_fee_usdc(1.0, price, fee_rate=fee_rate)
            need = 0.02 + fee_px + float(params.get("lag_slip_buffer") or 0.01) * 0.5
            if edge < need:
                return None
            conf = min(0.90, 0.70 + edge * 2.5 + abs(sret) * 20)
            return {
                **row,
                "source": "edge",
                "reason": "edge:spot_lag_endgame",
                "side": side,
                "token_id": token,
                "price": price,
                "fair": fair,
                "edge": edge,
                "confidence": conf,
                "spot": soft.get("spot"),
                "spot_ret": sret,
                "spot_lookback": max(lookback, 8.0),
                "fee_px": fee_px,
                "tag": f"lag_eg_{tf}",
                "prefer_limit": False,  # speed — taker/FAK path
                "high_prob": True,
                "rank_score": 9.5 + edge * 18 + conf,
                "ts": time.time(),
            }

        want_up = ret > 0
        price = up_px if want_up else down_px
        token = up_token if want_up else down_token
        side = "UP" if want_up else "DOWN"
        lo = float(params.get("lag_min_price") or 0.42)
        hi = float(params.get("lag_max_price") or 0.82)
        if price < lo or price > hi:
            return None

        # Map impulse → fair for the sided contract
        tilt = math.tanh(abs(ret) / 0.0025) * 0.42
        fair = 0.5 + tilt
        edge = fair - price
        fee_rate = float(params.get("paper_fee_rate") or 0.07)
        fee_px = poly_fees.taker_fee_usdc(1.0, price, fee_rate=fee_rate)
        need = (
            float(params.get("lag_min_edge") or 0.04)
            + fee_px
            + float(params.get("lag_slip_buffer") or 0.01)
        )
        if edge < need:
            return None

        conf = min(0.88, 0.58 + abs(ret) * 55 + edge * 1.4)
        if conf < min_conf * 0.88:
            return None

        return {
            **row,
            "source": "edge",
            "reason": "edge:spot_lag",
            "side": side,
            "token_id": token,
            "price": price,
            "fair": fair,
            "edge": edge,
            "confidence": conf,
            "spot": lead.get("spot"),
            "spot_ret": ret,
            "spot_lookback": lookback,
            "fee_px": fee_px,
            "tag": f"lag_{tf}",
            "prefer_limit": False,
            "high_prob": True,
            "rank_score": 10.0 + edge * 20 + abs(ret) * 80,
            "ts": time.time(),
        }

    def _crypto_fair_edge(self, row: dict, spot: dict[str, float]) -> dict | None:
        title = row["title"]
        yes = float(row["yes_price"])
        no = float(row["no_price"])
        if yes <= 0.001 or no <= 0.001:
            return None
        mode = None
        asset = None
        strike = None
        m = _REACH.search(title)
        if m:
            mode = "reach"
            asset = _ASSET_MAP.get(m.group(1).lower())
            strike = float(m.group(2).replace(",", ""))
        else:
            m = _DIP.search(title)
            if m:
                mode = "dip"
                asset = _ASSET_MAP.get(m.group(1).lower())
                strike = float(m.group(2).replace(",", ""))
        if not (mode and asset and strike and spot.get(asset)):
            return None
        px = float(spot[asset])
        if mode == "reach":
            dist = (px - strike) / max(strike, 1.0)
            fair_yes = 0.5 + max(-0.48, min(0.48, dist * 3.2))
        else:
            dist = (strike - px) / max(strike, 1.0)
            fair_yes = 0.5 + max(-0.48, min(0.48, dist * 3.2))
        fair_yes = max(0.02, min(0.98, fair_yes))
        edge_yes = fair_yes - yes
        edge_no = (1.0 - fair_yes) - no

        def pack(side: str, edge: float, price: float, fair: float, token: str) -> dict:
            return {
                **row,
                "source": "edge",
                "reason": "edge:crypto_spot_gap",
                "side": side,
                "token_id": token,
                "price": price,
                "fair": fair,
                "edge": edge,
                "spot": px,
                "strike": strike,
                "asset": asset,
                "mode": mode,
                "confidence": min(0.9, 0.48 + edge),
                "tag": "crypto",
            }

        if edge_yes >= edge_no and edge_yes > 0.02:
            return pack("YES", edge_yes, yes, fair_yes, row["yes_token"])
        if edge_no > 0.02:
            return pack("NO", edge_no, no, 1.0 - fair_yes, row["no_token"])
        return None

    def refresh(self, params: dict[str, Any]) -> dict[str, Any]:
        short_only = bool(params.get("short_crypto_only", True))
        min_conf = float(params.get("min_confidence") or 0.72)
        errors: list[str] = []
        scored: list[dict] = []

        # --- Primary: live 1m / 5m updown ---
        try:
            shorts = self._collect_short_crypto()
            with self._lock:
                self._shorts_cache = list(shorts or [])
                self._shorts_cache_ts = time.time()
        except Exception as e:
            shorts = []
            errors.append(f"short:{e}")
        for row in shorts:
            try:
                hit = self._score_short_updown(row, min_conf, params)
            except Exception as e:
                errors.append(f"score:{e}")
                hit = None
            if hit:
                scored.append(hit)

        # --- RetroValix #4: cross-timeframe lag (5m lead → 15m lag) ---
        if bool(params.get("cross_timeframe", True)):
            by_asset: dict[str, dict[str, dict]] = {}
            for row in shorts:
                a = str(row.get("asset") or "").upper()
                tf = str(row.get("timeframe") or "")
                if not a or tf not in ("5m", "15m"):
                    continue
                by_asset.setdefault(a, {})[tf] = row
            for a, tmap in by_asset.items():
                if "5m" not in tmap or "15m" not in tmap:
                    continue
                try:
                    lag = micro.cross_tf_lag(
                        tmap["5m"],
                        tmap["15m"],
                        min_gap=float(params.get("cross_tf_min_gap") or 0.08),
                    )
                except Exception as e:
                    errors.append(f"xtf:{e}")
                    lag = None
                if not lag:
                    continue
                long_row = tmap["15m"]
                # Resolve tokens with same Up/Down label logic
                up_px = float(long_row["yes_price"])
                down_px = float(long_row["no_price"])
                outcomes = [str(x).lower() for x in (long_row.get("outcomes") or [])]
                if outcomes and "down" in outcomes[0]:
                    up_px, down_px = down_px, up_px
                    up_tok, down_tok = long_row["no_token"], long_row["yes_token"]
                else:
                    up_tok, down_tok = long_row["yes_token"], long_row["no_token"]
                side = lag["side"]
                price = float(lag["price"])
                token = up_tok if side == "UP" else down_tok
                gap = float(lag["gap"])
                conf = min(0.86, 0.58 + gap * 2.0)
                if conf < min_conf * 0.9:
                    continue
                scored.append(
                    {
                        **long_row,
                        "source": "edge",
                        "reason": "edge:cross_timeframe",
                        "side": side,
                        "token_id": token,
                        "price": price,
                        "fair": price + gap,
                        "edge": gap,
                        "confidence": conf,
                        "tag": "xtf_15m",
                        "prefer_limit": True,
                        "high_prob": True,
                        "rank_score": 7.0 + gap * 14 + conf,
                        "lead_tf": lag.get("lead_tf"),
                        "lag_tf": lag.get("lag_tf"),
                        "ts": time.time(),
                    }
                )

        # --- Optional longer crypto (off in short-only turbo mode) ---
        if not short_only:
            spot = poly.crypto_spot()
            # light volume board for crypto strike markets
            try:
                for m in poly.markets(limit=20, tag_slug="crypto", order="volume24hr", ascending=False):
                    row = self._market_row(m, tag="crypto")
                    if not row:
                        continue
                    hit = self._crypto_fair_edge(row, spot)
                    if hit and float(hit.get("confidence") or 0) >= min_conf:
                        hit["rank_score"] = float(hit.get("edge") or 0)
                        hit["ts"] = time.time()
                        scored.append(hit)
            except Exception as e:
                errors.append(str(e))

        # Prefer live (soonest) windows, then rank_score
        scored.sort(
            key=lambda x: (
                float(x.get("secs_left") or 9e9) > 360,  # future boards last
                -float(x.get("rank_score") or 0),
                float(x.get("secs_left") or 9e9),
            )
        )
        top = self._prune_stale(scored[:40])
        if bool(params.get("lag_only", True)):
            # Board can still show other scored edges; seat queue is lag-only
            lag_edges = [
                e for e in top if "spot_lag" in str(e.get("reason") or "")
            ]
            cand_src = lag_edges[:10]
        else:
            cand_src = top[:10]
        new_cands = []
        for e in cand_src:
            new_cands.append(
                {
                    "source": "edge",
                    "reason": e.get("reason"),
                    "title": e.get("title"),
                    "market_slug": e.get("market_slug"),
                    "event_slug": e.get("event_slug"),
                    "url": e.get("url") or poly.poly_url(
                        str(e.get("event_slug") or ""),
                        str(e.get("market_slug") or ""),
                    ),
                    "condition_id": e.get("condition_id"),
                    "token_id": e.get("token_id"),
                    "side": e.get("side"),
                    "price": e.get("price"),
                    "edge": e.get("edge"),
                    "fair": e.get("fair"),
                    "confidence": e.get("confidence"),
                    "liquidity": e.get("liquidity"),
                    "volume24hr": e.get("volume24hr"),
                    "timeframe": e.get("timeframe"),
                    "asset": e.get("asset"),
                    "secs_left": e.get("secs_left"),
                    "window_ts": e.get("window_ts"),
                    "end_date": e.get("end_date"),
                    "pair_sum": e.get("pair_sum"),
                    "both_legs": e.get("both_legs"),
                    "legs": e.get("legs"),
                    "hold_to_resolve": e.get("hold_to_resolve"),
                    "prefer_limit": e.get("prefer_limit"),
                    "tilt": e.get("tilt"),
                    "high_prob": True,
                    "urgent_fak": "spot_lag" in str(e.get("reason") or ""),
                    "spot_ret": e.get("spot_ret"),
                    "spot": e.get("spot"),
                    "fee_px": e.get("fee_px"),
                    "ts": e.get("ts") or time.time(),
                    "copy_trader": "",
                }
            )

        with self._lock:
            self.edges = top
            # Replace queue with fresh high-prob shorts (don't pile stale)
            self.candidates = new_cands
            self.refresh_count += 1
            self.last_windows = len(shorts)
            self.last_error = "; ".join(errors)[:240]
            # ok as long as the Polymarket probe completed (even if no edge scored)
            if not errors or shorts:
                self.last_ok_ts = time.time()
            self.save()

        return {
            "edges": len(top),
            "candidates": len(new_cands),
            "short_windows": len(shorts),
            "errors": errors[:5],
        }

    def _shorts_fresh(self, *, max_age: float = 2.5) -> list[dict]:
        """Reuse recent short windows so lag hot-path skips Gamma round-trips."""
        now = time.time()
        with self._lock:
            cached = list(self._shorts_cache)
            age = now - float(self._shorts_cache_ts or 0)
        if cached and age <= max_age:
            out: list[dict] = []
            for row in cached:
                r = dict(row)
                try:
                    win = float(r.get("window_ts") or 0)
                    tf = str(r.get("timeframe") or "5m")
                    step = 60.0 if tf == "1m" else (900.0 if tf == "15m" else 300.0)
                    if win:
                        r["secs_left"] = win + step - now
                except Exception:
                    pass
                out.append(r)
            return out
        try:
            shorts = self._collect_short_crypto()
        except Exception:
            shorts = cached
        with self._lock:
            self._shorts_cache = list(shorts or [])
            self._shorts_cache_ts = now
            self.last_windows = len(self._shorts_cache)
        return list(shorts or [])

    def _cand_from_edge(self, e: dict) -> dict:
        return {
            "source": "edge",
            "reason": e.get("reason"),
            "title": e.get("title"),
            "market_slug": e.get("market_slug"),
            "event_slug": e.get("event_slug"),
            "url": e.get("url")
            or poly.poly_url(
                str(e.get("event_slug") or ""),
                str(e.get("market_slug") or ""),
            ),
            "condition_id": e.get("condition_id"),
            "token_id": e.get("token_id"),
            "side": e.get("side"),
            "price": e.get("price"),
            "edge": e.get("edge"),
            "fair": e.get("fair"),
            "confidence": e.get("confidence"),
            "liquidity": e.get("liquidity"),
            "volume24hr": e.get("volume24hr"),
            "timeframe": e.get("timeframe"),
            "asset": e.get("asset"),
            "secs_left": e.get("secs_left"),
            "window_ts": e.get("window_ts"),
            "end_date": e.get("end_date"),
            "prefer_limit": e.get("prefer_limit"),
            "high_prob": True,
            "urgent_fak": True,
            "spot_ret": e.get("spot_ret"),
            "spot": e.get("spot"),
            "fee_px": e.get("fee_px"),
            "ts": e.get("ts") or time.time(),
            "copy_trader": "",
        }

    def refresh_lag_only(self, params: dict[str, Any]) -> list[dict]:
        """Fast path: score spot_lag on cached windows; prepend to candidate queue."""
        if not bool(params.get("lag_snipe", True)):
            return []
        min_conf = float(params.get("min_confidence") or 0.72)
        shorts = self._shorts_fresh(max_age=float(params.get("lag_cache_sec") or 2.5))
        hits: list[dict] = []
        for row in shorts:
            try:
                up_px = float(row["yes_price"])
                down_px = float(row["no_price"])
                outcomes = [str(x).lower() for x in (row.get("outcomes") or [])]
                if outcomes and "down" in outcomes[0]:
                    up_px, down_px = down_px, up_px
                    up_token, down_token = row["no_token"], row["yes_token"]
                else:
                    up_token, down_token = row["yes_token"], row["no_token"]
                hit = self._score_lag_snipe(
                    row,
                    params,
                    min_conf=min_conf,
                    up_px=up_px,
                    down_px=down_px,
                    up_token=up_token,
                    down_token=down_token,
                    secs_left=float(row.get("secs_left") or 0),
                    asset=str(row.get("asset") or ""),
                    tf=str(row.get("timeframe") or "5m"),
                )
            except Exception:
                hit = None
            if hit:
                hits.append(hit)
        hits.sort(key=lambda x: -float(x.get("rank_score") or 0))
        cands = [self._cand_from_edge(h) for h in hits[:6]]
        with self._lock:
            # Merge lag hits onto the live board (don't wipe other edges)
            others = [
                e
                for e in self.edges
                if "spot_lag" not in str(e.get("reason") or "")
            ]
            self.edges = self._prune_stale(hits[:6] + others)[:40]
            rest = [
                c
                for c in self.candidates
                if "spot_lag" not in str(c.get("reason") or "")
            ]
            self.candidates = cands + rest
            self.lag_hot_count += 1
            if hits:
                self.last_ok_ts = time.time()
        return cands

    def pop_candidates(self, limit: int = 12) -> list[dict]:
        with self._lock:
            live = self._prune_stale(list(self.candidates))
            out = live[:limit]
            self.candidates = live[limit:]
            return out

    def status(self) -> dict[str, Any]:
        with self._lock:
            live = self._prune_stale(list(self.edges))
            self.edges = live
            age = time.time() - float(self.last_ok_ts or 0) if self.last_ok_ts else None
            return {
                "ok": bool(self.last_ok_ts) and (age is not None and age < 90),
                "last_ok_ts": self.last_ok_ts,
                "age_sec": age,
                "last_error": self.last_error,
                "refresh_count": self.refresh_count,
                "short_windows": self.last_windows,
                "edges": live[:25],
                "pending_candidates": len(self.candidates),
                "lag_hot_count": self.lag_hot_count,
                "shorts_cache_age": (
                    time.time() - float(self._shorts_cache_ts or 0)
                    if self._shorts_cache_ts
                    else None
                ),
            }


edge_scanner = EdgeScanner()
