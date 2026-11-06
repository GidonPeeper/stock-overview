"""Live price layer.

Given market-data tickers, return the current price and the previous close for
each (the previous close powers the per-holding "today" change). This is the
source of "what is it worth right now" for holdings that didn't arrive with a
price (i.e. everything except Trading 212), and of the prior close for all.

Provider is yfinance (free, no key, broad European coverage). Results are cached
briefly so repeated dashboard loads don't re-hit yfinance for every ticker. A
small sample map keeps the demo rendering when offline / rate-limited.
"""

from __future__ import annotations

import time

# Fallback (price, prev_close) for the sample tickers, so the demo shows
# realistic values and a small daily move when offline. Ignored once real
# data flows.
SAMPLE_PRICES = {
    "ASML.AS": (685.0, 681.0), "INGA.AS": (15.8, 15.7),
    "SAP.DE": (188.0, 189.2), "AIR.PA": (158.0, 156.9),
    "AAPL": (212.4, 211.0), "MSFT": (441.8, 439.0),
    "VWRL.L": (118.2, 117.6),
}

# ticker -> (price, prev_close, fetched_at)
_CACHE: dict[str, tuple[float, float | None, float]] = {}
_TTL = 60  # seconds

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


def fetch_quotes(tickers: list[str]) -> dict[str, dict]:
    """Return {ticker: {"price": float, "prev_close": float|None}}."""
    tickers = sorted(set(t for t in tickers if t))
    if not tickers:
        return {}

    now = time.time()
    out: dict[str, dict] = {}
    stale: list[str] = []
    for t in tickers:
        c = _CACHE.get(t)
        if c and now - c[2] < _TTL:
            out[t] = {"price": c[0], "prev_close": c[1]}
        else:
            stale.append(t)

    if stale:
        try:
            import yfinance as yf

            for t in stale:
                try:
                    info = yf.Ticker(t).fast_info
                    last = info.get("last_price") or info.get("lastPrice")
                    prev = info.get("previous_close") or info.get("previousClose")
                    if last:
                        if t.endswith(".L"):
                            _SCALE[t] = _scale_from(info)
                        scale = _SCALE.get(t, 1.0)
                        price = float(last) * scale
                        pc = float(prev) * scale if prev else None
                        _CACHE[t] = (price, pc, now)
                        out[t] = {"price": price, "prev_close": pc}
                except Exception:
                    continue
        except Exception:
            pass  # yfinance unavailable -> fall back below

    for t in tickers:
        if t not in out and t in SAMPLE_PRICES:
            p, pc = SAMPLE_PRICES[t]
            out[t] = {"price": p, "prev_close": pc}

    return out


def fetch_prices(tickers: list[str]) -> dict[str, float]:
    """Back-compat helper: just the current prices."""
    return {t: q["price"] for t, q in fetch_quotes(tickers).items()}
