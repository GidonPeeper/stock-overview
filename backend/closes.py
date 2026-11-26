"""Fill the holes in Yahoo's daily price history.

Yahoo's daily bars now and then miss a run of days for a London ETF (for
example 11 to 25 November 2026 for PACW.L), while its hourly bars for the same
days are there. A hole is not a holiday: carried forward, the last close before
it becomes the price for every missing day, so the chart stays flat and then
jumps, and "this week" starts from a price that is weeks old.

`missing_days` finds such holes (three or more weekdays in a row without a
close; public holidays never run that long) and `hourly_closes` reads the
last hourly price of each day to fill them. Hourly prices reach back about
two years; an older hole is left as it is. The last hourly price can differ
from the official close by a few hundredths of a percent.
"""

from __future__ import annotations

import os
from datetime import date, timedelta

_MIN_HOLE = 3          # weekdays in a row without a close
_HOURLY_REACH = 725    # days of hourly history Yahoo serves (730, with a margin)
_STILL_TRADING = 45    # days since the last close within which a share counts as still trading


def missing_days(days: list[date], until: date | None = None) -> list[date]:
    """Weekdays inside holes of `_MIN_HOLE` or more in the sorted list `days`
    (days with a close). With `until`, the stretch after the last close up to
    and including that day is checked too."""
    out: list[date] = []
    ends = list(days) + ([until + timedelta(days=1)] if until and days and until > days[-1] else [])
    for a, b in zip(ends, ends[1:]):
        hole, d = [], a + timedelta(days=1)
        while d < b:
            if d.weekday() < 5:
                hole.append(d)
            d += timedelta(days=1)
        if len(hole) >= _MIN_HOLE:
            out.extend(hole)
    return out


def _period(oldest: date, today: date) -> str | None:
    age = (today - oldest).days
    for limit, period in ((25, "1mo"), (85, "3mo"), (175, "6mo"), (360, "1y"), (_HOURLY_REACH, "2y")):
        if age <= limit:
            return period
    return None


def hourly_closes(ticker: str, wanted: list[date], today: date | None = None) -> dict[date, float]:
    """{day: last hourly price of that day} for the `wanted` days Yahoo has
    hourly bars for. Empty when it has none or the request fails: the caller
    then keeps what it had."""
    if os.getenv("PRICE_GAPS") == "0":     # tests: no network
        return {}
    today = today or date.today()
    recent = [d for d in wanted if (today - d).days <= _HOURLY_REACH]
    period = _period(min(recent), today) if recent else None
    if period is None:
        return {}
    try:
        import yfinance as yf

        bars = yf.Ticker(ticker).history(period=period, interval="1h", auto_adjust=False)["Close"].dropna()
    except Exception:
        return {}
    last: dict[date, float] = {}
    for ts, v in bars.items():        # oldest first: the last bar of a day wins
        last[ts.date()] = float(v)
    want = set(recent)
    return {d: v for d, v in last.items() if d in want and v > 0}


def fill_frame(close, until: date | None = None, fetch=hourly_closes):
    """Fill holes in a table of daily closes (pandas, one column per ticker,
    one row per day), per ticker, between its first and last close and up to
    `until`. Returns the table (changed in place when nothing is added) and
    the tickers it filled."""
    import pandas as pd

    filled = []
    for t in list(close.columns):
        col = close[t].dropna()
        if col.empty:
            continue
        days = [ts.date() for ts in col.index]
        # the stretch after the last close counts only for a share still trading
        gaps = missing_days(days, until if until and (until - days[-1]).days <= _STILL_TRADING else None)
        if not gaps:
            continue
        got = fetch(t, gaps)
        if not got:
            continue
        for d, v in got.items():
            close.loc[pd.Timestamp(d), t] = v
        filled.append(t)
    return (close.sort_index() if filled else close), filled


def fill_list(ticker: str, closes: list[tuple[date, float]], until: date | None = None,
              fetch=hourly_closes) -> list[tuple[date, float]]:
    """The same for one ticker's [(day, close)] list, oldest first."""
    if not closes:
        return closes
    last = closes[-1][0]
    gaps = missing_days([d for d, _ in closes],
                        until if until and (until - last).days <= _STILL_TRADING else None)
    got = fetch(ticker, gaps) if gaps else {}
    if not got:
        return closes
    have = dict(closes)
    for d, v in got.items():
        have.setdefault(d, v)
    return sorted(have.items())
