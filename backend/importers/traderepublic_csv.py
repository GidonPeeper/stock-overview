"""Trade Republic "Transaction export" CSV -> trades_/income_trade_republic.json.

Trade Republic's app exports every booking as one CSV row (Profile → Account
statements → Transaction export; comma- or semicolon-separated). Columns we
use: datetime/date, category (TRADING / CASH / CORPORATE_ACTION / DELIVERY),
type, asset_class, name, symbol (ISIN, or the coin for crypto), shares,
amount, fee, tax -- all cash figures in EUR. amount, fee and tax are separate
parts of the cash moved: cash = amount + fee + tax (fee and withheld tax are
negative), as Portfolio Performance's Trade Republic importer reads them.

  BUY (and PRIVATE_MARKET_BUY)     cost = cash paid: |amount + fee + tax|
  SELL (and PRIVATE_MARKET_SELL,   proceeds = amount + fee: net of the fee,
  bond FINAL_MATURITY, TILG)       before tax withheld (as the live sync)
  DIVIDEND, DISTRIBUTION,          net = amount + fee + tax
  DIVIDEND_EQUIVALENT_PAYMENT
  INTEREST_PAYMENT                 net = amount + fee + tax
  BENEFITS_SAVEBACK                a bonus, counted with interest (as the live sync)
  CORPORATE_ACTION SPLIT           the shares added, at no cost
DELIVERY rows (custodian move: shares out and back in) normally net to zero
and are ignored. Shares that move in a way this module does not read are
reported in `warnings`, as the holdings would be wrong. Transfers are cash
flows for the summary only.
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
_DIVIDENDS = {"DIVIDEND", "DISTRIBUTION", "DIVIDEND_EQUIVALENT_PAYMENT"}
# CASH rows that also move shares: private-market funds, bonds and warrants paid back
_CASH_TRADES = {"PRIVATE_MARKET_BUY": "BUY", "PRIVATE_MARKET_SELL": "SELL",
                "FINAL_MATURITY": "SELL", "TILG": "SELL"}
# CORPORATE_ACTION rows whose money and shares are in one of those CASH rows
_INFO_ACTIONS = {"REDEMPTION", "FINAL_MATURITY", "WARRANT_EXERCISE"}


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
    text = text.lstrip("﻿")
    header = text.split("\n", 1)[0]
    delimiter = ";" if header.count(";") > header.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames or not _REQUIRED.issubset(reader.fieldnames):
        raise ValueError("Not a Trade Republic transaction export (missing columns: "
                         f"{sorted(_REQUIRED - set(reader.fieldnames or []))})")

    trades, income, flows = [], [], []
    cash = 0.0
    unknown_isins: set[str] = set()
    by_isin_qty: dict[str, float] = defaultdict(float)
    delivered: dict[str, float] = defaultdict(float)   # DELIVERY shares in/out per symbol
    not_read: dict[str, int] = defaultdict(int)         # share-moving rows not imported

    def trade(r: dict, when: str, side: str, isin: str, qty: float, eur: float, fee: float) -> None:
        crypto = (r.get("asset_class") or "").upper() == "CRYPTO"
        # crypto has no ISIN: the coin's EUR quote on Yahoo ("BTC-EUR")
        ticker = f"{isin}-EUR" if crypto and isin else ticker_for(isin)
        if not ticker:
            unknown_isins.add(isin)
        by_isin_qty[isin] += qty if side == "BUY" else -qty
        trades.append({"date": when, "isin": isin, "name": NAMES.get(isin, r.get("name") or isin),
                       "ticker": ticker, "side": side, "quantity": qty,
                       "price": _f(r.get("price", "")), "fee_eur": abs(fee),
                       "amount_eur": round(eur, 2)})

    for r in reader:
        cat, typ = r["category"], r["type"]
        when = (r.get("datetime") or r["date"])[:16].replace("T", " ")
        isin = r.get("symbol", "")
        amount, fee, tax = _f(r["amount"]), -abs(_f(r.get("fee", ""))), _f(r.get("tax", ""))
        shares = _f(r["shares"])
        side = (typ if cat == "TRADING" and typ in ("BUY", "SELL")
                else _CASH_TRADES.get(typ) if cat == "CASH" else None)
        if side:
            # a buy costs everything paid; a sale counts before the tax withheld
            eur = -(amount + fee + tax) if side == "BUY" else amount + fee
            cash += amount + fee + tax
            trade(r, when, side, isin, abs(shares), eur, fee)
        elif cat == "CORPORATE_ACTION" and typ == "SPLIT" and shares > 0:
            trade(r, when, "BUY", isin, shares, 0.0, 0.0)   # the shares added, at no cost
        elif cat == "CORPORATE_ACTION" and typ in _INFO_ACTIONS:
            continue
        elif cat == "CASH" and typ in _DIVIDENDS:
            net = amount + fee + tax
            cash += net
            income.append({"date": r["date"], "type": "dividend", "isin": isin,
                           "name": NAMES.get(isin, r["name"]), "amount_eur": round(net, 2),
                           "gross_eur": round(amount, 2), "tax_eur": round(tax, 2)})
        elif cat == "CASH" and typ == "INTEREST_PAYMENT":
            net = amount + fee + tax
            cash += net
            income.append({"date": r["date"], "type": "interest", "amount_eur": round(net, 2),
                           "gross_eur": round(amount, 2), "tax_eur": round(tax, 2)})
        elif cat == "CASH" and typ == "BENEFITS_SAVEBACK":
            cash += amount + fee + tax
            income.append({"date": r["date"], "type": "interest", "kind": "bonus",
                           "amount_eur": round(amount + fee + tax, 2)})
        elif cat == "CASH" and typ.startswith(("TRANSFER", "CUSTOMER_")):
            cash += amount + fee + tax
            flows.append({"date": r["date"], "amount_eur": round(amount, 2)})
        elif cat == "DELIVERY":
            delivered[isin] += shares   # custodian migration: out and back in
        else:
            cash += amount + fee + tax
            if shares:
                not_read[f"{cat} {typ}"] += 1
    trades.sort(key=lambda t: t["date"])
    income.sort(key=lambda t: t["date"])
    warnings = []
    moved = sorted(s for s, q in delivered.items() if abs(q) > 1e-9)
    if moved:
        warnings.append("Shares delivered in or out without a trade were not imported, so "
                        "holdings may be wrong: " + ", ".join(moved) + ".")
    if not_read:
        warnings.append("Rows that move shares were not imported, so holdings may be wrong: "
                        + ", ".join(f"{k} ({n})" for k, n in sorted(not_read.items())) + ".")
    if any(q < -1e-6 for q in by_isin_qty.values()):
        warnings.append("Some sells have no matching buy: the export should start when the "
                        "account opened.")
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
              "last_date": max((t["date"][:10] for t in trades), default=None),
              "warnings": warnings}
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
