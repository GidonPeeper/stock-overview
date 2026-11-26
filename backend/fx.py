"""Currency conversion.

All amounts are computed internally in EUR (the base currency) and converted to
the display currency (EUR or GBP) in the browser, at today's rate. Live rates
come from yfinance, cached for an hour, with a static fallback if the fetch
fails.

London prices are often quoted in pence ("GBX" at Trading 212, "GBp" at Yahoo).
`unit()` turns those into pounds, so a 600p share is never read as €600.

A currency outside USD/GBP/CHF (SEK, JPY, CAD, ...: an IBKR or Trading 212
holding, a Wise balance) gets its own rate from Yahoo on first use, so it is
never silently taken as one euro. Only when no rate can be found at all is
1.0 used, and that is logged.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import cache

BASE_CURRENCY = "EUR"
DISPLAY_CURRENCIES = ("EUR", "GBP", "USD")

# Fallback rates (units of EUR per 1 unit of currency) if the live fetch fails.
_FALLBACK = {"EUR": 1.0, "USD": 0.88, "GBP": 1.16, "CHF": 1.05}

# Case matters: "GBp" is pence but "GBP" is pounds.
_PENCE = {"GBX", "GBp", "GBx"}

# Pegged to the euro (ERM II central rate), used only if Yahoo has no rate.
_PEGGED = {"DKK": 1 / 7.46038}

_cache: dict[str, float] = {}  # last good live rate per currency
_TTL = 3600         # 1 hour fresh
_STALE = 6 * 3600   # then served while a background refresh runs
_PAIRS = (("USD", "EURUSD=X"), ("GBP", "EURGBP=X"), ("CHF", "EURCHF=X"))


def _fetch_rates() -> dict[str, float]:
    """Live rates, fetched in parallel. A pair that fails keeps its last live
    rate, or the static fallback if it never had one."""
    rates = {**_FALLBACK, **_cache}
    try:
        import yfinance as yf

        def one(pair: str) -> float | None:
            try:
                return float(yf.Ticker(pair).fast_info["last_price"])
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=len(_PAIRS)) as pool:
            per_eur = list(pool.map(one, [pair for _, pair in _PAIRS]))
        for (ccy, _), v in zip(_PAIRS, per_eur):
            if _good(v):  # NaN or 0 from Yahoo keeps the last good rate
                rates[ccy] = _cache[ccy] = 1.0 / v
    except Exception:
        pass
    return rates


def _live_rates() -> dict[str, float]:
    """EUR-per-unit rates from yfinance, cached (see backend/cache.py)."""
    return cache.get("fx.rates", _fetch_rates, ttl=_TTL, stale=_STALE, data=False)


def _good(v) -> bool:
    try:
        return math.isfinite(v) and v > 0
    except TypeError:
        return False


_ISO = re.compile(r"^[A-Z]{3}$")
_other: dict[str, tuple[float | None, float]] = {}  # currency -> (EUR per unit, tried at)
_other_lock = threading.Lock()
log = logging.getLogger(__name__)


def _fetch_pair(ccy: str) -> float | None:
    try:
        import yfinance as yf

        v = float(yf.Ticker(f"EUR{ccy}=X").fast_info["last_price"])
    except Exception:
        return None
    return 1.0 / v if _good(v) else None


def _other_rate(ccy: str) -> float | None:
    """EUR per unit of a currency outside _PAIRS, fetched on first use and
    kept for an hour; a failed fetch keeps the last live rate."""
    with _other_lock:
        hit = _other.get(ccy)
        if hit and time.time() - hit[1] < _TTL:
            return hit[0]
        rate = _fetch_pair(ccy)
        if rate is None and hit:
            rate = hit[0]
        _other[ccy] = (rate, time.time())
        return rate


def _rate(ccy: str) -> float:
    """EUR per unit of `ccy` (an ISO code, pounds not pence)."""
    r = _live_rates().get(ccy)
    if r is None and ccy != BASE_CURRENCY and _ISO.match(ccy):
        r = _other_rate(ccy) or _PEGGED.get(ccy)
        if r is None:
            log.warning("no exchange rate for %s; counted as 1 EUR", ccy)
    return r if r is not None else 1.0


# Kept for callers that want to read the table directly.
FX_TO_EUR = _FALLBACK


def unit(currency: str | None) -> tuple[float, str]:
    """(scale, ISO currency). Pence quotes become pounds: ("GBX") -> (0.01, "GBP")."""
    c = (currency or BASE_CURRENCY).strip()
    if c in _PENCE or c.upper() == "GBX":
        return 0.01, "GBP"
    return 1.0, c.upper()


def to_eur(amount: float, currency: str | None) -> float:
    scale, ccy = unit(currency)
    return amount * scale * _rate(ccy)


def from_eur(amount_eur: float, currency: str) -> float:
    """EUR amount expressed in `currency` (at today's rate)."""
    scale, ccy = unit(currency)
    return amount_eur / (_rate(ccy) * scale)


def display_currency() -> str:
    """Default display currency for the UI: the one chosen in Settings, else
    DISPLAY_CURRENCY, else EUR (prefs.py)."""
    from . import prefs

    c = prefs.value("currency")
    return c if c in DISPLAY_CURRENCIES else BASE_CURRENCY


def display_rates() -> dict:
    """Units of each display currency per 1 EUR, for the browser's formatter."""
    rates = _live_rates()
    return {
        "base": BASE_CURRENCY,
        "default": display_currency(),
        "per_eur": {c: round(1.0 / rates.get(c, 1.0), 6) for c in DISPLAY_CURRENCIES},
    }
