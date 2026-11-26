"""Trading 212 "Activity statement" PDF -> trading212_statement.json.

The statement (Invest account) has fixed sections: Overview, executed trades,
open positions, cash breakdown, transactions (deposits / withdrawals / daily
interest / card purchases) and dividends. This module turns the *decoded text
lines* of those pages into one JSON document the Trading 212 connector can use
as a fallback whenever the live API key is absent, and that the trendline and
income modules can use for exact history.

Why "decoded"? Trading 212 embeds Type3 fonts whose ToUnicode table maps every
digit (and a few letters) to U+0000, so ordinary text extraction yields
"€?,???.??". `text_layer.py` recovers the real characters; this module only
parses. The result is cross-checked against the statement's own Overview totals
(realised return, dividends, interest, deposits) and the open-positions table,
so a mis-read digit fails loudly.
"""

from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict

from ..instruments import NAMES, is_valid_isin, ticker_for

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}$")
_MONEY = re.compile(r"^(-?)([€$£])(-?)([\d,]+(?:\.\d+)?)$")
_NUM = re.compile(r"^-?[\d,]+(?:\.\d+)?$")

# Trading 212's own short symbols -> yfinance, where they differ.
_T212_ALIAS = {"FB": "META", "ZPRRd": "ZPRR.DE", "ZPRR": "ZPRR.DE", "HEIA": "HEIA.AS",
               "BESI": "BESI.AS", "ASML": "ASML.AS"}


def _money(tok: str) -> tuple[str, float] | None:
    m = _MONEY.match(tok.replace("−", "-"))
    if not m:
        return None
    sign = -1 if (m.group(1) or m.group(3)) else 1
    return m.group(2), sign * float(m.group(4).replace(",", ""))


def _num(tok: str) -> float | None:
    return float(tok.replace(",", "")) if _NUM.match(tok) else None


def _fix_isin(tok: str) -> str:
    """Repair O/0 and I/1 confusions using the ISIN check digit."""
    if is_valid_isin(tok):
        return tok
    cands = [tok]
    for a, b in (("O", "0"), ("I", "1"), ("0", "O"), ("1", "I")):
        cands += [c[:2] + c[2:].replace(a, b) for c in list(cands)]
    for c in cands:
        if is_valid_isin(c):
            return c
    return tok


def _section(lines_text: list[str]) -> str | None:
    for t in lines_text[:8]:
        tl = t.lower()
        if "executed trades" in tl or tl.startswith("execution time"):
            return "trades"
        if "open positions summary" in tl:
            return "positions"
        if "cash breakdown" in tl:
            return "cash"
        if "transactions and dividends" in tl or tl == "time type amount":
            return "transactions"
        if tl == "dividends" or tl.startswith("instrument isin issuing country"):
            return "dividends"
        if tl == "overview":
            return "overview"
    return None


def _row_tokens(line: dict) -> list[str]:
    return [w["text"] for w in line["words"]]


def parse(pages: list[dict]) -> dict:
    """pages: [{page, lines:[{top, words:[{text,x0,x1,top,bottom}]}]}] with
    the text already decoded. Returns the statement as a dict."""
    out: dict = {"overview": {}, "trades": [], "positions": [], "cash": {},
                 "transactions": [], "dividends": [], "warnings": []}
    section = None
    for pg in pages:
        lines = pg["lines"]
        texts = [" ".join(_row_tokens(l)) for l in lines]
        sec = _section(texts)
        if sec:
            section = sec
        if section == "overview":
            _parse_overview(texts, out["overview"])
        elif section == "trades":
            _parse_trades(lines, out)
        elif section == "positions":
            _parse_positions(lines, out)
        elif section == "cash":
            _parse_cash(texts, out["cash"])
        elif section == "transactions":
            _parse_transactions(lines, out)
        elif section == "dividends":
            _parse_dividends(lines, out)
    if out["overview"].pop("_conflicts", None):
        # Its rows would be mixed into one account and the checks would use
        # only the last overview: refuse rather than import a mix.
        raise ValueError("This statement covers more than one Trading 212 account (it has two "
                         "overviews with different figures). Make the activity statement again "
                         "with only one account ticked, and upload that.")
    _finish(out)
    return out


