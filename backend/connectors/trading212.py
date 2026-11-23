"""Trading 212 connector -- live, read-only, one or more accounts.

Trading 212 gives every account (Invest, Stocks ISA) its own API key + secret,
sent as HTTP Basic auth. Generate each pair in the app with ONLY read scopes --
never the ordering scope. The secret is shown once, at generation time.

Configure one or more accounts in the environment (or .env):

    T212_API_KEY / T212_API_SECRET                  -> shown as "Trading212"
    T212_<NAME>_API_KEY / T212_<NAME>_API_SECRET    -> shown as "T212 <Name>"

e.g. T212_ISA_API_KEY + T212_INVEST_API_KEY give "T212 ISA" and "T212 Invest".
Any account whose name contains "ISA" is treated as tax-free (UK ISA).
Optional T212_<NAME>_NAME overrides the label.

Money fields come back in each account's own currency (GBP for a UK account,
EUR for an EU one); they are converted to EUR here, like every other source.

An imported activity statement (for an account without a key, e.g. a closed
one) is shown next to the live accounts. With neither, sample holdings keep
the dashboard running.

API reference: https://docs.trading212.com/api (beta, v0). Positions come from
/equity/positions; the older /equity/portfolio is used only if that 404s.
"""

from __future__ import annotations

import functools
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from .base import Holding
from ..datafiles import resolve as _resolve_data
from ..fx import to_eur, unit
from ..instruments import TICKERS, price_currency, ticker_for
from ..positions import Trade

BASE_URL = os.getenv("T212_BASE_URL", "https://live.trading212.com/api/v0")
_HOST = BASE_URL.split("/api/")[0]

# The Activity-statement PDF imported by backend/importers/trading212_pdf.py,
# for an account without an API key (e.g. a closed one). Shown next to live accounts.
STATEMENT_FILE = "trading212_statement.json"
STATEMENT_LABEL = "Trading212"

# Trading 212 symbols whose market-data ticker differs from the yfinance one
# (legacy FB for Meta; the Xetra SPDR Russell 2000 ETF).
_ALIAS = {"FB": "META", "ZPRRd": "ZPRR.DE"}

# Trading 212 marks non-US listings with a lowercase letter after the symbol:
# VWRPl_EQ (London), ZPRRd_EQ (Xetra), ASMLa_EQ (Amsterdam), MCp_EQ (Paris).
# Best effort: the ISIN table in instruments.py wins when it knows the ISIN, and
# Trading 212 supplies the live price itself, so a miss only loses the daily
# change and sparkline for that holding.
_EXCHANGE_SUFFIX = {
    "l": ".L", "d": ".DE", "a": ".AS", "p": ".PA", "m": ".MI",
    "e": ".MC", "s": ".SW", "b": ".BR",
}

_ENV_KEY = re.compile(r"^T212_(?:([A-Z0-9_]+?)_)?API_KEY$")
_UPPER_WORDS = {"ISA", "GIA", "EU", "UK", "US", "SIPP", "CFD"}


@dataclass(frozen=True)
class Account:
    label: str
    key: str
    secret: str
    isa: bool | None = None  # set for accounts added in-app (ISA box); else from the label

    @property
    def is_isa(self) -> bool:
        if self.isa is not None:
            return self.isa
        return "ISA" in self.label.upper().split()

    @property
    def auth(self) -> tuple[str, str]:
        return (self.key, self.secret)


def _label(tag: str | None) -> str:
    if not tag:
        return STATEMENT_LABEL
    words = [w.upper() if w.upper() in _UPPER_WORDS else w.title()
             for w in tag.split("_") if w]
    return "T212 " + " ".join(words)


def accounts() -> list[Account]:
    """Every account with both a key and a secret in the environment."""
    out = []
    for name in sorted(os.environ):
        m = _ENV_KEY.match(name)
        key = os.environ[name].strip()
        if not m or not key:
            continue
        prefix = name[: -len("API_KEY")]          # "T212_" or "T212_ISA_"
        secret = (os.getenv(prefix + "API_SECRET") or "").strip()
        if not secret:
            continue
        label = (os.getenv(prefix + "NAME") or "").strip() or _label(m.group(1))
        out.append(Account(label, key, secret))
    # Accounts added in Settings ("Add an account") use the same code path.
    try:
        from ..connections import t212_accounts

        keys = {a.key for a in out}
        out += [a for a in t212_accounts() if a.key and a.secret and a.key not in keys]
    except Exception:
        pass
    return out


