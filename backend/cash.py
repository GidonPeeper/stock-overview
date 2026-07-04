"""Cash & bank accounts — the non-investment side of your money.

Personal bank APIs are gated behind PSD2 licensing (Rabobank/Revolut only talk
to licensed AISPs), so balances live in a small local store you edit in-app in
seconds — no file exports. An aggregator (e.g. Enable Banking's free
own-accounts tier) can automate this later; the storage shape already fits.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .datafiles import DATA_DIR, resolve
from .fx import to_eur

_FILE = "cash_accounts.json"


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
    total = 0.0
    for a in accounts:
        a["balance_eur"] = round(to_eur(float(a.get("balance", 0)), a.get("currency", "EUR")), 2)
        total += a["balance_eur"]
    return {"accounts": accounts, "total_eur": round(total, 2),
            "sample": not real, "updated": data.get("updated")}


def upsert(name: str, institution: str, balance: float, currency: str) -> dict:
    data = load()
    # first real edit starts clean — never carry demo accounts into the real file
    accounts = [] if data.get("sample") else data["accounts"]
    for a in accounts:
        a.pop("balance_eur", None)
    key = name.strip().lower()
    existing = next((a for a in accounts if a["name"].strip().lower() == key), None)
    if existing:
        existing.update(institution=institution.strip(), balance=balance,
                        currency=currency.upper())
    else:
        accounts.append({"name": name.strip(), "institution": institution.strip(),
                         "balance": balance, "currency": currency.upper()})
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
    (DATA_DIR / _FILE).write_text(json.dumps(
        {"accounts": accounts, "updated": time.strftime("%Y-%m-%d %H:%M")}, indent=2))
    return load()
