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
INTEREST_TYPES = ("cash", "savings")   # counted in "interest a year" and its average rate


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
    for a in accounts:
        a["rate_known"] = True              # typed in with the account (0 = pays nothing)
        a["rate_source"] = "typed"
    auto = _broker_cash() + _connection_cash()
    _find_rates(auto)
    _apply_auto_settings(auto, data.get("auto"))
    accounts += auto
    total = interest = banked = no_rate = 0.0
    by_type: dict[str, float] = {}
    for a in accounts:
        a["type"] = a.get("type") if a.get("type") in TYPES else "cash"
        a["rate"] = float(a.get("rate", 0) or 0)
        a["balance_eur"] = round(to_eur(float(a.get("balance", 0)), a.get("currency", "EUR")), 2)
        a["interest_eur_yr"] = round(a["balance_eur"] * a["rate"] / 100, 2)
        total += a["balance_eur"]
        by_type[a["type"]] = round(by_type.get(a["type"], 0.0) + a["balance_eur"], 2)
        if a["type"] in INTEREST_TYPES:
            if a["rate_known"]:
                interest += a["interest_eur_yr"]
                banked += a["balance_eur"]
            else:
                no_rate += a["balance_eur"]
    # Interest and its average rate cover bank and savings accounts only: a
    # pension pot's growth is not interest on cash, and its balance would pull
    # the average rate down. An account whose rate nobody has entered is left
    # out of both and reported as `rate_unknown_eur`: unknown is not 0%.
    # `rate_checking`: some rate is still being looked up (ask again shortly).
    return {"accounts": accounts, "total_eur": round(total, 2), "by_type": by_type,
            "interest_eur_yr": round(interest, 2),
            "blended_rate_pct": round(interest / banked * 100, 2) if banked else 0,
            "rate_unknown_eur": round(no_rate, 2),
            "rate_checking": any((a.get("rate_auto") or {}).get("status") == "checking"
                                 for a in auto if not a.get("rate_known")),
            "sample": sample, "updated": data.get("updated")}


# Accounts that update themselves (Trading 212 cash, Starling, a linked bank)
# arrive without an interest rate. Their rate is found from the account itself
# where the provider allows it (backend/interest.py: a rate it states, or the
# one implied by the interest it paid); a rate typed in Settings wins, and is
# kept here with the kind of account, by account name, under "auto" in the
# same file. Each auto account gets:
#   rate, rate_known      the rate used (unknown = left out of the totals, not 0%)
#   rate_source           "typed" | "provider" | "paid" | None
#   rate_note             where it came from, in a few words
#   rate_auto             what was found automatically (also when a typed rate
#                         wins): {status: ok, pct, source, note, ...}, or
#                         {status, text, steps} saying why there is none
_SAVINGS_WORDS = ("saver", "saving", "spaar", "sparen", "epargne", "épargne", "tagesgeld", "deposit")


def _auto_key(name: str) -> str:
    return " ".join(str(name).lower().split())


def _apply_auto_settings(accounts: list[dict], settings) -> None:
    settings = settings if isinstance(settings, dict) else {}
    for a in accounts:
        mine = settings.get(_auto_key(a.get("name", "")))
        mine = mine if isinstance(mine, dict) else {}
        if mine.get("type") in TYPES:
            a["type"] = mine["type"]
        elif any(w in _auto_key(a.get("name", "")) for w in _SAVINGS_WORDS):
            a["type"] = "savings"
        found = a.get("rate_auto") if isinstance(a.get("rate_auto"), dict) else None
        a["rate_auto"] = found
        try:
            a["rate"] = float(mine["rate"])
            a["rate_known"], a["rate_source"], a["rate_note"] = True, "typed", "entered by you"
        except (KeyError, TypeError, ValueError):
            if found and found.get("status") == "ok":
                a["rate"], a["rate_known"] = float(found["pct"]), True
                a["rate_source"], a["rate_note"] = found.get("source") or "paid", found.get("note") or ""
            else:
                a["rate"], a["rate_known"], a["rate_source"], a["rate_note"] = 0.0, False, None, ""


def _find_rates(accounts: list[dict]) -> None:
    """`rate_auto` for each account that updates itself (never waits, never raises)."""
    try:
        from . import interest

        interest.attach(accounts)
    except Exception:
        for a in accounts:
            a.setdefault("rate_auto", None)


def set_auto(name: str, rate: float | None, type_: str | None = None) -> dict:
    """Remember the interest rate and the kind of an account that updates
    itself. rate None: no rate of your own (the one found automatically is
    used, if any)."""
    path = _store_path()
    data = read_json(path, {}, lists=("accounts",)) if path.exists() else {}
    auto = data.get("auto") if isinstance(data.get("auto"), dict) else {}
    entry = dict(auto.get(_auto_key(name)) or {})
    if rate is None:
        entry.pop("rate", None)
    else:
        entry["rate"] = rate
    if type_ in TYPES:
        entry["type"] = type_
    auto[_auto_key(name)] = entry
    data["accounts"] = _manual(data.get("accounts", []))
    data["auto"] = auto
    data["updated"] = time.strftime("%Y-%m-%d %H:%M")
    DATA_DIR.mkdir(exist_ok=True)
    write_atomic(DATA_DIR / _FILE, json.dumps(data, indent=2))
    return load()


def _write(accounts: list[dict]) -> None:
    """Save the typed-in accounts, keeping the settings of the others."""
    path, real = resolve(_FILE)
    kept = read_json(path, {}, lists=("accounts",)).get("auto") if real else None
    for a in accounts:
        for k in ("balance_eur", "interest_eur_yr", "rate_known", "rate_source"):
            a.pop(k, None)
    out = {"accounts": accounts, "updated": time.strftime("%Y-%m-%d %H:%M")}
    if isinstance(kept, dict) and kept:
        out["auto"] = kept
    DATA_DIR.mkdir(exist_ok=True)
    write_atomic(DATA_DIR / _FILE, json.dumps(out, indent=2))


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
             "rate": 0.0, "auto": True, "feed": "trading212", "ref": r["account"]}
            for r in rows if r["balance"]]


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
    _write(accounts)
    return load()


def delete(name: str) -> dict:
    data = load()
    accounts = [] if data.get("sample") else [
        a for a in _manual(data["accounts"])
        if a["name"].strip().lower() != name.strip().lower()]
    _write(accounts)
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