def is_live() -> bool:
    return bool(accounts())


# --------------------------------------------------------------------- HTTP
_session = requests.Session()


def _url(path: str) -> str:
    # nextPagePath may come back as a full "/api/v0/..." path or relative to v0
    return _HOST + path if path.startswith("/api/") else BASE_URL + path


def _get(acct: Account, path: str):
    """GET with one polite retry on 429 (limits are per account, per endpoint)."""
    for attempt in range(3):
        resp = _session.get(_url(path), auth=acct.auth, timeout=30)
        if resp.status_code == 429 and attempt < 2:
            try:
                wait = float(resp.headers.get("x-ratelimit-reset", 0)) - time.time()
            except ValueError:
                wait = 2
            time.sleep(min(max(wait, 1.0), 15.0))
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return resp.json()


def _pages(acct: Account, path: str) -> list[dict]:
    """All items of a cursor-paginated history endpoint."""
    items: list[dict] = []
    seen: set[str] = set()
    while path and path not in seen:
        seen.add(path)
        page = _get(acct, path)
        items.extend(page.get("items", []))
        path = page.get("nextPagePath")
    return items


def _not_found(err: Exception) -> bool:
    resp = getattr(err, "response", None)
    return resp is not None and resp.status_code in (404, 405)


def _ttl_cache(seconds: int):
    """Per-argument time-based memoization. Trading 212 rate-limits hard, and a
    single dashboard load touches each endpoint more than once, so recent
    results are reused. On error, a stale value is returned if there is one."""
    def decorator(fn):
        lock = threading.Lock()
        cache: dict[object, tuple[object, float]] = {}

        @functools.wraps(fn)
        def wrapper(*args):
            with lock:
                hit = cache.get(args)
                if hit and time.time() - hit[1] < seconds:
                    return hit[0]
                try:
                    value = fn(*args)
                except Exception:
                    if hit:
                        return hit[0]
                    raise
                cache[args] = (value, time.time())
                return value

        wrapper.cache_clear = cache.clear  # type: ignore[attr-defined]
        return wrapper

    return decorator


# ------------------------------------------------------------- symbology
def yf_ticker(t212: str, isin: str = "") -> str:
    """Trading 212 ticker -> yfinance ticker. 'AAPL_US_EQ' -> 'AAPL',
    'VWRPl_EQ' -> 'VWRP.L', 'ZPRRd_EQ' -> 'ZPRR.DE'."""
    if isin and isin in TICKERS:
        return TICKERS[isin]
    sym = t212[:-3] if t212.endswith("_EQ") else t212
    if sym.endswith("_US"):
        sym = sym[:-3]
        return _ALIAS.get(sym, sym.replace("_", "-"))
    if sym in _ALIAS:
        return _ALIAS[sym]
    if len(sym) > 1 and sym[-1].islower() and not sym[:-1].islower():
        suffix = _EXCHANGE_SUFFIX.get(sym[-1])
        if suffix:
            return sym[:-1] + suffix
    return sym


# ---------------------------------------------------------------- account
@_ttl_cache(seconds=3600)
def account_currency(acct: Account) -> str:
    """The account's own currency (GBP for a UK account)."""
    try:
        return _get(acct, "/equity/account/summary").get("currency") or "EUR"
    except requests.RequestException as e:
        if not _not_found(e):
            raise
    info = _get(acct, "/equity/account/info")  # legacy
    return info.get("currencyCode") or "EUR"


