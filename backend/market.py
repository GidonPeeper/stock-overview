"""Extra market data for the pro dashboard: index snapshot, per-holding
sparklines, and news headlines. All fetched from yfinance and cached in memory
so they don't slow down the main dashboard (the frontend loads them lazily).
"""

from __future__ import annotations

import time

# Major indices shown in the CNBC-style ticker strip.
_INDEX_SYMBOLS = [
    ("S&P 500", "^GSPC"), ("Nasdaq", "^IXIC"), ("Dow", "^DJI"),
    ("AEX", "^AEX"), ("DAX", "^GDAXI"), ("EUR/USD", "EURUSD=X"),
]

_idx_cache: tuple[list[dict], float] | None = None
_IDX_TTL = 300  # 5 min

_spark_cache: dict[str, tuple[list[float], float]] = {}
_SPARK_TTL = 1800  # 30 min

_news_cache: dict[str, tuple[list[dict], float]] = {}
_NEWS_TTL = 1800


def indices() -> list[dict]:
    global _idx_cache
    now = time.time()
    if _idx_cache and now - _idx_cache[1] < _IDX_TTL:
        return _idx_cache[0]
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
    if out:
        _idx_cache = (out, now)
    return out


def sparklines(tickers: list[str]) -> dict[str, list[float]]:
    """~1 month of daily closes per ticker (for row sparklines), batched + cached."""
    tickers = sorted(set(t for t in tickers if t))
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
                except Exception:
                    continue
        except Exception:
            pass
    return out


def news(ticker: str, limit: int = 6) -> list[dict]:
    now = time.time()
    c = _news_cache.get(ticker)
    if c and now - c[1] < _NEWS_TTL:
        return c[0]
    out: list[dict] = []
    try:
        import yfinance as yf

        for item in (yf.Ticker(ticker).news or [])[:limit]:
            # yfinance news schema has shifted; support both shapes.
            content = item.get("content", item)
            title = content.get("title")
            if not title:
                continue
            provider = (content.get("provider") or {})
            publisher = provider.get("displayName") if isinstance(provider, dict) else item.get("publisher")
            url = (content.get("canonicalUrl") or {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else item.get("link")
            out.append({"title": title, "publisher": publisher or "", "url": url or ""})
    except Exception:
        pass
    _news_cache[ticker] = (out, now)
    return out