def _parse_overview(texts, ov):
    keys = {"deposits": "deposits", "withdrawals": "withdrawals", "bonus": "bonus",
            "realised return": "realised_return", "open return change": None,
            "open return": "open_return", "dividends": "dividends",
            "interest on cash": "interest", "fx fee": "fx_fee",
            "third-party fees": "third_party_fees", "account value": "account_value"}
    def put(key, value):
        # A second overview with other figures: the statement covers more than
        # one account (Trading 212 ticks every account by default).
        if key in ov and ov[key] != value:
            ov.setdefault("_conflicts", []).append(key)
        ov[key] = value

    for t in texts:
        low = t.lower()
        for label, key in sorted(keys.items(), key=lambda kv: -len(kv[0])):
            if low.startswith(label) and key:
                m = _money(t.split()[-1])
                if m:
                    put(key, m[1])
                break
        m = re.search(r"covering from (\d{2}\.\d{2}\.\d{4}).*?to (\d{2}\.\d{2}\.\d{4})", t)
        if m:
            put("period", [_ddmmyyyy(m.group(1)), _ddmmyyyy(m.group(2))])


def _ddmmyyyy(s: str) -> str:
    d, m, y = s.split(".")
    return f"{y}-{m}-{d}"


def _header_positions(line: dict, wanted: dict[str, str]) -> dict[str, float]:
    """x0 of each header label we care about ({label_text: key})."""
    pos = {}
    toks = line["words"]
    for i, w in enumerate(toks):
        for label, key in wanted.items():
            first = label.split()[0]
            if w["text"] == first and " ".join(t["text"] for t in toks[i:i + len(label.split())]) == label:
                pos[key] = w["x0"]
    return pos


def _parse_trades(lines, out):
    hdr = None
    for ln in lines:
        toks = _row_tokens(ln)
        if toks[:2] == ["EXECUTION", "TIME"]:
            hdr = _header_positions(ln, {"FX RATE": "fx_rate", "FX FEE": "fx_fee",
                                         "GOVT FEES": "govt_fees", "RETURN": "return"})
            continue
        if not toks or not _DATE.match(toks[0]) or len(toks) < 10:
            continue
        date, tm, symbol, isin = toks[0], toks[1], toks[2], _fix_isin(toks[3])
        order_id, side = toks[4], toks[5].capitalize()
        qty = _num(toks[6])
        price = _money(toks[7]); value = _money(toks[8])
        if qty is None or not price or not value:
            out["warnings"].append(f"trade row not understood: {' '.join(toks)}")
            continue
        # the last token is the EUR (or instrument-currency) total actually settled
        total = _money(toks[-1])
        # placeholder cells that OCR/decoding left as '?' are single chars: resolve by column
        rest = ln["words"][9:-1]
        fx_rate = fx_fee = ret = None
        for w in rest:
            t = w["text"]
            if hdr and "fx_rate" in hdr and abs(w["x0"] - hdr["fx_rate"]) < 40 and _num(t) is not None:
                fx_rate = _num(t)
            elif hdr and "fx_fee" in hdr and abs(w["x0"] - hdr["fx_fee"]) < 30 and _money(t):
                fx_fee = _money(t)[1]
            elif hdr and "return" in hdr and abs(w["x0"] - hdr["return"]) < 30 and _money(t):
                ret = _money(t)[1]
        if fx_rate is None:
            fx_rate = 1.0
        ccy = price[0]
        settled = {"$": "USD", "€": "EUR", "£": "GBP"}[total[0]]
        # settled in EUR: exact. Settled from a foreign cash balance: provisional
        # (row rate is 1) — fixed up by _calibrate_eur_amounts from the
        # statement's own EUR return figures.
        amount_eur = abs(total[1]) if settled == "EUR" else abs(total[1]) / (fx_rate if fx_rate > 1.01 else 1.0)
        out["trades"].append({
            "date": f"{date} {tm[:5]}", "symbol": symbol, "isin": isin,
            "name": NAMES.get(isin, symbol), "ticker": ticker_for(isin) or _T212_ALIAS.get(symbol, symbol),
            "order_id": order_id, "side": side.upper(), "quantity": qty,
            "price": abs(price[1]), "price_currency": {"$": "USD", "€": "EUR", "£": "GBP"}[ccy],
            "value": abs(value[1]), "fx_rate": fx_rate, "fx_fee_eur": abs(fx_fee or 0.0),
            "return_eur": ret if side.upper() == "SELL" else None,
            "settled_currency": settled, "amount_eur": round(amount_eur, 2),
        })