@_ttl_cache(seconds=60)
def account_summary(acct: Account) -> dict:
    """Cash and totals for one account: {currency, cash, value}. {} on failure."""
    try:
        s = _get(acct, "/equity/account/summary")
    except requests.RequestException:
        return {}
    cash = s.get("cash") or {}
    inv = s.get("investments") or {}
    return {
        "currency": s.get("currency") or "EUR",
        "cash": float(cash.get("availableToTrade") or 0) + float(cash.get("inPies") or 0)
        + float(cash.get("reservedForOrders") or 0),
        "value": float(inv.get("currentValue") or 0),
    }


# -------------------------------------------------------------- holdings
def _holding_from_position(acct: Account, pos: dict) -> Holding:
    inst = pos.get("instrument") or {}
    isin = inst.get("isin", "")
    scale, ccy = unit(inst.get("currency") or "")
    wallet = pos.get("walletImpact") or {}
    pnl = wallet.get("unrealizedProfitLoss")
    acct_ccy = wallet.get("currency") or account_currency(acct)
    ticker = yf_ticker(inst.get("ticker", ""), isin)
    return Holding(
        broker=acct.label,
        ticker=ticker,
        name=inst.get("name") or ticker,
        isin=isin,
        quantity=float(pos["quantity"]),
        # prices are per share, in the instrument's currency (pence -> pounds)
        buy_price=float(pos.get("averagePricePaid") or 0) * scale,
        currency=ccy if ccy else price_currency(ticker),
        price=float(pos["currentPrice"]) * scale,
        # Trading 212's own P/L in the account currency, FX at purchase included
        broker_profit_eur=to_eur(float(pnl), acct_ccy) if pnl is not None else None,
        wrapper="ISA" if acct.is_isa else "",
    )


def _legacy_holdings(acct: Account) -> list[Holding]:
    """Pre-2026 /equity/portfolio shape: averagePrice, currentPrice, ppl, fxPpl."""
    meta = _instrument_meta(acct)
    acct_ccy = account_currency(acct)
    out = []
    for pos in _get(acct, "/equity/portfolio"):
        info = meta.get(pos["ticker"], {})
        scale, ccy = unit(info.get("currencyCode") or "")
        ticker = yf_ticker(pos["ticker"], info.get("isin", ""))
        out.append(Holding(
            broker=acct.label, ticker=ticker, name=info.get("name", ticker),
            isin=info.get("isin", ""), quantity=float(pos["quantity"]),
            buy_price=float(pos["averagePrice"]) * scale,
            currency=ccy if ccy else price_currency(ticker),
            price=float(pos["currentPrice"]) * scale,
            broker_profit_eur=to_eur(float(pos.get("ppl") or 0) + float(pos.get("fxPpl") or 0), acct_ccy),
            wrapper="ISA" if acct.is_isa else "",
        ))
    return out


@_ttl_cache(seconds=30)
def account_holdings(acct: Account) -> list[Holding]:
    try:
        raw = _get(acct, "/equity/positions")
    except requests.RequestException as e:
        if not _not_found(e):
            raise
        return _legacy_holdings(acct)
    return [_holding_from_position(acct, p) for p in raw if float(p.get("quantity") or 0) > 0]


# The instrument list is large and static; only the legacy path needs it.
_META_CACHE = Path(__file__).resolve().parents[2] / "data" / "t212_instruments.json"
_META_TTL = 7 * 24 * 3600  # 1 week


def _instrument_meta(acct: Account) -> dict[str, dict]:
    if _META_CACHE.exists() and (time.time() - _META_CACHE.stat().st_mtime) < _META_TTL:
        try:
            return {row["ticker"]: row for row in json.loads(_META_CACHE.read_text())}
        except (ValueError, KeyError):
            pass
    try:
        data = _get(acct, "/equity/metadata/instruments")
    except requests.RequestException:
        return {}
    try:
        _META_CACHE.write_text(json.dumps(data))
    except OSError:
        pass
    return {row["ticker"]: row for row in data}


