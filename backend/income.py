"""Dividends and interest received — income, distinct from trading P/L.

Kept separate from realized P/L so "profit" stays unambiguous. Sources:
  * Trading 212 — dividends API (interest, if any, would show in transactions)
  * DeGiro       — dividend / tax / interest rows in the statement CSV
  * Trade Republic — transcribed from the statement PDF (`data/income_*.json`)
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .connectors import trading212
from .datafiles import resolve as _resolve_data
from .fx import FX_TO_EUR, to_eur

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def _num(s: str) -> float:
    return float(s.replace(".", "").replace(",", ".")) if s else 0.0


def _trading212() -> dict:
    items = trading212.get_dividends()
    dividends = sum(float(x.get("amountInEuro") or x.get("amount") or 0) for x in items)
    return {"dividends": round(dividends, 2), "interest": 0.0}


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


def summary() -> dict:
    by_broker = {
        "Trading212": _trading212(),
        "DeGiro": _degiro(),
        "Trade Republic": _trade_republic(),
    }
    dividends = round(sum(b["dividends"] for b in by_broker.values()), 2)
    interest = round(sum(b["interest"] for b in by_broker.values()), 2)
    return {
        "by_broker": by_broker,
        "dividends": dividends,
        "interest": interest,
        "total": round(dividends + interest, 2),
    }