def _parse_positions(lines, out):
    for ln in lines:
        toks = _row_tokens(ln)
        if len(toks) < 9 or not re.match(r"^[A-Z0-9.]{1,6}$", toks[0]):
            continue
        isin = _fix_isin(toks[1])
        if not is_valid_isin(isin):
            continue
        qty = _num(toks[2]); avg = _money(toks[3]); px = _money(toks[4])
        val = _money(toks[6]); eur_ret = _money(toks[-2]); eur_val = _money(toks[-1])
        if None in (qty,) or not (avg and px and val and eur_val):
            continue
        out["positions"].append({
            "symbol": toks[0], "isin": isin, "name": NAMES.get(isin, toks[0]),
            "ticker": ticker_for(isin) or _T212_ALIAS.get(toks[0], toks[0]),
            "quantity": qty, "avg_price": abs(avg[1]), "price": abs(px[1]),
            "currency": {"$": "USD", "€": "EUR", "£": "GBP"}[px[0]],
            "value": abs(val[1]), "fx_rate": _num(toks[7]) or 1.0,
            "return_eur": eur_ret[1] if eur_ret else None, "value_eur": abs(eur_val[1]),
        })


def _parse_cash(texts, cash):
    for t in texts:
        low = t.lower()
        m = _money(t.split()[-1]) if t.split() else None
        if low.startswith("eur cash") and m and "total_eur" not in cash:
            cash["total_eur"] = m[1]
        elif low.startswith("gbp cash") and "total_gbp" not in cash:
            g = _money(t.split()[2]) if len(t.split()) > 2 else None
            if g:
                cash["total_gbp"] = g[1]
                eur = re.search(r"\(€([\d,.]+)\)", t)
                if eur:
                    cash["gbp_in_eur"] = float(eur.group(1).replace(",", ""))


def _parse_transactions(lines, out):
    """Rows: DATE TIME TYPE... AMOUNT [ (EUR) ]. A tall TIME cell can wrap
    (date on one line, time on the next); re-assemble those."""
    pending_date = None
    for ln in lines:
        toks = _row_tokens(ln)
        if not toks:
            continue
        if len(toks) == 1 and _DATE.match(toks[0]):
            pending_date = toks[0]
            continue
        if len(toks) == 1 and _TIME.match(toks[0]):
            if out["transactions"] and out["transactions"][-1]["time"] == "":
                out["transactions"][-1]["time"] = toks[0]
            continue
        if _DATE.match(toks[0]) and len(toks) >= 3 and _TIME.match(toks[1]):
            date, tm, body = toks[0], toks[1], toks[2:]
        elif pending_date and not _DATE.match(toks[0]):
            date, tm, body = pending_date, "", toks
            pending_date = None
        else:
            continue
        # amount = last money token; an EUR equivalent may follow in parentheses
        eur_equiv = None
        if body[-1].startswith("(") and body[-1].endswith(")"):
            eq = _money(body[-1].strip("()"))
            if eq:
                eur_equiv = eq[1]
                body = body[:-1]
        amt = _money(body[-1]) if body else None
        if not amt:
            continue
        typ = " ".join(body[:-1])
        ccy = {"$": "USD", "€": "EUR", "£": "GBP"}[amt[0]]
        out["transactions"].append({
            "date": date, "time": tm, "type": typ, "amount": amt[1], "currency": ccy,
            "amount_eur": round(eur_equiv if eur_equiv is not None else (amt[1] if ccy == "EUR" else 0.0), 2),
        })


