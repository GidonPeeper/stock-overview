"""Daily value and profit since your first trade: the Performance chart.

Rebuilt from your trade history and daily market prices, so it needs no stored
snapshots and survives the free host's restarts. The last point is today's
live dashboard total, so the chart ends exactly where the headline numbers are.

Per account, like realized.summary(): which shares each account held at the end
of each day (average-cost method, positions.py), valued at that day's close and
that day's EUR exchange rate. Profit = unrealized (value - cost) + realized so
far + dividends and interest received so far, after fees charged outside trades
(income.py), the same total return as the dashboard headline. Selling, or
moving money between brokers, does not move the profit line.

Accounts kept in another currency (a pound Trading 212 account): the dashboard
counts their cost and realized profit in that currency, shown in euros at
today's rate. Each past day uses that day's rate instead, the same rate as the
value, so buying at the market price makes no profit on the day and the last
day meets today's live point.

Prices: Yahoo's closes are adjusted for later splits. They are turned back into
the prices quoted on each day, because the trade history holds the share counts
of the time (DeGiro books a split as shares out and shares in). Without this a
1-for-30 reverse split makes the shares held before it look 30 times too
valuable. A holding without price history is valued at its cost (no gain or
loss) and counted in `coverage`.

Points: one per trading day for the last ~13 months, one per week before that.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from . import cache, clock, closes, sources
from .instruments import TICKERS, prefetch, price_currency, ticker_for
from .positions import Trade, compute_positions
from .prices import quote_scale
from . import fx

log = logging.getLogger("stock_overview.trend")

DAILY_DAYS = 400          # newer points are daily, older ones weekly
BENCH = "^GSPC"           # S&P 500, for "Compare to S&P 500"
last_build: dict = {}     # coverage and timing of the last build (datastatus)


def _accounts() -> dict[str, list[Trade]]:
    """{account: its trades in the currency they were booked in, oldest first}
    (same input as realized.py, which converts them with sources.in_eur)."""
    out = {}
    for label, (_, get) in sources.trade_sources().items():
        try:
            out[label] = sorted(get(), key=lambda t: t.when)
        except Exception:
            continue
    return out


def _booked_in(accts: dict[str, list[Trade]]) -> dict[tuple[str, str], str | None]:
    """{(account, instrument): the one currency its trades are booked in},
    None when they mix currencies (then today's rate is used, as before)."""
    out: dict[tuple[str, str], str | None] = {}
    for label, ts in accts.items():
        for t in ts:
            k, c = (label, t.key), fx.unit(t.currency)[1]
            out[k] = c if out.get(k, c) == c else None
    return out


def _fingerprint(accts: dict[str, list[Trade]]) -> str:
    """Changes when a trade is added or removed (not when exchange rates move)."""
    parts = tuple((label, len(ts), ts[-1].when if ts else "",
                   round(sum(t.quantity for t in ts), 4))
                  for label, ts in sorted(accts.items()))
    return format(hash(parts) & 0xFFFFFFFFFFFF, "x")


def _income_events() -> list[tuple[str, float]]:
    try:
        from . import income
        return income.events()
    except Exception:
        return []


def series() -> list[dict]:
    """Past points (up to yesterday). Served from memory; rebuilt in the
    background a few times a day, and at once when the trades or the income
    change."""
    accts = _accounts()
    if not any(accts.values()):
        return []
    inc = _income_events()
    key = _fingerprint(accts) + format(hash((len(inc), round(sum(a for _, a in inc), 2))) & 0xFFFFFF, "x")
    return cache.get("trend.series:" + key, lambda: build(accts, income_events=inc),
                     ttl=3 * 3600, stale=7 * 24 * 3600, data=False)


def history(live_value: float, live_cost: float, live_realized: float,
            live_income: float = 0.0) -> list[dict]:
    """The chart: past points plus today's live point."""
    past = [p for p in series() if p["day"] < clock.today().isoformat()]
    return past + [_point(clock.today().isoformat(), live_value, live_cost, live_realized,
                          past[-1].get("sp") if past else None, live_income)]


def _point(day: str, value: float, cost: float, realized: float, sp=None,
           income: float = 0.0) -> dict:
    value, cost, realized, income = float(value), float(cost), float(realized), float(income)
    unreal = value - cost
    out = {"day": day, "total_value": round(value, 2), "total_cost": round(cost, 2),
           "unrealized": round(unreal, 2), "realized": round(realized, 2),
           "income": round(income, 2), "total_profit": round(unreal + realized + income, 2)}
    if sp:
        out["sp"] = sp
    return out


# ------------------------------------------------------------------ prices
def _fetch(tickers: list[str], start: str, threads: bool):
    import yfinance as yf

    data = yf.download(sorted(set(tickers)), start=start, interval="1d",
                       auto_adjust=False, actions=True, progress=False, threads=threads)

    def frame(field):
        f = data[field] if field in data else None
        if f is not None and not hasattr(f, "columns"):
            f = f.to_frame(tickers[0])
        return f

    return frame("Close"), frame("Stock Splits")


def _download(tickers: list[str], start: str):
    """Daily closes (prices as quoted on each day, split adjustment undone),
    forward-filled over holidays. A pandas DataFrame, one column per ticker.
    Yahoo now and then drops a few tickers from a bulk download; those are
    asked for again, one at a time."""
    import pandas as pd

    close, splits = _fetch(tickers, start, threads=True)
    missing = [t for t in tickers if t not in close.columns or not close[t].notna().any()]
    if missing:
        c2, s2 = _fetch(missing, start, threads=False)
        got = [t for t in c2.columns if c2[t].notna().any()]
        if got:
            close = close.drop(columns=[t for t in got if t in close.columns])
            close = pd.concat([close, c2[got]], axis=1)
            if s2 is not None:
                keep = [t for t in got if t in s2.columns]
                splits = s2[keep] if splits is None else pd.concat(
                    [splits.drop(columns=[t for t in keep if t in splits.columns]), s2[keep]], axis=1)
    close = close.sort_index().copy()
    # holes in Yahoo's daily history (closes.py), before the split adjustment
    # is undone: hourly prices are split-adjusted like the daily closes
    close, _ = closes.fill_frame(close, until=clock.today() - timedelta(days=1))
    if splits is not None:
        for t in close.columns:
            if t not in splits.columns:
                continue
            s = splits[t].fillna(0.0)
            for when, ratio in s[s > 0].items():
                # prices before a split were quoted in old shares
                close.loc[close.index < when, t] *= float(ratio)
    return close.ffill()


def build(accts: dict[str, list[Trade]] | None = None, today: date | None = None,
          income_events: list[tuple[str, float]] | None = None) -> list[dict]:
    """Rebuild the series from scratch (a few seconds: one price download)."""
    import time

    t0 = time.time()
    accts = accts if accts is not None else _accounts()
    booked = _booked_in(accts)
    accts = {label: sources.in_eur(ts) for label, ts in accts.items()}   # today's rate
    inc = sorted(income_events if income_events is not None else _income_events())
    inc_i, inc_sum = 0, 0.0
    trades = [t for ts in accts.values() for t in ts]
    if not trades:
        return []
    today = today or clock.today()
    first = min(t.when for t in trades)[:10]

    ticker_of: dict[str, str] = {}
    for t in trades:  # the ISIN table first, else the ticker the broker gave
        ticker_of[t.key] = TICKERS.get(t.key, ticker_of.get(t.key) or t.ticker)
    unknown = [k for k, v in ticker_of.items() if not v and k not in TICKERS]
    prefetch(unknown)                    # else a Yahoo lookup (instruments.py)
    for k in unknown:
        ticker_of[k] = ticker_for(k)
    tickers = sorted({tk for tk in ticker_of.values() if tk})
    # rates for the prices, the accounts' own currencies and the S&P 500 (dollars)
    ccys = ({price_currency(tk) for tk in tickers} | {c for c in booked.values() if c}
            | {"USD"}) - {"EUR"}
    fx_pairs = {c: f"EUR{c}=X" for c in ccys}
    # today's rate for the accounts' own currencies (what sources.in_eur used)
    now_rate = {c: fx.to_eur(1.0, c) for c in {b for b in booked.values() if b} - {"EUR"}}
    close = _download(tickers + list(fx_pairs.values()) + [BENCH], start=first[:7] + "-01")

    cols = {c: close[c].to_numpy() for c in close.columns if close[c].notna().any()}
    days = [(i, ts.date()) for i, ts in enumerate(close.index)
            if first <= ts.date().isoformat() and ts.date() < today]
    scale = {tk: quote_scale(tk) for tk in tickers}
    unit_ccy = {tk: price_currency(tk) for tk in tickers}

    def rate_on(ccy: str, i: int) -> float:
        """EUR per unit of `ccy` at day i's close (NaN if unknown)."""
        if ccy == "EUR":
            return 1.0
        r = cols.get(fx_pairs.get(ccy, ""), None)
        return (1.0 / r[i]) if r is not None and r[i] == r[i] and r[i] else float("nan")

    state = {label: [0, {}] for label in accts}       # trades applied, positions
    out: list[dict] = []
    covered = total_cost_last = 0.0
    unpriced: set[str] = set()
    for i, d in days:
        iso = d.isoformat()
        while inc_i < len(inc) and inc[inc_i][0] <= iso:
            inc_sum += inc[inc_i][1]
            inc_i += 1
        # cost and realized booked in another currency: from today's rate
        # (sources.in_eur) to this day's rate; today's rate if it is unknown
        to_day = {c: rate_on(c, i) / now_rate[c] if now_rate[c] else float("nan") for c in now_rate}
        to_day = {c: f if f == f and f > 0 else 1.0 for c, f in to_day.items()}
        value = cost = realized = priced = 0.0
        no_price: set[str] = set()
        for label, ts in accts.items():
            st = state[label]
            n = st[0]
            while n < len(ts) and ts[n].when[:10] <= iso:
                n += 1
            if n != st[0]:
                st[0], st[1] = n, compute_positions(ts[:n])
            for key, p in st[1].items():
                k = to_day.get(booked.get((label, key)) or "EUR", 1.0)
                realized += p.realized * k
                if p.quantity <= 1e-6:
                    continue
                p_cost = p.cost * k
                cost += p_cost
                tk = ticker_of.get(key, "")
                px = cols[tk][i] if tk in cols else float("nan")
                v = px * scale.get(tk, 1.0) * p.quantity * rate_on(unit_ccy.get(tk, "EUR"), i)
                if v == v:                              # not NaN
                    value += v
                    priced += p_cost
                else:
                    value += p_cost                     # no price: neither gain nor loss
                    no_price.add(p.name)
        if cost <= 0 and value <= 0 and realized == 0:
            continue
        # the S&P 500 in euros (it is quoted in dollars), for benchmark()
        sp = cols.get(BENCH)
        sp_eur = float(sp[i]) * float(rate_on("USD", i)) if sp is not None else float("nan")
        point = _point(iso, value, cost, realized, round(sp_eur, 4) if sp_eur == sp_eur else None, inc_sum)
        # the day's rate of each account currency, for benchmark()
        day_fx = {c: float(rate_on(c, i)) for c in now_rate}
        day_fx = {c: round(r, 6) for c, r in day_fx.items() if r == r and r > 0}
        if day_fx:
            point["fx"] = day_fx
        out.append(point)
        covered, total_cost_last, unpriced = priced, cost, no_price

    cutoff = (today - timedelta(days=DAILY_DAYS)).isoformat()
    thinned = [p for j, p in enumerate(out)
               if p["day"] >= cutoff or j == 0 or j == len(out) - 1
               or date.fromisoformat(out[j + 1]["day"]).isocalendar()[1]
               != date.fromisoformat(p["day"]).isocalendar()[1]]
    last_build.update(points=len(thinned), seconds=round(time.time() - t0, 1),
                      coverage_pct=round(100 * covered / total_cost_last, 1) if total_cost_last else 100.0,
                      unpriced=sorted(unpriced))
    log.info("trend rebuilt: %s", last_build)
    return thinned


# --------------------------------------------------------------- benchmark
def benchmark(hist: list[dict]) -> dict[str, float]:
    """What the money you put in would be worth in the S&P 500: every buy
    (sell) bought (sold) index units at that day's close. {day: EUR value}.
    `sp` on each point is the index in euros (at that day's EUR/USD rate), so
    a falling dollar lowers it just as it lowers your dollar shares. Money in
    an account kept in another currency counts at the day's rate (`fx` on the
    point), as in the value line; today's live point uses today's rate."""
    if not hist or not any(p.get("sp") for p in hist):
        return {}
    accts = _accounts()
    events = sorted((t.when[:10], (1 if t.side == "BUY" else -1) * t.amount, t.currency)
                    for ts in accts.values() for t in ts)

    def in_eur(amount: float, ccy: str, day_fx: dict) -> float:
        scale, c = fx.unit(ccy)
        if c == "EUR":
            return amount * scale
        return amount * scale * day_fx[c] if c in day_fx else fx.to_eur(amount, ccy)

    out: dict[str, float] = {}
    units, j, last_sp = 0.0, 0, None
    for p in hist:
        day, sp = p["day"], p.get("sp") or last_sp
        last_sp = sp
        # money in (or out) before the first known index level waits for it
        while sp and j < len(events) and events[j][0] <= day:
            units += in_eur(events[j][1], events[j][2], p.get("fx") or {}) / sp
            j += 1
        if sp:
            out[day] = round(units * sp, 2)
    return out
