"""Extra market data for the pro dashboard: index snapshot, per-holding
sparklines, and news headlines. All fetched from yfinance and cached in memory
so they don't slow down the main dashboard (the frontend loads them lazily).
Index levels, sparklines and the benchmark are served stale-while-revalidate
(see backend/cache.py): once fetched, a request never waits on Yahoo again.
"""

from __future__ import annotations

import time

from . import cache

# Major indices shown in the CNBC-style ticker strip.
_INDEX_SYMBOLS = [
    ("S&P 500", "^GSPC"), ("Nasdaq", "^IXIC"), ("FTSE 100", "^FTSE"),
    ("FTSE 250", "^FTMC"), ("AEX", "^AEX"), ("DAX", "^GDAXI"),
    ("GBP/USD", "GBPUSD=X"), ("GBP/EUR", "GBPEUR=X"), ("EUR/USD", "EURUSD=X"),
]

_IDX_TTL = 300  # 5 min

_spark_cache: dict[str, tuple[list[float], float]] = {}
_SPARK_TTL = 1800  # 30 min

_news_cache: dict[str, tuple[list[dict], float]] = {}
_NEWS_TTL = 1800

_MAX_KEYS = 300  # per small cache below: any ticker can be asked for, memory stays bounded


def indices() -> list[dict]:
    try:
        return cache.get("market.indices", _indices, ttl=_IDX_TTL, stale=6 * 3600, data=False)
    except Exception:
        return []


def _indices() -> list[dict]:
    out: list[dict] = []
    try:
        import yfinance as yf

        syms = [s for _, s in _INDEX_SYMBOLS]
        data = yf.download(syms, period="5d", interval="1d",
                           progress=False, auto_adjust=True)["Close"]
        for name, sym in _INDEX_SYMBOLS:
            try:
                series = data[sym] if hasattr(data, "columns") else data
                vals = [float(v) for v in series.dropna().values]
                if len(vals) >= 2:
                    out.append({
                        "name": name, "price": round(vals[-1], 2),
                        "change_pct": round((vals[-1] / vals[-2] - 1) * 100, 2),
                    })
            except Exception:
                continue
    except Exception:
        pass
    if not out:
        raise ValueError("no index data")  # keep the last good snapshot
    return out


def sparklines(tickers: list[str]) -> dict[str, list[float]]:
    """~1 month of daily closes per ticker (for row sparklines), batched + cached."""
    tickers = sorted(set(t for t in tickers if t))
    if not tickers:
        return {}
    try:
        return cache.get("market.sparklines:" + ",".join(tickers), lambda: _sparklines(tickers),
                         ttl=_SPARK_TTL, stale=24 * 3600, data=False)
    except Exception:
        return {}


def _sparklines(tickers: list[str]) -> dict[str, list[float]]:
    now = time.time()
    out: dict[str, list[float]] = {}
    stale: list[str] = []
    for t in tickers:
        c = _spark_cache.get(t)
        if c and now - c[1] < _SPARK_TTL:
            out[t] = c[0]
        else:
            stale.append(t)

    if stale:
        try:
            import yfinance as yf

            data = yf.download(stale, period="1mo", interval="1d",
                               progress=False, auto_adjust=True)["Close"]
            for t in stale:
                try:
                    series = data[t] if hasattr(data, "columns") else data
                    vals = [round(float(v), 4) for v in series.dropna().values]
                    if vals:
                        out[t] = vals
                        _spark_cache[t] = (vals, now)
                        cache.trim(_spark_cache, _MAX_KEYS)
                except Exception:
                    continue
        except Exception:
            pass
    if not out:
        raise ValueError("no sparkline data")
    return out


_hist_cache: dict[str, tuple[list[float], float]] = {}
# range -> (yfinance period, interval, cache TTL seconds)
_RANGES = {
    "1d": ("1d", "5m", 300), "5d": ("5d", "30m", 900),
    "1mo": ("1mo", "1d", 1800), "1y": ("1y", "1d", 3600),
}


def ticker_history(ticker: str, rng: str) -> list[float]:
    """Closing prices for one ticker over a range, for the detail-sheet chart."""
    period, interval, ttl = _RANGES.get(rng, _RANGES["1mo"])
    key = f"{ticker}:{rng}"
    now = time.time()
    c = _hist_cache.get(key)
    if c and now - c[1] < ttl:
        return c[0]
    vals: list[float] = []
    try:
        import yfinance as yf

        series = yf.download(ticker, period=period, interval=interval,
                             progress=False, auto_adjust=True)["Close"]
        vals = [round(float(v), 4) for v in series.dropna().values.flatten()]
    except Exception:
        pass
    if vals:
        _hist_cache[key] = (vals, now)
        cache.trim(_hist_cache, _MAX_KEYS)   # any ticker can be asked for: stay bounded
    return vals


def benchmark(start_iso: str, symbol: str = "^GSPC") -> dict[str, float]:
    """Monthly closes for a benchmark index since `start_iso`, keyed YYYY-MM —
    used to overlay 'what if this had tracked the S&P 500' on the value chart."""
    try:
        return cache.get(f"market.benchmark:{symbol}:{start_iso}",
                         lambda: _benchmark(start_iso, symbol), ttl=3600, stale=24 * 3600, data=False)
    except Exception:
        return {}


def _benchmark(start_iso: str, symbol: str) -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        import yfinance as yf

        close = yf.download(symbol, start=start_iso, interval="1mo",
                            progress=False, auto_adjust=True)["Close"]
        series = (close.iloc[:, 0] if hasattr(close, "columns") else close).dropna()
        for ts, val in series.items():
            out[ts.strftime("%Y-%m")] = round(float(val), 2)
    except Exception:
        pass
    if not out:
        raise ValueError("no benchmark data")
    return out


def news(ticker: str, limit: int = 6) -> list[dict]:
    now = time.time()
    c = _news_cache.get(ticker)
    if c and now - c[1] < _NEWS_TTL:
        return c[0]
    out: list[dict] = []
    try:
        import yfinance as yf

        items = list(yf.Ticker(ticker).news or [])[:limit]
    except Exception:
        return c[0] if c else []   # Yahoo did not answer: try again next time, keep the old ones
    for item in items:
        try:
            # yfinance news schema has shifted; support both shapes.
            content = item.get("content", item)
            title = content.get("title")
            if not title:
                continue
            provider = (content.get("provider") or {})
            publisher = provider.get("displayName") if isinstance(provider, dict) else item.get("publisher")
            url = (content.get("canonicalUrl") or {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else item.get("link")
            out.append({"title": title, "publisher": publisher or "", "url": url or ""})
        except (AttributeError, TypeError):   # an item of an unexpected shape
            continue
    _news_cache[ticker] = (out, now)
    cache.trim(_news_cache, _MAX_KEYS)
    return out
