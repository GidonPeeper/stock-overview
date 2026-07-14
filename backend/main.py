"""Stock Overview API.

Combines holdings across brokers and serves:
  * /api/portfolio — open holdings, unrealized P/L, realized P/L, all-time total
  * /api/closed    — closed (previous) positions with realized profit
  * /api/profit    — period views (today / week / month / year)
  * /api/history   — daily snapshots for the trendline

Run:  uvicorn backend.main:app --reload
Open: http://127.0.0.1:8000
"""

from __future__ import annotations

import os
import threading
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import FileResponse, RedirectResponse, Response

try:
    from dotenv import load_dotenv

    # Load .env from the project root explicitly, regardless of launch cwd.
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from collections import defaultdict

from . import github_sync, vault
github_sync.pull_vault()       # fetch freshest cloud vault first (no-op without token)
_VAULT_STATE = vault.unlock()  # hydrate private data files before anything reads them

from .connectors import degiro, traderepublic, trading212
from .fx import BASE_CURRENCY, to_eur
from .prices import fetch_quotes
from . import (analytics, cash, datafiles, finances, income, insights, market,
               periods, realized, reports, sectors, store, watch)

DEMO_MODE = not (os.getenv("T212_API_KEY") and os.getenv("T212_API_SECRET"))
FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"

app = FastAPI(title="Stock Overview")

# Login gate with a session cookie + a login page (works nicely as a phone web
# app: iOS saves the password to Keychain and unlocks it with Face ID). Disabled
# when DASHBOARD_PASSWORD is unset, so local use stays frictionless.
import secrets

_DASH_USER = os.getenv("DASHBOARD_USER", "admin")
_DASH_PASS = os.getenv("DASHBOARD_PASSWORD")
_DASH_SECRET = os.getenv("DASHBOARD_SECRET") or secrets.token_urlsafe(32)

# Reachable without logging in: the login page and the PWA assets.
_PUBLIC = {
    "/login", "/manifest.webmanifest", "/favicon.ico",
    "/icon-180.png", "/icon-192.png", "/icon-512.png",
}


@app.middleware("http")
async def _auth(request: Request, call_next):
    if not _DASH_PASS:  # auth disabled locally
        return await call_next(request)
    path = request.url.path
    if path in _PUBLIC or request.session.get("auth"):
        return await call_next(request)
    if path.startswith("/api"):
        return Response("Authentication required", status_code=401)
    return RedirectResponse("/login")


# Added after _auth so SessionMiddleware wraps it (request.session is available).
app.add_middleware(
    SessionMiddleware, secret_key=_DASH_SECRET,
    max_age=7 * 24 * 3600, same_site="lax",
)


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(FRONTEND_DIR / "login.html")


@app.post("/login", include_in_schema=False)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ok = (_DASH_PASS and secrets.compare_digest(username, _DASH_USER)
          and secrets.compare_digest(password, _DASH_PASS))
    if ok:
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login?e=1", status_code=303)


@app.get("/logout", include_in_schema=False)
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

_backfill_started = False
_backfill_lock = threading.Lock()


def _ensure_backfill() -> None:
    """If the trendline lacks historical (pre-current-month) points, reconstruct
    them from trade history once, in the background. Self-heals a deleted DB."""
    global _backfill_started
    if DEMO_MODE:
        return
    with _backfill_lock:
        if _backfill_started:
            return
        _backfill_started = True
        cur_month = date.today().isoformat()[:7]
        historical = [s for s in store.get_history() if s["day"][:7] < cur_month]
        if len(historical) >= 12:
            return  # already backfilled

    def task():
        try:
            from . import backfill
            backfill.run()
        except Exception:
            pass

    threading.Thread(target=task, daemon=True).start()


