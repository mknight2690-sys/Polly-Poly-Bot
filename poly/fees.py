"""Polymarket taker fee helpers for paper / dry-run accounting.

Official formula (docs.polymarket.com/trading/fees):
    fee = C × feeRate × p × (1 − p)

Crypto short Up/Down uses feeRate = 0.07. Makers pay 0; paper assumes taker
fills (FAK / aggressive) unless explicitly marked maker.
"""
from __future__ import annotations

# Category → taker feeRate (protocol)
TAKER_FEE_RATE = {
    "crypto": 0.07,
    "sports": 0.05,
    "finance": 0.04,
    "politics": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "mentions": 0.04,
    "tech": 0.04,
    "geopolitics": 0.0,
}

DEFAULT_CRYPTO_RATE = 0.07


def taker_fee_usdc(
    shares: float,
    price: float,
    *,
    fee_rate: float = DEFAULT_CRYPTO_RATE,
) -> float:
    """USDC taker fee for C shares at price p. Rounded to 5 decimals."""
    c = float(shares or 0.0)
    p = float(price or 0.0)
    if c <= 0 or p <= 0 or p >= 1.0:
        return 0.0
    rate = max(0.0, float(fee_rate or 0.0))
    if rate <= 0:
        return 0.0
    raw = c * rate * p * (1.0 - p)
    # Protocol: round to 5 decimals; dust below 1e-5 → 0
    fee = round(raw + 1e-12, 5)
    return fee if fee >= 1e-5 else 0.0


def fee_rate_for_category(category: str = "crypto") -> float:
    return float(TAKER_FEE_RATE.get(str(category or "crypto").lower(), DEFAULT_CRYPTO_RATE))
