"""Trade Republic "Transaction export" CSV -> trades_/income_trade_republic.json.

Trade Republic's app exports every booking as one CSV row (Profile → Activity →
Export). Columns we use: date/datetime, category (TRADING / CASH / DELIVERY),
type (BUY, SELL, DIVIDEND, INTEREST_PAYMENT, TRANSFER_*, MIGRATION), name,
symbol (= ISIN), shares, amount, fee, tax — all cash figures in EUR.

Cash actually moved per row:
  BUY   |amount| + |fee|            (the statement debits price + €1 fee)
  SELL  amount - |fee|              (proceeds net of the €1 fee)
  DIVIDEND  amount + tax            (tax is negative: net credited)
  INTEREST_PAYMENT  amount
DELIVERY/MIGRATION rows (custodian move: shares out and back in) net to zero
and are ignored; transfers are reported as cash flows for the summary only.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import time
from collections import defaultdict

from ..instruments import NAMES, ticker_for

_REQUIRED = {"date", "category", "type", "symbol", "shares", "amount"}


def _f(s: str) -> float:
    return float(s) if s not in ("", None) else 0.0


def looks_like(text: str) -> bool:
    head = text[:400].lower()
    return "account_type" in head and "transaction_id" in head and "category" in head


def convert(source) -> tuple[dict, dict, dict]:
    """CSV (path or bytes/str) -> (trades_json, income_json, report)."""
    if isinstance(source, bytes):
        text = source.decode("utf-8-sig")
    elif isinstance(source, str) and "\n" not in source:
        text = open(source, encoding="utf-8-sig").read()
    else:
        text = source
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not _REQUIRED.issubset(reader.fieldnames):
        raise ValueError("Not a Trade Republic transaction export (missing columns: "
                         f"{sorted(_REQUIRED - set(reader.fieldnames or []))})")

    trades, income, flows = [], [], []
    cash = 0.0
    unknown_isins: set[str] = set()
    by_isin_qty: dict[str, float] = defaultdict(float)
    for r in reader:
        cat, typ = r["category"], r["type"]
        when = (r.get("datetime") or r["date"])[:16].replace("T", " ")
        isin = r.get("symbol", "")
        amount, fee, tax = _f(r["amount"]), _f(r.get("fee", "")), _f(r.get("tax", ""))
        if cat == "TRADING" and typ in ("BUY", "SELL"):
            qty = abs(_f(r["shares"]))
            eur = abs(amount) + abs(fee) if typ == "BUY" else amount - abs(fee)
            cash += -eur if typ == "BUY" else eur
            by_isin_qty[isin] += qty if typ == "BUY" else -qty
            ticker = ticker_for(isin)
            if not ticker and isin not in unknown_isins:
                unknown_isins.add(isin)
            trades.append({"date": when, "isin": isin, "name": NAMES.get(isin, r["name"]),
                           "ticker": ticker, "side": typ, "quantity": qty,
                           "price": _f(r.get("price", "")), "fee_eur": abs(fee),
                           "amount_eur": round(eur, 2)})
        elif cat == "CASH" and typ == "DIVIDEND":
            net = amount + tax
            cash += net
            income.append({"date": r["date"], "type": "dividend", "isin": isin,
                           "name": NAMES.get(isin, r["name"]), "amount_eur": round(net, 2),
                           "gross_eur": round(amount, 2), "tax_eur": round(tax, 2)})
        elif cat == "CASH" and typ == "INTEREST_PAYMENT":
            cash += amount
            income.append({"date": r["date"], "type": "interest", "amount_eur": round(amount, 2)})
        elif cat == "CASH" and typ.startswith("TRANSFER"):
            cash += amount
            flows.append({"date": r["date"], "amount_eur": round(amount, 2)})
        elif cat == "DELIVERY":
            continue  # custodian migration: net zero
        else:
            cash += amount + fee + tax
    trades.sort(key=lambda t: t["date"])
    income.sort(key=lambda t: t["date"])
    stamp = time.strftime("%Y-%m-%d %H:%M")
    trades_json = {"_source": "Trade Republic transaction export", "_generated": stamp,
                   "trades": trades}
    income_json = {"_source": "Trade Republic transaction export", "_generated": stamp,
                   "income": income}
    open_positions = {k: round(v, 6) for k, v in by_isin_qty.items() if abs(v) > 1e-6}
    report = {"trades": len(trades), "dividends": sum(1 for i in income if i["type"] == "dividend"),
              "interest_payments": sum(1 for i in income if i["type"] == "interest"),
              "deposits_eur": round(sum(f["amount_eur"] for f in flows if f["amount_eur"] > 0), 2),
              "withdrawals_eur": round(-sum(f["amount_eur"] for f in flows if f["amount_eur"] < 0), 2),
              "cash_balance_eur": round(cash, 2), "open_positions": open_positions,
              "unknown_isins": sorted(unknown_isins),
              "first_date": trades[0]["date"][:10] if trades else None,
              "last_date": max((t["date"][:10] for t in trades), default=None)}
    return trades_json, income_json, report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python -m backend.importers.traderepublic_csv <Transaction export.csv> [data_dir]")
    from pathlib import Path
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parents[2] / "data"
    t, i, rep = convert(sys.argv[1])
    (out_dir / "trades_trade_republic.json").write_text(json.dumps(t, indent=1, ensure_ascii=False))
    (out_dir / "income_trade_republic.json").write_text(json.dumps(i, indent=1, ensure_ascii=False))
    print(json.dumps(rep, indent=1))
