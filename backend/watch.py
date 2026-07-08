"""Watchlist + global ticker search — follow stocks you don't (yet) own.

The watchlist is a small local JSON store (git-ignored; sample bundled).
Quotes ride the same cached price layer as holdings, so this adds no extra
API pressure. Search proxies Yahoo Finance's symbol search via yfinance.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .datafiles import DATA_DIR, resolve
from . import market
from .prices import fetch_quotes

_FILE = "watchlist.json"

_search_cache: dict[str, tuple[list, float]] = {}


def _load_raw() -> tuple[list[dict], bool]:
    path, real = resolve(_FILE)
    if not path.exists():
        return [], not real
    try:
        return json.loads(path.read_text()).get("tickers", []), not real
    except ValueError:
        return [], not real


def _save(tickers: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / _FILE).write_text(json.dumps(
        {"tickers": tickers, "updated": time.strftime("%Y-%m-%d %H:%M")}, indent=2))


def load() -> dict:
    tickers, sample = _load_raw()
    quotes = fetch_quotes([t["ticker"] for t in tickers])
    sparks = market.sparklines([t["ticker"] for t in tickers])
    out = []
    for t in tickers:
        q = quotes.get(t["ticker"], {})
        price, prev = q.get("price"), q.get("prev_close")
        out.append({
            "ticker": t["ticker"], "name": t.get("name", t["ticker"]),
            "price": round(price, 2) if price else None,
            "daily_pct": round((price / prev - 1) * 100, 2) if price and prev else None,
            "spark": sparks.get(t["ticker"], []),
        })
    return {"tickers": out, "sample": sample}


def add(ticker: str, name: str) -> dict:
    tickers, sample = _load_raw()
    if sample:
        tickers = []  # first real add starts clean of demo entries
    if not any(t["ticker"] == ticker for t in tickers):
        tickers.append({"ticker": ticker, "name": name})
    _save(tickers)
    return load()


def remove(ticker: str) -> dict:
    tickers, sample = _load_raw()
    tickers = [] if sample else [t for t in tickers if t["ticker"] != ticker]
    _save(tickers)
    return load()


def search(query: str, limit: int = 8) -> list[dict]:
    key = query.strip().lower()
    hit = _search_cache.get(key)
    if hit and time.time() - hit[1] < 3600:
        return hit[0]
    out: list[dict] = []
    try:
        import yfinance as yf

        for q in yf.Search(query, max_results=limit * 2).quotes:
            if q.get("quoteType") not in ("EQUITY", "ETF"):
                continue
            out.append({"ticker": q.get("symbol"),
                        "name": q.get("shortname") or q.get("longname") or q.get("symbol"),
                        "exchange": q.get("exchange", "")})
            if len(out) >= limit:
                break
    except Exception:
        pass
    if out:
        _search_cache[key] = (out, time.time())
    return out
