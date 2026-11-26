"""Cash, savings & retirement accounts — the non-brokerage side of your money.

Balances you type in live in a small local store you edit in-app in seconds.
Read-only ("auto") entries are added next to them: cash in live Trading 212
accounts, and balances from accounts added in Settings (Starling, Wise, and
EU banks linked through Enable Banking). Accounts carry a type (cash /
savings / retirement / other) so 401(k)-style pension pots sit beside bank
balances in the net-worth view.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .datafiles import DATA_DIR, DataFileError, read_json, resolve, write_atomic
from .fx import to_eur

_FILE = "cash_accounts.json"

TYPES = ("cash", "savings", "retirement", "other")


def _store_path() -> Path:
    path, real = resolve(_FILE)
    return path if real else DATA_DIR / _FILE


def load() -> dict:
    path, real = resolve(_FILE)
    sample = not real and path.exists()   # the bundled demo file (demo mode only)
    # no typed-in balances yet: still show cash in connected accounts; a
    # damaged file raises DataFileError (an error on the card, never a 0)
    data = read_json(path, {}, lists=("accounts",))
    accounts = [a for a in data.get("accounts", []) if not a.get("auto")]
    accounts += _broker_cash() + _connection_cash()
    total = interest = 0.0
    by_type: dict[str, float] = {}
    for a in accounts:
        a["type"] = a.get("type") if a.get("type") in TYPES else "cash"
        a["rate"] = float(a.get("rate", 0) or 0)
        a["balance_eur"] = round(to_eur(float(a.get("balance", 0)), a.get("currency", "EUR")), 2)
        a["interest_eur_yr"] = round(a["balance_eur"] * a["rate"] / 100, 2)
        total += a["balance_eur"]
        interest += a["interest_eur_yr"]
        by_type[a["type"]] = round(by_type.get(a["type"], 0.0) + a["balance_eur"], 2)
    return {"accounts": accounts, "total_eur": round(total, 2), "by_type": by_type,
            "interest_eur_yr": round(interest, 2),
            "blended_rate_pct": round(interest / total * 100, 2) if total else 0,
            "sample": sample, "updated": data.get("updated")}


def _broker_cash() -> list[dict]:
    """Uninvested cash in live Trading 212 accounts, read from the API. Shown
    as read-only accounts (`auto`) and never written to the file."""
    try:
        from .connectors import trading212

        rows = trading212.get_cash()
    except Exception:
        return []
    return [{"name": f"{r['account']} cash", "institution": "Trading 212",
             "balance": r["balance"], "currency": r["currency"], "type": "cash",
             "rate": 0.0, "auto": True} for r in rows if r["balance"]]


def _connection_cash() -> list[dict]:
    """Balances from accounts added in Settings (Starling, Wise, IBKR cash),
    also read-only `auto` entries."""
    try:
        from . import connections

        return connections.cash_entries()
    except Exception:
        return []


def _manual(accounts: list[dict]) -> list[dict]:
    return [a for a in accounts if not a.get("auto")]


def manual_accounts() -> list[dict]:
    """The balances typed in by hand (not the demo file), without fetching
    any live balance."""
    path, real = resolve(_FILE)
    if not real:
        return []
    try:
        data = read_json(path, {}, lists=("accounts",))
    except DataFileError:
        return []
    return [a for a in _manual(data.get("accounts", [])) if isinstance(a, dict)]


def upsert(name: str, institution: str, balance: float, currency: str,
           type_: str = "cash", rate: float = 0.0) -> dict:
    data = load()
    # first real edit starts clean — never carry demo accounts into the real file
    accounts = [] if data.get("sample") else _manual(data["accounts"])
    for a in accounts:
        a.pop("balance_eur", None)
        a.pop("interest_eur_yr", None)
    type_ = type_ if type_ in TYPES else "cash"
    key = name.strip().lower()
    existing = next((a for a in accounts if a["name"].strip().lower() == key), None)
    if existing:
        existing.update(institution=institution.strip(), balance=balance,
                        currency=currency.upper(), type=type_, rate=rate)
    else:
        accounts.append({"name": name.strip(), "institution": institution.strip(),
                         "balance": balance, "currency": currency.upper(),
                         "type": type_, "rate": rate})
    DATA_DIR.mkdir(exist_ok=True)
    write_atomic(DATA_DIR / _FILE, json.dumps(
        {"accounts": accounts, "updated": time.strftime("%Y-%m-%d %H:%M")}, indent=2))
    return load()


def delete(name: str) -> dict:
    data = load()
    accounts = [] if data.get("sample") else [
        a for a in _manual(data["accounts"])
        if a["name"].strip().lower() != name.strip().lower()]
    for a in accounts:
        a.pop("balance_eur", None)
        a.pop("interest_eur_yr", None)
    write_atomic(DATA_DIR / _FILE, json.dumps(
        {"accounts": accounts, "updated": time.strftime("%Y-%m-%d %H:%M")}, indent=2))
    return load()


# ------------------------------------------------------------------ caching
# Live balances (Trading 212 cash, and any other broker or bank API added to
# `_broker_cash`) are fresh for 60 s, then served while one background refresh
# runs (backend/cache.py), so the money card never waits on a slow API. Any
# write under /api/ recomputes them. `load()` gets its own copy to edit.
from . import cache as _cache

_uncached_broker_cash = _broker_cash


def _broker_cash() -> list[dict]:  # noqa: F811 - cached wrapper of the function above
    return _cache.get("cash.broker_cash", _uncached_broker_cash, ttl=60, stale=900, copy=True)
