# Polly Poly Bot

Polymarket **Alert Deck** — paper trading, live dry-run / armed CLOB execution, voice alerts, lag edges, and mental trailing stops.

Dashboard: `http://127.0.0.1:18112`

## Quick start

```bash
pip install -r requirements.txt
cp credentials/poly_clob.example.txt credentials/poly_clob.txt
# agent fills PRIVATE_KEY + FUNDER (see below) — leave API_* blank
python run_poly.py
```

Open the deck in Chrome and hard-refresh after upgrades. Execution boots **dry_run + disarmed** — arm only when the user explicitly asks to spend.

## Desktop shortcuts (Windows) — start / stop Polly

From the repo folder you can one-click create Desktop shortcuts:

```powershell
cd path\to\Polly-Poly-Bot
powershell -NoProfile -ExecutionPolicy Bypass -File .\create_desktop_shortcuts.ps1
```

That puts **Start Polly** and **Stop Polly** on your Desktop.

| Shortcut | What it runs | Effect |
|----------|----------------|--------|
| **Start Polly** | `Start Polly.bat` → `start_polly.ps1` | Starts `python run_poly.py` in the background (skips if port **18112** is already listening). Deck: http://127.0.0.1:18112/ |
| **Stop Polly** | `Stop Polly.bat` → `stop_polly.ps1` | Kills the process listening on **18112** and any `run_poly.py` Python process |

### Make the shortcuts yourself (manual)

1. Open the repo folder in File Explorer.
2. Right-click **Start Polly.bat** → **Show more options** (Win11) → **Send to** → **Desktop (create shortcut)** — or drag to Desktop while holding `Alt`.
3. Repeat for **Stop Polly.bat**.
4. Optional: rename the Desktop icons to **Start Polly** / **Stop Polly**.

### Run without shortcuts

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\start_polly.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\stop_polly.ps1
```

Logs (start): `logs/polly_stdout.log` and `logs/polly_stderr.log`.

## Layout

| Path | Role |
|------|------|
| `run_poly.py` | Launch server on port 18112 |
| `start_polly.ps1` / `Start Polly.bat` | Start deck |
| `stop_polly.ps1` / `Stop Polly.bat` | Stop deck |
| `create_desktop_shortcuts.ps1` | Install Desktop Start/Stop shortcuts |
| `poly/` | Engine, edges, live exec, UI |
| `credentials/poly_clob.example.txt` | Creds template (copy → `poly_clob.txt`) |
| `data/` | Local runtime state (gitignored) |

---

## AI agent playbook — get CLOB working for a new user

**Goal:** The human only turns on a VPN in an allowed country, creates a Polymarket account, and deposits USDC. **You (the agent) wire CLOB, derive API keys, connect, and verify balance** — same flow used to onboard the original Polly operator.

### Hard rules for agents

1. **Never** print, paste into chat, commit, or screenshot the `PRIVATE_KEY`, `API_SECRET`, or full `poly_clob.txt`.
2. **Never** set `exec_mode=live` or **ARM** unless the user explicitly says to spend real money. Default stays **dry_run + disarmed**.
3. **Never** commit `credentials/poly_clob.txt` (gitignored). Only the `.example.txt` belongs in git.
4. Do **not** open a second dashboard tab if the user already has one — tell them to hard-refresh their existing Chrome tab at `http://127.0.0.1:18112/`.
5. Prefer writing secrets only to the local file `credentials/poly_clob.txt` on disk.

### What the user must do (and only this)

Instruct the user clearly, in order:

