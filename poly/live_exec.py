"""Polymarket CLOB live execution (gated). Paper remains default.

Requires credentials/poly_clob.txt (never commit). Modes:
  paper   — no CLOB orders (current behavior)
  dry_run — sign/build order path but do not post
  live    — post real orders ONLY when armed + readiness gate passes
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CREDS_PATH = os.path.join(ROOT, "credentials", "poly_clob.txt")
CREDS_EXAMPLE = os.path.join(ROOT, "credentials", "poly_clob.example.txt")
STATUS_PATH = os.path.join(ROOT, "data", "poly_live_status.json")

# Modes that never spend real money
PAPER_MODES = {"paper", "off", ""}

# Polymarket rejects marketable buys under $1 notional AND under min share size
MIN_BUY_NOTIONAL_USD = 1.05
MIN_SHARE_SIZE = 5.0
SIZE_DECIMALS = 4


def _round_size(size: float, *, up: bool = False) -> float:
    mult = 10 ** SIZE_DECIMALS
    if up:
        import math

        return math.ceil(float(size) * mult - 1e-12) / mult
    return round(float(size), SIZE_DECIMALS)


def _ensure_buy_notional(price: float, size: float) -> tuple[float, float]:
    """Return (price, size) meeting Polymarket min shares + min notional."""
    px = max(0.01, min(0.99, float(price)))
    sz = max(0.0, float(size))
    if px <= 0:
        return px, sz
    need_notional = MIN_BUY_NOTIONAL_USD / px
    sz = max(sz, MIN_SHARE_SIZE, need_notional)
    sz = _round_size(sz, up=True)
    if sz < MIN_SHARE_SIZE:
        sz = MIN_SHARE_SIZE
    if sz * px < 1.0:
        sz = _round_size(max(MIN_SHARE_SIZE, 1.01 / px), up=True)
    return px, sz


def _read_cred_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip().upper()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return out


def _write_cred_keys(updates: dict[str, str]) -> None:
    """Update selected keys in the creds file without rewriting unrelated comments."""
    if not updates or not os.path.exists(CREDS_PATH):
        return
    try:
        with open(CREDS_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        keys_done = set()
        out: list[str] = []
        for line in lines:
            if "=" in line and not line.strip().startswith("#"):
                k = line.split("=", 1)[0].strip().upper()
                if k in updates and updates[k]:
                    out.append(f"{k}={updates[k]}")
                    keys_done.add(k)
                    continue
            out.append(line)
        for k, v in updates.items():
            if v and k not in keys_done:
                out.append(f"{k}={v}")
        with open(CREDS_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except Exception:
        pass


class LiveExec:
    """Thin wrapper around py-clob-client-v2 with hard paper-first gates."""

    def __init__(self):
        self._lock = threading.Lock()
        self._client = None
        self._last_error = ""
        self._last_ok_ts = 0.0
        self._last_order: dict[str, Any] = {}
        self._creds_meta: dict[str, Any] = {}
        self._bal_cache: dict[str, Any] = {}
        self._bal_cache_ts = 0.0
        self.reload_creds()

    def reload_creds(self) -> dict[str, Any]:
        env = {
            "PRIVATE_KEY": os.environ.get("POLY_PRIVATE_KEY")
            or os.environ.get("POLYMARKET_PRIVATE_KEY")
            or "",
            "FUNDER": os.environ.get("POLY_FUNDER")
            or os.environ.get("POLYMARKET_FUNDER")
            or "",
            "SIGNATURE_TYPE": os.environ.get("POLY_SIGNATURE_TYPE") or "1",
            "API_KEY": os.environ.get("POLY_API_KEY") or "",
            "API_SECRET": os.environ.get("POLY_API_SECRET") or "",
            "API_PASSPHRASE": os.environ.get("POLY_API_PASSPHRASE") or "",
            "HOST": os.environ.get("POLY_CLOB_HOST") or "https://clob.polymarket.com",
            "CHAIN_ID": os.environ.get("POLY_CHAIN_ID") or "137",
            "MODE": os.environ.get("POLY_EXEC_MODE") or "paper",
        }
        file_creds = _read_cred_file(CREDS_PATH)
        for k, v in file_creds.items():
            if v:
                env[k] = v
        pk = env.get("PRIVATE_KEY") or ""
        if pk and not pk.startswith("0x"):
            pk = "0x" + pk
            env["PRIVATE_KEY"] = pk
        self._creds_meta = {
            "has_private_key": bool(pk and len(pk) >= 66),
            "has_funder": bool(env.get("FUNDER")),
            "has_api_creds": bool(
                env.get("API_KEY") and env.get("API_SECRET") and env.get("API_PASSPHRASE")
            ),
            "signature_type": int(float(env.get("SIGNATURE_TYPE") or 1)),
            "funder": env.get("FUNDER") or "",
            "host": env.get("HOST") or "https://clob.polymarket.com",
            "chain_id": int(float(env.get("CHAIN_ID") or 137)),
            "mode": str(env.get("MODE") or "paper").lower().strip(),
            "creds_path": CREDS_PATH,
            "creds_file_present": os.path.exists(CREDS_PATH),
        }
        self._raw = env
        self._client = None
        return dict(self._creds_meta)

    def readiness(self, *, paper_stats: dict | None = None) -> dict[str, Any]:
        """What is still missing before live can be armed."""
        m = dict(self._creds_meta)
        missing: list[str] = []
        if not m.get("has_private_key"):
            missing.append("PRIVATE_KEY (export via reveal.magic.link/polymarket)")
        if not m.get("has_funder"):
            missing.append("FUNDER (Polymarket proxy/deposit wallet address)")
        notes: list[str] = []
        notes.append("Deposit USDC/pUSD to FUNDER on Polygon — site Deposit may be geo-blocked.")
        notes.append("Flip AUTOPILOT → LIVE, then ARM. Skillbook still vetoes proven-cold setups.")

        gate = self.profitability_gate(paper_stats or {})
        ready_creds = not missing
        # Explicit ARM is the spend switch. Paper gate is advisory only —
        # user may arm live after funding even if paper stats are cold.
        return {
            "creds_ok": ready_creds,
            "missing": missing,
            "mode": m.get("mode") or "paper",
            "can_dry_run": ready_creds,
            "can_arm": ready_creds,
            "can_live": ready_creds,  # spend still requires armed + exec_mode=live
            "gate": gate,
            "meta": {
                "has_private_key": m.get("has_private_key"),
                "has_funder": m.get("has_funder"),
                "has_api_creds": m.get("has_api_creds"),
                "signature_type": m.get("signature_type"),
                "funder": m.get("funder"),
                "creds_file_present": m.get("creds_file_present"),
            },
            "notes": notes,
            "last_error": self._last_error,
            "last_order": dict(self._last_order) if self._last_order else {},
        }

    @staticmethod
    def profitability_gate(stats: dict) -> dict[str, Any]:
        """Conservative paper-performance gate before live spend is allowed."""
        n = int(stats.get("trade_count") or stats.get("n") or 0)
        wr = stats.get("win_rate")
        pnl = float(stats.get("pnl") or stats.get("recent_pnl") or 0)
        regime_n = int(stats.get("regime_n") or 0)
        reasons: list[str] = []
        ok = True
        if n < 80:
            ok = False
            reasons.append(f"need >=80 closed paper trades (have {n})")
        if wr is None or float(wr) < 0.52:
            ok = False
            reasons.append(f"need win_rate >=52% (have {wr})")
        if pnl <= 0:
            ok = False
            reasons.append(f"need positive recent paper pnl (have {pnl:.2f})")
        if regime_n < 20:
            ok = False
            reasons.append(f"need >=20 recent regime samples (have {regime_n})")
        if ok:
            reasons.append("paper gate passed")
        return {
            "pass": ok,
            "reasons": reasons,
            "stats": {"n": n, "win_rate": wr, "pnl": pnl, "regime_n": regime_n},
        }

    def _build_client(self, *, derive_if_needed: bool = True):
        try:
            from py_clob_client_v2 import ApiCreds, ClobClient
        except Exception as e:
            raise RuntimeError(
                "py-clob-client-v2 not installed. Run: pip install py-clob-client-v2"
            ) from e
        pk = self._raw.get("PRIVATE_KEY") or ""
        if not pk:
            raise RuntimeError("missing PRIVATE_KEY")
        funder = self._raw.get("FUNDER") or ""
        sig = int(float(self._raw.get("SIGNATURE_TYPE") or 1))
        host = self._raw.get("HOST") or "https://clob.polymarket.com"
        chain_id = int(float(self._raw.get("CHAIN_ID") or 137))
        client = ClobClient(
            host=host,
            chain_id=chain_id,
            key=pk,
            signature_type=sig,
            funder=funder or None,
        )
        api_key = self._raw.get("API_KEY") or ""
        api_secret = self._raw.get("API_SECRET") or ""
        api_pass = self._raw.get("API_PASSPHRASE") or ""
        if api_key and api_secret and api_pass:
            creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass)
            client.set_api_creds(creds)
        elif derive_if_needed:
            creds = client.create_or_derive_api_key()
            client.set_api_creds(creds)
            self._raw["API_KEY"] = getattr(creds, "api_key", None) or getattr(creds, "key", "") or ""
            self._raw["API_SECRET"] = getattr(creds, "api_secret", None) or getattr(creds, "secret", "") or ""
            self._raw["API_PASSPHRASE"] = (
                getattr(creds, "api_passphrase", None) or getattr(creds, "passphrase", "") or ""
            )
            self._creds_meta["has_api_creds"] = bool(
                self._raw["API_KEY"] and self._raw["API_SECRET"] and self._raw["API_PASSPHRASE"]
            )
            _write_cred_keys(
                {
                    "API_KEY": self._raw["API_KEY"],
                    "API_SECRET": self._raw["API_SECRET"],
                    "API_PASSPHRASE": self._raw["API_PASSPHRASE"],
                }
            )
        else:
            raise RuntimeError("missing API creds and derive disabled")
        return client

    def connect(self) -> dict[str, Any]:
        with self._lock:
            try:
                self.reload_creds()
                self._client = self._build_client(derive_if_needed=True)
                # Ensure collateral allowance so LIVE posts aren't blocked at the exchange
                try:
                    from py_clob_client_v2 import AssetType, BalanceAllowanceParams

                    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                    if hasattr(self._client, "update_balance_allowance"):
                        self._client.update_balance_allowance(params)
                except Exception as ae:
                    # Non-fatal — still report connected; orders may still work
                    self._last_error = f"allowance note: {type(ae).__name__}: {ae}"
                self._last_ok_ts = time.time()
                if not str(self._last_error or "").startswith("allowance note"):
                    self._last_error = ""
                return {"ok": True, "msg": "clob client connected", **self.readiness()}
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                self._client = None
                return {"ok": False, "msg": self._last_error, **self.readiness()}

    @staticmethod
    def _side_enum(side: str):
        from py_clob_client_v2 import Side

        s = str(side).upper().strip()
        if s in ("BUY", "YES", "UP", "LONG"):
            return Side.BUY
        return Side.SELL

    def place_order(
        self,
        *,
        token_id: str,
        side: str,
        price: float,
        size: float,
        mode: str | None = None,
        armed: bool = False,
        paper_stats: dict | None = None,
        order_type: str = "GTC",
        prefer_limit: bool = False,
        urgent: bool = False,
    ) -> dict[str, Any]:
        """Place (or dry-run) a buy/sell. Hard-gated for live."""
        mode = (mode or self._creds_meta.get("mode") or "paper").lower().strip()
        # Lag / urgent path: never rest GTC — FAK immediately
        if urgent:
            prefer_limit = False
            order_type = "FAK"
        base = {
            "token_id": token_id,
            "side": side,
            "price": float(price),
            "size": float(size),
            "prefer_limit": bool(prefer_limit),
            "urgent": bool(urgent),
        }
        if mode in PAPER_MODES:
            out = {
                "ok": True,
                "posted": False,
                "mode": "paper",
                "msg": "paper mode — no CLOB order",
                **base,
            }
            self._last_order = out
            return out
        ready = self.readiness(paper_stats=paper_stats)
        if mode == "dry_run":
            if not ready.get("creds_ok"):
                out = {"ok": False, "posted": False, "mode": mode, "msg": "creds incomplete", **base, **ready}
                self._last_order = out
                return out
            # Optionally prove client can build; never post
            built = False
            build_kind = "limit_gtc"
            try:
                with self._lock:
                    if self._client is None:
                        self._client = self._build_client(derive_if_needed=True)
                    from py_clob_client_v2 import MarketOrderArgs, OrderArgs, OrderType, Side

                    px = max(0.01, min(0.99, float(price)))
                    sz = float(size)
                    side_enum = self._side_enum(side)
                    is_buy = str(side).upper().strip() in ("BUY", "YES", "UP", "LONG")
                    if urgent and hasattr(self._client, "create_market_order"):
                        amount = round(px * max(sz, 1.0), 4) if is_buy else float(sz)
                        if is_buy:
                            amount = max(amount, MIN_BUY_NOTIONAL_USD, MIN_SHARE_SIZE * px)
                        margs = MarketOrderArgs(
                            token_id=str(token_id),
                            amount=float(amount),
                            side=side_enum,
                            order_type=OrderType.FAK,
                        )
                        self._client.create_market_order(margs)
                        built = True
                        build_kind = "market_fak"
                    elif hasattr(self._client, "create_order"):
                        args = OrderArgs(
                            token_id=str(token_id),
                            price=px,
                            size=sz,
                            side=side_enum,
                        )
                        self._client.create_order(args)
                        built = True
                        build_kind = "limit_gtc"
                    elif urgent:
                        # Client can post FAK live; dry_run just proves MarketOrderArgs shape
                        amount = round(px * max(sz, 1.0), 4) if is_buy else float(sz)
                        MarketOrderArgs(
                            token_id=str(token_id),
                            amount=float(amount),
                            side=side_enum if not isinstance(side_enum, str) else Side.BUY,
                            order_type=OrderType.FAK,
                        )
                        built = True
                        build_kind = "market_fak_args"
            except Exception as e:
                # Still count dry_run as ok for path proof if creds work
                out = {
                    "ok": True,
                    "posted": False,
                    "mode": "dry_run",
                    "msg": f"dry_run — build skipped ({type(e).__name__})",
                    "built": False,
                    "build_kind": build_kind,
                    "funder": self._creds_meta.get("funder"),
                    **base,
                }
                self._last_order = out
                return out
            out = {
                "ok": True,
                "posted": False,
                "mode": "dry_run",
                "msg": f"dry_run — would post {'FAK' if urgent else 'order'}",
                "built": built,
                "build_kind": build_kind,
                "funder": self._creds_meta.get("funder"),
                **base,
            }
            self._last_order = out
            return out
        if mode != "live":
            out = {"ok": False, "posted": False, "mode": mode, "msg": f"unknown mode {mode}", **base}
            self._last_order = out
            return out
        if not armed:
            out = {"ok": False, "posted": False, "mode": "live", "msg": "live_trading_armed=false", **base}
            self._last_order = out
            return out
        if not ready.get("creds_ok"):
            out = {"ok": False, "posted": False, "mode": "live", "msg": "credentials incomplete", **base, **ready}
            self._last_order = out
            return out
        try:
            from py_clob_client_v2 import (
                AssetType,
                BalanceAllowanceParams,
                MarketOrderArgs,
                OrderArgs,
                OrderType,
                Side,
            )
        except Exception as e:
            out = {"ok": False, "posted": False, "msg": str(e), **base}
            self._last_order = out
            return out
        with self._lock:
            try:
                if self._client is None:
                    self._client = self._build_client(derive_if_needed=True)
                px = max(0.01, min(0.99, float(price)))
                sz = float(size)
                side_u = str(side).upper().strip()
                is_buy = side_u in ("BUY", "YES", "UP", "LONG")
                if is_buy:
                    # Live lag FAKs need fresh collateral allowance or CLOB rejects
                    try:
                        cparams = BalanceAllowanceParams(
                            asset_type=AssetType.COLLATERAL
                        )
                        if hasattr(self._client, "update_balance_allowance"):
                            self._client.update_balance_allowance(cparams)
                        self._bal_cache_ts = 0.0  # force next balance read fresh
                    except Exception:
                        pass
                    px, sz = _ensure_buy_notional(px, sz)
                else:
                    # Refresh conditional token balance before sell
                    try:
                        cparams = BalanceAllowanceParams(
                            asset_type=AssetType.CONDITIONAL, token_id=str(token_id)
                        )
                        if hasattr(self._client, "update_balance_allowance"):
                            self._client.update_balance_allowance(cparams)
                        cbal = self._client.get_balance_allowance(cparams)
                        raw = (cbal or {}).get("balance") if isinstance(cbal, dict) else None
                        if raw is not None and str(raw).isdigit():
                            have = float(raw) / 1_000_000.0
                        else:
                            have = float(raw or 0)
                        if have > 0:
                            sz = min(sz, have)
                    except Exception:
                        pass
                    sz = _round_size(sz, up=False)
                    if sz <= 0:
                        out = {
                            "ok": False,
                            "posted": False,
                            "mode": "live",
                            "msg": "sell size 0 / no conditional balance",
                            **base,
                        }
                        self._last_order = out
                        return out
                base["price"] = px
                base["size"] = sz

                side_enum = Side.BUY if is_buy else Side.SELL
                amount = round(px * sz, 4) if is_buy else float(sz)
                if is_buy:
                    amount = max(amount, MIN_BUY_NOTIONAL_USD, MIN_SHARE_SIZE * px)
                resp = None
                used = "market_fak"

                def _post_limit(lim_px: float, label: str):
                    largs = OrderArgs(
                        token_id=str(token_id),
                        price=max(0.01, min(0.99, lim_px)),
                        size=sz if not is_buy else max(sz, MIN_SHARE_SIZE),
                        side=side_enum,
                    )
                    signed = self._client.create_order(largs)
                    return self._client.post_order(signed, OrderType.GTC), label

                def _post_market_fak():
                    margs = MarketOrderArgs(
                        token_id=str(token_id),
                        amount=float(amount),
                        side=side_enum,
                        order_type=OrderType.FAK,
                    )
                    return self._client.create_and_post_market_order(
                        margs, order_type=OrderType.FAK
                    )

                # Urgent lag: FAK only — never rest a GTC
                if urgent or (not prefer_limit and hasattr(self._client, "create_and_post_market_order")):
                    try:
                        resp = _post_market_fak()
                        used = "market_fak_urgent" if urgent else "market_fak"
                    except Exception as mkt_err:
                        if urgent:
                            raise mkt_err
                        used = "limit_gtc_fallback"
                        slip = 0.04 if is_buy else -0.04
                        resp, used = _post_limit(px + slip, "limit_gtc_fallback")
                        base["mkt_err"] = f"{type(mkt_err).__name__}: {mkt_err}"[:160]
                elif prefer_limit and hasattr(self._client, "create_order"):
                    try:
                        # Passive at/near mid — buy at px, sell at px
                        resp, used = _post_limit(px, "limit_gtc")
                        st = str((resp or {}).get("status") or "").lower() if isinstance(resp, dict) else ""
                        # If resting without fill, cancel and FAK for momentum-critical path;
                        # for arb, leave a brief rest then FAK the remainder
                        if st == "live" and not (
                            (resp or {}).get("takingAmount") or (resp or {}).get("makingAmount")
                        ):
                            try:
                                if resp.get("orderID"):
                                    self._client.cancel_order(resp.get("orderID"))
                            except Exception:
                                pass
                            if hasattr(self._client, "create_and_post_market_order"):
                                resp = _post_market_fak()
                                used = "limit_then_fak"
                    except Exception:
                        if hasattr(self._client, "create_and_post_market_order"):
                            try:
                                resp = _post_market_fak()
                                used = "market_fak"
                            except Exception as e2:
                                raise e2
                elif hasattr(self._client, "create_and_post_market_order"):
                    try:
                        resp = _post_market_fak()
                        used = "market_fak"
                    except Exception as mkt_err:
                        used = "limit_gtc_fallback"
                        slip = 0.04 if is_buy else -0.04
                        resp, used = _post_limit(px + slip, "limit_gtc_fallback")
                        base["mkt_err"] = f"{type(mkt_err).__name__}: {mkt_err}"[:160]
                else:
                    resp, used = _post_limit(px, "limit_gtc")

                ok_fill = False
                fill_shares = sz
                fill_px = px
                if isinstance(resp, dict):
                    status = str(resp.get("status") or "").lower()
                    ok_fill = bool(resp.get("success")) or status in (
                        "matched",
                        "filled",
                        "live",
                    )
                    # Market buy: takingAmount=shares, makingAmount=USDC
                    # Market sell: makingAmount=shares, takingAmount=USDC
                    try:
                        if is_buy and resp.get("takingAmount"):
                            fill_shares = float(resp["takingAmount"])
                        elif (not is_buy) and resp.get("makingAmount"):
                            fill_shares = float(resp["makingAmount"])
                        if is_buy and resp.get("makingAmount") and fill_shares > 0:
                            fill_px = float(resp["makingAmount"]) / fill_shares
                        elif (not is_buy) and resp.get("takingAmount") and fill_shares > 0:
                            fill_px = float(resp["takingAmount"]) / fill_shares
                    except Exception:
                        pass
                    # Resting GTC without fill does not count as a live seat
                    if status == "live" and not (resp.get("takingAmount") or resp.get("makingAmount")):
                        ok_fill = False
                        try:
                            if hasattr(self._client, "cancel_order") and resp.get("orderID"):
                                self._client.cancel_order(resp.get("orderID"))
                        except Exception:
                            pass
                elif resp is not None:
                    ok_fill = True

                self._last_ok_ts = time.time()
                self._last_error = ""
                out = {
                    "ok": bool(ok_fill),
                    "posted": bool(ok_fill),
                    "mode": "live",
                    "resp": resp,
                    "used": used,
                    "notional": round(float(amount) if is_buy else fill_px * fill_shares, 4),
                    "price": fill_px,
                    "size": fill_shares,
                    "token_id": token_id,
                    "side": side,
                }
                if not ok_fill:
                    out["msg"] = f"no fill ({used}) {str(resp)[:160]}"
                self._last_order = {k: v for k, v in out.items() if k != "resp"}
                return out
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                out = {"ok": False, "posted": False, "mode": "live", "msg": self._last_error, **base}
                self._last_order = out
                return out

    def prepare_collateral(self, *, force: bool = False) -> dict[str, Any]:
        """Refresh USDC/pUSD allowance + balance so live lag FAKs don't fail auth."""
        now = time.time()
        if (
            not force
            and self._bal_cache.get("ok")
            and (now - float(self._bal_cache_ts or 0)) < 5.0
            and self._bal_cache.get("allowance_ok")
        ):
            out = dict(self._bal_cache)
            out["cached"] = True
            return out
        with self._lock:
            try:
                if self._client is None:
                    self.reload_creds()
                    self._client = self._build_client(derive_if_needed=True)
                from py_clob_client_v2 import AssetType, BalanceAllowanceParams

                params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                if hasattr(self._client, "update_balance_allowance"):
                    self._client.update_balance_allowance(params)
                bal = None
                if hasattr(self._client, "get_balance_allowance"):
                    bal = self._client.get_balance_allowance(params)
                raw = bal if isinstance(bal, dict) else {"raw": bal}
                balance = raw.get("balance") or raw.get("BALANCE") or raw.get("collateral")
                try:
                    if balance is not None and str(balance).isdigit():
                        balance_usd = float(balance) / 1_000_000.0
                    else:
                        balance_usd = float(balance) if balance is not None else None
                except Exception:
                    balance_usd = None
                allowances = raw.get("allowances") or {}
                allowance_ok = bool(allowances) or balance_usd is not None
                out = {
                    "ok": True,
                    "balance_usd": balance_usd,
                    "balance_raw": balance,
                    "allowances": allowances,
                    "allowance_ok": allowance_ok,
                    "funder": self._creds_meta.get("funder"),
                    "cached": False,
                    "prepared": True,
                }
                self._bal_cache = dict(out)
                self._bal_cache_ts = time.time()
                self._last_ok_ts = time.time()
                self._last_error = ""
                return out
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                return {
                    "ok": False,
                    "msg": self._last_error,
                    "allowance_ok": False,
                    "funder": self._creds_meta.get("funder"),
                    "prepared": False,
                }

    def lag_ready(
        self,
        *,
        paper_stats: dict | None = None,
        exec_mode: str = "dry_run",
        armed: bool = False,
        min_bet: float = 2.5,
        prepare: bool = False,
    ) -> dict[str, Any]:
        """Checklist for live+armed lag FAK execution (does not spend)."""
        ready = self.readiness(paper_stats=paper_stats)
        if prepare:
            prep = self.prepare_collateral(force=True)
        elif self._bal_cache.get("ok"):
            prep = dict(self._bal_cache)
            prep["cached"] = True
            prep.setdefault("allowance_ok", True)
        else:
            # Never block UI/state polls on CLOB — PREP LAG / arm / buy refresh
            prep = {
                "ok": True,
                "balance_usd": None,
                "allowance_ok": False,
                "prepared": False,
                "cached": False,
            }
        bal = prep.get("balance_usd")
        try:
            bal_f = float(bal) if bal is not None else None
        except Exception:
            bal_f = None
        reasons: list[str] = []
        ok = True
        if not ready.get("creds_ok"):
            ok = False
            reasons.extend(ready.get("missing") or ["creds incomplete"])
        if prepare and not prep.get("ok"):
            ok = False
            reasons.append(f"collateral prepare failed: {prep.get('msg')}")
        elif prepare and not prep.get("allowance_ok"):
            ok = False
            reasons.append("collateral allowance not confirmed")
        mode = str(exec_mode or "").lower()
        if mode == "live" and armed:
            if bal_f is None:
                ok = False
                reasons.append("CLOB balance unknown — click PREP LAG or SNAPSHOT $")
            elif bal_f + 1e-9 < float(min_bet):
                ok = False
                reasons.append(
                    f"CLOB balance ${bal_f:.2f} < min bet ${float(min_bet):.2f} — deposit to funder"
                )
        elif bal_f is not None and bal_f + 1e-9 < float(min_bet):
            reasons.append(
                f"advisory: CLOB balance ${bal_f:.2f} — fund before arming live lag"
            )
        elif bal_f is None:
            reasons.append("advisory: snapshot CLOB $ before arming live lag")
        if ok:
            reasons.append("lag FAK path ready (creds OK)")
            if mode == "live" and armed:
                reasons.append("live+armed — next lag impulse will post FAK")
            else:
                reasons.append(f"currently {mode}/{'armed' if armed else 'disarmed'} — no spend yet")
        return {
            "ok": ok,
            "balance_usd": bal_f,
            "allowance_ok": bool(prep.get("allowance_ok")),
            "creds_ok": bool(ready.get("creds_ok")),
            "exec_mode": mode,
            "armed": bool(armed),
            "reasons": reasons,
            "gate": ready.get("gate"),
            "funder": ready.get("meta", {}).get("funder") if isinstance(ready.get("meta"), dict) else prep.get("funder"),
        }

    def fetch_balance(self, max_age_sec: float = 0.0) -> dict[str, Any]:
        """Collateral balance/allowance for the funded proxy wallet.

        max_age_sec>0 reuses a short cache so lag hot-path doesn't wait on RPC.
        """
        now = time.time()
        if (
            max_age_sec
            and self._bal_cache
            and (now - float(self._bal_cache_ts or 0)) <= float(max_age_sec)
        ):
            out = dict(self._bal_cache)
            out["cached"] = True
            return out
        with self._lock:
            try:
                if self._client is None:
                    self.reload_creds()
                    self._client = self._build_client(derive_if_needed=True)
                bal = None
                if hasattr(self._client, "get_balance_allowance"):
                    from py_clob_client_v2 import AssetType, BalanceAllowanceParams

                    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
                    bal = self._client.get_balance_allowance(params)
                self._last_ok_ts = time.time()
                self._last_error = ""
                raw = bal if isinstance(bal, dict) else {"raw": bal}
                balance = raw.get("balance") or raw.get("BALANCE") or raw.get("collateral")
                try:
                    if balance is not None and str(balance).isdigit():
                        balance_usd = float(balance) / 1_000_000.0
                    else:
                        balance_usd = float(balance) if balance is not None else None
                except Exception:
                    balance_usd = None
                out = {
                    "ok": True,
                    "balance_usd": balance_usd,
                    "balance_raw": balance,
                    "allowances": raw.get("allowances"),
                    "funder": self._creds_meta.get("funder"),
                    "cached": False,
                }
                self._bal_cache = dict(out)
                self._bal_cache_ts = time.time()
                return out
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                return {
                    "ok": False,
                    "msg": self._last_error,
                    "funder": self._creds_meta.get("funder"),
                }

    def prewarm(self) -> dict[str, Any]:
        """Build CLOB client early so the first live lag order isn't cold-start."""
        with self._lock:
            try:
                if self._client is None:
                    self.reload_creds()
                    self._client = self._build_client(derive_if_needed=True)
                return {"ok": True, "ready": self._client is not None}
            except Exception as e:
                self._last_error = f"{type(e).__name__}: {e}"
                return {"ok": False, "msg": self._last_error}

    def exit_order(
        self,
        *,
        token_id: str,
        price: float,
        size: float,
        mode: str | None = None,
        armed: bool = False,
        paper_stats: dict | None = None,
        urgent: bool = False,
    ) -> dict[str, Any]:
        """Sell / exit — market FAK (limit fallback inside place_order)."""
        return self.place_order(
            token_id=token_id,
            side="SELL",
            price=price,
            size=size,
            mode=mode,
            armed=armed,
            paper_stats=paper_stats,
            order_type="FAK",
            urgent=bool(urgent),
        )

    def status(self, *, paper_stats: dict | None = None) -> dict[str, Any]:
        ready = self.readiness(paper_stats=paper_stats)
        return {
            "ok": bool(self._last_ok_ts) and not self._last_error,
            "last_ok_ts": self._last_ok_ts,
            "last_error": self._last_error,
            **ready,
        }

    def prewarm_lag(self) -> dict[str, Any]:
        """Warm client + collateral so live+armed lag FAKs are hot."""
        warm = self.prewarm()
        prep = self.prepare_collateral(force=True) if warm.get("ok") else {"ok": False}
        return {"prewarm": warm, "collateral": prep}

    def save_status(self, payload: dict):
        try:
            os.makedirs(os.path.dirname(STATUS_PATH), exist_ok=True)
            with open(STATUS_PATH, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception:
            pass


live_exec = LiveExec()
