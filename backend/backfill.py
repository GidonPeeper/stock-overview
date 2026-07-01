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
from .positions import compute_positions
from . import store

# ISIN -> market-data ticker for every instrument ever traded (incl. closed).
# "" => no usable price history (delisted / obscure); skipped in valuation.
HIST_TICKER = {
    "GB00B03MLX29": "SHELL.AS", "US09075V1026": "BNTX", "US34619R1023": "",
    "US0378331005": "AAPL", "US4884452065": "ZVRA", "US62914V1061": "NIO",
    "US98422D1054": "XPEV", "US0079031078": "AMD", "US69608A1088": "PLTR",
    "US6311031081": "NDAQ", "US35953D1046": "FUBO", "US33813J1060": "",
    "US21077C1071": "LOGC", "US84677R1068": "", "US74374T1097": "",
    "US44951Y1029": "", "US83406F1021": "SOFI", "US22041X1028": "CRSR",
    "US30303M1027": "META", "US01671P1003": "", "GB00BP6MXD84": "SHEL",
    "US30231G1022": "XOM", "US6745991058": "OXY", "US83304A1060": "SNAP",
    "US64110L1061": "NFLX", "US88160R1014": "TSLA", "US02079K3059": "GOOGL",
    "NL0012969182": "ADYEN.AS", "US21077C3051": "LOGC", "US76954A1034": "RIVN",
    "US5801351017": "MCD", "US36315X1019": "LKFT", "FR001400J770": "AF.PA",
    "US4581401001": "INTC", "US67066G1040": "NVDA", "NL0011872650": "BFIT.AS",
    "IE00BZCQB185": "QDV5.DE", "USN070592100": "ASML", "NL0010273215": "ASML.AS",
    "US44951Y2019": "", "NL0000888691": "AMG.AS", "NL0011585146": "RACE",
    "US46222L1089": "IONQ", "NL0000334118": "ASM.AS", "NL0012866412": "BESI.AS",
    "US91324P1021": "UNH", "NL0000009165": "HEIA.AS", "US00724F1012": "ADBE",
    "US68389X1054": "ORCL", "US5949181045": "MSFT", "US86800U3023": "SMCI",
    "US79466L3024": "CRM", "IE00BJ38QD84": "ZPRR.DE", "US6541061031": "NKE",
    "US0231351067": "AMZN", "US60770K1079": "MRNA", "US70450Y1038": "PYPL",
}


def _ticker_currency(t: str) -> str:
    if t.endswith((".AS", ".DE", ".PA", ".MI", ".F")):
        return "EUR"
    if t.endswith(".L"):
        return "GBP"
    return "USD"


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
