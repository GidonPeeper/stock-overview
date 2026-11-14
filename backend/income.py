"""Dividends and interest received — income, distinct from trading P/L.

Kept separate from realized P/L so "profit" stays unambiguous. Sources:
  * Trading 212 — dividends + interest from the API, per account; or the
    imported activity statement (which also carries the daily interest on cash)
  * DeGiro       — dividend / tax / interest rows in the statement CSV
  * Trade Republic — transcribed from the statement PDF (`data/income_*.json`)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from . import cache
from .connectors import trading212
from .datafiles import resolve as _resolve_data
from .fx import FX_TO_EUR, to_eur

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _num(s: str) -> float:
    return float(s.replace(".", "").replace(",", ".")) if s else 0.0


def _trading212() -> dict[str, dict]:
    """{account label: {dividends, interest}} — one entry per Trading 212 account."""
    out: dict[str, dict] = {label: {"dividends": 0.0, "interest": 0.0}
                            for label in trading212.trade_sources()}
    for x in trading212.get_dividends():
        out.setdefault(x["account"], {"dividends": 0.0, "interest": 0.0})["dividends"] += x["amount_eur"]
    for x in trading212.get_interest():
        out.setdefault(x["account"], {"dividends": 0.0, "interest": 0.0})["interest"] += x["amount_eur"]
    return {k: {"dividends": round(v["dividends"], 2), "interest": round(v["interest"], 2)}
            for k, v in out.items()}


def _degiro() -> dict:
    csv_file = _resolve_data("degiro_account.csv")[0]
    if not csv_file.exists():
        return {"dividends": 0.0, "interest": 0.0}
    dividends = interest = 0.0
    with csv_file.open(encoding="utf-8") as fh:
        reader = csv.reader(fh)
        next(reader, None)
        for row in reader:
            if len(row) < 9:
                continue
            desc = row[5].lower()
            eur = to_eur(_num(row[8]), row[7] if row[7] in FX_TO_EUR else "EUR")
            if desc.startswith("dividend"):          # includes Dividendbelasting (tax, negative)
                dividends += eur
            elif any(w in desc for w in ("rente", "interest", "securities lending")):
                interest += eur
    return {"dividends": round(dividends, 2), "interest": round(interest, 2)}


def _trade_republic() -> dict:
    f = _resolve_data("income_trade_republic.json")[0]
    if not f.exists():
        return {"dividends": 0.0, "interest": 0.0}
    rows = json.loads(f.read_text()).get("income", [])
    dividends = sum(r["amount_eur"] for r in rows if r["type"] == "dividend")
    interest = sum(r["amount_eur"] for r in rows if r["type"] == "interest")
    return {"dividends": round(dividends, 2), "interest": round(interest, 2)}


def by_year() -> dict[str, float]:
    """Dividends + interest received per calendar year (EUR), all brokers."""
    years: dict[str, float] = {}

    def add(year: str, amount: float):
        if year and len(year) == 4:
            years[year] = years.get(year, 0.0) + amount

    # DeGiro: statement rows dated dd-mm-yyyy
    csv_file = _resolve_data("degiro_account.csv")[0]
    if csv_file.exists():
        with csv_file.open(encoding="utf-8") as fh:
            reader = csv.reader(fh)
            next(reader, None)
            for row in reader:
                if len(row) < 9:
                    continue
                desc = row[5].lower()
                if desc.startswith("dividend") or any(
                        w in desc for w in ("rente", "interest", "securities lending")):
                    eur = to_eur(_num(row[8]), row[7] if row[7] in FX_TO_EUR else "EUR")
                    add(row[0][6:10], eur)

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
    return {
        "by_broker": by_broker,
        "dividends": dividends,
        "interest": interest,
        "total": round(dividends + interest, 2),
    }
