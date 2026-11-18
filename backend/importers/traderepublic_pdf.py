"""Trade Republic account statement PDF -> trades_/income_trade_republic.json.

The statement ("Rekeningoverzicht" / "Kontoauszug" / "Account statement",
under Profile → Account statements) lists every cash booking with a running
balance: trades ("Buy trade <ISIN> <name>, quantity: <n>"), dividends ("Cash
Dividend for ISIN <ISIN>"), interest and transfers. Each row spans three text
lines (day + month, the booking, the year) and long descriptions wrap onto
the first and last of them, so words are grouped per row by position.

Every amount is checked against the running balance (previous balance ± amount
= new balance), which also tells credits from debits. Output matches
traderepublic_csv.convert, so either file can be uploaded.
"""

from __future__ import annotations

import io
import json
import re
import sys
import time
from collections import defaultdict

from ..instruments import NAMES, ticker_for

TR_FEE_EUR = 1.0  # Trade Republic's fee per order; the statement amount includes it

_MONTHS = {
    # Dutch, German, English, French, Spanish, Italian short month names
    "jan": 1, "feb": 2, "mrt": 3, "mar": 3, "mär": 3, "apr": 4, "mei": 5, "mai": 5, "may": 5,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "okt": 10, "oct": 10, "nov": 11,
    "dec": 12, "dez": 12, "janv": 1, "févr": 2, "avr": 4, "juin": 6, "juil": 7, "août": 8,
    "ene": 1, "abr": 4, "ago": 8, "dic": 12, "gen": 1, "mag": 5, "giu": 6, "lug": 7,
    "set": 9, "ott": 10,
}
_ISIN = r"[A-Z]{2}[A-Z0-9]{9}\d"
_TRADE = re.compile(rf"\b(Buy|Sell|Kauf|Verkauf|Koop|Verkoop|Achat|Vente)\b.*?({_ISIN})\s+(.*?),?\s*"
                    rf"(?:quantity|Anzahl|aantal|quantité|Stück)\s*:?\s*([\d.,]+)", re.I)
_SAVINGS = re.compile(rf"(Savings plan|Sparplan|Spaarplan|Saveback|Round ?up).*?({_ISIN})\s+(.*?),?\s*"
                      rf"(?:quantity|Anzahl|aantal|quantité|Stück)\s*:?\s*([\d.,]+)", re.I)
_DIVIDEND = re.compile(rf"(Dividend|Dividende|Distribution|Ausschüttung).*?({_ISIN})", re.I)
_MONEY = re.compile(r"^-?[\d.]+,\d{2}$")
_FOOTER_WORDS = ("Aangemaakt", "Erstellt", "Created", "Pagina", "Seite", "Page")


def _amount(tok: str) -> float:
    return float(tok.replace(".", "").replace(",", "."))


def _qty(tok: str) -> float:
    # quantities are written with a decimal point ("2.972048"); a comma is a decimal comma
    return float(tok.replace(",", ".")) if tok.count(".") <= 1 else float(tok.replace(".", ""))


def looks_like(content: bytes) -> bool:
    if content[:5] != b"%PDF-":
        return False
    head = content[:200_000]
    return b"Trade Republic" in head or b"TRADE REPUBLIC" in head or b"Rekeningoverzicht" in head


def _columns(words: list[dict]) -> dict | None:
    """x positions of the booking table's columns on one page."""
    by = {w["text"]: w for w in words}
    if "TYPE" not in by:
        return None
    hdr_top = by["TYPE"]["top"]
    row = [w for w in words if abs(w["top"] - hdr_top) < 3]
    names = [w["text"] for w in row]
    desc = next((w for w in row if w["text"] not in ("DATUM", "TYPE", "DATE", "DATUM")
                 and w["x0"] > by["TYPE"]["x1"]), None)
    money = [w for w in row if w["text"].upper() in ("BEDRAG", "BETRAG", "AMOUNT", "MONTANT", "IMPORTE")]
    if not desc or not money:
        return None
    return {"top": hdr_top, "type": by["TYPE"]["x0"] - 2, "desc": desc["x0"] - 2,
            "money": min(w["x0"] for w in money) - 4, "names": names}


def _rows(pdf) -> list[dict]:
    """[{date, type, text, money: [values in order]}] in statement order."""
    out = []
    for page in pdf.pages:
        words = page.extract_words(x_tolerance=1)
        cols = _columns(words)
        if cols is None:
            continue
        body = [w for w in words if w["top"] > cols["top"] + 3]
        dates = sorted((w for w in body if w["x0"] < cols["type"] and re.fullmatch(r"\d{1,2}", w["text"])),
                       key=lambda w: w["top"])
        years = sorted((w for w in body if w["x0"] < cols["type"] and re.fullmatch(r"\d{4}", w["text"])),
                       key=lambda w: w["top"])
        for d in dates:
            year = next((y for y in years if y["top"] > d["top"]), None)
            if year is None:
                continue
            month_w = next((w for w in body if abs(w["top"] - d["top"]) < 2 and d["x1"] < w["x0"] < cols["type"]), None)
            month = _MONTHS.get((month_w["text"] if month_w else "").lower().rstrip("."))
            if not month:
                continue
            lo, hi = d["top"] - 2, year["top"] + 2
            inside = sorted((w for w in body if lo <= w["top"] <= hi and w["x0"] >= cols["type"]),
                            key=lambda w: (round(w["top"]), w["x0"]))
            if any(w["text"].startswith(_FOOTER_WORDS) for w in inside):
                continue
            typ = " ".join(w["text"] for w in inside if w["x0"] < cols["desc"])
            text = " ".join(w["text"] for w in inside if cols["desc"] <= w["x0"] < cols["money"])
            money = [_amount(w["text"]) for w in sorted((w for w in inside if w["x0"] >= cols["money"]),
                                                          key=lambda w: w["x0"])
                     if _MONEY.match(w["text"])]
            out.append({"date": f"{year['text']}-{month:02d}-{int(d['text']):02d}",
                        "type": typ, "text": text, "money": money, "page": page.page_number})
    return out


