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
positions) so a re-laid-out statement still parses; the header may be in any
language in backend/degiro_lang.py, as may the rows. Long descriptions wrap to
a continuation line (no date) and are re-joined.

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

from .. import degiro_lang as lang

_DATE = re.compile(r"^\d{2}-\d{2}-\d{4}$")
_TIME = re.compile(r"^\d{2}:\d{2}$")
_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")

HEADER = ["Date", "Time", "Value date", "Product", "ISIN", "Description",
          "FX", "Change", "", "Balance", "", "Order Id"]

# Rows that belong to a trade and therefore get the synthetic order id:
# fills (lang.fill) and the currency conversion and fee rows booked with them.
_ORDER_SIDE_ROWS = lang.ORDER_ROWS
# Corporate actions / fund conversions are never order rows.
_NOT_ORDER = lang.MONEY_MARKET + lang.SPLIT + lang.CORPORATE + lang.ISIN_CHANGE

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


def _mark(rows: list[Row]) -> str:
    """The statement's decimal mark, from its amount columns ("-1.116,03" or
    "-1,116.03"; Swiss "1’116.03", French "1 116,03")."""
    return lang.decimal_mark((x for r in rows for x in (r.change, r.balance, r.fx)), ",")


_KEYS = ("date", "time", "value", "product", "isin", "desc", "fx", "change", "balance")


def _header(words) -> "_Columns | None":
    """Column anchors if this line is the table header, in any known language.
    Older statements have no value-date column."""
    texts = [w["text"] for w in words]
    if "ISIN" not in texts:
        return None
    for titles in lang.PDF_HEADERS.values():
        x, i = {}, 0
        for key, title in zip(_KEYS, titles):
            parts = title.split()
            got = texts[i:i + len(parts)]
            # a clipped title ("Uhrze") still counts
            if len(got) == len(parts) and all(g == t or (len(g) >= 4 and t.startswith(g))
                                              for g, t in zip(got, parts)):
                x[key] = words[i]["x0"]
                i += len(parts)
            elif key != "value":
                break
        else:
            return _Columns(x)
    return None


class _Columns:
    """x-anchors derived from a page's header line."""

    def __init__(self, x: dict[str, float]):
        self.time = x["time"]
        self.product = x["product"]
        self.value = x.get("value", self.product)
        self.isin = x["isin"]
        self.desc = x["desc"]
        self.fx = x["fx"]
        self.change = x["change"]
        self.balance = x["balance"]

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
            lines = _lines(page)
            # Free the parsed page at once: kept, a 300-page statement needs
            # ~900 MB, more than a free server has (the upload then dies).
            page.close()
            for words in lines:
                texts = [w["text"] for w in words]
                head = _header(words)
                if head is not None:
                    cols = head
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
                    elif b in ("fx", "change", "balance"):
                        # "1 116,03": a space-grouped number comes as two words
                        setattr(row, b, getattr(row, b) + w["text"])
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
        if lang.fill(d) or d.startswith(_ORDER_SIDE_ROWS):
            groups[(r.date, r.time, r.isin)].append(r)
    for (date, time, isin), members in groups.items():
        if not any(lang.fill(m.description) for m in members):
            continue  # FX/fee rows without a fill at that minute: leave ungrouped
        oid = f"{date[6:]}{date[3:5]}{date[:2]}-{time.replace(':', '')}-{isin}"
        for m in members:
            m.order_id = oid


def to_csv(rows: list[Row]) -> str:
    """Amounts as DEGIRO's CSV export writes them: no thousands separators,
    the statement's own decimal mark ("-1.116,03" -> "-1116,03")."""
    mark = _mark(rows)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(HEADER)
    for r in rows:
        w.writerow([r.date, r.time, r.value_date, r.product, r.isin, r.description,
                    r.fx, r.change_ccy, lang.plain(r.change, mark), r.balance_ccy,
                    lang.plain(r.balance, mark), r.order_id])
    return buf.getvalue()


def validate(rows: list[Row]) -> dict:
    """Replay the running balance per currency (oldest -> newest). Rows sharing
    a timestamp may be printed in either order, so a mismatch is only counted
    when it does not resolve within the same minute."""
    mark = _mark(rows)

    def _num(s: str) -> float:
        return lang.to_float(s, mark)

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
        kinds["fills" if lang.fill(d) else
              "corporate" if d.startswith("SPLIT") or lang.corporate(d) else
              "dividend" if lang.income_kind(d) == "dividends" else
              "other"] += 1
    # Trade-level checks — these are what P/L is built from.
    text_mark = lang.Numbers([["", "", "", "", "", r.description] for r in rows]).text_mark
    # the account's own currency: the one most rows are booked in
    base = Counter(r.change_ccy for r in rows if r.change_ccy).most_common(1)
    base = base[0][0] if base else "EUR"
    fill_bad, fx_bad, fx_missing = [], [], 0
    by_order: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        if r.order_id:
            by_order[r.order_id].append(r)
    for oid, members in by_order.items():
        fills = [(m, g) for m in members if (g := lang.fill(m.description))]
        for m, g in fills:
            if not g.price:
                fill_bad.append(f"{m.date} {m.description}")
                continue
            expected = lang.to_float(g.qty, text_mark) * lang.to_float(g.price, text_mark)
            if abs(expected - abs(_num(m.change))) > max(0.02, expected * 0.0005) or m.change_ccy != g.ccy:
                fill_bad.append(f"{m.date} {m.description} booked {m.change_ccy} {m.change}")
        non_eur = any(g.ccy != base for _, g in fills)
        deb = [m for m in members if m.description.startswith(lang.FX_DEBIT)]
        cred = [m for m in members if m.description.startswith(lang.FX_CREDIT)]
        conv = deb or cred or [m for m in members if m.description.startswith(lang.ORDER_ROWS)
                               and m.change_ccy != base]
        if non_eur and not conv:
            fx_missing += 1
        if deb and cred and all(d.fx for d in deb):
            # sum of foreign debits / rate should equal the EUR credits of the order
            # (rate is per-row but identical within one order; use the first)
            foreign = sum(abs(_num(d.change)) for d in deb if d.change_ccy != base)
            eur_out = sum(abs(_num(d.change)) for d in deb if d.change_ccy == base)
            eur_in = sum(abs(_num(c.change)) for c in cred if c.change_ccy == base)
            foreign_in = sum(abs(_num(c.change)) for c in cred if c.change_ccy != base)
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
