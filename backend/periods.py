"""Period profit views: today, this week, this month, this year.

These measure how the market value of your *current* holdings has moved since
the start of the calendar period (midnight today, Monday, the 1st of the month,
1 January; see backend/clock.py), computed from daily close prices (yfinance).
This is distinct from all-time unrealized P/L (value vs cost) and from realized
P/L (closed trades).

"Today" is the sum of each holding's today change (main._open_holdings_rows),
so it always matches the dashboard's headline. The other periods use the same
rule against an older close, so on a Monday "this week" equals "today".

Caveat surfaced to the caller: it values *today's* quantities at past prices, so
it reflects market movement of what you hold now, not your actual past balance.
Holdings without a resolvable market ticker are excluded and reported as
coverage gaps, so nothing is silently fabricated.
"""

from __future__ import annotations

from datetime import date, timedelta

from . import cache, clock
from .fx import to_eur
from .prices import quote_scale

_PERIODS = ("today", "week", "month", "year")

# Symbols whose market-data ticker differs from the broker's symbol.
# (Trading 212 uses its own symbology, e.g. FB for Meta, ZPRRd for the Xetra
# SPDR Russell 2000 ETF.)
_ALIAS = {"FB": "META", "ZPRRd": "ZPRR.DE"}


def _ref(closes: list[tuple[date, float]], start: date) -> float | None:
    """The last close before `start` (the period's opening price)."""
    ref = None
    for d, c in closes:
        if d >= start:
            break
        ref = c
    return ref


def _patched(closes, row: dict, scale: float, today: date):
    """Yahoo's daily history now and then misses days for a London ETF. The
    previous session's close is also known from the live quote: when the
    history has no close in the last 4 days before today, add it on the
    previous weekday, so "this week" still starts from the right price."""
    prev = (row.get("_mkt") or {}).get("prev")
    if not closes or not prev or not scale:
        return closes
    before = [d for d, _ in closes if d < today]
    last_day = today - timedelta(days=1)
    while last_day.weekday() >= 5:
        last_day -= timedelta(days=1)
    if before and (today - before[-1]).days > 4 and before[-1] < last_day:
        closes = [c for c in closes if c[0] < today] + [(last_day, prev / scale)] \
            + [c for c in closes if c[0] >= today]
    return closes


def _exact(row: dict) -> dict:
    """The row's unrounded amounts (main._open_holdings_rows keeps them)."""
    return row.get("_exact") or row


def today_total(rows: list[dict]) -> float:
    """Sum of the rows' today changes, unrounded. The dashboard's headline
    and the "today" period both use this one sum."""
    return sum(_exact(r)["daily_eur"] for r in rows if r.get("daily_eur") is not None)


def move_eur(row: dict, ref: float) -> float | None:
    """EUR change of one holding row since the market price `ref` (same rule
    as the today change in main._move_eur). None for a row the market does
    not price (valued at cost): its cost against a past price is no move."""
    mkt = row.get("_mkt") or {}
    q_price, q_ccy = mkt.get("price"), mkt.get("currency") or ""
    own = mkt.get("own_price")
    if not own and not q_price:
        return None
    value = _exact(row)["value_eur"]
    if not own or not q_ccy or q_ccy == row["currency"]:
        if own and q_price and q_price == mkt.get("prev"):
            # market not traded today: the broker's gap to its last price is
            # no session's move (as for today), so measure the market's price
            return to_eur((q_price - ref) * row["quantity"], row["currency"])
        return value - to_eur(ref * row["quantity"], row["currency"])
    if not q_price:
        return None
    pct = q_price / ref - 1
    return value * pct / (1 + pct)


def compute(holdings: list[dict], today: date | None = None) -> dict:
    """holdings: rows from main._open_holdings_rows (ticker, quantity,
    currency, value_eur, daily_eur, name, _mkt)."""
    priceable = [h for h in holdings if h.get("ticker")]
    tickers = sorted({_ALIAS.get(h["ticker"], h["ticker"]) for h in priceable})
    if not tickers:
        return _empty(holdings)

    try:
        # A year of daily closes per ticker set: fresh for 5 minutes, then
        # served while a background download refreshes it.
        series = cache.get("periods.closes:" + ",".join(tickers),
                           lambda: _closes(tickers), ttl=300, stale=24 * 3600, data=False)
    except Exception:
        return _empty(holdings)

    today = today or clock.today()
    starts = clock.period_starts(today)
    periods: dict[str, float | None] = {"today": round(today_total(holdings), 2)}
    for name in ("week", "month", "year"):
        total, any_ref = 0.0, False
        for h in priceable:
            t = _ALIAS.get(h["ticker"], h["ticker"])
            closes = _patched(series.get(t), h, quote_scale(t), today)
            ref = _ref(closes, starts[name]) if closes else None
            if ref is None:
                continue
            m = move_eur(h, ref * quote_scale(t))
            if m is not None:
                total += m
                any_ref = True
        periods[name] = round(total, 2) if any_ref else None

    covered = {_ALIAS.get(h["ticker"], h["ticker"]) for h in priceable} & series.keys()
    covered_value = sum(
        h["value_eur"] for h in priceable
        if _ALIAS.get(h["ticker"], h["ticker"]) in covered
    )
    total_value = sum(h.get("value_eur", 0) for h in holdings)
    missing = sorted(
        {h["name"] for h in holdings
         if _ALIAS.get(h.get("ticker", ""), h.get("ticker", "")) not in covered}
    )
    return {
        "periods": periods,
        "starts": {k: v.isoformat() for k, v in starts.items()},
        "coverage_pct": round(100 * covered_value / total_value, 1) if total_value else 0,
        "excluded": missing,
    }


def _closes(tickers: list[str]) -> dict[str, list[tuple[date, float]]]:
    """{ticker: [(exchange date, close)] over the last ~13 months}, oldest
    first. Closes are split-adjusted (today's share count) but not dividend-
    adjusted. Raises when nothing came back, so the cache keeps its last good
    download."""
    import yfinance as yf

    hist = yf.download(
        tickers, period="13mo", interval="1d",
        progress=False, auto_adjust=False,
    )["Close"]
    series: dict[str, list[tuple[date, float]]] = {}
    if hasattr(hist, "columns"):
        cols = [(t, hist[t]) for t in tickers if t in hist.columns]
    else:  # single ticker -> Series
        cols = [(tickers[0], hist)]
    for t, col in cols:
        col = col.dropna()
        if len(col):
            series[t] = [(ts.date(), float(v)) for ts, v in col.items()]
    if not series:
        raise ValueError("no price history returned")
    return series


def _empty(holdings: list[dict]) -> dict:
    return {
        "periods": {k: None for k in _PERIODS},
        "coverage_pct": 0,
        "excluded": sorted({h.get("name", "") for h in holdings}),
    }
