"""Key numbers of a share or fund for your own analysis: P/E, EBITDA, margins,
debt, growth and more. Served at GET /api/keynumbers/{ticker}; the dashboard
shows them in a holding's sheet and in the price lookup, each with a short
explanation (frontend/index.html, "key numbers").

Where they come from (Yahoo Finance):
  * Yahoo's company info (`Ticker.get_info`, through lookup._company_info):
    every number in one request, Yahoo's own figures. Hosted servers are
    often refused it.
  * For a share without company info (or with info that lacks the accounts),
    the company's reported accounts: Yahoo's fundamentals-timeseries request,
    which needs no crumb and so also works from hosted servers. The last 12
    months (TTM) and the latest balance sheet; the ratios are worked out here
    with today's price (lookup.quote). Forward P/E, PEG and beta need
    analysts or price history and are left out on this path.
  * A fund's yearly cost falls back to the checked table in fund_fees.json.

Units in `numbers`:
  * ratios as plain numbers (pe 24.5 = 24.5 times earnings; debt_to_equity
    0.84 = 84 of debt for every 100 of the owners' money);
  * percentages as percent (gross_margin 48.8 = 48.8%);
  * amounts from the accounts (revenue, ebitda, net_income, free_cash_flow,
    total_cash, total_debt, eps) in `report_currency`, the currency the
    company reports in, which can differ from the one it trades in;
  * market_cap and enterprise_value in `currency`, the trading currency
    (pounds for a share quoted in pence).
A number Yahoo does not have, or that means nothing (P/E of a company that
made a loss), is left out, never 0.

The raw answer is cached for 6 hours (stale-while-revalidate for 18 more);
the price-based ratios of the accounts path use the current price each time.
"""

from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from . import cache, fx, lookup

log = logging.getLogger("stock_overview.keynumbers")

_TTL = 6 * 3600
_STALE = 18 * 3600
_WAIT = 15.0          # seconds the request waits for Yahoo in all
_HTTP_TIMEOUT = 8

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="keynumbers")

KINDS = ("share", "etf", "fund")

# The accounts asked for in the fundamentals-timeseries request. trailing* =
# the last 12 months; quarterly*/annual* = one period (the balance sheet is
# a moment, so its latest value is used whichever of the two is newer).
_SERIES = (
    "trailingTotalRevenue", "trailingGrossProfit", "trailingOperatingIncome", "trailingEBITDA",
    "trailingNetIncomeCommonStockholders", "trailingDilutedEPS", "trailingFreeCashFlow",
    "trailingCashDividendsPaid",
    "quarterlyTotalRevenue", "quarterlyDilutedEPS", "annualTotalRevenue", "annualDilutedEPS",
    "quarterlyStockholdersEquity", "annualStockholdersEquity",
    "quarterlyTotalDebt", "annualTotalDebt",
    "quarterlyCashCashEquivalentsAndShortTermInvestments", "annualCashCashEquivalentsAndShortTermInvestments",
    "quarterlyCurrentAssets", "annualCurrentAssets",
    "quarterlyCurrentLiabilities", "annualCurrentLiabilities",
    "quarterlyInventory", "annualInventory",
    "quarterlyTotalAssets", "annualTotalAssets",
    "quarterlyOrdinarySharesNumber", "annualOrdinarySharesNumber",
)
_TS_URL = ("https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{t}"
           "?symbol={t}&type={types}&period1={p1}&period2={p2}")


class NoKeyNumbers(lookup.QuoteError):
    status = 503


_DOWN = "Yahoo Finance is not answering right now, so there are no key numbers. Try again in a minute."


