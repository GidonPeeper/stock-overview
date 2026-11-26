"""Watchlist + global ticker search — follow stocks you don't (yet) own.

The watchlist is a small local JSON store (git-ignored; sample bundled).
Quotes ride the same cached price layer as holdings, so this adds no extra
API pressure. Search proxies Yahoo Finance's symbol search via yfinance:
`search` (shares and ETFs, for the watchlist) and `search_any` (every kind
the price lookup shows, see lookup.py).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .datafiles import DATA_DIR, read_json, resolve, write_atomic
from . import cache, market
from .prices import fetch_quotes

_FILE = "watchlist.json"

_search_cache: dict[str, tuple[list, float]] = {}


def _load_raw() -> tuple[list[dict], bool]:
    path, real = resolve(_FILE)
    real = real or not path.exists()     # "sample" = the bundled demo file only
    if not path.exists():
        return [], not real
    data = read_json(path, {}, lists=("tickers",))   # damaged: DataFileError, never empty
    return [t for t in data.get("tickers", []) if t.get("ticker")], not real


def _save(tickers: list[dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    write_atomic(DATA_DIR / _FILE, json.dumps(
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


# Yahoo's quoteType -> the kind the app shows. Futures and options are left
# out of searches: this is an app for people who buy shares and funds.
KINDS = {"EQUITY": "share", "ETF": "etf", "MUTUALFUND": "fund", "INDEX": "index",
         "CURRENCY": "currency", "CRYPTOCURRENCY": "crypto"}
_SEARCH_WAIT = 8   # seconds Yahoo's symbol search may take


class SearchUnavailable(RuntimeError):
    """Yahoo's symbol search did not answer."""


def _yahoo_search(query: str) -> list[dict]:
    """Yahoo's raw search hits for `query` (name, ticker or ISIN), cached for
    an hour. Raises SearchUnavailable when Yahoo does not answer."""
    key = query.strip().lower()
    hit = _search_cache.get(key)
    if hit and time.time() - hit[1] < 3600:
        return hit[0]
    try:
        import yfinance as yf

        quotes = [q for q in (yf.Search(query, max_results=16, news_count=0, timeout=_SEARCH_WAIT).quotes or [])
                  if isinstance(q, dict) and q.get("symbol")]
    except Exception as exc:
        raise SearchUnavailable(str(exc)) from None
    if quotes:
        _search_cache[key] = (quotes, time.time())
        cache.trim(_search_cache, 200)   # any text can be searched: keep memory bounded
    return quotes


def _name(q: dict) -> str:
    return q.get("longname") or q.get("shortname") or q.get("symbol")


def search(query: str, limit: int = 8) -> list[dict]:
    """Shares and ETFs for the watchlist's search box. Empty when Yahoo does
    not answer."""
    try:
        quotes = _yahoo_search(query)
    except SearchUnavailable:
        return []
    out: list[dict] = []
    for q in quotes:
        if q.get("quoteType") not in ("EQUITY", "ETF"):
            continue
        out.append({"ticker": q.get("symbol"),
                    "name": q.get("shortname") or q.get("longname") or q.get("symbol"),
                    "exchange": q.get("exchange", ""), "kind": KINDS[q["quoteType"]]})
        if len(out) >= limit:
            break
    return out


def search_any(query: str, limit: int = 8) -> list[dict]:
    """Every kind the price lookup shows (shares, ETFs, funds, indices,
    currencies, crypto), by name, ticker or ISIN: [{ticker, name, exchange,
    kind}]. Raises SearchUnavailable when Yahoo does not answer, so the
    caller can tell "nothing found" from "try again"."""
    out: list[dict] = []
    seen: set[str] = set()
    for q in _yahoo_search(query):
        kind, sym = KINDS.get(q.get("quoteType")), str(q["symbol"]).upper()
        if kind is None or sym in seen:
            continue
        seen.add(sym)
        out.append({"ticker": sym, "name": _name(q),
                    "exchange": q.get("exchDisp") or q.get("exchange") or "", "kind": kind})
        if len(out) >= limit:
            break
    return out
