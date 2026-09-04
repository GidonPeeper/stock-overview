"""Backfill the value/profit trendline since inception.

Reconstructs the portfolio at each month-end: which shares were held then (from
trade history up to that date) valued at that month's historical close price and
the historical EUR/USD (or EUR/GBP) rate. Writes one snapshot per month into the
history store, so the trendline shows the real curve since 2020 instead of only
points gathered from today onward.

Approximate by nature: instruments without price history (delisted SPACs / penny
stocks) can't be valued and are skipped — `coverage` reports how much of the cost
basis was priceable each month, so gaps are visible rather than hidden.
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import date

from .connectors import degiro, traderepublic, trading212
from .instruments import TICKERS, price_currency
from .positions import compute_positions
from . import store

# ISIN -> market-data ticker for every instrument ever traded (incl. closed);
# shared table in backend/instruments.py. "" => no usable price history.
HIST_TICKER = TICKERS


def _ticker_currency(t: str) -> str:
    return price_currency(t)


def _all_trades():
    return degiro.get_trades() + traderepublic.get_trades() + trading212.get_trades()


def _month_ends(first_iso: str) -> list[str]:
    """Month-end dates for every *completed* month from `first_iso` up to (but
    not including) the current month. The current month is left to the live
    daily snapshots, avoiding a duplicate/future point."""
    y, m = int(first_iso[:4]), int(first_iso[5:7])
    today = date.today()
    out = []
    while (y, m) < (today.year, today.month):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        last = date(ny, nm, 1).toordinal() - 1
        out.append(date.fromordinal(last).isoformat())
        y, m = ny, nm
    return out


def _fetch_monthly(tickers: list[str], start: str) -> dict[str, dict[str, float]]:
    """{ticker: {YYYY-MM: close}} of monthly closes from `start`."""
    import yfinance as yf

    data = yf.download(
        sorted(set(tickers)), start=start, interval="1mo",
        progress=False, auto_adjust=True,
    )["Close"]
    out: dict[str, dict[str, float]] = defaultdict(dict)
    cols = data.columns if hasattr(data, "columns") else [tickers[0]]
    for t in cols:
        series = data[t] if hasattr(data, "columns") else data
        for ts, val in series.items():
            if val == val:  # not NaN
                out[t][ts.strftime("%Y-%m")] = float(val)
    return out


def run() -> dict:
    trades = _all_trades()
    if not trades:
        return {"months": 0}
    first = min(t.when for t in trades)[:10]

    tickers = [HIST_TICKER.get(t.key, "") for t in trades]
    tickers = [t for t in tickers if t]
    fx_tickers = ["EURUSD=X", "EURGBP=X"]
    prices = _fetch_monthly(list(set(tickers)) + fx_tickers, start=first[:7] + "-01")

    def fx(currency: str, ym: str) -> float:
        if currency == "EUR":
            return 1.0
        pair = "EURUSD=X" if currency == "USD" else "EURGBP=X"
        # nearest available month at or before ym
        months = [m for m in prices.get(pair, {}) if m <= ym]
        rate = prices[pair][max(months)] if months else 1.0
        return 1.0 / rate if rate else 1.0

    def price(ticker: str, ym: str):
        months = [m for m in prices.get(ticker, {}) if m <= ym]
        return prices[ticker][max(months)] if months else None

    months = _month_ends(first)
    written = 0
    last_coverage = 0.0
    for day in months:
        ym = day[:7]
        upto = [t for t in trades if t.when[:10] <= day]
        positions = compute_positions(upto)
        value = cost = priced_cost = 0.0
        realized = 0.0
        for isin, p in positions.items():
            realized += p.realized  # cumulative realized locked in by this date
            if p.quantity <= 1e-6:
                continue
            cost += p.cost  # EUR cost basis already
            tk = HIST_TICKER.get(isin, "")
            px = price(tk, ym) if tk else None
            if px is None:
                continue
            priced_cost += p.cost
            value += px * p.quantity * fx(_ticker_currency(tk), ym)
        if value <= 0:
            continue
        last_coverage = priced_cost / cost if cost else 0
        store.record_snapshot(round(value, 2), round(cost, 2),
                              realized=round(realized, 2), day=day)
        written += 1

    return {"months": written, "from": months[0], "to": months[-1],
            "latest_coverage_pct": round(100 * last_coverage, 1)}


if __name__ == "__main__":
    # allow: python -m backend.backfill   (loads .env via main side-effect)
    from . import main  # noqa: F401  (triggers load_dotenv)
    print(run())