def _parse_dividends(lines, out):
    for ln in lines:
        toks = _row_tokens(ln)
        if len(toks) < 9 or not re.match(r"^[A-Z0-9.]{1,6}$", toks[0]):
            continue
        isin = _fix_isin(toks[1])
        if not is_valid_isin(isin):
            continue
        # ... ELIGIBLE HOLDINGS  PAY DATE(dd.mm.yyyy hh:mm)  PER SHARE  TOTAL  WHT%  WHT  FX  NET(EUR)
        date = next((t for t in toks if re.match(r"^\d{2}\.\d{2}\.\d{4}$", t)), None)
        net = _money(toks[-1])
        if not date or not net:
            continue
        i = toks.index(date)
        gross = _money(toks[i + 2]) if i + 2 < len(toks) else None
        tax = _money(toks[i + 4]) if i + 4 < len(toks) else None
        out["dividends"].append({
            "date": _ddmmyyyy(date), "symbol": toks[0], "isin": isin, "name": NAMES.get(isin, toks[0]),
            "amount_eur": net[1],
            "gross": abs(gross[1]) if gross else None, "tax": abs(tax[1]) if tax else None,
        })


def _calibrate_eur_amounts(out):
    """Trades settled from a USD/GBP cash balance show no EUR total. Trading 212
    values those in EUR at the FX rate of the day; recover that EUR cost from the
    statement's own EUR figures — the open position's (value − return), or a
    sell's (proceeds − return) — and spread it over the foreign-settled buys in
    proportion to their value. Reproduces T212's realised/open return closely
    (exact when all buys precede the sells)."""
    by_isin: dict[str, list[dict]] = defaultdict(list)
    for t in out["trades"]:
        by_isin[t["isin"]].append(t)
    pos = {p["isin"]: p for p in out["positions"]}
    for isin, trades in by_isin.items():
        foreign = [t for t in trades if t["side"] == "BUY" and t.get("settled_currency") not in (None, "EUR")]
        if not foreign:
            continue
        buys = [t for t in trades if t["side"] == "BUY"]
        sells = [t for t in trades if t["side"] == "SELL"]
        avg = None
        if isin in pos and pos[isin]["return_eur"] is not None and pos[isin]["quantity"]:
            avg = (pos[isin]["value_eur"] - pos[isin]["return_eur"]) / pos[isin]["quantity"]
        elif sells and all(s.get("return_eur") is not None for s in sells):
            q = sum(s["quantity"] for s in sells)
            avg = sum(s["amount_eur"] - s["return_eur"] for s in sells) / q if q else None
        if not avg or avg <= 0:
            out["warnings"].append(f"{isin}: cannot derive EUR cost of foreign-settled buys")
            continue
        total_eur = avg * sum(b["quantity"] for b in buys)
        known_eur = sum(b["amount_eur"] for b in buys if b not in foreign)
        remaining = total_eur - known_eur
        weight = sum(b["value"] for b in foreign)
        if remaining <= 0 or not weight:
            out["warnings"].append(f"{isin}: implausible EUR calibration ({remaining:.2f})")
            continue
        for b in foreign:
            b["amount_eur"] = round(remaining * b["value"] / weight, 2)
            b["fx_rate_implied"] = round(b["value"] / b["amount_eur"], 5)
            b["eur_calibrated"] = True


