# Polly Poly Bot

Polymarket **Alert Deck** — paper trading, live dry-run / armed CLOB execution, voice alerts, lag edges, and mental trailing stops.

Dashboard: `http://127.0.0.1:18112`

## Quick start

```bash
pip install -r requirements.txt
cp credentials/poly_clob.example.txt credentials/poly_clob.txt
# edit credentials/poly_clob.txt with your Polymarket key + funder
python run_poly.py
```

Open the deck in Chrome and hard-refresh after upgrades. Execution boots **dry_run + disarmed** — arm only when you intend to spend.

## Layout

| Path | Role |
|------|------|
| `run_poly.py` | Launch server on port 18112 |
| `poly/` | Engine, edges, live exec, UI |
| `credentials/poly_clob.example.txt` | Creds template (copy → `poly_clob.txt`) |
| `data/` | Local runtime state (gitignored) |

## Safety

- Never commit `credentials/poly_clob.txt` or private keys.
- Live trading is gated; leave **disarmed** until paper gate / bankroll are ready.
- Deposit USDC/pUSD on Polygon to your **funder** wallet before live FAKs.

## License

Private use / your terms — ship carefully; markets can lose money.
