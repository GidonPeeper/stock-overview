"""ISIN -> market-data ticker, shared by every connector and importer.

One table instead of three: the DeGiro connector (open positions), the trendline (trend.py)
(every instrument ever traded) and the Trade Republic importer all need the same
answer to "which yfinance symbol prices this ISIN?". "" means no usable quote
(delisted SPACs, penny stocks) — such positions are shown but not priced.

Convention: US listings price in USD; ".AS" (Euronext Amsterdam), ".DE" (Xetra),
".PA", ".MI" in EUR; ".L" in GBP (see `price_currency`).

An ISIN missing from the table is looked up once on Yahoo's search (the first
listing quoted in EUR, USD, GBP or CHF, preferring the currency you traded
in), so shares you hold that nobody added here still get a live price. Found
tickers are kept in data/tickers_found.json; a failed lookup is retried after
6 hours. TICKER_LOOKUP=0 turns lookups off (the tests do).
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TICKERS: dict[str, str] = {
    # --- equities ---
    "US0378331005": "AAPL",     "US5949181045": "MSFT",     "US02079K3059": "GOOGL",
    "US0231351067": "AMZN",     "US30303M1027": "META",     "US67066G1040": "NVDA",
    "US88160R1014": "TSLA",     "US64110L1061": "NFLX",     "US00724F1012": "ADBE",
    "US68389X1054": "ORCL",     "US79466L3024": "CRM",      "US6541061031": "NKE",
    "US86800U3023": "SMCI",     "US0079031078": "AMD",      "US4581401001": "INTC",
    "US69608A1088": "PLTR",     "US6311031081": "NDAQ",     "US5801351017": "MCD",
    "US91324P1021": "UNH",      "US70450Y1038": "PYPL",     "US60770K1079": "MRNA",
    "US09075V1026": "BNTX",     "US62914V1061": "NIO",      "US98422D1054": "XPEV",
    "US76954A1034": "RIVN",     "US83406F1021": "SOFI",     "US22041X1028": "CRSR",
    "US83304A1060": "SNAP",     "US35953D1046": "FUBO",     "US46222L1089": "IONQ",
    "US4884452065": "ZVRA",     "US21077C1071": "LOGC",     "US21077C3051": "LOGC",
    "US36315X1019": "LKFT",     # Lakefront Biotherapeutics (ex-Galapagos, renamed 2026)
    "US30231G1022": "XOM",      # Exxon Mobil Corp (old ISIN, until 2026-07)
    "US30233Q1085": "XOM",      # ExxonMobil Holdings Corp (new ISIN after 2026-07 reorganisation)
    "US6745991058": "OXY",
    "NL0010273215": "ASML.AS",  "USN070592100": "ASML",     "NL0011585146": "RACE",
    "NL0012969182": "ADYEN.AS", "NL0011872650": "BFIT.AS",  "NL0000888691": "AMG.AS",
    "NL0000334118": "ASM.AS",   "NL0012866412": "BESI.AS",  "NL0000009165": "HEIA.AS",
    "GB00B03MLX29": "SHELL.AS", "GB00BP6MXD84": "SHEL",     "FR001400J770": "AF.PA",
    # --- ETFs / ETCs (priced on the EUR listing Trade Republic / DeGiro trade) ---
    "IE00BZCQB185": "QDV5.DE",  # iShares MSCI India UCITS ETF USD (Acc)
    "IE00BJ38QD84": "ZPRR.DE",  # SPDR Russell 2000 US Small Cap UCITS ETF
    "IE00B5BMR087": "SXR8.DE",  # iShares Core S&P 500 UCITS ETF USD (Acc)
    "IE00B4ND3602": "PPFB.DE",  # iShares Physical Gold ETC
    "US5024413065": "LVMUY",    # LVMH ADR (5 ADRs = 1 share)
    # --- no usable market data (delisted / worthless) ---
    "US34619R1023": "", "US33813J1060": "", "US84677R1068": "", "US74374T1097": "",
    "US44951Y1029": "", "US44951Y2019": "", "US01671P1003": "",
}

# Human-readable names where the broker's own label is truncated or cryptic
# (Trading 212 statements only carry the ticker symbol).
NAMES: dict[str, str] = {
    "US0231351067": "Amazon.com Inc", "USN070592100": "ASML Holding (ADR)",
    "NL0010273215": "ASML Holding", "NL0012866412": "BE Semiconductor Industries",
    "US02079K3059": "Alphabet Inc Class A", "US30303M1027": "Meta Platforms Inc",
    "US6311031081": "Nasdaq Inc", "NL0000009165": "Heineken", "US60770K1079": "Moderna Inc",
    "US70450Y1038": "PayPal Holdings", "US0378331005": "Apple Inc", "US5949181045": "Microsoft Corp",
    "US68389X1054": "Oracle Corp", "US79466L3024": "Salesforce Inc", "US6541061031": "Nike Inc Class B",
    "US00724F1012": "Adobe Inc", "US86800U3023": "Super Micro Computer",
    "IE00B4ND3602": "iShares Physical Gold ETC", "US5024413065": "LVMH (ADR)",
    "IE00B5BMR087": "iShares Core S&P 500 UCITS ETF (Acc)",
    "IE00BZCQB185": "iShares MSCI India UCITS ETF (Acc)",
    "IE00BJ38QD84": "SPDR Russell 2000 US Small Cap UCITS ETF",
    "US30233Q1085": "ExxonMobil Holdings Corp",
}


_FOUND_FILE = Path(__file__).resolve().parents[1] / "data" / "tickers_found.json"
_CURRENCIES = {"EUR", "USD", "GBP", "CHF"}    # the ones fx.py converts
_RETRY_AFTER = 6 * 3600
_lock = threading.Lock()
_found: dict[str, dict] | None = None        # isin -> {"ticker", "currency"}
_failed: dict[str, float] = {}               # isin -> time of a lookup that found nothing


def _found_map() -> dict[str, dict]:
    global _found
    if _found is None:
        try:
            _found = json.loads(_FOUND_FILE.read_text())
        except (OSError, ValueError):
            _found = {}
    return _found


_ccy_cache: list = [None, None]   # [found map size, {ticker: currency}]


def _ccy_of() -> dict[str, str]:
    found = _found_map()
    if _ccy_cache[0] != len(found):
        _ccy_cache[1] = {h["ticker"]: h["currency"] for h in found.values() if h.get("ticker")}
        _ccy_cache[0] = len(found)
    return _ccy_cache[1]


def _lookups_on() -> bool:
    return os.getenv("TICKER_LOOKUP", "1") != "0"


def _quote_currency(yf, symbol: str) -> str | None:
    """The currency Yahoo quotes `symbol` in (GBp counts as GBP), or None
    when it has no price."""
    tk = yf.Ticker(symbol)
    tk.history(period="5d", interval="1d", auto_adjust=False)
    meta = dict(getattr(tk, "history_metadata", None) or {})
    if not meta.get("regularMarketPrice"):
        return None
    ccy = meta.get("currency") or ""
    return "GBP" if ccy in ("GBp", "GBX", "GBx") else ccy.upper()


def _pick(symbols: list[str], currency: str | None, quote_currency) -> dict | None:
    """The first of (up to 3) listings with a price in a currency fx.py
    converts, preferring `currency`."""
    first = None
    for sym in symbols[:3]:
        try:
            ccy = quote_currency(sym)
        except Exception:
            continue
        if ccy not in _CURRENCIES:
            continue
        hit = {"ticker": sym, "currency": ccy}
        if not currency or ccy == currency.upper():
            return hit
        first = first or hit
    return first


def _search(isin: str, currency: str | None) -> dict | None:
    import yfinance as yf

    quotes = yf.Search(isin, max_results=8, news_count=0, lists_count=0,
                       raise_errors=False).quotes or []
    symbols = [q.get("symbol") for q in quotes
               if q.get("symbol") and not q["symbol"].upper().startswith(isin)
               and q.get("quoteType") in ("EQUITY", "ETF", "MUTUALFUND")]
    return _pick(symbols, currency, lambda s: _quote_currency(yf, s))


def lookup(isin: str, currency: str | None = None) -> str:
    """Ticker for an ISIN missing from TICKERS ("" if none was found)."""
    with _lock:
        hit = _found_map().get(isin)
        if hit:
            return hit["ticker"]
        if not _lookups_on() or not is_valid_isin(isin) \
                or time.time() - _failed.get(isin, -1e9) < _RETRY_AFTER:
            return ""
    try:
        hit = _search(isin, currency)
    except Exception:
        hit = None
    with _lock:
        if not hit:
            _failed[isin] = time.time()
            return ""
        _found_map()[isin] = hit
        try:
            _FOUND_FILE.parent.mkdir(exist_ok=True)
            _FOUND_FILE.write_text(json.dumps(_found_map(), indent=1, sort_keys=True))
        except OSError:
            pass
    return hit["ticker"]


def ticker_for(isin: str, currency: str | None = None) -> str:
    """The Yahoo ticker that prices `isin`: the table above, else a lookup.
    `currency` (what you traded in) picks between listings."""
    if not isin:
        return ""
    if isin in TICKERS:
        return TICKERS[isin]
    return lookup(isin, currency)


def prefetch(isins) -> None:
    """Look up several unknown ISINs at once (a few at a time), so a first
    load does not wait for them one by one. `isins`: ISINs, or {ISIN: the
    currency you traded it in}."""
    hints = dict(isins) if isinstance(isins, dict) else dict.fromkeys(isins)
    with _lock:
        todo = sorted(i for i in hints if i and i not in TICKERS and i not in _found_map())
    if todo and _lookups_on():
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda i: lookup(i, hints[i]), todo))


def price_currency(ticker: str) -> str:
    """Currency a yfinance ticker is quoted in: what a lookup saw, else the
    exchange suffix."""
    if not ticker:
        return "EUR"
    found = _ccy_of().get(ticker)
    if found:
        return found
    if ticker.endswith((".AS", ".DE", ".F", ".PA", ".MI", ".BR")):
        return "EUR"
    if ticker.endswith(".L"):
        return "GBP"
    if ticker.endswith(".SW"):
        return "CHF"
    return "USD"


def is_valid_isin(isin: str) -> bool:
    """ISO 6166 check digit (Luhn over the digit expansion)."""
    if len(isin) != 12 or not isin[:2].isalpha() or not isin[-1].isdigit():
        return False
    digits = "".join(str(int(c, 36)) for c in isin[:-1])
    total, double = 0, True
    for ch in reversed(digits):
        d = int(ch) * (2 if double else 1)
        total += d // 10 + d % 10
        double = not double
    return (10 - total % 10) % 10 == int(isin[-1])
