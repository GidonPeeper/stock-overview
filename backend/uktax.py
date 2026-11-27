"""UK tax year at a glance (6 April – 5 April).

Facts only, no tax bill: what you realised and received outside your ISA this
tax year, how much of the ISA allowance you have paid in, and which recent
sales the 30-day ("bed and breakfast") rule still covers.

  * Gains follow HMRC's share matching rules, per person across all taxable
    accounts (`match_disposals`): shares bought the same day first, then
    shares bought in the 30 days after the sale (earliest first), then one
    average cost per share (the section 104 pool).
    https://www.gov.uk/hmrc-internal-manuals/capital-gains-manual/cg51560
    https://www.legislation.gov.uk/ukpga/1992/12/section/106A
  * ISA accounts are tax-free and left out of gains and dividends.
  * Amounts are converted to GBP at today's rate. For a GBP account that is
    exact. For a EUR account HMRC converts the cost at the rate on the buy
    date and the proceeds at the rate on the sale date (CG78310), so the gap
    is about cost x the change in EUR/GBP between the two dates (EUR 10,000
    of shares and a 3% move: about £260).
    https://www.gov.uk/hmrc-internal-manuals/capital-gains-manual/cg78310
  * UK_RESIDENT_FROM (YYYY-MM-DD, optional) starts the count later in your
    first UK tax year (split-year treatment). Shares bought before that date
    are not matched under the 30-day rule (TCGA 1992 s106A(5A)).

Allowances below are for 2026/27, checked on 2026-11-26 against the Budget
2026 rates annex (unchanged for 2026/27 and 2027/28):
https://www.gov.uk/government/publications/budget-2026-overview-of-tax-legislation-and-rates-ootlar/annex-a-rates-and-allowances
Update them if a Budget changes them. The tax year they are for is set in
rules.py (UK_TAX_YEAR); the card says so once a later tax year has started.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

from . import cache, connections, rules, sources
from .connectors import trading212
from .fx import from_eur

# https://www.gov.uk/capital-gains-tax/allowances
CGT_ANNUAL_EXEMPT_GBP = 3000
# https://www.gov.uk/tax-on-dividends
DIVIDEND_ALLOWANCE_GBP = 500
# https://www.gov.uk/individual-savings-accounts/how-isas-work
ISA_ALLOWANCE_GBP = 20000
# TCGA 1992 s106A(5): shares bought "within the period of thirty days after
# the disposal" are matched before the pool.
BED_AND_BREAKFAST_DAYS = 30
_EPS = 1e-9


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


def match_disposals(trades: list, accounts: dict[int, str] | None = None,
                    resident_from: date | None = None) -> list[dict]:
    """One event per sale, with the gain worked out by HMRC's matching rules
    for shares (helpsheet HS284, CG51560):

      1. same day (TCGA 1992 s105): the day's buys and sells of a share count
         as one buy and one sell, and are matched first;
      2. the next 30 days (s106A(5)): then shares bought in the 30 days after
         the sale, earliest first, but not shares bought while not UK
         resident (s106A(5A));
      3. the section 104 pool (s104): the rest at the average cost of the
         shares bought before and not matched above.

    `trades` are positions.Trade in one currency, for one person, outside
    ISAs. `accounts` ({id(trade): account name}) says which account each sale
    came from. A sale of shares with no recorded purchase counts only the
    matched part. Same shape as positions.realized_events():
    {when, key, name, quantity, proceeds, realized, currency, account}."""
    accounts = accounts or {}
    by_key: dict[str, dict[date, dict]] = {}
    for t in trades:
        d = _day(t.when)
        if d is None or t.quantity <= 0:
            continue
        day = by_key.setdefault(t.key, {}).setdefault(
            d, {"buy_qty": 0.0, "buy_cost": 0.0, "sells": [], "sell_qty": 0.0,
                "matched_qty": 0.0, "matched_cost": 0.0})
        if t.side == "BUY":
            day["buy_qty"] += t.quantity
            day["buy_cost"] += t.amount
        else:
            day["sells"].append(t)
            day["sell_qty"] += t.quantity

    def match(sale: dict, buys: dict, qty: float) -> None:
        """Match `qty` of a day's sales with `qty` of a day's buys."""
        cost = buys["buy_cost"] * qty / buys["buy_qty"]
        buys["buy_qty"] -= qty
        buys["buy_cost"] -= cost
        sale["matched_qty"] += qty
        sale["matched_cost"] += cost

    out: list[dict] = []
    for key, days in by_key.items():
        order = sorted(days)
        # 1. same day
        for d in order:
            x = days[d]
            q = min(x["buy_qty"], x["sell_qty"])
            if q > _EPS:
                match(x, x, q)
        # 2. buys in the 30 days after each sale, earlier sales first
        window = timedelta(days=BED_AND_BREAKFAST_DAYS)
        for d in order:
            x = days[d]
            for b in order:
                left = x["sell_qty"] - x["matched_qty"]
                if left <= _EPS:
                    break
                if not d < b <= d + window or (resident_from and b < resident_from):
                    continue
                q = min(days[b]["buy_qty"], left)
                if q > _EPS:
                    match(x, days[b], q)
        # 3. the section 104 pool, in date order (a day's sale before its
        #    leftover buys: both cannot be left after the same-day match)
        pool = {"buy_qty": 0.0, "buy_cost": 0.0}
        for d in order:
            x = days[d]
            q = min(x["sell_qty"] - x["matched_qty"], pool["buy_qty"])
            if q > _EPS:
                match(x, pool, q)
            pool["buy_qty"] += x["buy_qty"]
            pool["buy_cost"] += x["buy_cost"]
        # one event per sale: the day's matched cost split by quantity
        for d in order:
            x = days[d]
            if x["matched_qty"] <= _EPS:
                continue
            for t in x["sells"]:
                share = t.quantity / x["sell_qty"]
                qty = x["matched_qty"] * share
                proceeds = t.amount * qty / t.quantity
                out.append({"when": t.when, "key": key, "name": t.name, "quantity": qty,
                            "proceeds": proceeds,
                            "realized": proceeds - x["matched_cost"] * share,
                            "currency": t.currency, "account": accounts.get(id(t), "")})
    out.sort(key=lambda e: e["when"])
    return out


# Cached (backend/cache.py): recomputed after uploads/edits, otherwise
# fresh for 5 min and then refreshed in the background.
@cache.cached(ttl=300, stale=3600, copy=True)
def summary(today: date | None = None) -> dict:
    outdated = rules.uk_notice(today)      # the figures are for an earlier tax year
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

    # Matched per person across all taxable accounts: the section 104 pool and
    # the same-day and 30-day rules are per person, not per broker (HS284).
    pooled, owner = [], {}
    for name, trades in taxable.items():
        for t in sources.in_eur(trades):
            pooled.append(t)
            owner[id(t)] = name
    for ev in match_disposals(pooled, owner, resident):
        name = ev["account"]
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
    for x in trading212.get_dividends() + connections.get_dividends():
        d = _day(x.get("date", ""))
        if not x.get("isa") and d and since <= d <= end:
            div += _gbp(x["amount_eur"])
    for x in trading212.get_interest() + connections.get_interest():
        d = _day(x.get("date", ""))
        if not x.get("isa") and d and since <= d <= end:
            interest += _gbp(x["amount_eur"])

    # ISA subscriptions: deposits into ISA accounts this tax year. Only
    # Trading 212 deposits are read, so the allowance counts as tracked only
    # when every ISA account is a Trading 212 one.
    isa_paid = 0.0
    for x in trading212.get_transactions():
        d = _day(x.get("date", ""))
        if x.get("isa") and x.get("type") == "DEPOSIT" and d and start <= d <= end:
            isa_paid += _gbp(x["amount_eur"])
    has_isa = any(a["isa"] for a in accounts)
    try:
        t212_isas = {a.label for a in trading212.accounts() if a.is_isa}
    except Exception:
        t212_isas = set()
    every_isa_read = all(a["name"] in t212_isas for a in accounts if a["isa"])

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
            "tracked": has_isa and every_isa_read and trading212.is_live(),
            "paid_in_gbp": round(isa_paid, 2),
            "allowance_gbp": ISA_ALLOWANCE_GBP,
            "left_gbp": round(max(ISA_ALLOWANCE_GBP - isa_paid, 0), 2),
        },
        "recent_sales": recent,
        "rules_year": rules.UK_TAX_YEAR,
        "outdated": outdated,
        "method": (
            "Taxable accounts only (ISAs left out). Each sale is matched as HMRC does: shares "
            "bought the same day first, then shares bought in the next 30 days, then one average "
            "cost per share across all taxable accounts (section 104 pool). Amounts use today's "
            "GBP rate; HMRC uses the rate on each trade date. ISA allowance counts deposits into "
            "Trading 212 ISA accounts this tax year, not transfers. Facts, not a tax "
            "calculation — check your Self Assessment."
        ),
    }
