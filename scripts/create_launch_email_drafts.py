#!/usr/bin/env python3
"""Create Gmail drafts for product launch (approval-only; does not send).

Requires valid token.json in vertex-ai-trader (run reauth_gmail.py if revoked).
"""

from __future__ import annotations

import base64
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

VERTEX = Path(r"C:\Users\mknig\vertex-ai-trader")
TOKEN = VERTEX / "token.json"
CREDS = VERTEX / "credentials.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

STORE = "https://mknight2690-sys.github.io/Polly-Poly-Bot/"

# Only use addresses you have permission to email (self / opted-in).
EMAILS = [
    {
        "to": "me",  # resolved to authenticated Gmail address
        "subject": "Launch checklist — Hermes + Polly storefront is live",
        "body": f"""Storefront is ready:

{STORE}

Products:
- Hermes API setup — $47 — {STORE}hermes-setup-buy.html
- Polly Alert Deck setup — $37 — {STORE}poly-setup-buy.html
- Bundle — $67 — {STORE}bundle-buy.html

Next traffic (owned channels):
1) Hard-refresh YouTube descriptions (script: scripts/owned_channel_traffic.py)
2) Post on X when API credits available
3) Pin store link in YouTube / GitHub README (done in README)

Reply to yourself with any opted-in list addresses before mass send.
""",
    },
]


def service():
    os.chdir(VERTEX)
    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                print("GMAIL_REFRESH_FAILED", e)
                creds = None
        if not creds or not creds.valid:
            if not CREDS.exists():
                raise SystemExit("Missing credentials.json — cannot open OAuth")
            print("Launching Gmail OAuth browser...")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds, static_discovery=False)


def main() -> None:
    svc = service()
    profile = svc.users().getProfile(userId="me").execute()
    me = profile.get("emailAddress", "")
    print("GMAIL", me)
    for item in EMAILS:
        to = me if item["to"] == "me" else item["to"]
        msg = MIMEMultipart()
        msg["to"] = to
        msg["subject"] = item["subject"]
        msg.attach(MIMEText(item["body"], "plain"))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        draft = (
            svc.users()
            .drafts()
            .create(userId="me", body={"message": {"raw": raw}})
            .execute()
        )
        print("DRAFT", draft.get("id"), "->", to)


if __name__ == "__main__":
    main()
