"""Cash, savings & retirement accounts — the non-brokerage side of your money.

Personal bank APIs are gated behind PSD2 licensing (Rabobank/Revolut only talk
to licensed AISPs), so balances live in a small local store you edit in-app in
seconds — no file exports. Accounts carry a type (cash / savings / retirement /
other) so 401(k)-style pension pots sit beside bank balances in the net-worth
view. An aggregator (e.g. Enable Banking) can automate this later.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .datafiles import DATA_DIR, resolve
from .fx import to_eur

_FILE = "cash_accounts.json"

TYPES = ("cash", "savings", "retirement", "other")


def _store_path() -> Path:
    path, real = resolve(_FILE)
    return path if real else DATA_DIR / _FILE


def load() -> dict:
    path, real = resolve(_FILE)
    if not path.exists():
        return {"accounts": [], "sample": not real}
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {"accounts": [], "sample": not real}
    accounts = data.get("accounts", [])
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
            "sample": not real, "updated": data.get("updated")}


def upsert(name: str, institution: str, balance: float, currency: str,
           type_: str = "cash", rate: float = 0.0) -> dict:
    data = load()
    # first real edit starts clean — never carry demo accounts into the real file
    accounts = [] if data.get("sample") else data["accounts"]
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
    (DATA_DIR / _FILE).write_text(json.dumps(
        {"accounts": accounts, "updated": time.strftime("%Y-%m-%d %H:%M")}, indent=2))
    return load()


def delete(name: str) -> dict:
    data = load()
    accounts = [] if data.get("sample") else [
        a for a in data["accounts"]
        if a["name"].strip().lower() != name.strip().lower()]
    for a in accounts:
        a.pop("balance_eur", None)
        a.pop("interest_eur_yr", None)
    (DATA_DIR / _FILE).write_text(json.dumps(
        {"accounts": accounts, "updated": time.strftime("%Y-%m-%d %H:%M")}, indent=2))
    return load()
