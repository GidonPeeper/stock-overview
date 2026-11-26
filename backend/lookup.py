"""Look up the price of any share, fund, index, currency or crypto, held or not.

Serves the search in the dashboard's top bar (GET /api/quote/{ticker},
/api/quote/{ticker}/chart, /api/search?kinds=all) and the MCP tool get_quote.

Where each part comes from (Yahoo Finance, through yfinance):
  * price, today's change, the time of the price, name, exchange, kind,
    trading currency, day range, 52-week range and whether the market is
    open: one chart request (`Ticker.history` and its `history_metadata`),
    the same request prices.py makes for holdings. It works from hosted
    servers too.
  * the chart for a range: the chart request for that range. Holes in
    Yahoo's daily bars are filled from hourly prices (closes.py).
  * market value, P/E, dividend yield and a fund's yearly cost: Yahoo's
    company-info request (`Ticker.get_info`). Hosted servers are often
    refused it, so these are a bonus: fetched in the background after the
    price, waited for a moment at most, and left out when missing (never 0).
    A share's market value can also come from fast_info's share count, and a
    fund's yearly cost from the checked table in fund_fees.json.
  * search by name, ticker or ISIN: Yahoo's symbol search (watch.py).

London prices in pence (GBp) become pounds here, once, for every price-like
number of an answer: price, closes, ranges and chart. (Yahoo's market value
is already in pounds.)

Every Yahoo request has a time limit, so a slow Yahoo ends in a plain error
instead of a page that hangs. Answers are cached stale-while-revalidate
(cache.py): a repeated lookup within a minute does not ask Yahoo again, and
for a quarter of an hour after that it is answered at once while one
background refresh runs.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as _Timeout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import cache, clock, closes, fx, market, watch
from .prices import _number

log = logging.getLogger("stock_overview.lookup")

# A Yahoo symbol: AAPL, RMV.L, BRK-B, ^GSPC, EURUSD=X, BTC-EUR, M&M.NS, ES=F.
_TICKER = re.compile(r"\^?[A-Z0-9][A-Z0-9.=&-]{0,23}")
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
RANGES = ("1d", "5d", "1mo", "6mo", "1y", "5y")   # 1D, 1W, 1M, 6M, 1Y, 5Y (market._RANGES)

_WAIT = 12.0          # seconds a lookup waits for Yahoo's chart request in all
_HTTP_TIMEOUT = 8     # seconds per request (yfinance's own limit)
_FACTS_GRACE = 0.35   # seconds the price waits for company facts already on their way
_FACTS_TTL = 6 * 3600
_FACTS_STALE = 18 * 3600
_INFO_OFF = 3600      # after three refused company-info requests in a row, skip it this long

# Kinds whose price is an amount of money (an index is points, a currency
# pair is a rate): only these get a display-currency price and a calculator.
_MONEY_KINDS = {"share", "etf", "fund", "crypto", "other"}
# Which company facts make sense for which kind.
_FACT_KINDS = {"market_cap": ("share", "crypto"), "pe": ("share", "etf"),
               "dividend_yield_pct": ("share", "etf", "fund"), "yearly_cost_pct": ("etf", "fund")}

# Separate pools, so a slow company-info request never holds up a price.
_quotes_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lookup")
_facts_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="lookup-facts")
_jobs: dict[str, Future] = {}           # ticker -> company-facts job running now
_jobs_lock = threading.Lock()
_info = {"fails": 0, "off_until": 0.0}  # company-info requests refused in a row
_info_lock = threading.Lock()
_fees: dict[str, float] | None = None


# ------------------------------------------------------------------ errors
class QuoteError(Exception):
    """A lookup that cannot be answered. `status` is the HTTP status; the
    message is plain words for the person (or Claude) who asked."""
    status = 503


class BadInput(QuoteError):
    status = 400


class UnknownTicker(QuoteError):
    status = 404


class YahooUnavailable(QuoteError):
    status = 503


_DOWN = "Yahoo Finance is not answering right now, so there is no price. Try again in a minute."
_SLOW = "Yahoo Finance took too long to answer. Try again in a minute."


def reset() -> None:
    """Forget the company-info failures (tests)."""
    with _info_lock:
        _info.update(fails=0, off_until=0.0)


# ----------------------------------------------------------------- helpers
def clean_ticker(text) -> str:
    """The ticker in capitals, or BadInput before anything is asked of Yahoo."""
    t = str(text or "").strip().upper()
    if not _TICKER.fullmatch(t):
        raise BadInput("That is not a ticker. A ticker is up to 24 letters, digits and . - = ^ &, "
                       "for example AAPL, RMV.L, ^GSPC, EURUSD=X or BTC-EUR.")
    return t


def _yf():
    try:
        import yfinance as yf
    except Exception:
        raise YahooUnavailable(_DOWN) from None
    return yf


def _within(pool: ThreadPoolExecutor, seconds: float, fn, *args):
    """fn(*args) in `pool`, waiting at most `seconds`: then a plain error,
    and the request finishes (or fails) on its own in the background."""
    try:
        job = pool.submit(fn, *args)
    except RuntimeError:          # the server is shutting down
        raise YahooUnavailable(_DOWN) from None
    try:
        return job.result(timeout=seconds)
    except _Timeout:
        log.warning("Yahoo did not answer %s%r within %ss", fn.__name__, args, seconds)
        raise YahooUnavailable(_SLOW) from None


def _unit(ccy: str) -> tuple[float, str]:
    """(scale, ISO currency) for Yahoo's currency code. Prices in pence (GBp)
    are pounds x 0.01, as fx.unit says; South African cents and Israeli
    agorot likewise. Empty stays empty (never taken for euros)."""
    c = (ccy or "").strip()
    if not c:
        return 1.0, ""
    if c in ("ZAc", "ZAC"):
        return 0.01, "ZAR"
    if c == "ILA":
        return 0.01, "ILS"
    return fx.unit(c)


def _scaled(v, scale: float) -> float | None:
    n = _number(v)
    return n * scale if n is not None else None


def _r(v: float | None, digits: int = 6) -> float | None:
    return round(v, digits) if v is not None else None


def _now() -> datetime:
    return datetime.now(clock.tz())


def _moment(v) -> datetime | None:
    """A Yahoo time (pandas Timestamp, datetime or epoch seconds) as an aware
    datetime in the dashboard's time zone (clock.py)."""
    if v is None:
        return None
    try:
        if hasattr(v, "to_pydatetime"):
            v = v.to_pydatetime()
        if isinstance(v, datetime):
            return v.astimezone(clock.tz()) if v.tzinfo else None
        return datetime.fromtimestamp(float(v), clock.tz())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _when_text(when: datetime | None, today: date) -> str | None:
    """'at 16:35' for a price from today, else which day's close it is."""
    if when is None:
        return None
    d = when.date()
    if d >= today:
        return f"at {when:%H:%M}"
    if d == today - timedelta(days=1):
        return "at yesterday's close"
    if (today - d).days < 7:
        return f"at {when:%A}'s close"
    return f"at the close on {d.day} {d:%b %Y}"


