# 📈 Stock Overview

A self-hosted dashboard that **combines your stock portfolio across multiple
brokers** — Trading 212, DeGiro and Trade Republic — into one live overview, with
profit tracking, allocation breakdowns and a performance chart since inception.

Runs locally or as an installable phone web-app. **Read-only by design: it can
never place a trade or move money.**

> Clone it and it runs immediately on bundled **sample data** (demo mode). Add
> your own keys and statements to see your real portfolio. Your private data
> never enters the repository — see [Privacy](#-privacy).

## ✨ Features

- **One combined view** — the same company held at several brokers is merged into
  a single position (matched by ISIN).
- **Trading 212** live via the official **read-only** API; **DeGiro** and
  **Trade Republic** reconstructed from their exported statements.
- **Profit, done properly** — realized + unrealized capital gains, dividends &
  interest, and a "total return" that reconciles with each broker's own figure.
  Exact EUR from the statements (real per-trade FX + fees), handling corporate
  actions (splits, delistings, ISIN changes).
- **Per-holding today / overall P/L**, sector & broker allocation doughnuts.
- **Performance chart since inception**, reconstructed from trade history with
  historical prices — toggle between profit and portfolio value.
- **Installable PWA** with a login gate (Face ID via the phone Keychain).
- **Auto-refreshes** while open (pauses in the background).

## 🚀 Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.main:app --reload
# open http://127.0.0.1:8000  (demo data, no setup needed)
```

## 🔑 Using your own data

Everything below is git-ignored, so it stays private.

1. **Trading 212** — generate a **read-only** API key/secret in the app
   (Settings → API; no ordering scope). Copy `.env.sample` to `.env` and fill in
   `T212_API_KEY` / `T212_API_SECRET`.
2. **DeGiro** — export your account statement (all dates) as CSV to
   `data/degiro_account.csv`. Add any ISIN→ticker entries you hold to
   `backend/connectors/degiro.py`.
3. **Trade Republic** — no API; transcribe trades/dividends from the statement
   PDF into `data/trades_trade_republic.json` and `data/income_trade_republic.json`
   (see the `.sample` files for the format).

To require a login (recommended before exposing it), also set `DASHBOARD_USER`,
`DASHBOARD_PASSWORD` and `DASHBOARD_SECRET` in `.env`.

## ☁️ Deploy (phone access, always-on)

Deployable to any host that runs a Python web service. See
[`DEPLOY_RENDER.md`](DEPLOY_RENDER.md) for a free Render setup — the code comes
from the (public) repo, while your keys go in as **environment variables** and
your statement files as **Render Secret Files**, so nothing private is ever in
git.

## 🔒 Privacy

- **Secrets** (`.env`) and **statement files** (`data/*.csv`, `data/*.json`
  except `*.sample`) are git-ignored — the repo only ever contains code + demo
  data.
- The **Trading 212 key is read-only**: a leak could at most reveal holdings, and
  can never trade or withdraw.

## 🛠 Tech

FastAPI · vanilla JS + Chart.js · SQLite · yfinance for market data. No build
step, single-page frontend.

## 📁 Layout

```
backend/
  connectors/   trading212 (live) · degiro (CSV) · traderepublic (JSON)
  positions.py  average-cost math (open, closed, realized) incl. corporate actions
  prices.py     live quotes (price + previous close) via yfinance
  realized.py · income.py · periods.py · sectors.py · backfill.py · fx.py
  main.py       API + auth + serves the dashboard
frontend/       single-page PWA (index.html) + login + icons
data/           *.sample files (demo); your real files go here, git-ignored
```

## License

MIT — see [LICENSE](LICENSE).