def convert(source) -> tuple[dict, dict, dict]:
    """PDF (path or bytes) -> (trades_json, income_json, report)."""
    import pdfplumber

    stream = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    with pdfplumber.open(stream) as pdf:
        rows = _rows(pdf)
    if not rows:
        raise ValueError("No bookings found — is this the Trade Republic account statement PDF?")

    trades, income, flows, other, problems = [], [], [], [], []
    balance = None
    by_isin_qty: dict[str, float] = defaultdict(float)
    unknown: set[str] = set()
    for r in rows:
        if len(r["money"]) < 2:
            problems.append(f"{r['date']} p{r['page']}: amount or balance not found ({r['type']} {r['text'][:40]})")
            continue
        amount, new_balance = r["money"][0], r["money"][-1]
        if balance is None:
            # first row: the opening balance is whichever sign makes the books balance
            signed = amount if abs(new_balance - amount) < abs(new_balance + amount) else -amount
        else:
            signed = round(new_balance - balance, 2)
            if abs(abs(signed) - amount) > 0.005:
                problems.append(f"{r['date']} p{r['page']}: balance {balance:.2f} -> {new_balance:.2f} "
                                f"does not fit amount {amount:.2f}")
                signed = amount if signed >= 0 else -amount
        balance = new_balance
        text = r["text"]
        m = _TRADE.search(text) or _SAVINGS.search(text)
        if m:
            word, isin, name, qty_tok = m.groups()
            side = "SELL" if word.lower() in ("sell", "verkauf", "verkoop", "vente") else "BUY"
            if (side == "BUY") != (signed < 0):
                problems.append(f"{r['date']}: {side} of {isin} but cash went {'down' if signed < 0 else 'up'}")
            qty = _qty(qty_tok)
            fee = 0.0 if _SAVINGS.search(text) and not _TRADE.search(text) else TR_FEE_EUR
            gross = amount - fee if side == "BUY" else amount + fee
            by_isin_qty[isin] += qty if side == "BUY" else -qty
            ticker = ticker_for(isin)
            if not ticker:
                unknown.add(isin)
            trades.append({"date": f"{r['date']} 00:00", "isin": isin,
                           "name": NAMES.get(isin, name.strip().title()), "ticker": ticker,
                           "side": side, "quantity": qty,
                           "price": round(gross / qty, 4) if qty else 0.0, "fee_eur": fee,
                           "amount_eur": round(amount, 2)})
            continue
        m = _DIVIDEND.search(text)
        if m:
            isin = m.group(2)
            income.append({"date": r["date"], "type": "dividend", "isin": isin,
                           "name": NAMES.get(isin, isin), "amount_eur": round(signed, 2),
                           "gross_eur": None, "tax_eur": None})
            continue
        if re.search(r"\b(Interest|Zinsen|Rente|Intérêts)\b", f"{r['type']} {text}", re.I):
            income.append({"date": r["date"], "type": "interest", "amount_eur": round(signed, 2)})
            continue
        if re.search(r"transfer|overboeking|überweisung|virement|einzahlung|auszahlung", f"{r['type']} {text}", re.I):
            flows.append({"date": r["date"], "amount_eur": round(signed, 2)})
            continue
        other.append({"date": r["date"], "type": r["type"], "text": text[:60], "amount_eur": round(signed, 2)})

    stamp = time.strftime("%Y-%m-%d %H:%M")
    source_name = "Trade Republic account statement (PDF)"
    trades_json = {"_source": source_name, "_generated": stamp, "trades": trades}
    income_json = {"_source": source_name, "_generated": stamp, "income": income}
    report = {"trades": len(trades), "dividends": sum(1 for i in income if i["type"] == "dividend"),
              "interest_payments": sum(1 for i in income if i["type"] == "interest"),
              "deposits_eur": round(sum(f["amount_eur"] for f in flows if f["amount_eur"] > 0), 2),
              "withdrawals_eur": round(-sum(f["amount_eur"] for f in flows if f["amount_eur"] < 0), 2),
              "cash_balance_eur": round(balance or 0.0, 2),
              "open_positions": {k: round(v, 6) for k, v in by_isin_qty.items() if abs(v) > 1e-6},
              "unknown_isins": sorted(unknown), "other": other,
              "first_date": rows[0]["date"], "last_date": rows[-1]["date"],
              "balance_checked": len(rows), "problems": problems,
              "warnings": [f"{len(problems)} rows did not add up — check the trades"] if problems else []}
    if any(q < -1e-6 for q in by_isin_qty.values()):
        report["warnings"].append("Some sells have no matching buy: the statement should start when the account opened.")
    return trades_json, income_json, report


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python -m backend.importers.traderepublic_pdf <statement.pdf> [data_dir]")
    from pathlib import Path
    t, i, rep = convert(Path(sys.argv[1]).read_bytes())
    if len(sys.argv) > 2:
        out_dir = Path(sys.argv[2])
        (out_dir / "trades_trade_republic.json").write_text(json.dumps(t, indent=1, ensure_ascii=False))
        (out_dir / "income_trade_republic.json").write_text(json.dumps(i, indent=1, ensure_ascii=False))
    print(json.dumps(rep, indent=1, ensure_ascii=False))