# ---------------------------------------------------------------- history
@_ttl_cache(seconds=600)
def account_trades(acct: Account) -> list[Trade]:
    """Filled orders -> trades, with the exact cash amount in the account
    currency (fees and stamp duty folded in). Used for realized P/L."""
    trades: list[Trade] = []
    for item in _pages(acct, "/equity/history/orders?limit=50"):
        order = item.get("order") or {}
        fill = item.get("fill")
        if order.get("status") != "FILLED" or not fill:
            continue
        inst = order.get("instrument") or {}
        wallet = fill.get("walletImpact") or {}
        net = wallet.get("netValue")
        if net is None:
            net = order.get("filledValue") or order.get("value")
        if net is None:
            continue
        # Fees (FX fee, stamp duty, ...) make a buy cost more and a sell net less.
        fees = sum(abs(float(t.get("quantity") or 0)) for t in (wallet.get("taxes") or []))
        side = order.get("side") or ("BUY" if float(fill.get("quantity") or 0) > 0 else "SELL")
        amount = abs(float(net)) + fees if side == "BUY" else abs(float(net)) - fees
        t212 = inst.get("ticker") or order.get("ticker", "")
        isin = inst.get("isin", "")
        trades.append(Trade(
            when=fill.get("filledAt") or order.get("createdAt") or "",
            key=isin or t212,
            name=inst.get("name") or t212,
            side=side,
            quantity=abs(float(fill.get("quantity") or order.get("filledQuantity") or 0)),
            amount=round(amount, 2),
            currency=wallet.get("currency") or order.get("currency") or account_currency(acct),
            ticker=yf_ticker(t212, isin),
        ))
    return trades


@_ttl_cache(seconds=900)
def account_dividends(acct: Account) -> list[dict]:
    try:
        items = _pages(acct, "/equity/history/dividends?limit=50")
    except requests.RequestException as e:
        if not _not_found(e):
            raise
        items = _pages(acct, "/history/dividends?limit=50")  # legacy path
    out = []
    for d in items:
        ccy = d.get("currency") or account_currency(acct)
        amount = float(d.get("amount") or 0)
        inst = d.get("instrument") or {}
        out.append({
            "account": acct.label, "isa": acct.is_isa,
            "date": str(d.get("paidOn", ""))[:10],
            "ticker": d.get("ticker") or inst.get("ticker", ""),
            "isin": inst.get("isin", ""),
            "amount": amount, "currency": ccy,
            "amount_eur": round(to_eur(amount, ccy), 2),
        })
    return out


@_ttl_cache(seconds=900)
def account_transactions(acct: Account) -> list[dict]:
    """Deposits, withdrawals, fees, transfers and interest on cash."""
    out = []
    for t in _pages(acct, "/equity/history/transactions?limit=50"):
        ccy = t.get("currency") or account_currency(acct)
        amount = float(t.get("amount") or 0)
        out.append({
            "account": acct.label, "isa": acct.is_isa,
            "date": str(t.get("dateTime", ""))[:10],
            "type": t.get("type", ""),
            "amount": amount, "currency": ccy,
            "amount_eur": round(to_eur(amount, ccy), 2),
        })
    return out


# ------------------------------------------------------- statement fallback
def _statement() -> dict | None:
    """Parsed activity statement, if the user imported one (None otherwise)."""
    path, real = _resolve_data(STATEMENT_FILE)
    if not real or not path.exists():
        return None
    try:
        return _read_statement(str(path), path.stat().st_mtime)
    except (ValueError, OSError):
        return None


@functools.lru_cache(maxsize=2)
def _read_statement(path: str, mtime: float) -> dict:
    return json.loads(Path(path).read_text())


def has_statement() -> bool:
    return _statement() is not None


def _statement_label(accts: list[Account]) -> str:
    """The statement's account name; renamed if a live account already uses it."""
    if any(a.label == STATEMENT_LABEL for a in accts):
        return STATEMENT_LABEL + " (statement)"
    return STATEMENT_LABEL


