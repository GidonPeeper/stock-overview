"""UK tax year at a glance (6 April – 5 April).

Facts only, no tax bill: what you realised and received outside your ISA this
tax year, how much of the ISA allowance you have paid in, and which recent
sales the 30-day ("bed and breakfast") rule still covers.

  * Gains are per account, average-cost pools (HMRC's section 104 pooling).
    The same-day and 30-day matching rules are not applied to the figures; the
    30-day list below shows where they could change them.
  * ISA accounts are tax-free and left out of gains and dividends.
  * Amounts are converted to GBP at today's rate. For a GBP account that is
    exact; for a EUR account HMRC wants the rate on each trade date.
  * UK_RESIDENT_FROM (YYYY-MM-DD, optional) starts the count later in your
    first UK tax year (split-year treatment).

Allowances below are for 2026/27. Update them if a Budget changes them.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

from . import sources
from .connectors import trading212
from .fx import from_eur
from .positions import realized_events

CGT_ANNUAL_EXEMPT_GBP = 3000
DIVIDEND_ALLOWANCE_GBP = 500
ISA_ALLOWANCE_GBP = 20000
BED_AND_BREAKFAST_DAYS = 30


def tax_year(d: date) -> tuple[date, date, str]:
    """(first day, last day, '2026/27') of the UK tax year containing `d`."""
    y = d.year if d >= date(d.year, 4, 6) else d.year - 1
    return date(y, 4, 6), date(y + 1, 4, 5), f"{y}/{str(y + 1)[2:]}"


def _resident_from() -> date | None:
    raw = (os.getenv("UK_RESIDENT_FROM") or "").strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date() if raw else None
    except ValueError:
        return None


def _day(when: str) -> date | None:
    try:
        return datetime.strptime(when[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _gbp(amount_eur: float) -> float:
    return round(from_eur(amount_eur, "GBP"), 2)


def summary(today: date | None = None) -> dict:
    today = today or date.today()
    start, end, label = tax_year(today)
    resident = _resident_from()
    since = max(start, resident) if resident and resident <= end else start

    gains = losses = 0.0
    disposals = 0
    recent: list[dict] = []
    accounts: list[dict] = []

    # Taxable accounts only. Buys are collected across all of them first: the
    # 30-day rule applies per person, so a rebuy at another broker counts too.
    taxable: dict[str, list] = {}
    for name, (is_isa, get_trades) in sources.trade_sources().items():
        accounts.append({"name": name, "isa": is_isa})
        if is_isa:
            continue
        try:
            taxable[name] = get_trades()
        except Exception:
            continue
    buys_by_key: dict[str, list[date]] = {}
    for trades in taxable.values():
        for t in trades:
            d = _day(t.when)
            if t.side == "BUY" and d:
                buys_by_key.setdefault(t.key, []).append(d)

    for name, trades in taxable.items():
        for ev in realized_events(sources.in_eur(trades)):
            d = _day(ev["when"])
            if not d:
                continue
            gain = _gbp(ev["realized"])
            if since <= d <= end:
                disposals += 1
                if gain >= 0:
                    gains += gain
                else:
                    losses += gain
            if today - timedelta(days=BED_AND_BREAKFAST_DAYS) <= d <= today:
                window_end = d + timedelta(days=BED_AND_BREAKFAST_DAYS)
                # same-day buys are matched too (same-day rule)
                rebought = [b for b in buys_by_key.get(ev["key"], []) if d <= b <= window_end]
                recent.append({
                    "name": ev["name"], "account": name, "sold_on": d.isoformat(),
                    "gain_gbp": gain,
                    # first day a repurchase in a taxable account is no longer matched
                    "rebuy_from": (window_end + timedelta(days=1)).isoformat(),
                    "rebought": bool(rebought),
                })
    recent.sort(key=lambda r: r["sold_on"], reverse=True)

    # Dividends and interest, outside ISAs only (Trading 212 per account;
    # DeGiro/Trade Republic income is not dated per tax year here).
    div = interest = 0.0
    for x in trading212.get_dividends():
        d = _day(x.get("date", ""))
        if not x.get("isa") and d and since <= d <= end:
            div += _gbp(x["amount_eur"])
    for x in trading212.get_interest():
        d = _day(x.get("date", ""))
        if not x.get("isa") and d and since <= d <= end:
            interest += _gbp(x["amount_eur"])

    # ISA subscriptions: deposits into ISA accounts this tax year.
    isa_paid = 0.0
    for x in trading212.get_transactions():
        d = _day(x.get("date", ""))
        if x.get("isa") and x.get("type") == "DEPOSIT" and d and start <= d <= end:
            isa_paid += _gbp(x["amount_eur"])
    has_isa = any(a["isa"] for a in accounts)

    net = round(gains + losses, 2)
    return {
        "tax_year": label,
        "from": since.isoformat(),
        "to": end.isoformat(),
        "accounts": accounts,
        "gains_gbp": round(gains, 2),
        "losses_gbp": round(losses, 2),
        "net_gain_gbp": net,
        "disposals": disposals,
        "annual_exempt_gbp": CGT_ANNUAL_EXEMPT_GBP,
        "exempt_left_gbp": round(max(CGT_ANNUAL_EXEMPT_GBP - max(net, 0), 0), 2),
        "dividends_gbp": round(div, 2),
        "dividend_allowance_gbp": DIVIDEND_ALLOWANCE_GBP,
        "interest_gbp": round(interest, 2),
        "isa": {
            "tracked": has_isa and trading212.is_live(),
            "paid_in_gbp": round(isa_paid, 2),
            "allowance_gbp": ISA_ALLOWANCE_GBP,
            "left_gbp": round(max(ISA_ALLOWANCE_GBP - isa_paid, 0), 2),
        },
        "recent_sales": recent,
        "method": (
            "Taxable accounts only (ISAs left out). Gains use each account's "
            "average cost (section 104 pool) at today's GBP rate; same-day and "
            "30-day matching are not applied. ISA allowance counts deposits "
            "into ISA accounts this tax year, not transfers. Facts, not a tax "
            "calculation — check your Self Assessment."
        ),
    }