def _market(meta: dict, kind: str, when: datetime | None, traded_today: bool,
            now: datetime) -> tuple[bool | None, str | None]:
    """(is the market open now, a line to show while it is closed); (None,
    None) when Yahoo does not say. From the chart's current trading period."""
    if kind == "crypto":
        return True, None                     # trades day and night
    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    start, end = _moment(regular.get("start")), _moment(regular.get("end"))
    if start is None or end is None:
        return None, None
    if start <= now < end and (traded_today or when is None):
        return True, None
    if traded_today:
        return False, "Market closed for today."
    if now < start and start.date() == now.date():
        return False, f"Market closed. It opens at {start:%H:%M}."
    return False, "Market closed today."


def _decimals(hint, scale: float) -> int:
    """Decimals worth showing: Yahoo's price hint, two more for a price that
    was in pence (547.4p = £5.474)."""
    try:
        d = int(hint)
    except (TypeError, ValueError):
        d = 2
    if scale != 1.0:
        d += 2
    return max(0, min(d, 6))


def _blank(t: str) -> dict:
    """Every field of an answer, empty."""
    return {"ticker": t, "name": t, "kind": "other", "exchange": "", "currency": None, "decimals": 2,
            "price": None, "price_time": None, "price_time_text": None, "traded_today": None,
            "previous_close": None, "change": None, "change_pct": None, "last_session": None,
            "market_open": None, "market_note": None, "facts": {}, "eur_per_unit": None,
            "price_eur": None, "message": None, "fetched_at": round(time.time())}


