#!/usr/bin/env python3
"""Owned-channel traffic: YouTube description refresh + X post (if credits)."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERTEX = Path(r"C:\Users\mknig\vertex-ai-trader")
YT_TOKEN = VERTEX / "youtube_token.json"
X_CREDS = VERTEX / "credentials" / "x.txt"
CATALOG = ROOT / "data" / "stripe_catalog.json"

STORE = "https://mknight2690-sys.github.io/Polly-Poly-Bot/"
HERMES_BUY = STORE + "hermes-setup-buy.html"
POLY_BUY = STORE + "poly-setup-buy.html"
BUNDLE_BUY = STORE + "bundle-buy.html"


def load_kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def update_youtube() -> None:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    scopes = [
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.readonly",
    ]
    creds = Credentials.from_authorized_user_file(str(YT_TOKEN), scopes)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        YT_TOKEN.write_text(creds.to_json(), encoding="utf-8")
    yt = build("youtube", "v3", credentials=creds)

    poly_url = POLY_BUY
    bundle_url = BUNDLE_BUY
    if CATALOG.exists():
        cat = json.loads(CATALOG.read_text(encoding="utf-8"))
        # pages already patched; keep GH Pages URLs for SEO

    desc = (
        "Setup guides store:\n"
        f"{STORE}\n\n"
        f"Hermes ($47): {HERMES_BUY}\n"
        f"Polly Alert Deck ($37): {poly_url}\n"
        f"Bundle ($67): {bundle_url}\n\n"
        "Stripe checkouts are on each buy page.\n"
        "Paste API credentials only into local credential files — never into chat.\n"
    )
    for vid, title in [
        ("0YWuDWPQCtU", "Hermes: Trade Any Stock/Crypto Account via Your Brokerage API"),
        ("kFE1XvYrGeQ", "Hermes on Windows — 1-Minute Auto-Trader Setup ($47)"),
    ]:
        body = {
            "id": vid,
            "snippet": {
                "title": title,
                "categoryId": "28",
                "tags": ["Hermes", "Polly", "Polymarket", "trading", "API", "automation"],
                "description": desc,
            },
        }
        resp = yt.videos().update(part="snippet", body=body).execute()
        print("YT_OK", resp["id"], resp["snippet"]["title"])


def post_x() -> None:
    if not X_CREDS.exists():
        print("X_SKIP missing credentials/x.txt")
        return
    c = load_kv(X_CREDS)
    cid, csec, rt = c["X_CLIENT_ID"], c["X_CLIENT_SECRET"], c["X_REFRESH_TOKEN"]
    basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    data = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid}
    ).encode()
    req = urllib.request.Request(
        "https://api.x.com/2/oauth2/token",
        data=data,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.loads(r.read().decode())
    at = tok["access_token"]
    nrt = tok.get("refresh_token") or rt
    lines = []
    for line in X_CREDS.read_text(encoding="utf-8").splitlines():
        if line.startswith("X_ACCESS_TOKEN="):
            lines.append("X_ACCESS_TOKEN=" + at)
        elif line.startswith("X_REFRESH_TOKEN=") and nrt:
            lines.append("X_REFRESH_TOKEN=" + nrt)
        else:
            lines.append(line)
    X_CREDS.write_text("\n".join(lines) + "\n", encoding="utf-8")

    text = (
        "New storefront: Hermes + Polly setup guides\n\n"
        f"Hermes API ($47): {HERMES_BUY}\n"
        f"Polly Alert Deck ($37): {POLY_BUY}\n"
        f"Bundle ($67): {BUNDLE_BUY}\n\n"
        f"All guides: {STORE}"
    )
    body = json.dumps({"text": text}).encode()
    req2 = urllib.request.Request(
        "https://api.x.com/2/tweets",
        data=body,
        headers={"Authorization": f"Bearer {at}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req2, timeout=30) as r:
            print("X_OK", r.read().decode())
    except urllib.error.HTTPError as e:
        print("X_ERR", e.read().decode())


def main() -> None:
    try:
        update_youtube()
    except Exception as e:
        print("YT_ERR", type(e).__name__, str(e)[:240])
    try:
        post_x()
    except Exception as e:
        print("X_ERR", type(e).__name__, str(e)[:240])


if __name__ == "__main__":
    main()