def _open_holdings_rows() -> tuple[list[dict], float, float, dict]:
    """Returns (rows, total_value_eur, total_cost_eur, value_by_broker)."""
    holdings = (
        trading212.get_holdings()
        + degiro.get_holdings()
        + traderepublic.get_holdings()
    )

    # Live quotes (price + previous close) for every holding — the previous
    # close drives the per-holding "today" change.
    quotes = fetch_quotes([h.ticker for h in holdings])

    rows: list[dict] = []
    total_value = total_cost = 0.0
    by_broker: dict[str, float] = {}

    for h in holdings:
        q = quotes.get(h.ticker, {})
        price = h.price if h.price is not None else q.get("price", h.buy_price)
        value_eur = to_eur(price * h.quantity, h.currency)
        if h.broker_profit_eur is not None:
            # Trust the broker's exact P/L; derive cost from it.
            profit_eur = h.broker_profit_eur
            cost_eur = value_eur - profit_eur
        else:
            cost_eur = to_eur(h.buy_price * h.quantity, h.cost_currency())
            profit_eur = value_eur - cost_eur

        # Today's change: (price - previous close) x quantity, in EUR.
        prev = q.get("prev_close")
        daily_eur = to_eur((price - prev) * h.quantity, h.currency) if prev else None

        total_value += value_eur
        total_cost += cost_eur
        by_broker[h.broker] = by_broker.get(h.broker, 0.0) + value_eur

        rows.append(
            {
                "broker": h.broker,
                "ticker": h.ticker,
                "isin": h.isin,
                "name": h.name,
                "quantity": h.quantity,
                "currency": h.currency,
                "buy_price": round(h.buy_price, 2),
                "price": round(price, 2),
                "value_eur": round(value_eur, 2),
                "cost_eur": round(cost_eur, 2),
                "profit_eur": round(profit_eur, 2),
                "profit_pct": round((profit_eur / cost_eur * 100) if cost_eur else 0, 2),
                "daily_eur": round(daily_eur, 2) if daily_eur is not None else None,
                "priced_live": h.ticker in quotes or (h.price is not None and h.price > 0),
            }
        )

    rows.sort(key=lambda r: r["value_eur"], reverse=True)
    return rows, total_value, total_cost, by_broker