# ------------------------------------------------------------------- quote
def quote(ticker: str) -> dict:
    """One instrument's price, today's change, the time of the price, market
    state and facts (see the module docstring). Raises QuoteError."""
    t = clean_ticker(ticker)
    q = cache.get(f"lookup.quote:{t}:{clock.today().isoformat()}",   # a new day: a new "today"
                  lambda: _within(_quotes_pool, _WAIT, _fetch_quote, t),
                  ttl=60, stale=15 * 60, data=False)
    out = dict(q)                                    # never change the cached answer
    facts, pending = _facts(t, q) if q.get("price") is not None else ({}, False)
    out["facts"] = {**(q.get("facts") or {}), **facts}
    out["facts_pending"] = pending
    return out


def _meta(tk) -> dict:
    """The chart's metadata; empty when the request found nothing (yfinance
    then raises on reading it)."""
    try:
        return dict(getattr(tk, "history_metadata", None) or {})
    except Exception:
        return {}


def _fetch_quote(t: str) -> dict:
    yf = _yf()
    tk = yf.Ticker(t)
    try:
        tk.history(period="1d", interval="1d", auto_adjust=False, timeout=_HTTP_TIMEOUT)
    except Exception as exc:
        log.info("chart request for %s failed: %r", t, exc)
        raise YahooUnavailable(_DOWN) from None
    meta = _meta(tk)
    raw = _number(meta.get("regularMarketPrice"))
    if raw is None:
        return _without_price(t)

    scale, ccy = _unit(meta.get("currency") or "")
    kind = watch.KINDS.get(meta.get("instrumentType"), "other")
    price = raw * scale
    prev = _scaled(meta.get("chartPreviousClose"), scale) or _scaled(meta.get("previousClose"), scale)
    now = _now()
    today = now.date()
    when = _moment(meta.get("regularMarketTime"))
    traded_today = when is None or when.date() >= today   # as prices.py: unknown time counts as today
    is_open, note = _market(meta, kind, when, traded_today, now)

    out = _blank(t)
    out.update({
        "name": meta.get("longName") or meta.get("shortName") or t,
        "kind": kind,
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "",
        "currency": ccy or None,
        "decimals": _decimals(meta.get("priceHint"), scale),
        "price": _r(price),
        "price_time": when.isoformat(timespec="minutes") if when else None,
        "price_time_text": _when_text(when, today),
        "traded_today": traded_today,
        "market_open": is_open,
        "market_note": note,
    })
    if prev:
        move = {"change": _r(price - prev), "change_pct": round((price / prev - 1) * 100, 2)}
        if traded_today:
            out.update(previous_close=_r(prev), **move)
        else:   # today's change is nothing yet; say how the last session went
            out["last_session"] = {"day": when.date().isoformat(), **move}

    facts = {}
    for key, lo, hi in (("day_range", "regularMarketDayLow", "regularMarketDayHigh"),
                        ("year_range", "fiftyTwoWeekLow", "fiftyTwoWeekHigh")):
        a, b = _scaled(meta.get(lo), scale), _scaled(meta.get(hi), scale)
        if a and b and a <= b:
            facts[key] = [_r(a), _r(b)]
    out["facts"] = facts

    if kind in _MONEY_KINDS and ccy:
        per = None
        try:
            per = fx.to_eur(1.0, ccy)
        except Exception:
            pass
        if per and (ccy == "EUR" or per != 1.0):   # 1.0 for another currency = no rate known
            out["eur_per_unit"] = _r(per, 8)
            out["price_eur"] = _r(price * per, 4)
    return out


