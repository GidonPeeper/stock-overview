"""Sector lookup for holdings, for the allocation pie chart.

Resolves each market-data ticker to a GICS-style sector via yfinance, cached on
disk and in memory (sectors are static, so this is fetched once per ticker;
new tickers are looked up in parallel). ETFs/funds and unknown tickers get
sensible fallback buckets so every holding lands somewhere.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CACHE_FILE = Path(__file__).resolve().parents[1] / "data" / "sectors.json"

# Tickers that are funds/ETFs (no single sector).
_FUNDS = {"ZPRR.DE", "ZPRRd", "QDV5.DE"}

_lock = threading.Lock()          # one lookup at a time; waiters reuse its result
_memory: dict[str, str] | None = None
_failed: dict[str, float] = {}    # ticker -> when a lookup last failed (not saved)
_RETRY = 600                      # seconds before a failed lookup is tried again
_WORKERS = 4                      # Yahoo's info endpoint is the touchiest about bursts


def _load_cache() -> dict[str, str]:
    global _memory
    if _memory is None:
        _memory = {}
        if CACHE_FILE.exists():
            try:
                _memory = json.loads(CACHE_FILE.read_text())
            except ValueError:
                pass
    return _memory


def _save_cache(cache: dict[str, str]) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(cache, indent=0))
    except OSError:
        pass


def _from_search(yf, t: str) -> str | None:
    """Yahoo's symbol search also names the sector, and answers from hosts
    where the company-info request is refused (seen on Render)."""
    try:
        quotes = yf.Search(t, max_results=5, news_count=0).quotes
    except Exception:
        return None
    hit = next((q for q in quotes if (q.get("symbol") or "").upper() == t.upper()), None)
    if hit is None:
        return None
    if hit.get("quoteType") in ("ETF", "MUTUALFUND"):
        return "ETF / Fund"
    return hit.get("sector") or hit.get("sectorDisp") or None


def _lookup(yf, t: str) -> str | None:
    """The sector, or None when Yahoo did not answer (try again later)."""
    if t in _FUNDS:
        return "ETF / Fund"
    try:
        info = yf.Ticker(t).get_info() or {}
    except Exception:
        info = {}
    if info.get("quoteType") in ("ETF", "MUTUALFUND"):
        return "ETF / Fund"
    if info.get("sector"):
        return info["sector"]
    found = _from_search(yf, t)
    if found:
        return found
    # the company info answered but names no sector (an index, a currency, a SPAC)
    return "Other" if info.get("quoteType") else None


def get_sectors(tickers: list[str]) -> dict[str, str]:
    wanted = {t for t in tickers if t}
    cache = _load_cache()

    def todo() -> list[str]:
        now = time.time()
        return sorted(t for t in wanted - cache.keys() if now - _failed.get(t, -1e18) >= _RETRY)

    if todo():
        with _lock:
            cache = _load_cache()
            missing = todo()
            if missing:
                try:
                    import yfinance as yf

                    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(missing))) as pool:
                        found = dict(zip(missing, pool.map(lambda t: _lookup(yf, t), missing)))
                except Exception:
                    found = {t: None for t in missing}
                now = time.time()
                for t, sector in found.items():
                    if sector is None:
                        _failed[t] = now  # shown as "Other" for now, not saved
                    else:
                        cache[t] = sector
                        _failed.pop(t, None)
                if any(s is not None for s in found.values()):
                    _save_cache(cache)

    return {t: cache.get(t, "Other") for t in wanted}
