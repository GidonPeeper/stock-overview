"""Loans, income, recurring payments and things you own.

Four small collections edited in-app (Settings) and stored like the bank
accounts: git-ignored JSON, bundled into the encrypted vault, and synced to
the data-sync branch when a GITHUB_TOKEN is set — so entries persist across
restarts and deploys.

Income is entered before tax (the default for UK residents) or after tax.
Before-tax items go through `paye.py` (income tax, National Insurance,
pension), and every cash-flow total uses the take-home amount. Each item's
`monthly_eur` stays the amount as entered, so the tax advisors keep reading
gross income.

Assets ("things you own": a watch, a car, art) count toward net worth at the
value you enter, but not toward the liquid money used for Freedom Day.

Income and expense items carry a `fixed` flag (default True). The "fixed
monthly" view sums only the recurring, unchanging items — salary, allowance,
rent, loan repayments — and excludes anything marked variable (groceries,
nights out) so you see the dependable backbone of your month.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .datafiles import DATA_DIR, read_json, resolve, write_atomic
from .fx import to_eur

_FILE = "finances.json"
KINDS = ("loans", "income", "expenses", "assets")
ASSET_CATEGORIES = ("watch", "jewellery", "car", "art", "collectible", "property", "other")


def _load_raw() -> tuple[dict, bool]:
    path, real = resolve(_FILE)
    real = real or not path.exists()     # "sample" = the bundled demo file only
    if not path.exists():
        return {k: [] for k in KINDS}, not real
    data = read_json(path, {}, lists=KINDS)   # damaged: DataFileError, never empty
    return {k: data.get(k, []) for k in KINDS}, not real


def _save(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    data["updated"] = time.strftime("%Y-%m-%d %H:%M")
    write_atomic(DATA_DIR / _FILE, json.dumps(data, indent=2))


def load(liquid_assets_eur: float | None = None) -> dict:
    data, sample = _load_raw()

    # Income/expense rows store `amount` + `currency`; older rows only have
    # `monthly_eur` (entered in euros). Either way `monthly_eur` is the EUR value.
    for x in data["income"] + data["expenses"]:
        if "amount" in x:
            x["currency"] = x.get("currency") or "EUR"
            x["monthly_eur"] = round(to_eur(float(x["amount"] or 0), x["currency"]), 2)
        else:
            x["currency"] = "EUR"
            x["amount"] = float(x.get("monthly_eur", 0) or 0)

    total_debt = 0.0
    for loan in data["loans"]:
        ccy = loan.get("currency", "EUR")
        loan["balance_eur"] = round(to_eur(float(loan.get("balance", 0)), ccy), 2)
        loan["monthly_payment_eur"] = round(to_eur(float(loan.get("monthly_payment", 0)), ccy), 2)
        total_debt += loan["balance_eur"]
        # payoff estimate: months = balance / payment (with simple interest drag)
        pay = loan["monthly_payment_eur"]
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

    tax_info = _apply_tax(data["income"], sample)
    monthly_income = round(sum(float(i["net_monthly_eur"]) for i in data["income"]), 2)
    monthly_gross = round(sum(float(i.get("monthly_eur", 0)) for i in data["income"]), 2)
    loan_payments = sum(l["monthly_payment_eur"] for l in data["loans"])
    other_payments = sum(float(e.get("monthly_eur", 0)) for e in data["expenses"])
    monthly_payments = round(loan_payments + other_payments, 2)
    free_cashflow = round(monthly_income - monthly_payments, 2)

    assets_total = 0.0
    for a in data["assets"]:
        ccy = a.get("currency") or "EUR"
        a["currency"] = ccy
        a["category"] = a.get("category") if a.get("category") in ASSET_CATEGORIES else "other"
        a["value_eur"] = round(to_eur(float(a.get("value", 0) or 0), ccy), 2)
        paid = float(a.get("paid", 0) or 0)
        if paid:
            a["paid_eur"] = round(to_eur(paid, ccy), 2)
            a["change_eur"] = round(a["value_eur"] - a["paid_eur"], 2)
        assets_total += a["value_eur"]

    out = {"loans": data["loans"], "income": data["income"],
           "expenses": data["expenses"], "assets": data["assets"], "sample": sample,
           "totals": {"debt_eur": round(total_debt, 2),
                      "assets_eur": round(assets_total, 2),
                      "monthly_income": monthly_income,
                      "monthly_income_gross": monthly_gross,
                      "monthly_payments": monthly_payments,
                      "free_cashflow": free_cashflow},
           "tax": tax_info,
           "fixed": _fixed_flow(data)}

    if liquid_assets_eur is not None and monthly_payments > 0:
        out["freedom"] = _freedom(liquid_assets_eur - total_debt,
                                  monthly_payments, free_cashflow)
    return out


def _residence() -> str:
    try:
        from .harvest import residence
        return residence()
    except Exception:
        return "UK"


def _apply_tax(income: list[dict], sample: bool) -> dict:
    """Fill each income row's take-home (`net_monthly_eur`) and tax breakdown.

    Before-tax rows are taxed together, fixed items first, so a bonus or
    commission carries the tax at your top rate. Rows without a `tax` field
    count as before tax for UK residents (salaries are quoted gross)."""
    uk = _residence() == "UK"
    for i in income:
        if i.get("tax") not in ("before", "after"):
            i["tax"] = "before" if uk and not sample else "after"
        i["net_monthly_eur"] = float(i.get("monthly_eur", 0) or 0)
    gross_rows = [i for i in income if i["tax"] == "before"]
    if not gross_rows:
        return {"applied": False, "uk": uk}
    if not uk:
        return {"applied": False, "uk": False,
                "note": "Tax is only worked out for UK residents (TAX_RESIDENCE=UK). "
                        "Enter take-home amounts as after tax."}
    from . import paye
    from .fx import from_eur

    order = ([i for i in gross_rows if i.get("fixed", True) is not False]
             + [i for i in gross_rows if i.get("fixed", True) is False])
    gbp = lambda eur: from_eur(eur, "GBP")
    calc = paye.annual([{"gross": gbp(i["monthly_eur"]) * 12,
                         "pension_pct": float(i.get("pension_pct", 0) or 0),
                         "pension_type": i.get("pension_type") or "sacrifice",
                         "employer_pct": float(i.get("employer_pct", 0) or 0)} for i in order])
    eur = lambda gbp_year: round(to_eur(gbp_year / 12, "GBP"), 2)
    for row, r in zip(order, calc["items"]):
        row["net_monthly_eur"] = eur(r["net"])
        row["tax_monthly_eur"] = eur(r["income_tax"])
        row["ni_monthly_eur"] = eur(r["ni"])
        row["pension_monthly_eur"] = eur(r["pension"])
        row["pension_relief_monthly_eur"] = eur(r["pension_relief"])
        row["employer_pension_monthly_eur"] = eur(r["employer_pension"])
        row["pension_pot_monthly_eur"] = eur(r["pension_into_pot"])
        row["top_rate_pct"] = r["top_rate"]
        slips = paye.payslips(gbp(row["monthly_eur"]) * 12, row.get("started"),
                              float(row.get("pension_pct", 0) or 0),
                              row.get("pension_type") or "sacrifice",
                              employer_pct=float(row.get("employer_pct", 0) or 0))
        if slips and row is order[0]:
            row["payslips"] = [{**p, "net_eur": round(to_eur(p["net"], "GBP"), 2)} for p in slips]
    t = calc["total"]
    return {"applied": True, "uk": True, "rules": paye.RULES_NOTE,
            "yearly_eur": {k: round(to_eur(v, "GBP"), 2) for k, v in t.items()}}


def _fixed_flow(data: dict) -> dict:
    """Recurring, unchanging monthly cash flow. Income/expense items are fixed
    unless explicitly marked ``fixed: false`` (e.g. groceries); every loan
    repayment is fixed by nature."""
    def keep(x) -> bool:
        return x.get("fixed", True) is not False

    income = [{"name": i["name"], "monthly_eur": round(float(i["net_monthly_eur"]), 2),
               "gross_monthly_eur": round(float(i.get("monthly_eur", 0)), 2),
               "taxed": i.get("tax") == "before"}
              for i in data["income"] if keep(i)]
    outgoings = [{"name": e["name"], "monthly_eur": round(float(e.get("monthly_eur", 0)), 2),
                  "kind": "expense"}
                 for e in data["expenses"] if keep(e)]
    outgoings += [{"name": l["name"], "monthly_eur": l["monthly_payment_eur"], "kind": "loan"}
                  for l in data["loans"] if l["monthly_payment_eur"] > 0]

    in_total = round(sum(i["monthly_eur"] for i in income), 2)
    out_total = round(sum(o["monthly_eur"] for o in outgoings), 2)
    return {"income": income, "outgoings": outgoings,
            "income_total": in_total, "outgoings_total": out_total,
            "net": round(in_total - out_total, 2),
            "committed_pct": round(out_total / in_total * 100, 1) if in_total else 0.0}


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
        if "amount" in row:
            existing.pop("monthly_eur", None)  # superseded by amount + currency
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