def _without_price(t: str) -> dict:
    """The chart answered without a price: a ticker Yahoo does not know
    (UnknownTicker), or one it knows but cannot price right now."""
    try:
        hits = watch._yahoo_search(t)
    except watch.SearchUnavailable:
        raise YahooUnavailable(_DOWN) from None
    hit = next((q for q in hits if str(q.get("symbol", "")).upper() == t), None)
    if hit is None:
        raise UnknownTicker(f"Yahoo Finance does not know the ticker {t}. "
                            "Search by the name or the ISIN instead.")
    out = _blank(t)
    out.update(name=watch._name(hit), kind=watch.KINDS.get(hit.get("quoteType"), "other"),
               exchange=hit.get("exchDisp") or hit.get("exchange") or "",
               message="Yahoo Finance has no price for this right now.")
    return out


# ------------------------------------------------------------ company facts
def _facts(t: str, q: dict) -> tuple[dict, bool]:
    """(facts that make sense for this kind, still on their way?). Never
    waits more than _FACTS_GRACE: the price does not wait for these."""
    key, kind = f"lookup.facts:{t}", q["kind"]

    def compute():
        return _fetch_facts(t, kind)

    has, age = cache.peek(key)
    if has and age < _FACTS_TTL + _FACTS_STALE:    # answered at once (refreshed in the background)
        try:
            return _shape(cache.get(key, compute, ttl=_FACTS_TTL, stale=_FACTS_STALE, data=False),
                          kind, q["price"]), False
        except Exception:
            return {}, False
    with _jobs_lock:
        job = _jobs.get(t)
        if job is None:
            try:
                job = _jobs[t] = _facts_pool.submit(cache.get, key, compute, _FACTS_TTL, _FACTS_STALE, False)
            except RuntimeError:
                return {}, False
            job.add_done_callback(lambda _job, t=t: _jobs.pop(t, None))
    try:
        return _shape(job.result(timeout=_FACTS_GRACE), kind, q["price"]), False
    except _Timeout:
        return {}, True
    except Exception:
        return {}, False


def _shape(raw: dict, kind: str, price: float | None) -> dict:
    cap = raw.get("market_cap")
    if cap is None and raw.get("shares") and price:
        cap = raw["shares"] * price            # price is already in pounds, not pence
    values = {"market_cap": cap, "pe": raw.get("pe"),
              "dividend_yield_pct": raw.get("dividend_yield_pct"),
              "yearly_cost_pct": raw.get("yearly_cost_pct")}
    out = {}
    for k, v in values.items():
        if v and kind in _FACT_KINDS[k]:
            out[k] = round(v) if k == "market_cap" else round(v, 4 if k == "yearly_cost_pct" else 2)
    return out


def _fetch_facts(t: str, kind: str) -> dict:
    """Market value, P/E, dividend yield and yearly cost, each only when known.
    Raises when Yahoo could not be asked at all (try again later)."""
    yf = _yf()
    out: dict = {}
    info = _company_info(yf, t)
    if info is not None:
        out["market_cap"] = _number(info.get("marketCap"))   # in pounds for a pence-quoted share
        pe = _number(info.get("trailingPE"))
        out["pe"] = pe if pe and pe < 5000 else None
        dy = _number(info.get("dividendYield"))             # a percentage: 0.67 = 0.67%
        if dy is None and _number(info.get("yield")):
            dy = _number(info.get("yield")) * 100           # a fund's: a fraction
        out["dividend_yield_pct"] = dy if dy and dy <= 30 else None
        ter = _number(info.get("netExpenseRatio"))          # a percentage: 0.0945 = 0.0945%
        out["yearly_cost_pct"] = ter if ter and ter <= 5 else None
    if kind in ("etf", "fund") and not out.get("yearly_cost_pct"):
        out["yearly_cost_pct"] = _fund_fees().get(t)
    if kind == "share" and not out.get("market_cap"):
        try:
            out["shares"] = _number(yf.Ticker(t).fast_info["shares"])
        except Exception:
            pass
    out = {k: v for k, v in out.items() if v is not None}
    if info is None and not out:
        raise LookupError(f"no company facts for {t} right now")
    return out