def _finish(out):
    """Derived figures + consistency checks against the statement's own totals."""
    ov = out["overview"]
    checks = {}
    tx = out["transactions"]
    def kind(t) -> str:
        low = t["type"].lower()
        if low.startswith("interest"):
            return "interest"
        if low.startswith("currency exchange"):
            return "fx"
        if "free equity" in low or low.startswith("bonus"):
            return "bonus"
        if low.startswith("card") or low.startswith("withdraw") or (t["amount"] < 0):
            return "withdrawal"
        return "deposit"
    for t in tx:
        t["kind"] = kind(t)
    interest = round(sum(t["amount_eur"] for t in tx if t["kind"] == "interest"), 2)
    deposits = round(sum(t["amount_eur"] for t in tx if t["kind"] == "deposit"), 2)
    withdrawals = round(sum(t["amount_eur"] for t in tx if t["kind"] == "withdrawal"), 2)
    _calibrate_eur_amounts(out)
    realised = round(sum(t["return_eur"] or 0 for t in out["trades"] if t["side"] == "SELL"), 2)
    dividends = round(sum(d["amount_eur"] for d in out["dividends"]), 2)
    checks["interest"] = (interest, ov.get("interest"))
    checks["deposits"] = (deposits, ov.get("deposits"))
    checks["realised_return"] = (realised, ov.get("realised_return"))
    checks["dividends"] = (dividends, ov.get("dividends"))
    checks["withdrawals"] = (withdrawals, ov.get("withdrawals"))
    # positions rebuilt from trades must match the statement's open positions table
    qty = defaultdict(float)
    for t in out["trades"]:
        qty[t["isin"]] += t["quantity"] if t["side"] == "BUY" else -t["quantity"]
    pos_ok, pos_detail = True, []
    for p in out["positions"]:
        rebuilt = round(qty.get(p["isin"], 0.0), 6)
        same = abs(rebuilt - p["quantity"]) < 1e-5
        pos_ok &= same
        pos_detail.append(f"{p['symbol']}: statement {p['quantity']} vs trades {rebuilt}{'' if same else '  <-- MISMATCH'}")
    extra = [k for k, v in qty.items() if abs(v) > 1e-5 and k not in {p["isin"] for p in out["positions"]}]
    if extra:
        pos_ok = False
        pos_detail.append(f"held per trades but not in positions table: {extra}")
    out["summary"] = {
        "period": ov.get("period"), "account_value_eur": ov.get("account_value"),
        "trades": len(out["trades"]), "positions": len(out["positions"]),
        "transactions": len(tx), "dividends": len(out["dividends"]),
        "interest_eur": interest, "deposits_eur": deposits, "withdrawals_eur": withdrawals,
        "realised_eur": realised, "dividends_eur": dividends,
        "cash_eur": out["cash"].get("total_eur"),
    }
    out["checks"] = {k: {"computed": a, "statement": b,
                         "ok": (b is not None and abs(a - b) < 0.011)} for k, (a, b) in checks.items()}
    out["checks"]["positions_match_trades"] = {"ok": pos_ok, "detail": pos_detail}
    out["all_checks_ok"] = all(c["ok"] for c in out["checks"].values())
    out["_source"] = "Trading 212 activity statement"
    out["_generated"] = time.strftime("%Y-%m-%d %H:%M")


def looks_like(content: bytes) -> bool:
    return content[:5] == b"%PDF-"


def convert(source) -> tuple[dict, dict]:
    """Activity-statement PDF (path or bytes) -> (statement dict, report).
    The report carries the glyph-decoding stats and the consistency checks;
    `statement["all_checks_ok"]` is the one flag to look at."""
    from .text_layer import extract_pages

    pages, decode = extract_pages(source)
    st = parse(pages)
    if decode["unknown"]:
        st["warnings"].append(f"{decode['unknown']} characters could not be decoded "
                              f"(unseen glyphs: {decode['unknown_glyphs']})")
    report = {"decode": decode, "checks": st["checks"], "all_checks_ok": st["all_checks_ok"],
              "warnings": st["warnings"], "summary": st["summary"]}
    return st, report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python -m backend.importers.trading212_pdf <Activity-Statement.pdf> [out.json]")
    from pathlib import Path
    st, report = convert(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parents[2] / "data" / "trading212_statement.json"
    out.write_text(json.dumps(st, indent=1, ensure_ascii=False))
    print(f"wrote {out}")
    print(json.dumps(report, indent=1, ensure_ascii=False))
