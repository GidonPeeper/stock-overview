"""Trading 212 history CSV -> the statement JSON the Trading 212 connector reads.

Trading 212's History → Export gives one CSV per period (12 months at most).
Columns used: Action, Time (UTC), ISIN, Ticker, Name, ID, No. of shares,
Price / share, Currency (Price / share), Exchange rate, Result, Total,
Currency (Total), Withholding tax, Currency conversion fee. "Total" is the cash
moved in the account, the same figure as the activity-statement PDF's amount.

One export rarely covers a whole account, so `merge(base, new)` joins an
earlier statement (PDF or CSV) with a later export: rows from `new` dated after
the end of `base` are added, and `base`'s open positions are carried forward
through the new trades. The upload endpoint does this automatically.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import sys
import time
from collections import defaultdict
from datetime import date, timedelta

from ..fx import to_eur
from ..instruments import NAMES, ticker_for
from .trading212_pdf import _T212_ALIAS

_REQUIRED = {"Action", "Time (UTC)", "ISIN", "No. of shares", "Total", "Currency (Total)"}
PDF_SOURCE = "Trading 212 activity statement"   # trading212_pdf's "_source"
_SHORT = "Sells without the matching buys"


def looks_like(text: str) -> bool:
    head = text[:600]
    return "Action" in head and "Time (UTC)" in head and "No. of shares" in head


def _f(s) -> float:
    try:
        return float(s) if s not in ("", None) else 0.0
    except ValueError:
        return 0.0


def _strict(r: dict, col: str) -> float:
    """A number that becomes a quantity or an amount: empty is 0, anything
    else that is not a number stops the import (it used to count as 0)."""
    s = (r.get(col) or "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"Row of {r.get('Time (UTC)') or '?'} ({r.get('Action') or '?'}): "
                         f"\"{col}\" is not a number: {s!r}. Nothing was imported.") from None


def _eur(amount: float, ccy: str) -> float:
    ccy = (ccy or "EUR").upper()
    return round(amount if ccy == "EUR" else to_eur(amount, ccy), 2)


def _kind(action: str, amount: float) -> str | None:
    low = action.lower()
    if "interest" in low:
        return "interest"
    if low.startswith("currency conversion"):
        return "fx"
    if low.startswith("deposit"):
        return "deposit"
    if low.startswith("withdraw") or low.startswith("card") or low.startswith("spending"):
        return "withdrawal"
    if "free" in low or "bonus" in low or "promo" in low:
        return "bonus"
    return "deposit" if amount > 0 else "withdrawal" if amount < 0 else None


def convert(source) -> tuple[dict, dict]:
    """CSV (text, bytes or path) -> (statement dict, report)."""
    if isinstance(source, bytes):
        text = source.decode("utf-8-sig", errors="replace")
    elif isinstance(source, str) and "\n" not in source and source.lower().endswith(".csv"):
        with open(source, encoding="utf-8-sig") as f:
            text = f.read()
    else:
        text = source
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows or not _REQUIRED <= set(rows[0]):
        missing = sorted(_REQUIRED - set(rows[0] if rows else {}))
        raise ValueError(f"Not a Trading 212 history export (missing columns: {', '.join(missing)})")
    rows.sort(key=lambda r: r["Time (UTC)"])

    trades, dividends, transactions, skipped = [], [], [], defaultdict(int)
    moved_shares: dict[str, int] = defaultdict(int)  # share-moving rows not understood
    for r in rows:
        action = (r.get("Action") or "").strip()
        low = action.lower()
        when = r["Time (UTC)"][:16]
        total = _strict(r, "Total")
        ccy = r.get("Currency (Total)") or "EUR"
        isin = (r.get("ISIN") or "").strip()
        symbol = (r.get("Ticker") or "").strip()
        if (low.endswith(" buy") or low.endswith(" sell")) and isin:
            side = "BUY" if low.endswith("buy") else "SELL"
            fee = _f(r.get("Currency conversion fee"))
            trades.append({
                "date": when, "symbol": symbol, "isin": isin,
                "name": NAMES.get(isin, r.get("Name") or symbol),
                "ticker": ticker_for(isin) or _T212_ALIAS.get(symbol, symbol),
                "order_id": r.get("ID") or "", "side": side,
                "quantity": _strict(r, "No. of shares"), "price": _f(r.get("Price / share")),
                "price_currency": r.get("Currency (Price / share)") or "",
                "fx_rate": _f(r.get("Exchange rate")) or None,
                "fx_fee_eur": _eur(abs(fee), r.get("Currency (Currency conversion fee)") or ccy) if fee else 0.0,
                "return_eur": (_eur(_f(r.get("Result")), r.get("Currency (Result)") or ccy)
                               if side == "SELL" and r.get("Result") not in ("", None) else None),
                "settled_currency": ccy, "amount_eur": _eur(abs(total), ccy),
            })
        elif low.startswith("dividend") and isin:
            dividends.append({
                "date": when[:10], "symbol": symbol, "isin": isin,
                "name": NAMES.get(isin, r.get("Name") or symbol),
                "amount_eur": _eur(total, ccy),
                "gross": None, "tax": _f(r.get("Withholding tax")) or None,
            })
        elif isin and _strict(r, "No. of shares"):
            # shares in or out without a buy or sell (a split, a transfer...):
            # not imported, so the holdings would be wrong -- say so
            moved_shares[action or "?"] += 1
        else:
            kind = _kind(action, total)
            if kind is None:
                skipped[action] += 1
                continue
            transactions.append({
                "date": when[:10], "time": r["Time (UTC)"][11:19], "type": action,
                "amount": total, "currency": ccy, "amount_eur": _eur(total, ccy), "kind": kind,
            })

    period = [rows[0]["Time (UTC)"][:10], rows[-1]["Time (UTC)"][:10]]
    st = {"overview": {"period": period}, "trades": trades, "positions": [],
          "cash": {}, "transactions": transactions, "dividends": dividends, "warnings": []}
    qty = _net_quantities(trades)
    short = sorted(isin for isin, q in qty.items() if q < -1e-6)
    if short:
        st["warnings"].append(
            _SHORT + " in this file (it starts after the account opened): "
            + ", ".join(short) + ". Upload the earlier statement first; this file is then added to it.")
    if moved_shares:
        st["warnings"].append(
            "Rows that move shares without a buy or sell were not imported, so holdings may be "
            "wrong: " + ", ".join(f"{a} ({n})" for a, n in sorted(moved_shares.items())) + ".")
    st["positions"] = _positions_from_trades(trades)
    _finish(st, "Trading 212 history CSV")
    report = {"summary": st["summary"], "warnings": st["warnings"], "skipped": dict(skipped)}
    return st, report


def _net_quantities(trades: list[dict]) -> dict[str, float]:
    qty: dict[str, float] = defaultdict(float)
    for t in trades:
        qty[t["isin"]] += t["quantity"] if t["side"] == "BUY" else -t["quantity"]
    return qty


def _positions_from_trades(trades: list[dict], start: list[dict] | None = None) -> list[dict]:
    """Open positions after `trades`, starting from `start` (statement positions).
    Cost is kept as value_eur - return_eur, so the connector reads the same cost
    per share; sold shares take their share of the cost with them."""
    pos: dict[str, dict] = {}
    for p in copy.deepcopy(start or []):
        pos[p["isin"]] = {**p, "_cost": float(p["value_eur"]) - float(p.get("return_eur") or 0.0)}
    for t in trades:
        p = pos.setdefault(t["isin"], {
            "symbol": t["symbol"], "isin": t["isin"], "name": t["name"], "ticker": t["ticker"],
            "quantity": 0.0, "currency": t.get("price_currency") or "", "_cost": 0.0})
        if t["side"] == "BUY":
            p["quantity"] += t["quantity"]
            p["_cost"] += t["amount_eur"]
        elif p["quantity"] > 1e-9:
            sold = min(t["quantity"], p["quantity"])
            p["_cost"] *= (p["quantity"] - sold) / p["quantity"]
            p["quantity"] -= sold
    out = []
    for p in pos.values():
        if p["quantity"] <= 1e-6:
            continue
        cost = round(p.pop("_cost"), 2)
        p["quantity"] = round(p["quantity"], 8)
        # no market value in a history export: value = cost, return 0 (live prices fill in later)
        p.update({"value_eur": cost, "return_eur": 0.0,
                  "avg_price": round(cost / p["quantity"], 6) if p["quantity"] else 0.0})
        out.append(p)
    return out


def _finish(st: dict, source: str) -> None:
    tx = st["transactions"]
    st["summary"] = {
        "period": st["overview"]["period"], "trades": len(st["trades"]),
        "positions": len(st["positions"]), "transactions": len(tx),
        "dividends": len(st["dividends"]),
        "interest_eur": round(sum(t["amount_eur"] for t in tx if t["kind"] == "interest"), 2),
        "deposits_eur": round(sum(t["amount_eur"] for t in tx if t["kind"] == "deposit"), 2),
        "withdrawals_eur": round(sum(t["amount_eur"] for t in tx if t["kind"] == "withdrawal"), 2),
        "realised_eur": round(sum(t["return_eur"] or 0 for t in st["trades"] if t["side"] == "SELL"), 2),
        "dividends_eur": round(sum(d["amount_eur"] for d in st["dividends"]), 2),
    }
    st["all_checks_ok"] = not st["warnings"]
    st["_source"] = source
    st["_generated"] = time.strftime("%Y-%m-%d %H:%M")


def merge(base: dict, new: dict) -> tuple[dict, dict]:
    """`base` (an earlier statement) + the rows of `new` dated after base's end."""
    end = ((base.get("overview") or {}).get("period") or [None, None])[1]
    if not end:
        raise ValueError("The existing Trading 212 statement has no period; upload it again.")
    later = lambda d: str(d)[:10] > end  # noqa: E731
    added_trades = [t for t in new["trades"] if later(t["date"])]
    st = copy.deepcopy(base)
    st["trades"] = base["trades"] + added_trades
    st["dividends"] = base["dividends"] + [d for d in new["dividends"] if later(d["date"])]
    st["transactions"] = base["transactions"] + [t for t in new["transactions"] if later(t["date"])]
    # positions: carry base's forward through the new trades, keeping base's cost
    st["positions"] = _positions_from_trades(added_trades, base.get("positions"))
    for p in st["positions"]:
        p.pop("_cost", None)
    st["warnings"] = []
    if new.get("_source") == PDF_SOURCE:
        # a later activity statement has the exact open-positions table at the
        # new end date (Trading 212's own cost, FX included): use it, and say
        # so if it does not hold the shares the joined trades add up to
        carried = {p["isin"]: p["quantity"] for p in st["positions"]}
        st["positions"] = copy.deepcopy(new.get("positions") or [])
        table = {p["isin"]: float(p["quantity"]) for p in st["positions"]}
        off = sorted(i for i in set(carried) | set(table)
                     if abs(carried.get(i, 0.0) - table.get(i, 0.0)) > 1e-5)
        if off:
            st["warnings"].append("The joined trades do not add up to the statement's open "
                                  "positions for: " + ", ".join(off))
    new_end = max(end, (new.get("overview") or {}).get("period", [end, end])[1])
    st["overview"] = {**(base.get("overview") or {}), "period": [base["overview"]["period"][0], new_end]}
    st["cash"] = {}  # the cash table belongs to the old end date
    qty = _net_quantities(st["trades"])
    short = sorted(isin for isin, q in qty.items() if q < -1e-6)
    if short:
        st["warnings"].append("Sells without matching buys after merging: " + ", ".join(short))
    _finish(st, f"{base.get('_source', 'statement')} + Trading 212 history CSV")
    report = {"summary": st["summary"], "warnings": st["warnings"], "merged_after": end,
              "added": {"trades": len(added_trades),
                        "dividends": len(st["dividends"]) - len(base["dividends"]),
                        "transactions": len(st["transactions"]) - len(base["transactions"])}}
    return st, report