def _company_info(yf, t: str) -> dict | None:
    """Yahoo's company info, or None when it was refused. After three
    refusals in a row (a host Yahoo turns away) it is not asked for an hour."""
    if time.time() < _info["off_until"]:
        return None
    try:
        info = yf.Ticker(t).get_info() or {}
    except Exception:
        info = {}
    ok = isinstance(info, dict) and bool(info.get("quoteType"))
    with _info_lock:
        if ok:
            _info["fails"] = 0
        else:
            _info["fails"] += 1
            if _info["fails"] >= 3:
                _info.update(fails=0, off_until=time.time() + _INFO_OFF)
                log.info("Yahoo refused company info three times in a row; not asking for an hour")
    return info if ok else None


def _fund_fees() -> dict[str, float]:
    """Ticker -> yearly cost in % from the checked table (fund_fees.json)."""
    global _fees
    if _fees is None:
        table: dict[str, float] = {}
        try:
            data = json.loads(Path(__file__).with_name("fund_fees.json").read_text())
            for group in data.get("groups", []):
                for f in group.get("funds", []):
                    for tk in f.get("tickers", []):
                        table[tk.upper()] = float(f["ter_pct"])
                cheaper = group.get("cheaper") or {}
                if cheaper.get("yahoo"):
                    table[cheaper["yahoo"].upper()] = float(cheaper["ter_pct"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        _fees = table
    return _fees


# ------------------------------------------------------------------- chart
def chart(ticker: str, rng: str = "1mo") -> dict:
    """Prices over a range for the lookup's chart: {points: [[ms, price]],
    base, change, change_pct, ...}. For 1d the change is against the close
    before the session, as today's change is; else against the first point."""
    t = clean_ticker(ticker)
    if rng not in RANGES:
        raise BadInput("The range must be one of " + ", ".join(RANGES) + ".")
    ttl = market._RANGES[rng][2]
    return cache.get(f"lookup.chart:{t}:{rng}:{clock.today().isoformat()}",
                     lambda: _within(_quotes_pool, _WAIT, _fetch_chart, t, rng),
                     ttl=ttl, stale=ttl, data=False)


def _noon_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc).timestamp() * 1000)


def _fetch_chart(t: str, rng: str) -> dict:
    period, interval, _ = market._RANGES[rng]
    yf = _yf()
    tk = yf.Ticker(t)
    try:
        frame = tk.history(period=period, interval=interval, auto_adjust=False, timeout=_HTTP_TIMEOUT)
    except Exception as exc:
        log.info("chart request for %s (%s) failed: %r", t, rng, exc)
        raise YahooUnavailable(_DOWN) from None
    meta = _meta(tk)
    scale, ccy = _unit(meta.get("currency") or "")
    intraday = interval[-1] in "mh"
    try:
        rows = [(ts, float(v)) for ts, v in frame["Close"].dropna().items() if _number(v)]
    except (KeyError, TypeError, AttributeError, ValueError):
        rows = []

    if intraday:
        points = [[int(ts.timestamp() * 1000), v] for ts, v in rows]
        # the 5-minute bars stop before a closing auction: end at the quoted price
        last, when = _number(meta.get("regularMarketPrice")), _moment(meta.get("regularMarketTime"))
        if points and last and when and when.timestamp() * 1000 > points[-1][0]:
            points.append([int(when.timestamp() * 1000), last])
    else:
        days = sorted(dict((ts.date(), v) for ts, v in rows).items())   # the bar's own (exchange) date
        if interval == "1d":
            days = closes.fill_list(t, days, until=clock.today(), fetch=closes.hourly_closes)
        points = [[_noon_ms(d), v] for d, v in days]
    points = [[x, _r(v * scale)] for x, v in points]        # pence -> pounds, once

    base = _scaled(meta.get("chartPreviousClose"), scale) if rng == "1d" else None
    if base is None and points:
        base = points[0][1]
    change = points[-1][1] - base if points and base else None
    return {"ticker": t, "range": rng, "currency": ccy or None, "intraday": intraday,
            "points": points, "base": _r(base),
            "change": _r(change), "change_pct": round(change / base * 100, 2) if change is not None else None}