1. **VPN** — Connect to a VPN exit in a country where Polymarket allows access (US residents are typically blocked; use a permitted region). Keep VPN on for signup, Deposit UI, and Magic key export.
2. **Account** — Go to [https://polymarket.com](https://polymarket.com), sign up / log in (email/Google Magic wallet is the usual path).
3. **Deposit** — Deposit **USDC on Polygon** into their **Polymarket wallet** (the site “Deposit” / cash balance).  
   - Funds land in the **proxy / funder** address (not the Magic signer EOA).  
   - If Deposit is geo-blocked, VPN must stay on; on-chain send to the funder address on Polygon also works.

You may remind them: website login alone does **not** enable autonomous trading — CLOB needs key + funder + derived API creds (you handle that).

### What you (the agent) do next

#### A. Install & scaffold

```bash
pip install -r requirements.txt
cp credentials/poly_clob.example.txt credentials/poly_clob.txt
```

Ensure `credentials/poly_clob.txt` exists and is gitignored.

#### B. Collect FUNDER (proxy / deposit wallet)

This is the address that **holds USDC** and shows as portfolio cash.

Ways to obtain it (pick one):

- Logged-in Polymarket → **Portfolio / Cash / Deposit** — copy the deposit / proxy wallet `0x…`
- Settings → profile / wallet UI showing the trading wallet
- Relayer / API settings pages sometimes show signer vs proxy — **FUNDER = proxy/deposit**, not the signer EOA
- Data API pattern: portfolio queries use `user=<funder>`

Write into `credentials/poly_clob.txt`:

```text
FUNDER=0x…proxy_or_deposit_wallet…
```

#### C. Collect PRIVATE_KEY (Magic export) — user-assisted, agent writes file

Polymarket email/Google accounts use Magic. The trading signer key is exported as follows:

1. Tell user (VPN on): Polymarket → **Settings → Account → Start Export**  
   (opens [https://reveal.magic.link/polymarket](https://reveal.magic.link/polymarket))
2. User logs in with the **same** email/Google as Polymarket.
3. User reveals / copies the private key **once**.
4. Agent writes it into `credentials/poly_clob.txt` as `PRIVATE_KEY=0x…`  
   - Preferred: user pastes into the file themselves, **or** agent captures from a local browser session into the file **without echoing the key in chat**.
5. Confirm file has a non-empty `PRIVATE_KEY` and `FUNDER` — do not print values.

#### D. Signature type

In `poly_clob.txt`:

| Login style | `SIGNATURE_TYPE` |
|-------------|------------------|
| Newer Magic / POLY_1271 (common now) | `3` |
| Classic email/Magic proxy | `1` |
| Browser wallet as proxy | `2` |
| EOA trading directly | `0` |

**Start with `3`** for modern Magic accounts. If `connect` fails with signature / auth errors, try `1`, then `2`.

Leave blank:

```text
API_KEY=
API_SECRET=
API_PASSPHRASE=
```

`MODE=paper` in the file is fine; the deck’s runtime `exec_mode` is separate (keep **dry_run** until asked).

```text
HOST=https://clob.polymarket.com
CHAIN_ID=137
```

#### E. Derive CLOB API keys + connect (agent — no user action)

With the deck running (`python run_poly.py`):

```bash
# Derive/verify API creds + warm client (does NOT place orders)
curl -X POST http://127.0.0.1:18112/api/live/connect

# Optional: reload file after edits
curl -X POST http://127.0.0.1:18112/api/live/reload

# Check balance / allowance on funder
curl http://127.0.0.1:18112/api/live/balance

# Lag path collateral prep (safe; no spend)
curl -X POST http://127.0.0.1:18112/api/live/lag_prep
```

Or from Python:

```python
from poly.live_exec import live_exec
print(live_exec.connect())          # create_or_derive_api_key → writes API_* back to file
print(live_exec.fetch_balance())    # balance_usd on FUNDER
print(live_exec.prepare_collateral(force=True))
print(live_exec.lag_ready())        # checklist before live lag FAKs
```

Success looks like: `creds_ok: true`, `has_api_creds: true`, `balance_usd` matching deposit (may take a minute after chain confirm), no fatal `last_error`.

If derive fails: wrong `SIGNATURE_TYPE`, wrong key for that funder, VPN/geo issues on CLOB host, or missing `py-clob-client-v2` (`pip install py-clob-client-v2`).

#### F. Run the deck safely

```bash
python run_poly.py
```

- Confirm UI / `/api/state`: **dry_run**, **armed=false**, build tag matches `poly/version.py`.
- Paper trading + alerts can run immediately.
- **LIVE spend:** only after user says so → set `exec_mode=live`, then **ARM**. Sizing uses CLOB wallet, not paper equity. Disarm when done.

### One-screen user script (copy/paste to the human)

> 1. Turn on a VPN in a Polymarket-allowed country.  
> 2. Create / log into your Polymarket account.  
> 3. Deposit USDC (Polygon) into Polymarket so Cash &gt; $0.  
> 4. Settings → Account → Start Export → log into Magic → copy the private key **only into the local credentials file** (or hand it to the agent to write to disk — never into public chat).  
> 5. Tell the agent you’re done — they finish FUNDER, API derive, connect, and verification.  
> 6. Do **not** arm live trading until you explicitly want real orders.

### Troubleshooting cheatsheet

| Symptom | Likely fix |
|---------|------------|
| Site blocked / Deposit missing | VPN to allowed country |
| `creds_ok` false | Missing `PRIVATE_KEY` or `FUNDER` in `poly_clob.txt` |
| Connect / auth / signature errors | Flip `SIGNATURE_TYPE` `3` ↔ `1`; key must match Magic export for that account |
| `balance_usd` = 0 after deposit | Wrong FUNDER (used signer EOA instead of proxy); wait for Polygon confirm; refresh `/api/live/balance` |
| Orders reject allowance | `POST /api/live/connect` or `lag_prep` / `prepare_collateral` |
| Geo errors on CLOB | Keep VPN on during connect and live sessions |
| Accidental spend risk | Keep **disarmed**; `exec_mode=dry_run` until user asks |

### Safety

- Never commit `credentials/poly_clob.txt` or private keys.
- Live trading is gated; leave **disarmed** until the user explicitly arms.
- Deposit USDC/pUSD on Polygon to the **funder** before live FAKs.
- Markets can lose money — paper first is the default product posture.

## License

Private use / your terms — ship carefully; markets can lose money.