def _period(st: dict | None) -> tuple[str, str] | None:
    p = ((st or {}).get("overview") or {}).get("period") or []
    return (str(p[0])[:10], str(p[1])[:10]) if len(p) == 2 and p[0] and p[1] else None


def _traded(st: dict, lo: str, hi: str) -> set[tuple[str, str]]:
    return {(t.get("isin"), str(t.get("side", "")).upper()) for t in st.get("trades") or []
            if lo <= str(t.get("date", ""))[:10] <= hi}


def combine(base: dict | None, new: dict, report: dict | None = None) -> tuple[dict, dict]:
    """The statement to keep when `new` (a converted activity-statement PDF or
    History CSV) is uploaded while `base` is already saved. A PDF covers 18
    months at most and a CSV 12, so files are joined rather than replaced, and
    nothing uploaded before is lost:

      new covers every date of base       -> new replaces base
      new starts earlier or ends later    -> joined, each day taken from one
                                             file (no row twice); a gap
                                             between them is a warning
      new lies inside base's dates        -> refused: it would add nothing
      the overlapping dates show other
      trades                              -> refused: another account

    Returns (statement, report); `report` is the new file's own report, which
    is extended."""
    report = dict(report or {})
    warnings = list(report.get("warnings") or [])
    np_ = _period(new)
    if not base:
        return new, report
    bp = _period(base)
    if not bp or not np_:
        if not np_:
            raise ValueError("This file shows no dates, so it cannot be joined to the Trading 212 "
                             "statement uploaded before. Nothing was changed.")
        report["warnings"] = warnings + ["The statement uploaded before had no dates, "
                                         "so this file replaced it."]
        return new, report
    (bs, be), (ns, ne) = bp, np_
    if ns <= bs and ne >= be:
        return new, report
    if bs <= ns and ne <= be:
        raise ValueError(f"This file covers {ns} to {ne}, which the Trading 212 statement already "
                         f"uploaded ({bs} to {be}) covers too. Nothing was changed. To start "
                         f"again, upload a statement that covers at least {bs} to {be}.")
    earlier, later = (base, new) if ne > be else (new, base)
    (es, ee), (ls, le) = _period(earlier), _period(later)
    lo, hi = max(es, ls), min(ee, le)
    a, b = _traded(earlier, lo, hi), _traded(later, lo, hi)
    if a and b and not a & b:
        raise ValueError(f"The two Trading 212 files show different trades for the dates both "
                         f"cover ({lo} to {hi}): they look like two different accounts. Nothing "
                         f"was changed. Upload one account's files only.")
    st, joined = merge(earlier, later)
    warnings = [w for w in warnings if not w.startswith(_SHORT)]   # merge checks that again
    if ls > (date.fromisoformat(ee) + timedelta(days=1)).isoformat():
        st["warnings"].append(f"Nothing covers {(date.fromisoformat(ee) + timedelta(days=1))} to "
                              f"{(date.fromisoformat(ls) - timedelta(days=1))}: trades, dividends "
                              f"and interest of those dates are missing. Upload a file that covers them.")
        st["all_checks_ok"] = False
    report.update(joined)
    report["warnings"] = warnings + st["warnings"]
    return st, report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python -m backend.importers.trading212_csv <export.csv> "
                 "[--base earlier_statement.json] [-o out.json]")
    args = sys.argv[1:]
    out_path = args[args.index("-o") + 1] if "-o" in args else None
    st, report = convert(args[0])
    if "--base" in args:
        with open(args[args.index("--base") + 1]) as f:
            st, report = merge(json.load(f), st)
    print(json.dumps(report, indent=1))
    if out_path:
        with open(out_path, "w") as f:
            json.dump(st, f, indent=1, ensure_ascii=False)
        print("written", out_path)
