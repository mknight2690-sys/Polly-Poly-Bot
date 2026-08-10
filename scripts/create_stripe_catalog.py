#!/usr/bin/env python3
"""Create Polly + Bundle Stripe products, prices, and Payment Links."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CRED_CANDIDATES = [
    Path(r"C:\Users\mknig\vertex-ai-trader\credentials\stripe.txt"),
    Path(r"C:\Users\mknig\OneDrive\Documents\Stripe Secret Key.txt"),
    ROOT / "credentials" / "stripe.txt",
]
OUT = ROOT / "data" / "stripe_catalog.json"
POLY_BUY = DOCS / "poly-setup-buy.html"
BUNDLE_BUY = DOCS / "bundle-buy.html"
HERMES_BUY = DOCS / "hermes-setup-buy.html"


def load_key() -> str:
    env = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if env.startswith(("sk_", "rk_")):
        return env
    for path in CRED_CANDIDATES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"(?:STRIPE_SECRET_KEY\s*=\s*)?(sk_live_[A-Za-z0-9]+|rk_live_[A-Za-z0-9]+|sk_test_[A-Za-z0-9]+|rk_test_[A-Za-z0-9]+)", text)
        if m:
            return m.group(1)
        for line in text.splitlines():
            line = line.strip()
            if line.startswith(("sk_", "rk_")):
                return line
    raise SystemExit("No Stripe secret/restricted key found")


def stripe(method: str, path: str, key: str, data: dict | None = None) -> dict:
    body = urllib.parse.urlencode(data or {}, doseq=True).encode() if data is not None else None
    req = urllib.request.Request(
        f"https://api.stripe.com/v1/{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        raise SystemExit(f"Stripe {method} {path} failed: {err}") from e


def patch_href(path: Path, placeholder: str, url: str) -> None:
    text = path.read_text(encoding="utf-8")
    if placeholder in text:
        path.write_text(text.replace(placeholder, url), encoding="utf-8")
        return
    # already patched or manual — replace first buy.stripe.com in #buy if placeholder gone
    if "buy.stripe.com" in text and placeholder not in text:
        return
    raise SystemExit(f"Placeholder {placeholder} not found in {path}")


def main() -> None:
    key = load_key()
    print("KEY_OK", key[:12] + "...")

    poly_prod = stripe(
        "POST",
        "products",
        key,
        {
            "name": "Polly Poly Bot — Alert Deck Setup Kit",
            "description": "Windows setup guide for the Polymarket Alert Deck: install, dashboard, safe CLOB wiring, dry-run first. Instant digital access.",
            "metadata[tutorial_url]": "https://mknight2690-sys.github.io/Polly-Poly-Bot/poly-alert-deck-setup.html",
        },
    )
    poly_price = stripe(
        "POST",
        "prices",
        key,
        {
            "product": poly_prod["id"],
            "unit_amount": "3700",
            "currency": "usd",
        },
    )
    poly_link = stripe(
        "POST",
        "payment_links",
        key,
        {
            "line_items[0][price]": poly_price["id"],
            "line_items[0][quantity]": "1",
            "after_completion[type]": "redirect",
            "after_completion[redirect][url]": "https://mknight2690-sys.github.io/Polly-Poly-Bot/poly-alert-deck-setup.html",
            "allow_promotion_codes": "true",
        },
    )

    bundle_prod = stripe(
        "POST",
        "products",
        key,
        {
            "name": "Hermes + Polly Setup Bundle",
            "description": "Both digital guides: Hermes brokerage/exchange API setup + Polly Alert Deck setup. Instant access.",
            "metadata[unlock_url]": "https://mknight2690-sys.github.io/Polly-Poly-Bot/bundle-unlock.html",
        },
    )
    bundle_price = stripe(
        "POST",
        "prices",
        key,
        {
            "product": bundle_prod["id"],
            "unit_amount": "6700",
            "currency": "usd",
        },
    )
    bundle_link = stripe(
        "POST",
        "payment_links",
        key,
        {
            "line_items[0][price]": bundle_price["id"],
            "line_items[0][quantity]": "1",
            "after_completion[type]": "redirect",
            "after_completion[redirect][url]": "https://mknight2690-sys.github.io/Polly-Poly-Bot/bundle-unlock.html",
            "allow_promotion_codes": "true",
        },
    )

    catalog = {
        "poly": {
            "product_id": poly_prod["id"],
            "price_id": poly_price["id"],
            "payment_link_id": poly_link["id"],
            "url": poly_link["url"],
            "amount_usd": 37,
        },
        "bundle": {
            "product_id": bundle_prod["id"],
            "price_id": bundle_price["id"],
            "payment_link_id": bundle_link["id"],
            "url": bundle_link["url"],
            "amount_usd": 67,
        },
        "hermes_existing": {
            "url": "https://buy.stripe.com/bJe3cw9QG1pOehhbkoe3e05",
            "amount_usd": 47,
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(catalog, indent=2), encoding="utf-8")
    print(json.dumps(catalog, indent=2))

    patch_href(POLY_BUY, "STRIPE_POLY_PAYMENT_LINK", poly_link["url"])
    patch_href(BUNDLE_BUY, "STRIPE_BUNDLE_PAYMENT_LINK", bundle_link["url"])

    # Cross-sell note already on hermes page via later edit; keep Hermes link intact.
    print("PATCHED", POLY_BUY.name, BUNDLE_BUY.name)
    print("WROTE", OUT)


if __name__ == "__main__":
    main()
