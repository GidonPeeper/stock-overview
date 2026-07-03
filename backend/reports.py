"""Professional reporting layer: health score, stress tests, correlation
matrix, dividend income projection, annual report and CSV export.

All metrics are computed from data the app already has (holdings, trade
history, snapshot history, yfinance) with the methodology surfaced to the UI —
nothing is a black box.
"""

from __future__ import annotations

import csv
import io
import time
from collections import defaultdict

from .connectors import degiro, traderepublic, trading212
from .fx import to_eur
from . import analytics, income

_cache: dict[str, tuple[object, float]] = {}


def _cached(key: str, ttl: int, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[1] < ttl:
        return hit[0]
    try:
        val = fn()
    except Exception:
        val = hit[0] if hit else None
    if val is not None:
        _cache[key] = (val, now)
    return val


# ------------------------------------------------------------- health score
HEALTH_METHOD = (
    "Score out of 100: diversification 30 (effective positions ≥12 is full "
    "marks), concentration 20 (top-3 weight ≤35% is full marks), currency "
    "balance 15 (≤70% in one currency is full marks), risk-adjusted return 20 "
    "(Sharpe ≥1 full, ≤−1 zero), drawdown resilience 15 (max drawdown ≥−20% "
    "full marks). Grades: A ≥85, B ≥70, C ≥55, D ≥40, else E."
)


def health_score(holdings: list[dict]) -> dict:
    a = analytics.summary(holdings)
    parts = []

    eff = a.get("effective_positions") or 1
    parts.append(("Diversification", min(eff / 12, 1) * 30, 30,
                  f"{eff} effective positions"))
    top3 = a.get("top3_pct") or 100
    parts.append(("Concentration", max(0, min((80 - top3) / 45, 1)) * 20, 20,
                  f"top 3 = {top3}% of portfolio"))
    ccy = a.get("currency_exposure") or []
    dom = ccy[0]["pct"] if ccy else 100
    parts.append(("Currency balance", max(0, min((100 - dom) / 30, 1)) * 15, 15,
                  f"{ccy[0]['currency'] if ccy else '—'} is {dom}% of value"))
    sharpe = a.get("sharpe")
    s_score = 0.5 if sharpe is None else max(0, min((sharpe + 1) / 2, 1))
    parts.append(("Risk-adjusted return", s_score * 20, 20,
                  f"Sharpe {sharpe if sharpe is not None else 'n/a'}"))
    mdd = a.get("max_drawdown_pct") or 0
    parts.append(("Drawdown resilience", max(0, min((mdd + 60) / 40, 1)) * 15, 15,
                  f"max drawdown {mdd}%"))

    total = round(sum(p[1] for p in parts))
    grade = "A" if total >= 85 else "B" if total >= 70 else "C" if total >= 55 \
        else "D" if total >= 40 else "E"
    return {
        "score": total, "grade": grade, "method": HEALTH_METHOD,
        "components": [{"name": n, "points": round(v, 1), "max": mx, "detail": d}
                       for n, v, mx, d in parts],
    }


# ------------------------------------------------------------- stress tests
def stress(holdings: list[dict]) -> dict:
    a = analytics.summary(holdings)
    total = sum(h["value_eur"] for h in holdings)
    beta = a.get("beta") or 1.0
    usd_pct = next((c["pct"] for c in a.get("currency_exposure", [])
                    if c["currency"] == "USD"), 0)
    usd_value = total * usd_pct / 100
    scenarios = [
        {"name": "Market −10%", "detail": f"S&P −10% × your beta {beta}",
         "impact_eur": round(-0.10 * beta * total)},
        {"name": "Market −25% (bear)", "detail": f"S&P −25% × beta {beta}",
         "impact_eur": round(-0.25 * beta * total)},
        {"name": "EUR/USD +5%", "detail": f"euro strengthens; hits your {usd_pct}% USD exposure",
         "impact_eur": round(-0.05 * usd_value)},
        {"name": "EUR/USD −5%", "detail": "euro weakens; lifts USD holdings",
         "impact_eur": round(+0.05 * usd_value)},
    ]
    return {"portfolio_value": round(total), "scenarios": scenarios,
            "method": "Linear approximations: market moves scaled by portfolio "
                      "beta; FX moves applied to the USD-quoted share of value."}


# ---------------------------------------------------------- correlation map
def correlation(tickers: list[str]) -> dict:
    tickers = sorted(set(t for t in tickers if t))[:20]

    def build():
        import yfinance as yf

        data = yf.download(tickers, period="1y", interval="1d",
                           progress=False, auto_adjust=True)["Close"]
        rets = data.pct_change().dropna(how="all")
        corr = rets.corr()
        cols = [c for c in corr.columns if corr[c].notna().sum() > 1]
        matrix = [[round(float(corr.loc[r, c]), 2) if corr.loc[r, c] == corr.loc[r, c] else None
                   for c in cols] for r in cols]
        # most / least correlated pairs for the summary line
        pairs = []
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                v = matrix[i][j]
                if v is not None:
                    pairs.append((cols[i], cols[j], v))
        pairs.sort(key=lambda p: p[2])
        return {
            "tickers": cols, "matrix": matrix,
            "best_diversifier": {"pair": pairs[0][:2], "corr": pairs[0][2]} if pairs else None,
            "most_correlated": {"pair": pairs[-1][:2], "corr": pairs[-1][2]} if pairs else None,
            "method": "Pearson correlation of daily returns over the last year "
                      "(price data via Yahoo Finance).",
        }
    return _cached("corr:" + ",".join(tickers), 6 * 3600, build) or \
        {"tickers": [], "matrix": [], "method": ""}


# ------------------------------------------------------ income projection
def income_projection(holdings: list[dict]) -> dict:
    def build():
        import yfinance as yf

        rows = []
        total_annual = 0.0
        for h in holdings:
            t = h.get("ticker")
            if not t:
                continue
            try:
                tk = yf.Ticker(t)
                div = tk.dividends
                if div is None or len(div) == 0:
                    continue
                last12 = float(div[div.index >= div.index.max() - __import__("pandas").Timedelta(days=365)].sum())
                if last12 <= 0:
                    continue
                annual_eur = to_eur(last12 * h["quantity"], h["currency"])
                yield_pct = (to_eur(last12, h["currency"]) /
                             (h["value_eur"] / h["quantity"])) * 100 if h["quantity"] else 0
                total_annual += annual_eur
                rows.append({"name": h["name"], "ticker": t,
                             "yield_pct": round(yield_pct, 2),
                             "annual_eur": round(annual_eur, 2)})
            except Exception:
                continue
        rows.sort(key=lambda r: -r["annual_eur"])
        return {"holdings": rows, "projected_annual_eur": round(total_annual, 2),
                "projected_monthly_eur": round(total_annual / 12, 2),
                "method": "Trailing 12-month dividends per share × shares held, "
                          "converted to EUR at today's rate. A projection, not a promise."}
    key = "incomeproj:" + ",".join(sorted(h.get("ticker", "") for h in holdings))
    return _cached(key, 12 * 3600, build) or {"holdings": [], "projected_annual_eur": 0}


# ------------------------------------------------------------ annual report
def annual_report() -> dict:
    """Realized P/L and dividend income per calendar year."""
    from .positions import Position

    sources = {
        "Trading212": trading212.get_trades,
        "DeGiro": degiro.get_trades,
        "Trade Republic": traderepublic.get_trades,
    }
    realized_by_year: dict[str, float] = defaultdict(float)
    for get in sources.values():
        try:
            trades = sorted(get(), key=lambda t: t.when)
        except Exception:
            continue
        pos: dict[str, Position] = {}
        for t in trades:
            amount_eur = to_eur(t.amount, t.currency)
            p = pos.setdefault(t.key, Position(key=t.key, name=t.name))
            if t.side == "BUY":
                p.quantity += t.quantity
                p.cost += amount_eur
            else:
                if p.quantity > 1e-9:
                    avg = p.cost / p.quantity
                    sold = min(t.quantity, p.quantity)
                    realized_by_year[t.when[:4]] += amount_eur - avg * sold
                    p.cost -= avg * sold
                p.quantity -= t.quantity

    div_by_year = income.by_year() if hasattr(income, "by_year") else {}
    years = sorted(set(realized_by_year) | set(div_by_year))
    return {"years": [
        {"year": y,
         "realized_eur": round(realized_by_year.get(y, 0.0), 2),
         "dividends_eur": round(div_by_year.get(y, 0.0), 2),
         "total_eur": round(realized_by_year.get(y, 0.0) + div_by_year.get(y, 0.0), 2)}
        for y in years
    ]}


# ------------------------------------------------------------------ export
def holdings_csv(holdings: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "ticker", "isin", "brokers", "quantity", "price",
                "currency", "value_eur", "profit_eur", "profit_pct", "daily_eur"])
    for h in holdings:
        w.writerow([h["name"], h["ticker"], h.get("isin", ""),
                    "|".join(h["brokers"]), h["quantity"], h["price"],
                    h["currency"], h["value_eur"], h["profit_eur"],
                    h["profit_pct"], h.get("daily_eur", "")])
    return buf.getvalue()
