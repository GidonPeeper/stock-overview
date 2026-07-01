"""Sector lookup for holdings, for the allocation pie chart.

Resolves each market-data ticker to a GICS-style sector via yfinance, cached on
disk (sectors are static, so this is fetched once per ticker). ETFs/funds and
unknown tickers get sensible fallback buckets so every holding lands somewhere.
"""

from __future__ import annotations

import json
from pathlib import Path

CACHE_FILE = Path(__file__).resolve().parents[1] / "data" / "sectors.json"

# Tickers that are funds/ETFs (no single sector).
_FUNDS = {"ZPRR.DE", "ZPRRd", "QDV5.DE"}


def _load_cache() -> dict[str, str]:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except ValueError:
            pass
    return {}


def _save_cache(cache: dict[str, str]) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(cache, indent=0))
    except OSError:
        pass


def get_sectors(tickers: list[str]) -> dict[str, str]:
    cache = _load_cache()
    missing = [t for t in set(tickers) if t and t not in cache]

    if missing:
        try:
            import yfinance as yf

            for t in missing:
                if t in _FUNDS:
                    cache[t] = "ETF / Fund"
                    continue
                try:
                    info = yf.Ticker(t).get_info()
                    cache[t] = info.get("sector") or "Other"
                except Exception:
                    cache[t] = "Other"
        except Exception:
            for t in missing:
                cache.setdefault(t, "Other")
        _save_cache(cache)

    return {t: cache.get(t, "Other") for t in set(tickers) if t}