def _statement_holdings(st: dict, label: str = STATEMENT_LABEL) -> list[Holding]:
    """Open positions straight from the statement's own table: quantity and the
    exact EUR cost (value − return, i.e. incl. FX at purchase); the live price
    is fetched by the price layer."""
    out = []
    for p in st.get("positions", []):
        qty = float(p["quantity"])
        cost_eur = float(p["value_eur"]) - float(p.get("return_eur") or 0.0)
        ticker = p.get("ticker") or ticker_for(p["isin"]) or p["symbol"]
        ticker = _ALIAS.get(ticker, ticker)
        out.append(Holding(
            broker=label, ticker=ticker, name=p.get("name") or p["symbol"],
            isin=p["isin"], quantity=qty, buy_price=round(cost_eur / qty, 4) if qty else 0.0,
            currency=price_currency(ticker), buy_currency="EUR",
        ))
    return out


def _statement_trades(st: dict) -> list[Trade]:
    return [
        Trade(when=t["date"], key=t["isin"], name=t.get("name") or t["symbol"],
              side=t["side"], quantity=float(t["quantity"]), amount=float(t["amount_eur"]),
              currency="EUR", ticker=t.get("ticker") or ticker_for(t["isin"]))
        for t in st.get("trades", [])
    ]


# ------------------------------------------------- one account, two sources
@_ttl_cache(seconds=900)
def _same_account_as_statement(acct: Account) -> bool:
    """True when this key's order history is the imported statement's account:
    most of the statement's trades (same ISIN, same day) are in it."""
    st = _statement()
    if not st or not st.get("trades"):
        return False
    try:
        api = account_trades(acct)
    except requests.RequestException:
        return False
    stmt = {(t.get("isin"), str(t.get("date", ""))[:10]) for t in st["trades"]}
    mine = {(t.key, t.when[:10]) for t in api}
    common = len(stmt & mine)
    return common >= 3 and common >= 0.5 * min(len(stmt), len(mine))


@_ttl_cache(seconds=900)
def _still_in_use(acct: Account) -> bool:
    """The key's account holds shares, or traded after the statement ends.
    False for a closed account (positions unreadable or empty, nothing new)."""
    try:
        if account_holdings(acct):
            return True
    except requests.RequestException:
        return False
    st = _statement() or {}
    end = max((str(t.get("date", ""))[:10] for t in st.get("trades", [])), default="")
    try:
        return any(t.when[:10] > end for t in account_trades(acct))
    except requests.RequestException:
        return False


def _sources() -> tuple[list[Account], dict | None]:
    """(accounts whose history counts, the statement if it counts).

    A key and an imported statement can describe the same account, e.g. the
    key of an account that has since been closed. Counting both would double
    its realized P/L, dividends and interest. While the account is still in
    use (holds shares or traded after the statement) the key wins; for a
    closed account the statement does, as it was checked against the
    statement's own totals."""
    accts, st = accounts(), _statement()
    if not st:
        return accts, None
    keep = []
    for a in accts:
        if _same_account_as_statement(a):
            if _still_in_use(a):
                keep.append(a)
                st = None
        else:
            keep.append(a)
    return keep, st


def replaced_by_statement() -> list[str]:
    """Labels of keys left out because the imported statement covers them."""
    keep = {a.label for a in _sources()[0]}
    return [a.label for a in accounts() if a.label not in keep]


# ------------------------------------------------------------- public API
# The imported activity statement is for an account without an API key, e.g. a
# closed one (an old EU account after moving to the UK). It is shown next to the
# live accounts, so its history (trades, dividends, interest) is kept.
def get_holdings() -> list[Holding]:
    accts, st = _sources()
    if not accts and not st:
        from ..datafiles import demo
        return _sample_holdings() if demo() else []
    out: list[Holding] = _statement_holdings(st, _statement_label(accts)) if st else []
    for a in accts:
        try:
            out.extend(account_holdings(a))
        except requests.RequestException:
            continue  # one bad key must not break the dashboard; Settings shows the error
    return out


def trade_sources() -> dict[str, tuple[bool, callable]]:
    """{account label: (is_isa, get_trades)} — one entry per account, so
    realized P/L is computed per account (an ISA and a normal account holding
    the same share are separate pools)."""
    accts, st = _sources()
    out = {a.label: (a.is_isa, functools.partial(account_trades, a)) for a in accts}
    if st or not accts:
        out[_statement_label(accts)] = (False, lambda: _statement_trades(st) if st else [])
    return out


