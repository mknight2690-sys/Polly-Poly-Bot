"""Thin public Polymarket HTTP client (Gamma / CLOB / Data API). No auth required."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
SITE = "https://polymarket.com"


def poly_url(event_slug: str = "", market_slug: str = "") -> str:
    """Deep-link to the market on Polymarket's website."""
    ev = str(event_slug or "").strip().strip("/")
    mk = str(market_slug or "").strip().strip("/")
    if ev and mk and mk != ev:
        return f"{SITE}/event/{ev}/{mk}"
    if ev:
        return f"{SITE}/event/{ev}"
    if mk:
        return f"{SITE}/event/{mk}"
    return ""


def event_slug_from_market(m: dict) -> str:
    """Pull parent event slug from a Gamma market row when present."""
    events = m.get("events")
    if isinstance(events, list) and events:
        slug = events[0].get("slug") if isinstance(events[0], dict) else ""
        if slug:
            return str(slug)
    return str(m.get("eventSlug") or m.get("event_slug") or "")

_HEADERS = {
    "User-Agent": "poly-alert-deck/1.0 (+local; paper research)",
    "Accept": "application/json",
}


class PolyClientError(RuntimeError):
    pass


def _get(url: str, params: dict | None = None, timeout: float = 20.0) -> Any:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise PolyClientError(f"HTTP {e.code} {url}: {body}") from e
    except Exception as e:
        raise PolyClientError(f"{type(e).__name__}: {e}") from e
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise PolyClientError(f"bad JSON from {url}: {e}") from e


def leaderboard(
    period: str = "1d",
    order_by: str = "PNL",
    limit: int = 25,
    category: str | None = None,
) -> list[dict]:
    params: dict[str, Any] = {
        "period": period,
        "orderBy": order_by,
        "limit": int(limit),
    }
    if category:
        params["category"] = category
    data = _get(f"{DATA}/v1/leaderboard", params)
    return list(data or [])


def user_trades(wallet: str, limit: int = 20) -> list[dict]:
    data = _get(f"{DATA}/trades", {"user": wallet, "limit": int(limit)})
    return list(data or [])


def user_activity(wallet: str, limit: int = 20) -> list[dict]:
    data = _get(f"{DATA}/activity", {"user": wallet, "limit": int(limit)})
    return list(data or [])


def markets(
    *,
    active: bool = True,
    closed: bool = False,
    limit: int = 40,
    order: str = "volume24hr",
    ascending: bool = False,
    tag_slug: str | None = None,
) -> list[dict]:
    params: dict[str, Any] = {
        "active": str(active).lower(),
        "closed": str(closed).lower(),
        "limit": int(limit),
        "order": order,
        "ascending": str(ascending).lower(),
    }
    if tag_slug:
        params["tag_slug"] = tag_slug
    data = _get(f"{GAMMA}/markets", params)
    return list(data or [])


def events(
    *,
    active: bool = True,
    closed: bool = False,
    limit: int = 20,
    tag_slug: str | None = None,
) -> list[dict]:
    params: dict[str, Any] = {
        "active": str(active).lower(),
        "closed": str(closed).lower(),
        "limit": int(limit),
    }
    if tag_slug:
        params["tag_slug"] = tag_slug
    data = _get(f"{GAMMA}/events", params)
    return list(data or [])


def event_by_slug(slug: str) -> dict | None:
    data = _get(f"{GAMMA}/events", {"slug": slug})
    if isinstance(data, list) and data:
        return data[0] if isinstance(data[0], dict) else None
    if isinstance(data, dict):
        return data
    return None


def short_crypto_momentum(asset: str, bar: str = "1m", lookback: int = 3) -> dict[str, float]:
    """Spot momentum from Blofin public candles. Newest candle first."""
    sym = f"{asset}-USDT"
    try:
        from official_source_client import get_candles

        rows = get_candles(sym, bar=bar, limit=str(max(8, lookback + 2))) or []
    except Exception:
        rows = []
    closes: list[float] = []
    for r in rows:
        try:
            closes.append(float(r[4]))
        except Exception:
            continue
    if len(closes) < 2:
        return {"ret": 0.0, "strength": 0.0, "dir": 0.0, "last": 0.0}
    newest = closes[0]
    older = closes[min(lookback, len(closes) - 1)]
    ret = (newest - older) / older if older else 0.0
    # last 1-bar impulse
    impulse = (closes[0] - closes[1]) / closes[1] if closes[1] else 0.0
    strength = abs(ret) + 0.5 * abs(impulse)
    direction = 1.0 if ret > 0 else (-1.0 if ret < 0 else 0.0)
    return {
        "ret": ret,
        "impulse": impulse,
        "strength": strength,
        "dir": direction,
        "last": newest,
    }


def public_search(q: str, limit_per_type: int = 8) -> dict:
    data = _get(
        f"{GAMMA}/public-search",
        {"q": q, "limit_per_type": int(limit_per_type)},
    )
    return data if isinstance(data, dict) else {"events": []}


def midpoint(token_id: str) -> float | None:
    data = _get(f"{CLOB}/midpoint", {"token_id": token_id})
    if isinstance(data, dict) and data.get("mid") is not None:
        try:
            return float(data["mid"])
        except (TypeError, ValueError):
            return None
    return None


def parse_outcome_prices(raw: Any) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    out: list[float] = []
    for x in raw or []:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            continue
    return out


def parse_token_ids(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    return [str(x) for x in (raw or []) if x]


def parse_outcomes(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    return [str(x) for x in (raw or [])]


_last_spot: dict[str, float] = {}
_last_spot_ts = 0.0


def crypto_spot(symbols: list[str] | None = None) -> dict[str, float]:
    """Best-effort Blofin/public spot last prices for BTC/ETH etc."""
    global _last_spot, _last_spot_ts
    symbols = symbols or ["BTC-USDT", "ETH-USDT", "SOL-USDT"]
    if time.time() - _last_spot_ts < 5 and _last_spot:
        return dict(_last_spot)
    out: dict[str, float] = {}
    try:
        from official_source_client import get_tickers

        rows = get_tickers(symbols)
        for row in rows or []:
            sym = str(row.get("symbol") or row.get("instId") or "").upper()
            last = row.get("last") or row.get("lastPrice") or row.get("markPrice")
            try:
                px = float(last)
            except (TypeError, ValueError):
                continue
            if sym and px > 0:
                out[sym] = px
                base = sym.split("-")[0]
                out[base] = px
    except Exception:
        pass
    if out:
        _last_spot = out
        _last_spot_ts = time.time()
    return dict(_last_spot)
