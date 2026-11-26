"""Live price layer.

Given market-data tickers, return the current price and the reference close
for each: the last close before today (calendar day, see backend/clock.py).
That reference powers the per-holding "today" change, so a market that has not
traded yet today shows no change instead of the previous session's move. This
is the source of "what is it worth right now" for holdings that didn't arrive
with a price (i.e. everything except Trading 212), and of the reference for all.

Provider is yfinance (free, no key, broad European coverage). Quotes are
fetched in parallel (a small thread pool, not one ticker after another) and
cached: fresh for 60 s, then served for up to an hour more while one
background refresh runs (stale-while-revalidate). So a dashboard load only
waits on Yahoo for a ticker it has never priced. A failed fetch keeps the last
good quote, and a ticker Yahoo cannot price is retried in the background, not
on every request. A small sample map keeps the demo rendering when offline.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import clock

# Fallback (price, prev_close) for the sample tickers, so the demo shows
# realistic values and a small daily move when offline. Ignored once real
# data flows.
SAMPLE_PRICES = {
    "ASML.AS": (685.0, 681.0), "INGA.AS": (15.8, 15.7),
    "SAP.DE": (188.0, 189.2), "AIR.PA": (158.0, 156.9),
    "AAPL": (212.4, 211.0), "MSFT": (441.8, 439.0),
    "VWRL.L": (118.2, 117.6),
}

# ticker -> (price, prev_close, fetched_at, currency, day the quote was for)
_CACHE: dict[str, tuple] = {}
_TTL = 60          # seconds a quote counts as fresh
_STALE = 60 * 60   # then served at once while a background refresh runs
_RETRY = 300       # a ticker whose last fetch failed is retried after this
_WORKERS = 8       # parallel Yahoo requests

_FAILED: dict[str, float] = {}       # ticker -> time of the last failed fetch
_fetch_lock = threading.Lock()       # one fetch at a time; waiters then hit the cache
_bg_lock = threading.Lock()
_bg_pending: set[str] = set()        # tickers being refreshed in the background

# ticker -> 0.01 if Yahoo quotes it in pence (GBp), else 1.0. Only London (.L)
# listings can be pence-quoted, so nothing else costs a lookup.
_SCALE: dict[str, float] = {}


def _scale_from(info) -> float:
    try:
        ccy = info.get("currency") if hasattr(info, "get") else info["currency"]
    except Exception:
        return 1.0
    return 0.01 if ccy in ("GBp", "GBX") else 1.0


def quote_scale(ticker: str) -> float:
    """Multiply a Yahoo price for `ticker` by this to get pounds/euros/dollars.
    0.01 for pence-quoted London shares (e.g. RMV.L at 600p -> £6.00), else 1."""
    if not ticker or not ticker.endswith(".L"):
        return 1.0
    if ticker not in _SCALE:
        try:
            import yfinance as yf

            _SCALE[ticker] = _scale_from(yf.Ticker(ticker).fast_info)
        except Exception:
            return 1.0  # unknown for now; retry next call
    return _SCALE[ticker]


def _fetch_one(yf, t: str) -> tuple | None:
    """(price, prev_close, currency) in pounds/euros/dollars, or None if Yahoo
    has none. prev_close is the close of the session before today's, so it
    equals the price when the market has not traded today.

    One small request: today's chart, whose metadata carries the live price,
    the time of the last trade and Yahoo's previous close. (Yahoo's daily
    history has gaps for some London ETFs; this previous close does not.)"""
    try:
        tk = yf.Ticker(t)
        tk.history(period="1d", interval="1d", auto_adjust=False)
        meta = dict(getattr(tk, "history_metadata", None) or {})
        last = meta.get("regularMarketPrice")
        if not last:
            return None
        ccy = meta.get("currency") or ""
        scale = 0.01 if ccy in ("GBp", "GBX") else 1.0
        if t.endswith(".L"):
            _SCALE[t] = scale
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        traded = clock.local_date(meta.get("regularMarketTime"))
        if traded is not None and traded < clock.today():
            prev = last  # no trading yet today: nothing changed today
        ccy = "GBP" if scale != 1.0 else ccy.upper()
        return float(last) * scale, (float(prev) * scale if prev else None), ccy
    except Exception:
        return None


def _fetch_many(tickers: list[str]) -> None:
    """Fetch quotes in parallel into the cache. A ticker that fails keeps its
    previous quote (if any) and is marked as failed."""
    try:
        import yfinance as yf
    except Exception:
        return  # yfinance unavailable -> sample prices
    now, day = time.time(), clock.today()
    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(tickers))) as pool:
        results = list(pool.map(lambda t: _fetch_one(yf, t), tickers))
    for t, r in zip(tickers, results):
        if r:
            _CACHE[t] = (r[0], r[1], now, r[2], day)
            _FAILED.pop(t, None)
        else:
            _FAILED[t] = now


def _plan(t: str, now: float) -> str:
    """'ok' (use what we have), 'sync' (must fetch before answering) or 'bg'
    (answer now, refresh in the background)."""
    c = _CACHE.get(t)
    if c and c[4] != clock.today():
        c = None  # yesterday's quote: its "today" change is out of date
    age = now - c[2] if c else None
    if age is not None and age < _TTL:
        return "ok"
    failed_at = _FAILED.get(t)
    if failed_at is not None:  # last attempt failed: never block on it again
        return "bg" if now - failed_at >= _RETRY else "ok"
    if age is None or age >= _TTL + _STALE:
        return "sync"
    return "bg"


def _refresh_in_background(tickers: list[str]) -> None:
    with _bg_lock:
        todo = [t for t in tickers if t not in _bg_pending]
        _bg_pending.update(todo)
    if not todo:
        return

    def run():
        try:
            with _fetch_lock:
                now = time.time()
                due = [t for t in todo if _plan(t, now) != "ok"]
                if due:
                    _fetch_many(due)
        finally:
            with _bg_lock:
                _bg_pending.difference_update(todo)

    threading.Thread(target=run, name="quotes-refresh", daemon=True).start()


def fetch_quotes(tickers: list[str]) -> dict[str, dict]:
    """Return {ticker: {"price": float, "prev_close": float|None, "currency": str}}.
    prev_close is the last close before today (calendar day)."""
    tickers = sorted(set(t for t in tickers if t))
    if not tickers:
        return {}

    if any(_plan(t, time.time()) == "sync" for t in tickers):
        with _fetch_lock:  # concurrent callers wait here, then find the cache warm
            now = time.time()
            todo = [t for t in tickers if _plan(t, now) == "sync"]
            if todo:
                _fetch_many(todo)

    now = time.time()
    due = [t for t in tickers if _plan(t, now) == "bg"]
    if due:
        _refresh_in_background(due)

    out: dict[str, dict] = {}
    today = clock.today()
    for t in tickers:
        c = _CACHE.get(t)
        if c:
            # a quote from an earlier day (today's fetch failed) is still the
            # last known price, but its move is not today's: no "today" change
            out[t] = {"price": c[0], "prev_close": c[1] if c[4] == today else None, "currency": c[3]}
        elif t in SAMPLE_PRICES and _demo():   # made-up prices: never for your own holdings
            p, pc = SAMPLE_PRICES[t]
            out[t] = {"price": p, "prev_close": pc, "currency": ""}
    return out


def _demo() -> bool:
    from .datafiles import demo
    return demo()


def quotes_status(tickers: list[str]) -> dict:
    """{"as_of": epoch of the oldest quote used, "refreshing": bool}, so the UI
    can say "updating…" while it shows quotes that are being refreshed."""
    held = [t for t in set(tickers) if t in _CACHE]
    with _bg_lock:
        busy = any(t in _bg_pending for t in held)
    oldest = min((_CACHE[t][2] for t in held), default=None)
    return {"as_of": round(oldest) if oldest else None, "refreshing": busy}


def fetch_prices(tickers: list[str]) -> dict[str, float]:
    """Back-compat helper: just the current prices."""
    return {t: q["price"] for t, q in fetch_quotes(tickers).items()}
