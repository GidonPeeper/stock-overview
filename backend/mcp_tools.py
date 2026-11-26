"""What Claude can read through the MCP server (backend/mcp_server.py).

Each function answers one kind of question about the dashboard's data and
returns a small dict, built from the same functions the web app's /api/*
endpoints use (so the numbers match the dashboard, and the caches are
shared). Everything here only reads: nothing changes data, uploads, connects
an account or contacts a broker for anything but reading.

Conventions, so a language model can read the results without the code:
  * `currency` says which currency the amounts are in (EUR, GBP or USD; the
    dashboard's default unless the caller picks one). Fields that are in
    another currency say so in their name (`price_currency`, `..._gbp`).
  * Dates are ISO (YYYY-MM-DD). Percentages are numbers, e.g. 12.5 = 12.5%.
  * `sample_data: true` means the app has no data of the user's own yet and
    shows its built-in demo portfolio. The numbers are then not the user's.

No `from __future__ import annotations` here: the MCP SDK reads these
signatures (Annotated + pydantic Field) to build each tool's input schema.
"""

import json
import re
from datetime import date, datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field

from . import cache, clock, datafiles, fx

SAMPLE_WARNING = ("SAMPLE DATA: the user has not added any accounts yet, so these are the "
                  "app's built-in demo numbers, not the user's own. Say so when you use them.")

Currency = Annotated[Literal["EUR", "GBP", "USD"] | None, Field(
    description="Currency for the amounts. Default: the dashboard's display currency.")]

_SYMBOL = {"EUR": "€", "GBP": "£", "USD": "$"}


class ToolInputError(ValueError):
    """A bad argument; the message is shown to the caller."""


# ------------------------------------------------------------------ helpers
def _app():
    """The web app module (imported on first use: it is big, and the stdio
    server must claim stdout before anything that might print is loaded)."""
    from . import main
    return main


def _ccy(currency: str | None) -> str:
    c = (currency or fx.display_currency()).upper()
    if c not in fx.DISPLAY_CURRENCIES:
        raise ToolInputError(f"currency must be one of {', '.join(fx.DISPLAY_CURRENCIES)}")
    return c


def _m(eur, ccy: str):
    """An EUR amount in `ccy`, rounded to cents (None stays None)."""
    if eur is None:
        return None
    return round(fx.from_eur(float(eur), ccy), 2)


def _pct(part, whole):
    return round(100.0 * part / whole, 1) if whole else None


def _now() -> str:
    return datetime.now(clock.tz()).isoformat(timespec="minutes")


def _epoch_iso(epoch) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(float(epoch), clock.tz()).isoformat(timespec="minutes")


