"""DeGiro account-statement PDF -> the CSV the DeGiro connector reads.

DeGiro offers the account statement both as CSV and as PDF; the PDF is what
the app/website hands you most readily, so we accept it too. The PDF is a
fixed-column table (Date · Time · Value date · Product · ISIN · Description ·
FX · Change · Balance) — the same columns as the CSV export, minus the
`Order Id`. We rebuild that: all rows of one trade (fills, the EUR currency
conversion, the fee) share the same timestamp + ISIN, so that triple becomes a
synthetic order id, which is exactly how the connector groups an order's real
EUR cash flow.

Column boundaries come from the header row on each page (not hard-coded x
positions) so a re-laid-out statement still parses. Long descriptions wrap to a
continuation line (no date) and are re-joined.

Validation: the statement lists a running balance per currency; `validate()`
replays every Change against it and reports the mismatches, so a mis-parsed
number is caught rather than silently corrupting P/L.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

_DATE = re.compile(r"^\d{2}-\d{2}-\d{4}$")
_TIME = re.compile(r"^\d{2}:\d{2}$")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
_NUM = re.compile(r"^-?\d{1,3}(\.\d{3})*(,\d+)?$|^-?\d+(,\d+)?$")

HEADER = ["Date", "Time", "Value date", "Product", "ISIN", "Description",
          "FX", "Change", "", "Balance", "", "Order Id"]

# Rows that belong to a trade and therefore get the synthetic order id.
_FILL = ("Koop ", "Verkoop ")
_ORDER_SIDE_ROWS = ("Valuta Debitering", "Valuta Creditering",
                    "DEGIRO Transactiekosten")
# Corporate actions / fund conversions are never order rows.
_NOT_ORDER = ("Conversie geldmarktfonds", "SPLIT AANPASSING", "DELISTING",
              "PRODUCTWIJZIGING", "WIJZIGING ISIN")

# footer / letterhead words — a line starting with one of these is not data
_SKIP_FIRST = {"Mr", "Mrs", "Ms", "User", "Account", "Date", "flatexDEGIRO", "SE",
               "klanten@degiro.nl", "with", "Amstelplein", "Page", "Netherlands"}


@dataclass
class Row:
    date: str
    time: str
    value_date: str
    product: str = ""
    isin: str = ""
    description: str = ""
    fx: str = ""
    change_ccy: str = ""
    change: str = ""
    balance_ccy: str = ""
    balance: str = ""
    order_id: str = ""
    page: int = 0
    desc_parts: list[str] = field(default_factory=list)


def _num(s: str) -> float:
    return float(s.replace(".", "").replace(",", ".")) if s else 0.0


def _plain(s: str) -> str:
    """'-1.116,03' -> '-1116,03' (what DeGiro's CSV export writes)."""
    return s.replace(".", "") if s else ""


class _Columns:
    """x-anchors derived from a page's header line."""

    def __init__(self, words):
        pos = {w["text"]: w["x0"] for w in words}
        self.time = pos["Time"]
        self.value = pos["Value"]
        self.product = pos["Product"]
        self.isin = pos["ISIN"]
        self.desc = pos["Description"]
        self.fx = pos["FX"]
        self.change = pos["Change"]
        self.balance = pos["Balance"]

    def bucket(self, x0: float) -> str:
        if x0 < self.time - 5:
            return "date"
        if x0 < self.value - 5:
            return "time"
        if x0 < self.product - 5:
            return "value_date"
        if x0 < self.isin - 5:
            return "product"
        if x0 < self.desc - 5:
            return "isin"
        if x0 < self.fx - 3:
            return "description"
        if x0 < self.change - 3:
            return "fx"
        if x0 < self.change + 20:
            return "change_ccy"
        if x0 < self.balance - 3:
            return "change"
        if x0 < self.balance + 20:
            return "balance_ccy"
        return "balance"


def _lines(page):
    """Words grouped into visual lines (sorted top-to-bottom, left-to-right)."""
    rows: dict[int, list] = {}
    for w in page.extract_words(x_tolerance=1.5, y_tolerance=2):
        key = round(w["top"])
        k = next((r for r in rows if abs(r - key) <= 2), key)
        rows.setdefault(k, []).append(w)
    return [sorted(rows[t], key=lambda w: w["x0"]) for t in sorted(rows)]


def parse(source) -> list[Row]:
    """source: path or bytes of the PDF. Returns rows in statement order
    (newest first, as printed)."""
    import pdfplumber  # heavy import kept local: only needed when importing a PDF

    rows: list[Row] = []
    cols: _Columns | None = None
    opened = pdfplumber.open(io.BytesIO(source) if isinstance(source, bytes) else source)
    with opened as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            prev: Row | None = None
            for words in _lines(page):
                texts = [w["text"] for w in words]
                if texts[:2] == ["Date", "Time"] and "Balance" in texts:
                    cols = _Columns(words)
                    prev = None
                    continue
                if cols is None or texts[0] in _SKIP_FIRST:
                    prev = None
                    continue
                if not _DATE.match(texts[0]):
                    # continuation of the previous row's wrapped description
                    if prev is not None and words[0]["x0"] >= cols.desc - 5:
                        prev.desc_parts.extend(texts)
                    continue
                row = Row(date="", time="", value_date="", page=pno)
                for w in words:
                    b = cols.bucket(w["x0"])
                    if b == "date":
                        row.date = w["text"]
                    elif b == "time":
                        row.time = w["text"]
                    elif b == "value_date":
                        row.value_date = w["text"]
                    elif b == "product":
                        row.product = (row.product + " " + w["text"]).strip()
                    elif b == "isin":
                        # a long product name can spill into the ISIN column
                        if _ISIN.match(w["text"]):
                            row.isin = w["text"]
                        else:
                            row.product = (row.product + " " + w["text"]).strip()
                    elif b == "description":
                        row.desc_parts.append(w["text"])
                    else:
                        setattr(row, b, w["text"])
                rows.append(row)
                prev = row
    for r in rows:
        r.description = " ".join(r.desc_parts)
    _assign_order_ids(rows)
    return rows


def _assign_order_ids(rows: list[Row]) -> None:
    groups: dict[tuple, list[Row]] = defaultdict(list)
    for r in rows:
        d = r.description
        if not r.isin or d.startswith(_NOT_ORDER):
            continue
        if d.startswith(_FILL) or d.startswith(_ORDER_SIDE_ROWS):
            groups[(r.date, r.time, r.isin)].append(r)
    for (date, time, isin), members in groups.items():
        if not any(m.description.startswith(_FILL) for m in members):
            continue  # FX/fee rows without a fill at that minute: leave ungrouped
        oid = f"{date[6:]}{date[3:5]}{date[:2]}-{time.replace(':', '')}-{isin}"
        for m in members:
            m.order_id = oid


def to_csv(rows: list[Row]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(HEADER)
    for r in rows:
        w.writerow([r.date, r.time, r.value_date, r.product, r.isin, r.description,
                    r.fx, r.change_ccy, _plain(r.change), r.balance_ccy,
                    _plain(r.balance), r.order_id])
    return buf.getvalue()


def validate(rows: list[Row]) -> dict:
    """Replay the running balance per currency (oldest -> newest). Rows sharing
    a timestamp may be printed in either order, so a mismatch is only counted
    when it does not resolve within the same minute."""
    mismatches: list[dict] = []
    last: dict[str, float] = {}
    pending: dict[str, list[Row]] = defaultdict(list)
    n_chained = 0

    def settle(ccy: str):
        """Resolve one same-minute cluster: find an order that chains."""
        nonlocal n_chained
        cluster = pending.pop(ccy, [])
        if not cluster:
            return
        from itertools import permutations
        start = last.get(ccy)
        options = permutations(cluster) if len(cluster) <= 6 else [cluster]
        for order in options:
            bal, ok = start, True
            for r in order:
                nb = _num(r.balance)
                if bal is not None and abs(bal + _num(r.change) - nb) > 0.011:
                    ok = False
                    break
                bal = nb
            if ok:
                last[ccy] = bal
                n_chained += len(cluster)
                return
        # no order works (DeGiro books deposits/reservations out of printed
        # order): report it and re-anchor on the next cluster
        r = cluster[-1]
        mismatches.append({"page": r.page, "date": r.date, "time": r.time, "ccy": ccy,
                           "rows": [f"{x.description[:40]} {x.change}->{x.balance}" for x in cluster]})
        last.pop(ccy, None)

    key_prev: dict[str, tuple] = {}
    for r in reversed(rows):  # chronological
        if not r.balance_ccy or not r.balance:
            continue
        ccy = r.balance_ccy
        if not r.change:
            continue  # informational rows (flatex transfer notices) carry no change
        k = (r.date, r.time)
        if key_prev.get(ccy) != k:
            settle(ccy)
            key_prev[ccy] = k
        pending[ccy].append(r)
    for ccy in list(pending):
        settle(ccy)

    kinds = Counter()
    for r in rows:
        d = r.description
        kinds["fills" if d.startswith(_FILL) else
              "corporate" if d.startswith(("SPLIT", "DELISTING", "PRODUCTWIJZIGING", "WIJZIGING ISIN")) else
              "dividend" if d.lower().startswith("dividend") else
              "other"] += 1
    # Trade-level checks — these are what P/L is built from.
    fill_re = re.compile(r"(Koop|Verkoop)\s+([\d.,]+)\s+@\s+([\d.,]+)\s+(\w+)")
    fill_bad, fx_bad, fx_missing = [], [], 0
    by_order: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        if r.order_id:
            by_order[r.order_id].append(r)
    for oid, members in by_order.items():
        fills = [m for m in members if m.description.startswith(_FILL)]
        for m in fills:
            g = fill_re.match(m.description)
            if not g:
                fill_bad.append(f"{m.date} {m.description}")
                continue
            expected = _num(g.group(2)) * _num(g.group(3))
            if abs(expected - abs(_num(m.change))) > max(0.02, expected * 0.0005) or m.change_ccy != g.group(4):
                fill_bad.append(f"{m.date} {m.description} booked {m.change_ccy} {m.change}")
        non_eur = any(not m.description.endswith(" EUR") for m in fills)
        deb = [m for m in members if m.description.startswith("Valuta Debitering")]
        cred = [m for m in members if m.description.startswith("Valuta Creditering")]
        if non_eur and not (deb or cred):
            fx_missing += 1
        if deb and cred and all(d.fx for d in deb):
            # sum of foreign debits / rate should equal the EUR credits of the order
            # (rate is per-row but identical within one order; use the first)
            foreign = sum(abs(_num(d.change)) for d in deb if d.change_ccy != "EUR")
            eur_out = sum(abs(_num(d.change)) for d in deb if d.change_ccy == "EUR")
            eur_in = sum(abs(_num(c.change)) for c in cred if c.change_ccy == "EUR")
            foreign_in = sum(abs(_num(c.change)) for c in cred if c.change_ccy != "EUR")
            rate = _num(deb[0].fx)
            if foreign and eur_in:          # sell: foreign proceeds -> EUR
                implied, booked = foreign / rate, eur_in
            elif eur_out and foreign_in:    # buy: EUR -> foreign
                implied, booked = foreign_in / rate, eur_out
            else:
                continue
            if abs(implied - booked) > max(0.05, booked * 0.002):
                fx_bad.append(f"{deb[0].date} {deb[0].isin}: implied EUR {implied:.2f} vs booked {booked:.2f}")
    return {"rows": len(rows), "balance_checked": n_chained,
            "balance_mismatches": len(mismatches), "mismatch_detail": mismatches[:5],
            "orders": len(by_order), "orders_missing_fx_rows": fx_missing,
            "fill_amount_errors": fill_bad[:10], "fx_conversion_errors": fx_bad[:10],
            "first_date": rows[-1].date if rows else None,
            "last_date": rows[0].date if rows else None, **kinds}


def convert(source) -> tuple[str, dict]:
    """PDF (path or bytes) -> (csv_text, validation_report)."""
    rows = parse(source)
    return to_csv(rows), validate(rows)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python -m backend.importers.degiro_pdf <Account.pdf> [out.csv]")
    text, report = convert(sys.argv[1])
    out = sys.argv[2] if len(sys.argv) > 2 else None
    if out:
        open(out, "w", encoding="utf-8").write(text)
        print(f"wrote {out}")
    else:
        sys.stdout.write(text)
    print(report if out else "", file=sys.stderr)