def get_trades() -> list[Trade]:
    out: list[Trade] = []
    for _, fn in trade_sources().values():
        try:
            out.extend(fn())
        except requests.RequestException:
            continue
    return out


def get_dividends() -> list[dict]:
    """[{account, isa, date, ticker, isin, amount, currency, amount_eur}]"""
    accts, st = _sources()
    out: list[dict] = []
    if st:
        label = _statement_label(accts)
        out += [{"account": label, "isa": False, "date": d["date"][:10],
                 "ticker": d.get("symbol", ""), "isin": d["isin"],
                 "amount": d["amount_eur"], "currency": "EUR", "amount_eur": d["amount_eur"]}
                for d in st.get("dividends", [])]
    for a in accts:
        try:
            out.extend(account_dividends(a))
        except requests.RequestException:
            continue
    return out


def get_transactions() -> list[dict]:
    out: list[dict] = []
    for a in _sources()[0]:
        try:
            out.extend(account_transactions(a))
        except requests.RequestException:
            continue
    return out


_INTEREST_TYPES = {"INTEREST_ON_FREE_CASH", "LENDING_INTEREST"}


def get_interest() -> list[dict]:
    """Interest on cash (and share-lending income, sign-up bonuses):
    [{account, isa, date, amount_eur}]."""
    accts, st = _sources()
    out: list[dict] = []
    if st:
        label = _statement_label(accts)
        out += [{"account": label, "isa": False, "date": t["date"], "amount_eur": t["amount_eur"]}
                for t in st.get("transactions", []) if t.get("kind") in ("interest", "bonus")]
    if accts:
        out += [{"account": t["account"], "isa": t["isa"], "date": t["date"],
                 "amount_eur": t["amount_eur"]}
                for t in get_transactions() if t["type"] in _INTEREST_TYPES]
    return out


def get_fees() -> list[dict]:
    """Fees charged outside trades (currency conversion of deposits and
    withdrawals, account fees): [{account, isa, date, amount_eur}], negative."""
    accts, st = _sources()
    out: list[dict] = []
    if st:
        label = _statement_label(accts)
        out += [{"account": label, "isa": False, "date": t["date"], "amount_eur": t["amount_eur"]}
                for t in st.get("transactions", []) if t.get("kind") == "fx"]
    out += [{"account": t["account"], "isa": t["isa"], "date": t["date"],
             "amount_eur": -abs(t["amount_eur"])}
            for t in get_transactions() if t["type"] == "FEE"]
    return out


def get_cash() -> list[dict]:
    """Uninvested cash per live account: [{account, isa, currency, balance}]."""
    out = []
    for a in accounts():
        s = account_summary(a)
        if s:
            out.append({"account": a.label, "isa": a.is_isa,
                        "currency": s["currency"], "balance": round(s["cash"], 2)})
    return out


def status() -> list[dict]:
    """Per-account health for the Settings page (no secrets)."""
    out = []
    replaced = set(replaced_by_statement())
    for a in accounts():
        row = {"label": a.label, "isa": a.is_isa}
        if a.label in replaced:
            row["note"] = "same account as the uploaded statement; the statement is used"
        try:
            row["currency"] = account_currency(a)
            row["positions"] = len(account_holdings(a))
            row["ok"] = True
        except requests.RequestException as e:
            resp = getattr(e, "response", None)
            row["ok"] = False
            row["error"] = (f"HTTP {resp.status_code}" if resp is not None else type(e).__name__) + (
                " — key/secret wrong" if resp is not None and resp.status_code == 401 else
                " — key lacks a read scope" if resp is not None and resp.status_code == 403 else "")
        out.append(row)
    return out


def _sample_holdings() -> list[Holding]:
    return [
        Holding("Trading212", "AAPL", "Apple Inc.", 12, 165.0, "USD", price=212.4),
        Holding("Trading212", "MSFT", "Microsoft Corp.", 5, 330.0, "USD", price=441.8),
        Holding("Trading212", "VWRL.L", "Vanguard FTSE All-World", 8, 98.5, "GBP", price=118.2),
    ]