# ----------------------------------------------------------------- helpers
def _num(v) -> float | None:
    """A finite number (negative allowed), else None. Yahoo sends NaN, None,
    strings like 'Infinity' and 0 for 'not reported'; exactly 0 counts as
    missing."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v != 0 else None


def _pct(v) -> float | None:
    """A fraction (0.25) as a percentage (25.0)."""
    v = _num(v)
    return v * 100 if v is not None else None


def _div(a, b) -> float | None:
    """a / b for a positive b; None when either is missing."""
    if a is None or b is None or b <= 0:
        return None
    return a / b


def _positive(v, cap: float = 1e6) -> float | None:
    """Ratios such as P/E only mean something above 0 (and below an absurd cap)."""
    return v if v is not None and 0 < v < cap else None


def _rate(ccy: str | None) -> float | None:
    """EUR per unit of `ccy`, or None when no rate is known (fx counts an
    unknown currency as 1 EUR; that would make a P/E wrong, so say so)."""
    if not ccy:
        return None
    try:
        r = fx.to_eur(1.0, ccy)
    except Exception:
        return None
    return r if r and (ccy == "EUR" or r != 1.0) else None


def _iso_day(epoch) -> str | None:
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _clean(numbers: dict) -> dict:
    """Drop missing values; round so the JSON stays small."""
    out = {}
    for k, v in numbers.items():
        if v is None or not math.isfinite(v):
            continue
        a = abs(v)
        out[k] = round(v) if a >= 1e5 else round(v, 4 if a < 10 else 2)
    return out


# ------------------------------------------------------------------- public
def get(ticker: str) -> dict:
    """The key numbers of one share or fund (see the module docstring).
    Raises lookup.QuoteError (bad ticker, unknown ticker, Yahoo down)."""
    t = lookup.clean_ticker(ticker)
    q = lookup.quote(t)                      # cached; the sheet asked for it a moment ago
    kind = q.get("kind")
    out = {"ticker": t, "name": q.get("name") or t, "kind": kind, "currency": q.get("currency"),
           "report_currency": None, "sector": None, "industry": None, "category": None,
           "fund_family": None, "as_of": None, "source": None, "numbers": {}, "message": None}
    if kind not in KINDS:
        out["message"] = "Key numbers are for shares and funds."
        return out
    raw = cache.get(f"keynumbers.raw:{t}", lambda: lookup._within(_pool, _WAIT, _fetch, t, kind),
                    ttl=_TTL, stale=_STALE, data=False)
    if kind == "share":
        _share(out, raw, q)
    else:
        _fund(out, raw, t)
    if not out["numbers"]:
        out["message"] = "Yahoo Finance has no key numbers for this right now."
    return out


def _fetch(t: str, kind: str) -> dict:
    """{"info": {...}} and/or {"series": {...}}: what Yahoo answered. Raises
    when Yahoo could not be asked at all (so nothing is cached)."""
    yf = lookup._yf()
    info = lookup._company_info(yf, t)
    raw: dict = {}
    if info is not None:
        raw["info"] = {k: info.get(k) for k in _INFO_KEYS if info.get(k) is not None}
    if kind == "share" and (info is None or _num(info.get("totalRevenue")) is None):
        try:
            raw["series"] = _fetch_series(t)
        except Exception as exc:
            log.info("accounts request for %s failed: %r", t, exc)
            if info is None:
                raise NoKeyNumbers(_DOWN) from None
    return raw


def _fetch_series(t: str) -> dict[str, list[list]]:
    """{type: [[asOfDate, value, currency], ...] oldest first} for _SERIES,
    from Yahoo's fundamentals-timeseries request (one request, no crumb)."""
    from yfinance.data import YfData

    url = _TS_URL.format(t=t, types=",".join(_SERIES), p1=int(time.time()) - 6 * 366 * 86400,
                         p2=int(time.time()))
    r = YfData().get(url, timeout=_HTTP_TIMEOUT)
    if getattr(r, "status_code", 200) >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    result = ((r.json() or {}).get("timeseries") or {}).get("result") or []
    out: dict[str, list[list]] = {}
    for item in result:
        try:
            typ = item["meta"]["type"][0]
        except (KeyError, IndexError, TypeError):
            continue
        rows = []
        for v in item.get(typ) or []:
            if not isinstance(v, dict):
                continue
            val = _num((v.get("reportedValue") or {}).get("raw"))
            if val is None or not v.get("asOfDate"):
                continue
            rows.append([v["asOfDate"], val, v.get("currencyCode")])
        if rows:
            out[typ] = sorted(rows)
    return out


# ------------------------------------------------------------------- shares
_INFO_KEYS = (
    "quoteType", "financialCurrency", "sector", "industry", "category", "fundFamily", "mostRecentQuarter",
    "marketCap", "enterpriseValue", "trailingPE", "forwardPE", "pegRatio", "trailingPegRatio",
    "priceToBook", "priceToSalesTrailing12Months", "enterpriseToEbitda",
    "totalRevenue", "ebitda", "netIncomeToCommon", "grossMargins", "operatingMargins", "profitMargins",
    "returnOnEquity", "returnOnAssets", "revenueGrowth", "earningsGrowth",
    "totalCash", "totalDebt", "debtToEquity", "currentRatio", "quickRatio", "freeCashflow",
    "trailingEps", "dividendYield", "yield", "payoutRatio", "beta",
    "netExpenseRatio", "threeYearAverageReturn", "fiveYearAverageReturn", "beta3Year",
)


