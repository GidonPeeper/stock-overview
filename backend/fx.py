"""Currency conversion.

All amounts are computed internally in EUR (the base currency) and converted to
the display currency (EUR or GBP) in the browser, at today's rate. Live rates
come from yfinance, cached for an hour, with a static fallback if the fetch
fails.

London prices are often quoted in pence ("GBX" at Trading 212, "GBp" at Yahoo).
`unit()` turns those into pounds, so a 600p share is never read as €600.
"""

from __future__ import annotations

import os
import time

BASE_CURRENCY = "EUR"
DISPLAY_CURRENCIES = ("EUR", "GBP", "USD")

# Fallback rates (units of EUR per 1 unit of currency) if the live fetch fails.
_FALLBACK = {"EUR": 1.0, "USD": 0.88, "GBP": 1.16, "CHF": 1.05}

# Case matters: "GBp" is pence but "GBP" is pounds.
_PENCE = {"GBX", "GBp", "GBx"}

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

        for ccy, pair in (("USD", "EURUSD=X"), ("GBP", "EURGBP=X"), ("CHF", "EURCHF=X")):
            try:
                per_eur = float(yf.Ticker(pair).fast_info["last_price"])
                if per_eur:
                    rates[ccy] = 1.0 / per_eur
            except Exception:
                continue
    except Exception:
        pass
    _cache, _cache_at = rates, time.time()
    return rates


# Kept for callers that want to read the table directly.
FX_TO_EUR = _FALLBACK


def unit(currency: str | None) -> tuple[float, str]:
    """(scale, ISO currency). Pence quotes become pounds: ("GBX") -> (0.01, "GBP")."""
    c = (currency or BASE_CURRENCY).strip()
    if c in _PENCE:
        return 0.01, "GBP"
    return 1.0, c.upper()


def to_eur(amount: float, currency: str | None) -> float:
    scale, ccy = unit(currency)
    return amount * scale * _live_rates().get(ccy, 1.0)


def from_eur(amount_eur: float, currency: str) -> float:
    """EUR amount expressed in `currency` (at today's rate)."""
    scale, ccy = unit(currency)
    return amount_eur / (_live_rates().get(ccy, 1.0) * scale)


def display_currency() -> str:
    """Default display currency for the UI (DISPLAY_CURRENCY env, EUR if unset).
    The viewer can switch it in Settings; this is only the starting point."""
    c = (os.getenv("DISPLAY_CURRENCY") or BASE_CURRENCY).upper()
    return c if c in DISPLAY_CURRENCIES else BASE_CURRENCY


def display_rates() -> dict:
    """Units of each display currency per 1 EUR, for the browser's formatter."""
    rates = _live_rates()
    return {
        "base": BASE_CURRENCY,
        "default": display_currency(),
        "per_eur": {c: round(1.0 / rates.get(c, 1.0), 6) for c in DISPLAY_CURRENCIES},
    }
