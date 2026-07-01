"""Period profit views: today, this week, month, year.

These measure how the market value of your *current* holdings has moved over a
window — computed from historical close prices (yfinance). This is distinct from
all-time unrealized P/L (value vs cost) and from realized P/L (closed trades).

Caveat surfaced to the caller: it values *today's* quantities at past prices, so
it reflects market movement of what you hold now, not your actual past balance.
Holdings without a resolvable market ticker are excluded and reported as
coverage gaps, so nothing is silently fabricated.
"""

from __future__ import annotations

from .fx import to_eur

# Trading-day offsets from the latest close.
_WINDOWS = {"today": 1, "week": 5, "month": 21, "year": 251}

# Symbols whose market-data ticker differs from the broker's symbol.
# (Trading 212 uses its own symbology, e.g. FB for Meta, ZPRRd for the Xetra
# SPDR Russell 2000 ETF.)
_ALIAS = {"FB": "META", "ZPRRd": "ZPRR.DE"}


def compute(holdings: list[dict]) -> dict:
    """holdings: dicts with ticker, quantity, currency, value_eur, name."""
    priceable = [h for h in holdings if h.get("ticker")]
    tickers = sorted({_ALIAS.get(h["ticker"], h["ticker"]) for h in priceable})
    if not tickers:
        return _empty(holdings)

    try:
        import yfinance as yf

        hist = yf.download(
            tickers, period="1y", interval="1d",
            progress=False, auto_adjust=True,
        )["Close"]
    except Exception:
        return _empty(holdings)

    # Normalize to a {ticker: [closes...]} structure, newest last.
    series: dict[str, list[float]] = {}
    if hasattr(hist, "columns"):
        for t in tickers:
            if t in hist.columns:
                col = hist[t].dropna()
                if len(col):
                    series[t] = list(col.values)
    else:  # single ticker -> Series
        col = hist.dropna()
        if len(col):
            series[tickers[0]] = list(col.values)

    def value_at(offset: int) -> tuple[float, float]:
        """Total EUR value `offset` trading days back, and the EUR value covered
        today for the same set of tickers (so deltas compare like with like)."""
        then = now = 0.0
        for h in priceable:
            t = _ALIAS.get(h["ticker"], h["ticker"])
            closes = series.get(t)
            if not closes or len(closes) <= offset:
                continue
            qty = h["quantity"]
            then += to_eur(closes[-1 - offset] * qty, h["currency"])
            now += to_eur(closes[-1] * qty, h["currency"])
        return then, now

    periods = {}
    for name, offset in _WINDOWS.items():
        then, now = value_at(offset)
        periods[name] = round(now - then, 2)

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
        "coverage_pct": round(100 * covered_value / total_value, 1) if total_value else 0,
        "excluded": missing,
    }


def _empty(holdings: list[dict]) -> dict:
    return {
        "periods": {k: None for k in _WINDOWS},
        "coverage_pct": 0,
        "excluded": sorted({h.get("name", "") for h in holdings}),
    }