def _share(out: dict, raw: dict, q: dict) -> None:
    info = raw.get("info") or {}
    n: dict = {}
    if info:
        n = _from_info(info)
        out.update(source="company info", report_currency=info.get("financialCurrency") or q.get("currency"),
                   sector=info.get("sector"), industry=info.get("industry"),
                   as_of=_iso_day(info.get("mostRecentQuarter")))
    series = raw.get("series") or {}
    if series:
        s, rc, as_of = _from_series(series, q)
        if not info or not out["report_currency"] or rc == out["report_currency"]:
            for k, v in s.items():
                if n.get(k) is None:
                    n[k] = v
            if not info:
                out.update(source="reported accounts", report_currency=rc)
            elif s:
                out["source"] = "company info and reported accounts"
            out["as_of"] = out["as_of"] or as_of
    out["numbers"] = _clean(n)
    if out["numbers"] and not out["report_currency"]:
        out["report_currency"] = q.get("currency")


def _from_info(i: dict) -> dict:
    g = lambda k: _num(i.get(k))
    dy = g("dividendYield")                                  # already a percentage: 0.67 = 0.67%
    pe = g("trailingPE")
    return {
        "market_cap": g("marketCap"),
        "enterprise_value": g("enterpriseValue"),
        "pe": _positive(pe, 5000),
        "forward_pe": _positive(g("forwardPE"), 5000),
        "peg": _positive(g("pegRatio") or g("trailingPegRatio"), 100),
        "price_to_book": _positive(g("priceToBook")),
        "price_to_sales": _positive(g("priceToSalesTrailing12Months")),
        "ev_to_ebitda": _positive(g("enterpriseToEbitda"), 5000),
        "revenue": g("totalRevenue"),
        "ebitda": g("ebitda"),
        "net_income": g("netIncomeToCommon"),
        "gross_margin": _pct(i.get("grossMargins")),
        "operating_margin": _pct(i.get("operatingMargins")),
        "profit_margin": _pct(i.get("profitMargins")),
        "roe": _pct(i.get("returnOnEquity")),
        "roa": _pct(i.get("returnOnAssets")),
        "revenue_growth": _pct(i.get("revenueGrowth")),
        "earnings_growth": _pct(i.get("earningsGrowth")),
        "total_cash": g("totalCash"),
        "total_debt": g("totalDebt"),
        "debt_to_equity": _positive(g("debtToEquity")) / 100 if _positive(g("debtToEquity")) else None,
        "current_ratio": _positive(g("currentRatio")),
        "quick_ratio": _positive(g("quickRatio")),
        "free_cash_flow": g("freeCashflow"),
        "eps": g("trailingEps"),
        "dividend_yield": dy if dy is not None and 0 < dy <= 30 else None,
        "payout_ratio": _positive(_pct(i.get("payoutRatio")), 1000),
        "beta": g("beta"),
    }


def _last(series: dict, *names) -> tuple[str | None, float | None, str | None]:
    """(day, value, currency) of the newest point across `names`."""
    best = None
    for name in names:
        rows = series.get(name) or []
        if rows and (best is None or rows[-1][0] > best[0]):
            best = rows[-1]
    return (best[0], best[1], best[2]) if best else (None, None, None)


def _year_ago(rows: list, day: str) -> float | None:
    """The value of the point about one year (330 to 400 days) before `day`."""
    try:
        d = date.fromisoformat(day)
    except (TypeError, ValueError):
        return None
    for r in reversed(rows):
        try:
            gap = (d - date.fromisoformat(r[0])).days
        except (TypeError, ValueError):
            continue
        if 330 <= gap <= 400:
            return r[1]
    return None


def _growth(series: dict, *names) -> float | None:
    """Growth in % against a year earlier: from the first of `names` (most
    precise first: quarterly, then trailing 12 months, then yearly) that has
    a point a year before its newest. None when last year's value was 0 or
    below (growth from a loss means nothing)."""
    for name in names:
        rows = series.get(name) or []
        if len(rows) < 2:
            continue
        now, before = rows[-1][1], _year_ago(rows, rows[-1][0])
        if before is None:
            continue
        return (now / before - 1) * 100 if before > 0 else None
    return None