def _date(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        raise ToolInputError(f"{name} must be a date like 2026-01-31") from None


def _money_text(text: str, ccy: str) -> str:
    """The app writes computed amounts in tip texts as {{1234.56}} (EUR);
    turn them into readable money in `ccy`."""
    def one(match):
        v = fx.from_eur(float(match.group(1)), ccy)
        return ("-" if v < 0 else "") + f"{_SYMBOL.get(ccy, '')}{abs(v):,.2f}"
    return re.sub(r"\{\{(-?[\d.]+)\}\}", one, text or "")


def envelope(body: dict, ccy: str | None = None) -> dict:
    """Every result starts with whether it is sample data (and the currency)."""
    demo = datafiles.demo()
    out: dict = {"sample_data": demo}
    if demo:
        out["warning"] = SAMPLE_WARNING
    if ccy:
        out["currency"] = ccy
    out.update(body)
    return out


def to_text(result: dict) -> str:
    """Compact JSON for the tool result (no indentation: fewer tokens)."""
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"), default=str)


# ------------------------------------------------------------- the tools
def get_portfolio_summary(currency: Currency = None) -> dict:
    """Headline numbers of the whole portfolio."""
    ccy = _ccy(currency)
    p = _app().portfolio()
    t, total = p["totals"], p["totals"]["value"]
    inc = p.get("income_by_broker") or {}
    accounts = [{"account": b["broker"], "value": _m(b["value"], ccy),
                 "weight_pct": _pct(b["value"], total),
                 "dividends": _m((inc.get(b["broker"]) or {}).get("dividends", 0.0), ccy),
                 "interest": _m((inc.get(b["broker"]) or {}).get("interest", 0.0), ccy)}
                for b in p["by_broker"]]
    return envelope({
        "as_of": _now(),
        "quotes_as_of": _epoch_iso(p["quotes"].get("as_of")),
        "value": _m(total, ccy),
        "cost": _m(t["cost"], ccy),
        "unrealized_gain": _m(t["unrealized"], ccy),
        "unrealized_gain_pct": t["unrealized_pct"],
        "realized_gain": _m(t["realized"], ccy),
        "dividends": _m(t["dividends"], ccy),
        "interest": _m(t["interest"], ccy),
        "fees": _m(t["fees"], ccy),
        "total_return": _m(t["total_return"], ccy),
        "today_change": _m(t["daily"], ccy),
        "today_change_pct": t["daily_pct"],
        "holdings_count": len(p["holdings"]),
        "accounts": accounts,
        "sectors": [{"sector": s["sector"], "value": _m(s["value"], ccy),
                     "weight_pct": _pct(s["value"], total)} for s in p["by_sector"]],
        "top_holdings": [{"name": h["name"], "ticker": h["ticker"], "value": _m(h["value_eur"], ccy),
                          "weight_pct": _pct(h["value_eur"], total),
                          "unrealized_gain_pct": h["profit_pct"]} for h in p["holdings"][:5]],
        "definitions": {
            "total_return": "unrealized + realized gains + dividends + interest, after fees "
                            "charged outside trades (fees is negative)",
            "today_change": f"since midnight ({clock.tz().key}); 0 until markets open today",
        },
    }, ccy)


_SORTS = {"value": "value", "gain": "unrealized_gain", "gain_pct": "unrealized_gain_pct",
          "today": "today_change"}


def list_holdings(
        account: Annotated[str | None, Field(
            description="Only this account, e.g. 'DeGiro' or 'T212 ISA' (case-insensitive, part of "
                        "the name is enough). Positions are then per account instead of merged.")] = None,
        search: Annotated[str | None, Field(
            description="Only holdings whose name, ticker or ISIN contains this text.")] = None,
        sort_by: Annotated[Literal["value", "gain", "gain_pct", "today"], Field(
            description="value = position size; gain / gain_pct = unrealized gain; "
                        "today = today's change.")] = "value",
        ascending: Annotated[bool, Field(
            description="Smallest first (e.g. sort_by='gain' + ascending=true = biggest losers).")] = False,
        limit: Annotated[int, Field(ge=1, le=200, description="Maximum number of holdings.")] = 20,
        currency: Currency = None) -> dict:
    """Open positions, largest first, optionally filtered."""
    ccy = _ccy(currency)
    main = _app()
    p = main.portfolio()
    total = p["totals"]["value"]
    if account:
        rows, *_ = main._open_holdings_rows()
        names = sorted({r["broker"] for r in rows})
        want = account.strip().lower()
        chosen = [n for n in names if n.lower() == want] or [n for n in names if want in n.lower()]
        if not chosen:
            return envelope({"holdings": [], "total_matches": 0,
                             "message": f"No account matches {account!r}.",
                             "available_accounts": names}, ccy)
        items = [{
            "name": r["name"], "ticker": r["ticker"], "isin": r["isin"], "account": r["broker"],
            "tax_wrapper": r.get("wrapper") or None, "quantity": r["quantity"],
            "price": r["price"], "price_currency": r["currency"],
            "value": r["value_eur"], "cost": r["cost_eur"], "unrealized_gain": r["profit_eur"],
            "unrealized_gain_pct": r["profit_pct"], "today_change": r["daily_eur"],
            "price_is_live": r["priced_live"]} for r in rows if r["broker"] in chosen]
    else:
        items = [{
            "name": h["name"], "ticker": h["ticker"], "isin": h["isin"], "sector": h["sector"],
            "accounts": h["brokers"], "quantity": h["quantity"],
            "price": h["price"], "price_currency": h["currency"],
            "value": h["value_eur"], "cost": round(h["value_eur"] - h["profit_eur"], 2),
            "unrealized_gain": h["profit_eur"], "unrealized_gain_pct": h["profit_pct"],
            "today_change": h["daily_eur"], "today_change_pct": h["daily_pct"],
            "price_is_live": h["priced_live"]} for h in p["holdings"]]
    if search:
        s = search.strip().lower()
        items = [i for i in items if s in f"{i['name']} {i['ticker']} {i['isin']}".lower()]
    key = _SORTS[sort_by]
    items.sort(key=lambda i: (i[key] is None, (i[key] or 0) * (1 if ascending else -1)))
    matched_value = sum(i["value"] for i in items)
    for i in items:
        i["weight_pct"] = _pct(i["value"], total)
        for f in ("value", "cost", "unrealized_gain", "today_change"):
            i[f] = _m(i[f], ccy)
    return envelope({
        "total_matches": len(items),
        "shown": min(len(items), limit),
        "matches_value": _m(matched_value, ccy),
        "matches_weight_pct": _pct(matched_value, total),
        "holdings": items[:limit],
        "note": "weight_pct is the share of the whole portfolio. price is per share in "
                "price_currency; price_is_live=false means the last known or purchase price.",
    }, ccy)


_PERIOD = Literal["today", "week", "month", "year", "last_30_days", "last_12_months", "all"]


def _thin(points: list, n: int) -> list:
    if len(points) <= n:
        return points
    step = (len(points) - 1) / (n - 1)
    return [points[i] for i in sorted({round(k * step) for k in range(n)})]


def _time_weighted_pct(points: list[dict]) -> float | None:
    """Return over the points with deposits and withdrawals taken out: each
    step's gain (change in total profit) divided by the value at its start,
    compounded (the app's analytics use the same rule)."""
    growth, used = 1.0, False
    for a, b in zip(points, points[1:]):
        if a["total_value"] > 500:
            r = (b["total_profit"] - a["total_profit"]) / a["total_value"]
            growth *= 1 + max(-0.4, min(r, 0.4))
            used = True
    return round((growth - 1) * 100, 2) if used else None


def get_performance(
        period: Annotated[_PERIOD, Field(
            description="Calendar periods count from midnight, Monday, the 1st or 1 January. "
                        "Ignored when start_date is given.")] = "month",
        start_date: Annotated[str | None, Field(description="Custom range start, YYYY-MM-DD.")] = None,
        end_date: Annotated[str | None, Field(description="Custom range end, YYYY-MM-DD (default today).")] = None,
        points: Annotated[int, Field(ge=2, le=200, description="Number of chart points to return.")] = 20,
        currency: Currency = None) -> dict:
    """How the portfolio did over a period: gain, return %, value change and a
    thinned value/gain series."""
    ccy = _ccy(currency)
    main = _app()
    today = clock.today()
    start, end = _date(start_date, "start_date"), _date(end_date, "end_date") or today
    if start is None:
        starts = clock.period_starts(today)
        start = {"today": today, "week": starts["week"], "month": starts["month"],
                 "year": starts["year"], "last_30_days": today - timedelta(days=30),
                 "last_12_months": today - timedelta(days=365), "all": None}[period]
        label = period
    else:
        label = "custom"
    if start and start > end:
        raise ToolInputError("start_date is after end_date")

    hist = sorted(main._history(), key=lambda h: h["day"])
    s, e = start.isoformat() if start else "", end.isoformat()
    inside = [h for h in hist if h["day"] >= s and h["day"] <= e]
    before = [h for h in hist if s and h["day"] < s]
    if not inside:
        return envelope({"period": label, "message": "No portfolio history in that range.",
                         "history_starts": hist[0]["day"] if hist else None,
                         "history_ends": hist[-1]["day"] if hist else None}, ccy)
    base = before[-1] if before else inside[0]
    chain = ([base] if before else []) + inside
    last = chain[-1]
    gain = last["total_profit"] - base["total_profit"]
    value_change = last["total_value"] - base["total_value"]
    high = max(chain, key=lambda h: h["total_value"])
    low = min(chain, key=lambda h: h["total_value"])
    out = {
        "period": label,
        "start_date": start.isoformat() if start else hist[0]["day"],
        "end_date": last["day"],
        "measured_from": base["day"],
        "start_value": _m(base["total_value"], ccy),
        "end_value": _m(last["total_value"], ccy),
        "gain": _m(gain, ccy),
        "return_pct": _time_weighted_pct(chain),
        "value_change": _m(value_change, ccy),
        "net_money_added": _m(value_change - gain, ccy),
        "highest": {"date": high["day"], "value": _m(high["total_value"], ccy)},
        "lowest": {"date": low["day"], "value": _m(low["total_value"], ccy)},
        "series": [{"date": h["day"], "value": _m(h["total_value"], ccy),
                    "gain_since_start": _m(h["total_profit"] - base["total_profit"], ccy)}
                   for h in _thin(chain, points)],
        "definitions": {
            "gain": "price gains (realized and unrealized), dividends and interest, after fees; "
                    "money added or withdrawn is not a gain",
            "measured_from": "the close of the day before the range starts (the first day of "
                             "history if it starts later)",
            "return_pct": "time-weighted: deposits and withdrawals do not count",
            "net_money_added": "deposits minus withdrawals and sales taken out, approximately",
        },
    }
    if label in ("today", "week", "month", "year") and not start_date:
        prof = main.profit()
        move = (prof.get("periods") or {}).get(label)
        out["dashboard_card"] = {
            "change": _m(move, ccy),
            "since": (prof.get("starts") or {}).get(label, today.isoformat()),
            "coverage_pct": prof.get("coverage_pct"),
            "not_covered": prof.get("excluded") or [],
            "note": "the figure on the dashboard's period card: the price move of the holdings "
                    "you own now, at today's quantities. It differs from `gain` when you bought "
                    "or sold during the period.",
        }
    return envelope(out, ccy)


def get_realized_gains(
        limit: Annotated[int, Field(ge=1, le=200, description="Maximum closed positions to list.")] = 25,
        ascending: Annotated[bool, Field(description="Biggest losses first.")] = False,
        currency: Currency = None) -> dict:
    """Closed positions and realized profit, per account and per year."""
    ccy = _ccy(currency)
    main = _app()
    c = main.closed()
    rows = sorted(c["closed"], key=lambda r: r["realized_eur"], reverse=not ascending)
    years = main.annual_report().get("years", [])
    return envelope({
        "total_realized": _m(c["total_realized_eur"], ccy),
        "from_closed_positions": _m(c["closed_total_eur"], ccy),
        "from_partial_sales": _m(c["trims_total_eur"], ccy),
        "by_account": [{"account": k, "realized": _m(v, ccy)} for k, v in c["by_broker"].items()],
        "by_year": [{"year": y["year"], "realized": _m(y["realized_eur"], ccy)} for y in years
                    if y.get("realized_eur")],
        "closed_positions_count": len(rows),
        "closed_positions": [{"name": r["name"], "account": r["broker"],
                              "realized": _m(r["realized_eur"], ccy)} for r in rows[:limit]],
        "note": "Realized = sale proceeds minus average cost, fees included. Partial sales are "
                "positions you still hold part of. by_year uses one average cost per account.",
    }, ccy)


def _income_by_year() -> dict[str, dict[str, float]]:
    """{year: {dividends, interest, fees}} in EUR, from the same sources as
    income.summary() (DeGiro statement, Trade Republic file, Trading 212,
    accounts added in Settings)."""
    from . import connections, income
    from .connectors import trading212

    years: dict[str, dict[str, float]] = {}

    def add(day, kind: str, amount) -> None:
        y = str(day or "")[:4]
        if len(y) == 4 and y.isdigit():
            row = years.setdefault(y, {"dividends": 0.0, "interest": 0.0, "fees": 0.0})
            row[kind] += float(amount or 0)

    for r in income._degiro_rows():
        add(r["date"], r["kind"], r["amount_eur"])
    path, _ = datafiles.resolve("income_trade_republic.json")
    if path.exists():
        for r in json.loads(path.read_text()).get("income", []):
            add(r.get("date"), "dividends" if r.get("type") == "dividend" else "interest",
                r.get("amount_eur"))
    for kind, rows in (("dividends", trading212.get_dividends()), ("interest", trading212.get_interest()),
                       ("fees", trading212.get_fees())):
        for x in rows:
            add(x.get("date"), kind, x.get("amount_eur"))
    try:
        for kind, rows in (("dividends", connections.get_dividends()),
                           ("interest", connections.get_interest())):
            for x in rows:
                add(x.get("date"), kind, x.get("amount_eur"))
    except Exception:
        pass
    return dict(sorted(years.items()))


def get_income(currency: Currency = None) -> dict:
    """Dividends and interest received (per year and per account) and the
    expected dividends for the next 12 months."""
    ccy = _ccy(currency)
    from . import income
    main = _app()
    s = income.summary()
    years = _income_by_year()
    this_year = years.get(str(clock.today().year), {"dividends": 0.0, "interest": 0.0, "fees": 0.0})
    out = {
        "received_all_time": {"dividends": _m(s["dividends"], ccy), "interest": _m(s["interest"], ccy),
                              "total": _m(s["total"], ccy), "fees": _m(s["fees"], ccy)},
        "this_calendar_year": {k: _m(v, ccy) for k, v in this_year.items()},
        "by_year": [{"year": y, **{k: _m(v, ccy) for k, v in row.items()}} for y, row in years.items()],
        "by_account": [{"account": k, **{f: _m(v.get(f, 0.0), ccy) for f in ("dividends", "interest", "fees")}}
                       for k, v in s["by_broker"].items()],
        "note": "Dividends are after any tax withheld at source. fees = charged outside trades "
                "(negative). For the UK tax year (from 6 April) use get_tax_overview.",
    }
    try:
        proj = main.income_projection()
        out["next_12_months_dividends"] = {
            "expected": _m(proj.get("projected_annual_eur"), ccy),
            "per_month": _m(proj.get("projected_monthly_eur"), ccy),
            "top_payers": [{"name": h["name"], "ticker": h["ticker"], "yield_pct": h["yield_pct"],
                            "per_year": _m(h["annual_eur"], ccy)} for h in proj.get("holdings", [])[:10]],
            "method": proj.get("method"),
        }
    except Exception as exc:  # market data unavailable: the rest still answers
        out["next_12_months_dividends"] = {"error": f"projection unavailable ({type(exc).__name__})"}
    return envelope(out, ccy)


def get_net_worth(currency: Currency = None) -> dict:
    """Investments, cash accounts, things owned and loans, the net worth they
    add up to, and the monthly budget entered in Settings."""
    ccy = _ccy(currency)
    main = _app()
    invest = main.portfolio()["totals"]["value"]
    cash = main.cash_get()
    fin = main.finances_get()
    t = fin.get("totals", {})
    cash_total, debt, owned = cash.get("total_eur") or 0.0, t.get("debt_eur", 0.0), t.get("assets_eur", 0.0)
    out = {
        "net_worth": _m(invest + cash_total + owned - debt, ccy),
        "investments": _m(invest, ccy),
        "cash_and_savings": _m(cash_total, ccy),
        "things_owned": _m(owned, ccy),
        "debt": _m(debt, ccy),
        "formula": "net_worth = investments + cash_and_savings + things_owned - debt",
        "cash_by_type": {k: _m(v, ccy) for k, v in (cash.get("by_type") or {}).items()},
        "cash_accounts": [{"name": a["name"], "institution": a.get("institution") or None,
                           "type": a.get("type"), "balance": _m(a["balance_eur"], ccy),
                           "account_currency": a.get("currency"),
                           "interest_rate_pct": a.get("rate") or 0.0} for a in cash.get("accounts", [])],
        "loans": [{"name": l["name"], "balance": _m(l["balance_eur"], ccy),
                   "interest_rate_pct": l.get("rate"), "monthly_payment": _m(l["monthly_payment_eur"], ccy),
                   "paid_off_by": l.get("payoff")} for l in fin.get("loans", [])],
        "things_owned_list": [{"name": a.get("name"), "category": a.get("category"),
                               "value": _m(a.get("value_eur"), ccy),
                               "change_since_bought": _m(a.get("change_eur"), ccy)}
                              for a in fin.get("assets", [])],
        "monthly_budget": {
            "income_after_tax": _m(t.get("monthly_income"), ccy),
            "payments": _m(t.get("monthly_payments"), ccy),
            "left_over": _m(t.get("free_cashflow"), ccy),
            "income": [{"name": i["name"], "after_tax": _m(i.get("net_monthly_eur"), ccy)}
                       for i in fin.get("income", [])],
            "expenses": [{"name": x["name"], "amount": _m(x.get("monthly_eur"), ccy),
                          "fixed": bool(x.get("fixed"))} for x in fin.get("expenses", [])],
        },
    }
    fr = fin.get("freedom")
    if fr:
        out["financial_independence"] = {
            "target": _m(fr.get("target_eur"), ccy), "progress_pct": fr.get("progress_pct"),
            "expected_month": fr.get("eta_ym"), "runway_months": fr.get("runway_months"),
            "method": fr.get("method")}
    return envelope(out, ccy)


def get_risk_analysis(currency: Currency = None) -> dict:
    """Risk and diversification: volatility, beta, Sharpe, drawdown,
    concentration, currency and sector exposure, a health score and simple
    what-if scenarios."""
    ccy = _ccy(currency)
    main = _app()
    a = main.analytics_endpoint()
    h = main.health()
    p = main.portfolio()
    total = p["totals"]["value"]
    stress = main.stress()
    return envelope({
        "metrics": {
            "months_of_history": a.get("months"),
            "volatility_pct": a.get("volatility_pct"),
            "beta_vs_sp500": a.get("beta"),
            "sharpe_ratio": a.get("sharpe"),
            "max_drawdown_pct": a.get("max_drawdown_pct"),
            "top3_weight_pct": a.get("top3_pct"),
            "effective_positions": a.get("effective_positions"),
        },
        "metric_definitions": "From monthly time-weighted returns (deposits do not count): "
                              "volatility = annualised standard deviation; beta = vs S&P 500; "
                              "Sharpe uses a 2% risk-free rate; effective positions = 1/HHI. "
                              "Needs 6+ months of history, otherwise left out.",
        "currency_exposure": a.get("currency_exposure", []),
        "sector_exposure": [{"sector": s["sector"], "weight_pct": _pct(s["value"], total)}
                            for s in p["by_sector"]],
        "largest_positions": [{"name": x["name"], "weight_pct": _pct(x["value_eur"], total)}
                              for x in p["holdings"][:5]],
        "health_score": {"score": h.get("score"), "grade": h.get("grade"),
                         "components": [{"name": c["name"], "points": c["points"], "max": c["max"],
                                         "detail": c.get("detail")} for c in h.get("components", [])],
                         "method": h.get("method")},
        "what_if": [{"scenario": s["name"], "detail": s["detail"], "impact": _m(s["impact_eur"], ccy)}
                    for s in stress.get("scenarios", [])],
        "what_if_method": stress.get("method"),
    }, ccy)


def _tip(t: dict, ccy: str) -> dict:
    return {
        "title": _money_text(t.get("title", ""), ccy),
        "saving": _m(t.get("saving_eur"), ccy),
        "per": t.get("period"),
        "deadline": t.get("deadline"),
        "explanation": _money_text(t.get("explanation", ""), ccy),
        "how_worked_out": _money_text(t.get("how", ""), ccy),
        "what_to_do": _money_text(t.get("action", ""), ccy),
        "details": [f"{i.get('label')}: {_money_text(i.get('detail', ''), ccy)}" for i in t.get("items") or []],
    }


def get_tax_overview(currency: Currency = None) -> dict:
    """What the app already works out for tax: the UK tax-year card, positions
    that could be sold for a tax loss, and money-saving tips."""
    ccy = _ccy(currency)
    from .harvest import residence
    main = _app()
    res = residence()
    out: dict = {"tax_residence": res,
                 "disclaimer": "Facts and estimates from the app, not tax advice. Check with the "
                               "tax authority or a tax adviser before acting."}
    uk = main.uk_tax()
    if uk.get("applies", True):
        out["uk_tax_year"] = {k: v for k, v in uk.items() if k != "applies"}
    else:
        out["uk_tax_year"] = {"applies": False,
                              "note": "Only shown for UK tax residents (TAX_RESIDENCE)."}
    hv = main.harvest_endpoint()
    harvest = {"applies": hv.get("applies"), "summary": hv.get("headline") or hv.get("message"),
               "amounts_currency": hv.get("currency")}
    if hv.get("applies"):
        harvest.update({
            "tax_year": hv.get("tax_year"), "sell_by": hv.get("sell_by"),
            "can_rebuy_from": hv.get("rebuy_from"), "tax_rate_pct": hv.get("rate_pct"),
            "thresholds": hv.get("thresholds"),
            "candidates": [{
                "name": c["name"], "ticker": c["ticker"], "account": c["account"],
                "value": c["value"], "loss": c["loss"], "loss_pct": c["loss_pct"],
                "tax_saved_this_year": c.get("saved_now"), "loss_carried_forward": c.get("carried_forward"),
                "effect": c.get("effect"), "warning": c.get("warning"),
                "ways_to_stay_invested": [o.get("name") or o.get("ticker") or o.get("kind")
                                          for o in (c.get("options") or [])[:3]],
            } for c in hv.get("candidates", [])],
            "totals": hv.get("totals"), "tip": hv.get("tip"), "notes": hv.get("notes") or [],
            "how_it_works": hv.get("how_it_works") or [],
        })
    out["tax_loss_harvesting"] = harvest
    sv = main.savings()
    out["ways_to_save"] = {
        "currency": ccy,
        "total_per_year": _m(sv.get("total_per_year_eur"), ccy),
        "total_one_off": _m(sv.get("total_once_eur"), ccy),
        "tips": [_tip(t, ccy) for t in sv.get("tips", [])],
        "missing_data": sv.get("prompts") or [],
        "checked_and_fine": sv.get("all_clear") or [],
        "assumptions": sv.get("assumptions"),
        "rates_checked": sv.get("rates_checked"),
        "note": sv.get("note"),
        "disclaimer": sv.get("disclaimer"),
    }
    return envelope(out)


def get_data_status() -> dict:
    """Which accounts feed the dashboard, whether it shows sample data, and
    how fresh the prices are."""
    from . import connections, prices, sources, trend
    from .connectors import trading212
    from .harvest import residence

    demo = datafiles.demo()
    st = datafiles.status()
    files = [("degiro", "DeGiro account statement"), ("trade_republic", "Trade Republic transactions"),
             ("tr_income", "Trade Republic dividends and interest"),
             ("t212_statement", "Trading 212 activity statement")]
    out: dict = {
        "statements": [{"source": label, "added": bool(st[k]["real"])} for k, label in files],
        "trading212_api_accounts": [
            {k: v for k, v in a.items() if k in ("label", "isa", "ok", "error", "note", "positions")}
            for a in trading212.status()],
        "connected_accounts": [{
            "account": c.get("label"), "institution": c.get("institution_name"),
            "method": c.get("method"), "status": c.get("status"), "last_sync": c.get("last_sync"),
            "error": c.get("error") or None, "access_expires": c.get("consent_expires"),
        } for c in connections.load_all()],
    }
    tickers = [h.ticker for h in sources.holdings() if h.ticker]
    q = prices.quotes_status(tickers)
    out["quotes"] = {"oldest_quote": _epoch_iso(q.get("as_of")), "refreshing": q.get("refreshing"),
                     "note": None if q.get("as_of") else "not fetched yet in this process; "
                                                         "they load on the first portfolio question"}
    tb = trend.last_build or {}
    out["performance_history"] = {"points": tb.get("points"),
                                  "holdings_without_price_history": tb.get("unpriced") or []}
    out["settings"] = {"display_currency": fx.display_currency(), "tax_residence": residence(),
                       "time_zone": clock.tz().key, "today": clock.today().isoformat()}
    out["access"] = "read-only: these tools cannot trade, move money or change any data"
    if demo:
        out["how_to_add_data"] = ("Open the dashboard's Settings page and use 'Add an account' "
                                  "(broker key, bank link, statement file or a typed balance).")
    return envelope(out)


def get_watchlist() -> dict:
    """Shares on the watchlist with price, today's change and the change over
    about a month."""
    data = _app().watchlist_get()
    items = []
    for t in data.get("tickers", []):
        spark = [x for x in (t.get("spark") or []) if x]
        items.append({"ticker": t["ticker"], "name": t.get("name"), "price": t.get("price"),
                      "today_change_pct": t.get("daily_pct"),
                      "about_1_month_change_pct": round((spark[-1] / spark[0] - 1) * 100, 1)
                      if len(spark) > 1 else None})
    return envelope({"watchlist": items,
                     "note": "prices are in each share's own trading currency"})


TOOLS = [get_portfolio_summary, list_holdings, get_performance, get_realized_gains, get_income,
         get_net_worth, get_risk_analysis, get_tax_overview, get_data_status, get_watchlist]


def touch() -> None:
    """A tool call counts as using the app (keeps the web app's warmer going)."""
    cache.touch()