def _merge_holdings(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Merge the same company held at multiple brokers into one holding (by ISIN)
    and aggregate value by sector for the allocation chart."""
    sector_map = sectors.get_sectors([r["ticker"] for r in rows if r["ticker"]])

    merged: dict[str, dict] = {}
    for r in rows:
        key = r["isin"] or r["ticker"] or r["name"]
        m = merged.get(key)
        if m is None:
            m = merged[key] = {
                "name": r["name"], "ticker": r["ticker"], "isin": r["isin"],
                "sector": sector_map.get(r["ticker"], "Other"),
                "brokers": [], "quantity": 0.0, "price": r["price"],
                "currency": r["currency"], "value_eur": 0.0, "_cost": 0.0,
                "profit_eur": 0.0, "daily_eur": 0.0, "_has_daily": False,
                "priced_live": False,
            }
        m["quantity"] += r["quantity"]
        m["value_eur"] += r["value_eur"]
        m["_cost"] += r["cost_eur"]
        m["profit_eur"] += r["profit_eur"]
        if r.get("daily_eur") is not None:
            m["daily_eur"] += r["daily_eur"]
            m["_has_daily"] = True
        m["priced_live"] = m["priced_live"] or r["priced_live"]
        if r["broker"] not in m["brokers"]:
            m["brokers"].append(r["broker"])

    out = []
    for m in merged.values():
        cost = m.pop("_cost")
        has_daily = m.pop("_has_daily")
        m["quantity"] = round(m["quantity"], 4)
        m["value_eur"] = round(m["value_eur"], 2)
        m["profit_eur"] = round(m["profit_eur"], 2)
        m["profit_pct"] = round((m["profit_eur"] / cost * 100) if cost else 0, 2)
        # today's change: value now vs (value − today's move)
        prev_val = m["value_eur"] - m["daily_eur"]
        m["daily_eur"] = round(m["daily_eur"], 2) if has_daily else None
        m["daily_pct"] = round((m["daily_eur"] / prev_val * 100), 2) \
            if has_daily and prev_val else None
        out.append(m)
    out.sort(key=lambda x: x["value_eur"], reverse=True)

    by_sector: dict[str, float] = defaultdict(float)
    for m in out:
        by_sector[m["sector"]] += m["value_eur"]
    by_sector_list = [
        {"sector": s, "value": round(v, 2)}
        for s, v in sorted(by_sector.items(), key=lambda kv: -kv[1])
    ]
    return out, by_sector_list


@app.get("/api/portfolio")
def portfolio() -> dict:
    rows, total_value, total_cost, by_broker = _open_holdings_rows()
    holdings, by_sector = _merge_holdings(rows)
    unrealized = total_value - total_cost
    realized_total = realized.summary()["total_realized_eur"]
    inc = income.summary()
    all_time = unrealized + realized_total
    daily = round(sum(h["daily_eur"] for h in holdings if h["daily_eur"] is not None), 2)
    prev_value = total_value - daily

    if DEMO_MODE:
        store.seed_demo_history(total_value, total_cost)
    store.record_snapshot(total_value, total_cost, realized=realized_total)

    return {
        "base_currency": BASE_CURRENCY,
        "demo_mode": DEMO_MODE,
        "totals": {
            "value": round(total_value, 2),
            "cost": round(total_cost, 2),
            "unrealized": round(unrealized, 2),
            "unrealized_pct": round((unrealized / total_cost * 100) if total_cost else 0, 2),
            "realized": realized_total,
            "all_time": round(all_time, 2),
            "daily": daily,
            "daily_pct": round((daily / prev_value * 100), 2) if prev_value else 0,
            "dividends": inc["dividends"],
            "interest": inc["interest"],
            "income": inc["total"],
            "total_return": round(all_time + inc["total"], 2),
        },
        "income_by_broker": inc["by_broker"],
        "by_broker": [
            {"broker": b, "value": round(v, 2)}
            for b, v in sorted(by_broker.items(), key=lambda kv: -kv[1])
        ],
        "by_sector": by_sector,
        "holdings": holdings,
    }


@app.get("/api/closed")
def closed() -> dict:
    """Previous (fully closed) positions and the realized profit on them."""
    return realized.summary()


@app.get("/api/profit")
def profit() -> dict:
    """Period P/L (today/week/month/year) for current holdings, plus the
    all-time figures. Slower than /api/portfolio because it pulls price history."""
    rows, total_value, total_cost, _ = _open_holdings_rows()
    unrealized = total_value - total_cost
    realized_total = realized.summary()["total_realized_eur"]
    period = periods.compute(rows)
    return {
        "unrealized": round(unrealized, 2),
        "realized": realized_total,
        "all_time": round(unrealized + realized_total, 2),
        **period,
    }


@app.get("/api/history")
def history() -> list[dict]:
    _ensure_backfill()
    return store.get_history()


@app.get("/api/indices")
def indices() -> list[dict]:
    """Market snapshot for the ticker strip (S&P 500, Nasdaq, AEX, EUR/USD…)."""
    return market.indices()


@app.get("/api/sparklines")
def sparklines() -> dict[str, list[float]]:
    """~1 month of daily closes per held ticker, for the row mini-charts."""
    tickers = [h.ticker for h in (
        trading212.get_holdings() + degiro.get_holdings() + traderepublic.get_holdings()
    ) if h.ticker]
    return market.sparklines(tickers)


@app.get("/api/news/{ticker}")
def news(ticker: str) -> list[dict]:
    """Recent headlines for one holding (used in the detail view)."""
    return market.news(ticker)


@app.get("/api/ticker_history/{ticker}")
def ticker_history(ticker: str, range: str = "1mo") -> list[float]:
    """Price history for the detail-sheet chart at a chosen range."""
    return market.ticker_history(ticker, range)


@app.get("/api/datastatus")
def datastatus() -> dict:
    """Which data sources are live vs sample — shown as a banner in the UI."""
    out = datafiles.status()
    out["_vault"] = _VAULT_STATE
    out["_sync"] = {**github_sync.last_result,
                    "token": bool(os.getenv("GITHUB_TOKEN"))}
    return out


@app.get("/settings", include_in_schema=False)
def settings_page():
    return FileResponse(FRONTEND_DIR / "settings.html")


@app.get("/api/cash")
def cash_get() -> dict:
    """Bank/cash accounts + EUR total (edited in-app on the Settings page)."""
    return cash.load()


@app.post("/api/cash")
def cash_upsert(name: str = Form(...), institution: str = Form(""),
                balance: float = Form(...), currency: str = Form("EUR"),
                type: str = Form("cash"), rate: float = Form(0)) -> dict:
    if not name.strip():
        raise HTTPException(400, "Account name is required")
    out = cash.upsert(name, institution, balance, currency, type, rate)
    github_sync.push_vault_async()
    return out


@app.delete("/api/cash/{name}")
def cash_delete(name: str) -> dict:
    out = cash.delete(name)
    github_sync.push_vault_async()
    return out


@app.post("/api/upload/{kind}")
async def upload_statement(kind: str, file: UploadFile = File(...)) -> dict:
    """Upload a broker statement from the browser/phone — takes effect
    immediately. Light validation so a wrong file fails loudly, not silently."""
    content = await file.read()
    if len(content) > 10_000_000:
        raise HTTPException(413, "File too large")
    text = content.decode("utf-8", errors="replace")
    if kind == "degiro":
        if "Koop" not in text and "Verkoop" not in text:
            raise HTTPException(400, "That doesn't look like a DeGiro account statement CSV "
                                     "(no Koop/Verkoop rows found). Export: Activity → Account statement, all dates.")
    elif kind in ("trade_republic", "tr_income"):
        import json as _json
        try:
            payload = _json.loads(text)
        except ValueError:
            raise HTTPException(400, "Not valid JSON — see the .sample file for the format.")
        need = "trades" if kind == "trade_republic" else "income"
        if need not in payload:
            raise HTTPException(400, f'JSON must contain a "{need}" list — see the .sample file.')
    else:
        raise HTTPException(404, "Unknown upload kind")
    datafiles.save_upload(kind, content)
    # the trendline was built from the old data — rebuild it from the new
    global _backfill_started
    try:
        store.DB_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    with _backfill_lock:
        _backfill_started = False
    github_sync.push_vault_async()
    return datafiles.status()


@app.get("/api/analytics")
def analytics_endpoint() -> dict:
    """Quant metrics: beta, volatility, Sharpe, max drawdown, concentration,
    currency exposure. Computed from snapshot history + current holdings."""
    rows, *_ = _open_holdings_rows()
    holdings, _ = _merge_holdings(rows)
    return analytics.summary(holdings)


@app.get("/api/brief")
def brief() -> dict:
    """The Daily Brief: generated portfolio summary + renowned-source headlines."""
    port = portfolio()
    ana = analytics.summary(port["holdings"])
    return insights.daily_brief(port, ana)


@app.get("/api/headlines")
def headlines_endpoint() -> list[dict]:
    """Interleaved market headlines from CNBC / MarketWatch / Yahoo Finance
    (+ NewsAPI when a NEWSAPI_KEY env var is provided)."""
    return insights.headlines()


@app.get("/api/earnings")
def earnings() -> list[dict]:
    """Upcoming earnings dates for your current holdings."""
    rows, *_ = _open_holdings_rows()
    return insights.earnings_calendar([r["ticker"] for r in rows])


@app.get("/api/rising_stars")
def rising_stars() -> dict:
    """Transparent small-cap screener — method string included in the response."""
    return insights.rising_stars()


def _merged_holdings() -> list[dict]:
    rows, *_ = _open_holdings_rows()
    return _merge_holdings(rows)[0]


@app.get("/api/health")
def health() -> dict:
    """Portfolio Health Score (0-100, graded A-E) with disclosed methodology."""
    return reports.health_score(_merged_holdings())


@app.get("/api/stress")
def stress() -> dict:
    """What-if scenarios: market ±, EUR/USD ± — scaled by your beta & FX exposure."""
    return reports.stress(_merged_holdings())


@app.get("/api/correlation")
def correlation() -> dict:
    """1-year daily-return correlation matrix across holdings."""
    return reports.correlation([h["ticker"] for h in _merged_holdings()])


@app.get("/api/income_projection")
def income_projection() -> dict:
    """Forward dividend income: trailing-12M dividends/share × shares, in EUR."""
    return reports.income_projection(_merged_holdings())


@app.get("/api/annual_report")
def annual_report() -> dict:
    """Realized P/L + dividend income per calendar year."""
    return reports.annual_report()


def _liquid_assets() -> float:
    """Investments (latest snapshot) + cash — cheap, no live pricing needed."""
    hist = store.get_history()
    invest = hist[-1]["total_value"] if hist else 0.0
    return invest + (cash.load().get("total_eur") or 0.0)


@app.get("/api/finances")
def finances_get() -> dict:
    """Loans, income, monthly payments + cash-flow totals and Freedom Day."""
    return finances.load(_liquid_assets())


@app.post("/api/finances/{kind}")
def finances_upsert(kind: str, name: str = Form(...), balance: float = Form(0),
                    rate: float = Form(0), monthly_payment: float = Form(0),
                    monthly_eur: float = Form(0), currency: str = Form("EUR")) -> dict:
    if kind not in finances.KINDS:
        raise HTTPException(404, "Unknown kind")
    if not name.strip():
        raise HTTPException(400, "Name is required")
    fields = {"balance": balance, "rate": rate, "monthly_payment": monthly_payment,
              "currency": currency.upper()} if kind == "loans" else {"monthly_eur": monthly_eur}
    out = finances.upsert(kind, name, fields)
    github_sync.push_vault_async()
    return out


@app.delete("/api/finances/{kind}/{name}")
def finances_delete(kind: str, name: str) -> dict:
    if kind not in finances.KINDS:
        raise HTTPException(404, "Unknown kind")
    out = finances.delete(kind, name)
    github_sync.push_vault_async()
    return out


@app.get("/api/watchlist")
def watchlist_get() -> dict:
    """Watchlist with live quotes, daily change and sparklines."""
    return watch.load()


@app.post("/api/watchlist")
def watchlist_add(ticker: str = Form(...), name: str = Form("")) -> dict:
    out = watch.add(ticker.strip(), name.strip() or ticker.strip())
    github_sync.push_vault_async()
    return out


@app.delete("/api/watchlist/{ticker}")
def watchlist_remove(ticker: str) -> dict:
    out = watch.remove(ticker)
    github_sync.push_vault_async()
    return out


@app.get("/api/search")
def search_tickers(q: str) -> list[dict]:
    """Global ticker search (Yahoo symbol search via yfinance)."""
    if not q.strip():
        return []
    return watch.search(q.strip())


@app.get("/api/hindsight")
def hindsight_endpoint() -> dict:
    """What your exits are worth today vs what you sold them for."""
    return reports.hindsight()


@app.get("/api/projection")
def projection_endpoint() -> dict:
    """10-year net-worth projection under 3/6/9% scenarios with your real
    contribution rate."""
    return reports.projection()


@app.get("/api/export/holdings.csv")
def export_holdings():
    csv_text = reports.holdings_csv(_merged_holdings())
    return Response(csv_text, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=holdings.csv"})


@app.get("/api/benchmark")
def benchmark() -> dict[str, float]:
    """Money-weighted S&P 500 comparison: what the portfolio would be worth today
    if every euro put in (or taken out) had gone into the S&P 500 instead, valued
    at each snapshot month. Directly comparable to the portfolio value curve."""
    hist = store.get_history()
    if not hist:
        return {}
    sp = market.benchmark(hist[0]["day"][:7] + "-01")  # {YYYY-MM: close}
    if not sp:
        return {}
    months = sorted(sp)

    def sp_at(ym: str):
        if ym in sp:
            return sp[ym]
        earlier = [m for m in months if m <= ym]
        return sp[earlier[-1]] if earlier else None

    trades = (trading212.get_trades() + degiro.get_trades() + traderepublic.get_trades())
    events = sorted((t.when[:7], (1 if t.side == "BUY" else -1) * t.amount) for t in trades)

    out: dict[str, float] = {}
    units = 0.0
    i = 0
    for snap in hist:
        ym = snap["day"][:7]
        while i < len(events) and events[i][0] <= ym:
            m, amt = events[i]
            px = sp_at(m)
            if px:
                units += amt / px
            i += 1
        px = sp_at(ym)
        if px is not None:
            out[snap["day"]] = round(units * px, 2)
    return out


# Serve the dashboard explicitly at "/" (same reliable FileResponse the login
# page uses) rather than relying on the static mount's directory-index behaviour,
# which can 404 the root on some hosts.
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


# Static assets (icons, manifest, etc.). Mounted last so routes take precedence.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