# ------------------------------------------------------------------ search
def search(query: str, limit: int = 8) -> list[dict]:
    """[{ticker, name, exchange, kind, (isin), (held)}] for a name, ticker or
    ISIN. Listings the user holds are marked `held`; one that matches the ISIN
    or the ticker (VWRL finds your VWRL.L) comes first even when Yahoo lists
    another line of it first."""
    s = str(query or "").strip()
    if not s:
        return []
    if len(s) > 64:
        raise BadInput("Search for at most 64 characters: a name, a ticker or an ISIN.")
    up = s.upper()
    isin = up if _ISIN.fullmatch(up) else None
    mine = _own_listings()
    first = [h for h in mine if (isin and h["isin"] == isin) or up in (h["ticker"], h["ticker"].split(".")[0])]
    try:
        found = watch.search_any(s, limit)
    except watch.SearchUnavailable:
        if first:
            return first
        raise YahooUnavailable("Yahoo Finance is not answering right now, so the search cannot run. "
                               "Try again in a minute.") from None
    held = {h["ticker"] for h in mine}
    for r in found:
        if isin:
            r["isin"] = isin
        if r["ticker"] in held:
            r["held"] = True
    have = {r["ticker"] for r in found}
    for h in first:      # kind: from Yahoo's line of the same ISIN or the same ticker
        same = next((r for r in found if isin or r["ticker"].split(".")[0] == h["ticker"].split(".")[0]), None)
        h["kind"] = same["kind"] if same else None
    lead = {h["ticker"] for h in first}
    return ([r for r in found if r["ticker"] in lead] + [h for h in first if h["ticker"] not in have]
            + [r for r in found if r["ticker"] not in lead])[:limit]


def _own_listings() -> list[dict]:
    """The user's holdings that have a ticker, one per ticker."""
    try:
        from . import sources
        holdings = sources.holdings()
    except Exception:
        return []
    out, seen = [], set()
    for h in holdings:
        t = (h.ticker or "").upper()
        if t and t not in seen:
            seen.add(t)
            out.append({"ticker": t, "name": h.name, "exchange": "", "kind": None,
                        "isin": (h.isin or "").upper() or None, "held": True})
    return out


def find(text: str) -> tuple[dict, list[dict], str | None]:
    """(quote, other matches, the ISIN asked for) for a ticker, a name or an
    ISIN, for the MCP tool. Text typed in capitals or with . ^ = - & or
    digits is tried as a ticker first; anything else is searched for."""
    s = str(text or "").strip()
    if not s:
        raise BadInput("Give a ticker (AAPL), a name (Nvidia) or an ISIN (US67066G1040).")
    up = s.upper()
    isin = up if _ISIN.fullmatch(up) else None
    looks_like_ticker = s == up or any(c in s for c in ".^=-&") or any(c.isdigit() for c in s)
    if not isin and _TICKER.fullmatch(up) and looks_like_ticker:
        try:
            return quote(up), [], None
        except UnknownTicker:
            pass                       # perhaps a name in capitals: search for it
    results = search(s)
    if not results:
        raise UnknownTicker(f"Nothing on Yahoo Finance matches {s!r}. "
                            "Try the ticker (for example AAPL) or the ISIN (12 characters).")
    return quote(results[0]["ticker"]), results[1:6], isin
