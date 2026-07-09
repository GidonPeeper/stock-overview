"""Loans, income & recurring payments — the cash-flow side of your finances.

Three small collections edited in-app (Settings) and stored like the bank
accounts: git-ignored JSON, bundled into the encrypted vault, and synced to
the data-sync branch when a GITHUB_TOKEN is set — so entries persist across
restarts and deploys.

Also computes the fun part: Freedom Day — the projected date your liquid
assets could sustain your monthly payments forever on a 4% withdrawal rate.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .datafiles import DATA_DIR, resolve
from .fx import to_eur

_FILE = "finances.json"
KINDS = ("loans", "income", "expenses")


def _load_raw() -> tuple[dict, bool]:
    path, real = resolve(_FILE)
    if not path.exists():
        return {k: [] for k in KINDS}, not real
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return {k: [] for k in KINDS}, not real
    return {k: data.get(k, []) for k in KINDS}, not real


def _save(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    data["updated"] = time.strftime("%Y-%m-%d %H:%M")
    (DATA_DIR / _FILE).write_text(json.dumps(data, indent=2))


def load(liquid_assets_eur: float | None = None) -> dict:
    data, sample = _load_raw()

    total_debt = 0.0
    for loan in data["loans"]:
        loan["balance_eur"] = round(to_eur(float(loan.get("balance", 0)),
                                           loan.get("currency", "EUR")), 2)
        total_debt += loan["balance_eur"]
        # payoff estimate: months = balance / payment (with simple interest drag)
        pay = float(loan.get("monthly_payment", 0))
        rate_m = float(loan.get("rate", 0)) / 100 / 12
        if pay > 0 and pay > loan["balance_eur"] * rate_m:
            bal, months = loan["balance_eur"], 0
            while bal > 0 and months < 600:
                bal = bal * (1 + rate_m) - pay
                months += 1
            y, m = divmod((time.localtime().tm_year * 12 + time.localtime().tm_mon - 1
                           + months), 12)
            loan["payoff"] = f"{y}-{m + 1:02d}"
            loan["payoff_months"] = months

    monthly_income = round(sum(float(i.get("monthly_eur", 0)) for i in data["income"]), 2)
    loan_payments = sum(float(l.get("monthly_payment", 0)) for l in data["loans"])
    other_payments = sum(float(e.get("monthly_eur", 0)) for e in data["expenses"])
    monthly_payments = round(loan_payments + other_payments, 2)
    free_cashflow = round(monthly_income - monthly_payments, 2)

    out = {"loans": data["loans"], "income": data["income"],
           "expenses": data["expenses"], "sample": sample,
           "totals": {"debt_eur": round(total_debt, 2),
                      "monthly_income": monthly_income,
                      "monthly_payments": monthly_payments,
                      "free_cashflow": free_cashflow}}

    if liquid_assets_eur is not None and monthly_payments > 0:
        out["freedom"] = _freedom(liquid_assets_eur - total_debt,
                                  monthly_payments, free_cashflow)
    return out


def _freedom(net_liquid: float, monthly_spend: float, contribution: float,
             annual_return: float = 0.06) -> dict:
    """4%-rule independence: target = 25 × annual spend. Project net liquid
    assets forward at `annual_return`, adding monthly free cash flow."""
    target = monthly_spend * 12 * 25
    progress = max(0.0, min(net_liquid / target * 100, 100.0)) if target else 0.0
    months = None
    v = net_liquid
    if v >= target:
        months = 0
    elif contribution > 0 or v > 0:
        r = annual_return / 12
        for i in range(1, 12 * 60 + 1):
            v = v * (1 + r) + max(contribution, 0)
            if v >= target:
                months = i
                break
    eta = None
    if months is not None:
        t = time.localtime()
        total = t.tm_year * 12 + (t.tm_mon - 1) + months
        eta = f"{total // 12}-{total % 12 + 1:02d}"
    runway = round(net_liquid / monthly_spend, 1) if monthly_spend else None
    return {"target_eur": round(target), "progress_pct": round(progress, 1),
            "eta_ym": eta, "months_away": months, "runway_months": runway,
            "method": ("Freedom Day = when net liquid assets reach 25× your annual "
                       "payments (the 4% rule), projecting 6%/yr growth plus your "
                       "free cash flow. Runway = how long assets cover payments "
                       "with zero income. A model, not a guarantee.")}


def upsert(kind: str, name: str, fields: dict) -> dict:
    data, sample = _load_raw()
    if sample:
        data = {k: [] for k in KINDS}  # first real entry clears demo rows
    items = data[kind]
    key = name.strip().lower()
    existing = next((x for x in items if x.get("name", "").strip().lower() == key), None)
    row = {"name": name.strip(), **fields}
    if existing:
        existing.update(row)
    else:
        items.append(row)
    _save(data)
    return load()


def delete(kind: str, name: str) -> dict:
    data, sample = _load_raw()
    if sample:
        data = {k: [] for k in KINDS}
    else:
        data[kind] = [x for x in data[kind]
                      if x.get("name", "").strip().lower() != name.strip().lower()]
    _save(data)
    return load()
