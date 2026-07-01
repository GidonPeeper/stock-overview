"""Trading 212 connector -- live, read-only.

Trading 212 authenticates with a **key + secret pair** (HTTP Basic auth): set
both T212_API_KEY and T212_API_SECRET in your environment (or .env). The pair
should be generated in the Trading 212 app with ONLY read scopes enabled --
never the ordering scope. The secret is shown only once at generation time.
Without both, this returns sample holdings so the dashboard runs out of the box.

NB: the API is in beta -- verify the exact endpoint/fields against the current
docs at https://docs.trading212.com/api before relying on the live path.
"""

from __future__ import annotations

import functools
import json
import os
import threading
import time
from pathlib import Path

import requests

from .base import Holding
from ..positions import Trade

BASE_URL = "https://live.trading212.com/api/v0"

# Trading 212 symbols whose market-data ticker differs from the yfinance one
# (legacy FB for Meta; the Xetra SPDR Russell 2000 ETF).
_ALIAS = {"FB": "META", "ZPRRd": "ZPRR.DE"}


def _ttl_cache(seconds: int):
    """Thread-safe time-based memoization for zero-arg fetchers. Trading 212 is
    heavily rate-limited (429s), and a single dashboard load touches each
    endpoint more than once, so we serialize and reuse recent results. On error,
    a stale cached value is returned if we have one."""
    def decorator(fn):
        lock = threading.Lock()
        cache: dict[str, object] = {}

        @functools.wraps(fn)
        def wrapper():
            with lock:
                now = time.time()
                if "value" in cache and now - cache["at"] < seconds:
                    return cache["value"]
                try:
                    value = fn()
                except Exception:
                    if "value" in cache:
                        return cache["value"]
                    raise
                cache["value"], cache["at"] = value, now
                return value

        return wrapper

    return decorator

# The instrument list is large and static, so cache it on disk and only
# re-download occasionally. This keeps cold starts fast.
_META_CACHE = Path(__file__).resolve().parents[2] / "data" / "t212_instruments.json"
_META_TTL = 7 * 24 * 3600  # 1 week


def _auth() -> tuple[str, str] | None:
    key = os.getenv("T212_API_KEY")
    secret = os.getenv("T212_API_SECRET")
    return (key, secret) if (key and secret) else None


def _get(path: str, auth: tuple[str, str]):
    resp = requests.get(f"{BASE_URL}{path}", auth=auth, timeout=30)
    resp.raise_for_status()
    return resp.json()


@functools.lru_cache(maxsize=1)
def _instrument_meta() -> dict[str, dict]:
    """ticker -> {name, currencyCode, isin}. Cached on disk (the list is large
    but static), so only the first run of the week pays the download cost."""
    if _META_CACHE.exists() and (time.time() - _META_CACHE.stat().st_mtime) < _META_TTL:
        try:
            data = json.loads(_META_CACHE.read_text())
            return {row["ticker"]: row for row in data}
        except (ValueError, KeyError):
            pass  # corrupt cache -> re-fetch

    auth = _auth()
    if not auth:
        return {}
    try:
        data = _get("/equity/metadata/instruments", auth)
    except requests.RequestException:
        return {}
    try:
        _META_CACHE.write_text(json.dumps(data))
    except OSError:
        pass
    return {row["ticker"]: row for row in data}


@_ttl_cache(seconds=30)
def get_holdings() -> list[Holding]:
    auth = _auth()
    if not auth:
        return _sample_holdings()

    meta = _instrument_meta()
    holdings: list[Holding] = []
    for pos in _get("/equity/portfolio", auth):
        raw = pos["ticker"]
        info = meta.get(raw, {})
        symbol, fallback_ccy = _parse_ticker(raw)
        symbol = _ALIAS.get(symbol, symbol)  # FB -> META, so it merges & prices
        holdings.append(
            Holding(
                broker="Trading212",
                ticker=symbol,
                name=info.get("name", symbol),
                isin=info.get("isin", ""),
                quantity=float(pos["quantity"]),
                buy_price=float(pos["averagePrice"]),
                currency=info.get("currencyCode", fallback_ccy),
                # T212 already knows the live price -- no need for yfinance here.
                # averagePrice/currentPrice are in the instrument's own currency.
                price=float(pos["currentPrice"]),
                # T212's own EUR P/L: price P/L + the FX-since-purchase component.
                broker_profit_eur=float(pos["ppl"]) + float(pos.get("fxPpl") or 0),
            )
        )
    return holdings


@_ttl_cache(seconds=120)
def get_trades() -> list[Trade]:
    """Full filled-order history -> trades, with exact EUR cash impact per fill.
    Used to compute realized P/L and closed positions."""
    auth = _auth()
    if not auth:
        return []

    trades: list[Trade] = []
    path = "/equity/history/orders?limit=50"
    while path:
        page = _get(path, auth)
        for item in page.get("items", []):
            order = item.get("order") or {}
            fill = item.get("fill")
            if order.get("status") != "FILLED" or not fill:
                continue
            inst = order.get("instrument", {})
            wallet = fill.get("walletImpact") or {}
            net = wallet.get("netValue") or order.get("filledValue") or order.get("value")
            if net is None:
                continue
            # Fold fees (e.g. currency-conversion fee) into the cash amount:
            # a buy costs more, a sell nets less. taxes[].quantity is negative.
            fee = sum(float(t.get("quantity", 0)) for t in (wallet.get("taxes") or []))
            amount = float(net) - fee if order["side"] == "BUY" else float(net) + fee
            trades.append(
                Trade(
                    when=order["createdAt"],
                    key=inst.get("isin", order["ticker"]),
                    name=inst.get("name", order["ticker"]),
                    side=order["side"],          # BUY / SELL
                    quantity=abs(float(fill["quantity"])),
                    amount=round(amount, 2),     # exact EUR incl. fees
                    currency="EUR",
                )
            )
        path = page.get("nextPagePath")
    return trades


@_ttl_cache(seconds=300)
def get_dividends() -> list[dict]:
    """Dividend payments (EUR amounts) from the history API."""
    auth = _auth()
    if not auth:
        return []
    try:
        return _get("/history/dividends", auth).get("items", [])
    except requests.RequestException:
        return []


def _parse_ticker(raw: str) -> tuple[str, str]:
    """T212 tickers look like 'GOOGL_US_EQ' / 'ZPRRd_EQ'. Return (symbol,
    currency) -- the currency is a fallback when metadata is unavailable."""
    parts = raw.split("_")
    symbol = parts[0]
    if "US" in parts:
        currency = "USD"
    elif "GB" in parts or "L" in parts:
        currency = "GBP"
    else:
        currency = "EUR"  # Xetra / Euronext listings
    return symbol, currency


def _sample_holdings() -> list[Holding]:
    return [
        Holding("Trading212", "AAPL", "Apple Inc.", 12, 165.0, "USD", price=212.4),
        Holding("Trading212", "MSFT", "Microsoft Corp.", 5, 330.0, "USD", price=441.8),
        Holding("Trading212", "VWRL.L", "Vanguard FTSE All-World", 8, 98.5, "GBP", price=118.2),
    ]
