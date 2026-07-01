"""Currency conversion to the base currency (EUR).

Uses live EUR/USD and EUR/GBP rates (yfinance), cached for an hour, with a
static fallback if the fetch fails. Used anywhere a non-EUR amount needs to be
expressed in EUR (holdings value, realized P/L).
"""

from __future__ import annotations

import time

BASE_CURRENCY = "EUR"

# Fallback rates (units of EUR per 1 unit of currency) if the live fetch fails.
_FALLBACK = {"EUR": 1.0, "USD": 0.88, "GBP": 1.16, "CHF": 1.05}

_cache: dict[str, float] = {}
_cache_at = 0.0
_TTL = 3600  # 1 hour


def _live_rates() -> dict[str, float]:
    """EUR-per-unit rates from yfinance, cached. Falls back to static on error."""
    global _cache, _cache_at
    if _cache and time.time() - _cache_at < _TTL:
        return _cache
    rates = dict(_FALLBACK)
    try:
        import yfinance as yf

        eurusd = float(yf.Ticker("EURUSD=X").fast_info["last_price"])  # USD per EUR
        eurgbp = float(yf.Ticker("EURGBP=X").fast_info["last_price"])  # GBP per EUR
        if eurusd:
            rates["USD"] = 1.0 / eurusd
        if eurgbp:
            rates["GBP"] = 1.0 / eurgbp
    except Exception:
        pass
    _cache, _cache_at = rates, time.time()
    return rates


# Kept for callers that want to read the table directly.
FX_TO_EUR = _FALLBACK


def to_eur(amount: float, currency: str) -> float:
    return amount * _live_rates().get(currency.upper(), 1.0)