def _from_series(s: dict, q: dict) -> tuple[dict, str | None, str | None]:
    """(numbers, report currency, as-of day) from the reported accounts and
    today's price."""
    as_of, rev, rc = _last(s, "trailingTotalRevenue")
    if rc is None:
        for rows in s.values():
            if rows and rows[-1][2]:
                rc = rows[-1][2]
                break
    val = lambda *names: _last(s, *names)[1]
    gross, op, ebitda = val("trailingGrossProfit"), val("trailingOperatingIncome"), val("trailingEBITDA")
    ni, eps, fcf = val("trailingNetIncomeCommonStockholders"), val("trailingDilutedEPS"), val("trailingFreeCashFlow")
    paid = val("trailingCashDividendsPaid")
    equity = val("quarterlyStockholdersEquity", "annualStockholdersEquity")
    debt = val("quarterlyTotalDebt", "annualTotalDebt")
    cash = val("quarterlyCashCashEquivalentsAndShortTermInvestments", "annualCashCashEquivalentsAndShortTermInvestments")
    ca, cl = val("quarterlyCurrentAssets", "annualCurrentAssets"), val("quarterlyCurrentLiabilities", "annualCurrentLiabilities")
    inventory = val("quarterlyInventory", "annualInventory") or 0.0
    assets = val("quarterlyTotalAssets", "annualTotalAssets")
    shares = _positive(val("quarterlyOrdinarySharesNumber", "annualOrdinarySharesNumber"), 1e13)

    n = {
        "revenue": rev, "ebitda": ebitda, "net_income": ni, "free_cash_flow": fcf, "eps": eps,
        "total_cash": cash, "total_debt": debt,
        "gross_margin": _pct(_div(gross, rev)), "operating_margin": _pct(_div(op, rev)),
        "profit_margin": _pct(_div(ni, rev)),
        "roe": _pct(_div(ni, equity)), "roa": _pct(_div(ni, assets)),
        "debt_to_equity": _div(debt or 0.0, equity) if equity and equity > 0 else None,
        "current_ratio": _div(ca, cl),
        "quick_ratio": _div(ca - inventory, cl) if ca is not None else None,
        "payout_ratio": _pct(_div(-paid, ni)) if paid and paid < 0 and ni and ni > 0 else None,
        "revenue_growth": _growth(s, "quarterlyTotalRevenue", "trailingTotalRevenue", "annualTotalRevenue"),
        "earnings_growth": _growth(s, "quarterlyDilutedEPS", "trailingDilutedEPS", "annualDilutedEPS"),
    }
    # Ratios with the price: the price in the report currency.
    price, ccy = q.get("price"), q.get("currency")
    r_trade, r_report = _rate(ccy), _rate(rc)
    if price and shares and r_trade and r_report:
        p = price * r_trade / r_report                     # price in the report currency
        cap = shares * p
        n["market_cap"] = shares * price                   # trading currency
        n["pe"] = _positive(_div(p, eps), 5000)
        n["price_to_book"] = _positive(_div(cap, equity))
        n["price_to_sales"] = _positive(_div(cap, rev))
        if cash is not None:
            ev = cap + (debt or 0.0) - cash
            n["enterprise_value"] = ev * r_report / r_trade
            n["ev_to_ebitda"] = _positive(_div(ev, ebitda), 5000)
        if paid and paid < 0:
            n["dividend_yield"] = _positive(-paid / shares / p * 100, 30)
    return n, rc, as_of


# -------------------------------------------------------------------- funds
def _fund(out: dict, raw: dict, t: str) -> None:
    i = raw.get("info") or {}
    g = lambda k: _num(i.get(k))
    dy = g("dividendYield")
    if dy is None and g("yield"):
        dy = g("yield") * 100                                 # a fund's: a fraction
    cost = g("netExpenseRatio")                               # a percentage: 0.07 = 0.07%
    if cost is None or cost > 5:
        cost = lookup._fund_fees().get(t)

    def avg_return(k):                                        # a fraction (0.178 = 17.8% a year)
        v = g(k)
        return v * 100 if v is not None and abs(v) <= 1.5 else None

    out.update(source="company info" if i else None, category=i.get("category"), fund_family=i.get("fundFamily"),
               report_currency=out["currency"])
    out["numbers"] = _clean({
        "yearly_cost": cost,
        "pe": _positive(g("trailingPE"), 5000),
        "price_to_book": _positive(g("priceToBook")),
        "dividend_yield": dy if dy is not None and 0 < dy <= 30 else None,
        "return_3y": avg_return("threeYearAverageReturn"),
        "return_5y": avg_return("fiveYearAverageReturn"),
        "beta": g("beta3Year"),
    })
    if out["numbers"] and not out["source"]:
        out["source"] = "fund fee table"
