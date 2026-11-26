"""Dividends and interest received, and fees charged outside trades.

Kept separate from realized P/L so each part of the total return is visible.
Sources:
  * Trading 212 — dividends + interest from the API, per account; or the
    imported activity statement (which also carries the daily interest on cash,
    sign-up bonuses and currency-conversion fees on deposits)
  * DeGiro       — dividend / withholding tax / interest / securities lending
    rows in the statement CSV, and fees booked outside orders (exchange
    connection fees, ADR custody fees, French transaction tax)
  * Trade Republic — transcribed from the statement PDF (`data/income_*.json`)

Fees inside an order (commission, FX fee, stamp duty) are already in the
trade's cash amount, so in realized and unrealized P/L, not here.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from . import cache, degiro_lang
from .connectors import trading212
from .datafiles import resolve as _resolve_data
from .fx import FX_TO_EUR, to_eur

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _zero() -> dict:
    return {"dividends": 0.0, "interest": 0.0, "fees": 0.0}


def _trading212() -> dict[str, dict]:
    """{account label: {dividends, interest, fees}} — one entry per Trading 212 account."""
    out: dict[str, dict] = {label: _zero() for label in trading212.trade_sources()}
    for kind, rows in (("dividends", trading212.get_dividends()),
                       ("interest", trading212.get_interest()),
                       ("fees", trading212.get_fees())):
        for x in rows:
            out.setdefault(x["account"], _zero())[kind] += x["amount_eur"]
    return {k: {f: round(v, 2) for f, v in d.items()} for k, d in out.items()}


def _degiro_kind(desc: str) -> str | None:
    """DeGiro statement rows outside orders: what each one is, in any statement
    language (wording in backend/degiro_lang.py). Dividends include the
    withholding tax (Dividendbelasting, Dividend Tax: negative)."""
    return degiro_lang.income_kind(desc)


def _degiro_rows() -> list[dict]:
    """[{date, kind, amount_eur}] for the income and fee rows."""
    csv_file = _resolve_data("degiro_account.csv")[0]
    if not csv_file.exists():
        return []
    out = []
    with csv_file.open(encoding="utf-8") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        rows = list(reader)
    num = degiro_lang.Numbers(rows)         # "1234,56" or "1234.56", per statement
    for row in rows:
        if len(row) < 9 or (len(row) > 11 and row[11]):   # order rows are trades
            continue
        kind = _degiro_kind(row[5])
        if kind:
            eur = to_eur(num.amount(row[8]), row[7] if row[7] in FX_TO_EUR else "EUR")
            out.append({"date": f"{row[0][6:10]}-{row[0][3:5]}-{row[0][0:2]}",
                        "kind": kind, "amount_eur": eur})
    return out


def _degiro() -> dict:
    out = _zero()
    for r in _degiro_rows():
        out[r["kind"]] += r["amount_eur"]
    return {k: round(v, 2) for k, v in out.items()}


def _trade_republic() -> dict:
    f = _resolve_data("income_trade_republic.json")[0]
    if not f.exists():
        return _zero()
    rows = json.loads(f.read_text()).get("income", [])
    dividends = sum(r["amount_eur"] for r in rows if r["type"] == "dividend")
    interest = sum(r["amount_eur"] for r in rows if r["type"] == "interest")
    return {"dividends": round(dividends, 2), "interest": round(interest, 2), "fees": 0.0}


def events() -> list[tuple[str, float]]:
    """(YYYY-MM-DD, EUR) for every dividend, interest payment and fee, all
    accounts — the same items as summary(), dated (for the Performance chart)."""
    out: list[tuple[str, float]] = [(r["date"], r["amount_eur"]) for r in _degiro_rows()]
    f = _resolve_data("income_trade_republic.json")[0]
    if f.exists():
        out += [(str(r.get("date", ""))[:10], float(r.get("amount_eur", 0)))
                for r in json.loads(f.read_text()).get("income", [])
                if r.get("type") in ("dividend", "interest")]
    for x in (trading212.get_dividends() + trading212.get_interest() + trading212.get_fees()
              + _connections_income()):
        out.append((str(x.get("date", ""))[:10], float(x["amount_eur"])))
    return sorted(out)


def by_year() -> dict[str, float]:
    """Dividends + interest received per calendar year (EUR), all brokers
    (fees not included)."""
    years: dict[str, float] = {}

    def add(year: str, amount: float):
        if year and len(year) == 4:
            years[year] = years.get(year, 0.0) + amount

    for r in _degiro_rows():
        if r["kind"] != "fees":
            add(r["date"][:4], r["amount_eur"])

    # Trade Republic: transcribed income file
    f = _resolve_data("income_trade_republic.json")[0]
    if f.exists():
        for r in json.loads(f.read_text()).get("income", []):
            add(str(r.get("date", ""))[:4], float(r.get("amount_eur", 0)))

    # Trading 212: dividends + interest, every account; then accounts added in Settings
    for x in trading212.get_dividends() + trading212.get_interest() + _connections_income():
        add(str(x.get("date", ""))[:4], float(x["amount_eur"]))

    return years


def _connections_income() -> list[dict]:
    """Dividends + interest from accounts added in Settings (e.g. IBKR)."""
    try:
        from . import connections
        return connections.get_dividends() + connections.get_interest()
    except Exception:
        return []


def _connections_by_account() -> dict[str, dict]:
    try:
        from . import connections
        return connections.income_by_account()
    except Exception:
        return {}


# Cached (backend/cache.py): recomputed after uploads/edits, otherwise
# fresh for 5 min and then refreshed in the background.
@cache.cached(ttl=300, stale=3600, copy=True)
def summary() -> dict:
    by_broker = {
        **_trading212(),
        "DeGiro": _degiro(),
        "Trade Republic": _trade_republic(),
        **_connections_by_account(),
    }
    dividends = round(sum(b["dividends"] for b in by_broker.values()), 2)
    interest = round(sum(b["interest"] for b in by_broker.values()), 2)
    fees = round(sum(b.get("fees", 0.0) for b in by_broker.values()), 2)
    return {
        "by_broker": by_broker,
        "dividends": dividends,
        "interest": interest,
        "total": round(dividends + interest, 2),   # income received
        "fees": fees,                              # charged outside trades (negative)
    }
