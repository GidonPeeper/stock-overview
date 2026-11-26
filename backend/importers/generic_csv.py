"""Generic CSV positions import -- for brokers without a dedicated importer.

Most brokers can export "positions" or "holdings" as a CSV file, but every one
names the columns differently. This module:

  1. reads the file (comma, semicolon, tab or pipe separated; UTF-8 or
     Windows-1252; skips preamble lines above the header row);
  2. guesses which column holds what (`detect`): symbol/ticker, ISIN, name,
     quantity, cost (per share or total), market value, price, currency;
  3. turns rows into holdings with the mapping the user confirmed (`parse`).

Numbers like "£1,234.56", "1.234,56", "(12.50)" and "12 %" are understood.
Rows without a quantity (totals, cash lines, notes) are skipped.

CLI:  python -m backend.importers.generic_csv positions.csv
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys

from ..instruments import TICKERS, is_valid_isin, price_currency, ticker_for

FIELDS = ("symbol", "isin", "name", "quantity", "cost", "value", "price", "currency")

# Normalised header (lowercase, letters and digits only) -> field. Exact
# matches win; then a header containing one of these words.
_EXACT = {
    "symbol": ["symbol", "ticker", "tickersymbol", "code", "epic", "symbool", "instrumentcode",
               "stockcode", "securitysymbol"],
    "isin": ["isin", "isincode", "symboolisin", "securityid", "isinnumber"],
    "name": ["name", "description", "security", "securityname", "instrument", "product",
             "holding", "company", "stock", "investment", "naam", "fund", "fundname",
             "investmentname", "assetname", "asset", "title", "securitydescription"],
    "quantity": ["quantity", "qty", "shares", "units", "aantal", "position", "sharesheld",
                 "unitsheld", "numberofshares", "noofshares", "holdingquantity", "amount",
                 "quantityheld", "volume", "stuks"],
    "cost_per_share": ["averagecost", "avgcost", "averageprice", "avgprice", "costpershare",
                       "averagecostpershare", "averagepricepaid", "avgpricepaid", "gak",
                       "purchaseprice", "pricepaid", "buyprice", "averagebuyprice",
                       "costbasisprice", "openprice", "averagepurchaseprice", "avgcostbasis",
                       "averagecostbasis", "unitcost", "aankoopkoers", "gemiddeldeaankoopkoers"],
    "cost_total": ["costbasis", "bookcost", "totalcost", "cost", "bookvalue", "invested",
                   "costbasistotal", "totalcostbasis", "costbasismoney", "aankoopwaarde",
                   "amountinvested", "totalinvested", "purchasevalue", "costvalue"],
    "value": ["marketvalue", "value", "currentvalue", "totalvalue", "positionvalue", "waarde",
              "valuation", "presentvalue", "mktval", "marketval", "holdingvalue", "worth",
              "waardeineur", "lokalewaarde"],
    "price": ["price", "lastprice", "currentprice", "marketprice", "koers", "slotkoers",
              "markprice", "lasttradeprice", "closeprice", "shareprice", "unitprice", "last"],
    "currency": ["currency", "ccy", "valuta", "curr", "currencycode", "tradingcurrency",
                 "pricecurrency", "munt"],
}
_CONTAINS = [  # checked in this order
    ("isin", ["isin"]),
    ("cost_per_share", ["averagecost", "avgcost", "averageprice", "avgprice", "pershare",
                        "pricepaid", "purchaseprice"]),
    ("cost_total", ["costbasis", "bookcost", "totalcost", "invested", "bookvalue"]),
    ("value", ["marketvalue", "currentvalue", "value"]),
    ("quantity", ["quantity", "shares", "units", "qty"]),
    ("symbol", ["ticker", "symbol"]),
    ("currency", ["currency", "ccy"]),
    ("price", ["price", "koers"]),
    ("name", ["description", "name", "security", "instrument"]),
]
# Field priority when two fields want the same column.
_ORDER = ("isin", "symbol", "quantity", "cost_per_share", "cost_total", "currency", "value",
          "price", "name")

_CCY = re.compile(r"^[A-Za-z]{3}$")
_PENCE = {"GBX", "GBp", "GBx"}           # pence, as fx.unit reads them ("GBp" is not pounds)
_SYMBOLS = {"€": "EUR", "£": "GBP"}      # unambiguous currency signs in a number cell
# A total line, not a holding ("Total", "Subtotal", "Cash"), but not "TotalEnergies".
_TOTAL_ROW = re.compile(r"^(grand |sub)?totals?\b|^(totaal|summe|gesamt|cash)\b")
# Numeric fields that a "contains" match must not take from a percentage or
# change column ("Value change %", "Price change").
_AMOUNTS = {"quantity", "cost_per_share", "cost_total", "value", "price"}
_NOT_AMOUNT = ("change", "chg", "pct", "percent", "gain", "loss", "return", "profit", "weight",
               "allocation", "yield", "diff")


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


def _not_amount(header: str) -> bool:
    return "%" in header or any(w in _norm(header) for w in _NOT_AMOUNT)


def _decimal_comma(body: list[list[str]], delim: str) -> bool:
    """Does the file write 1.234,56 (decimal comma)? Decided by the numbers
    themselves where they say so ("12,50" or "1.234,56" vs "12.50" or
    "1,234.56"), else by the separator (";" files are European)."""
    comma = dot = 0
    for row in body[:200]:
        for c in row:
            s = re.sub(r"[^0-9,.]", "", c)
            if re.search(r"\d,(\d{1,2}|\d{4,})$|\.\d{3},\d", s):
                comma += 1
            elif re.search(r"\d\.(\d{1,2}|\d{4,})$|,\d{3}\.\d", s):
                dot += 1
    if comma and not dot:
        return True
    if dot and not comma:
        return False
    return delim == ";"


# ------------------------------------------------------------------ reading
def decode(content: bytes) -> str:
    if content[:2] == b"PK":
        raise ValueError("That looks like an Excel file. Save it as CSV first "
                         "(File > Save as > CSV) and upload that.")
    if content[:5] == b"%PDF-":
        raise ValueError("That is a PDF. Export the positions as a CSV file instead.")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def _delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:30])
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in ",;\t|"}
        return max(counts, key=counts.get) if any(counts.values()) else ","


def _known(header: str) -> bool:
    n = _norm(header)
    if not n:
        return False
    if any(n in words for words in _EXACT.values()):
        return True
    return any(w in n for _, words in _CONTAINS for w in words)


def read_table(content: bytes) -> tuple[list[str], list[list[str]], str]:
    """(header, rows, delimiter). The header is the first of the first 20 rows
    with at least two recognisable column names (so preamble lines such as
    "Account: ..." above the table are skipped)."""
    text = decode(content)
    delim = _delimiter(text)
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delim)]
    rows = [[c.strip() for c in r] for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("The file is empty.")
    start = 0
    for i, r in enumerate(rows[:20]):
        if sum(_known(c) for c in r) >= 2:
            start = i
            break
    header = rows[start]
    # make duplicate / blank headers unique so each column can be chosen
    seen: dict[str, int] = {}
    clean = []
    for i, h in enumerate(header):
        h = h or f"Column {i + 1}"
        if h in seen:
            seen[h] += 1
            h = f"{h} ({seen[h]})"
        else:
            seen[h] = 1
        clean.append(h)
    body = [r + [""] * (len(clean) - len(r)) for r in rows[start + 1:]]
    return clean, body, delim


# ---------------------------------------------------------------- detection
def detect(header: list[str]) -> dict:
    """Guess the column for each field. Returns
    {symbol, isin, name, quantity, cost, cost_kind, value, price, currency}
    with column names (or None) and cost_kind "per_share" or "total"."""
    normed = {h: _norm(h) for h in header}
    found: dict[str, str] = {}
    used: set[str] = set()

    def take(field: str, col: str) -> None:
        if field not in found and col not in used:
            found[field] = col
            used.add(col)

    for field in _ORDER:                       # exact names first
        for h in header:
            if normed[h] in _EXACT[field]:
                take(field, h)
                break
    for field, words in _CONTAINS:             # then "contains"
        if field in found:
            continue
        for h in header:
            if h not in used and any(w in normed[h] for w in words) \
                    and not (field in _AMOUNTS and _not_amount(h)):
                take(field, h)
                break

    if "cost_per_share" in found:
        cost, kind = found["cost_per_share"], "per_share"
    elif "cost_total" in found:
        cost, kind = found["cost_total"], "total"
    else:
        cost, kind = None, "per_share"
    return {"symbol": found.get("symbol"), "isin": found.get("isin"), "name": found.get("name"),
            "quantity": found.get("quantity"), "cost": cost, "cost_kind": kind,
            "value": found.get("value"), "price": found.get("price"),
            "currency": found.get("currency")}


# ------------------------------------------------------------------ numbers
def number(s: str, decimal_comma: bool = False) -> float | None:
    s = (s or "").strip()
    if not s or s in ("-", "--", "n/a", "N/A"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^0-9,.\-+eE]", "", s.replace("−", "-"))
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):     # 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:                               # 1,234.56
            s = s.replace(",", "")
    elif "," in s:
        last = s.rsplit(",", 1)[1]
        if decimal_comma or len(last) != 3 or s.count(",") == 1 and s.startswith(("0,", "-0,")):
            s = s.replace(".", "").replace(",", ".") if s.count(",") == 1 else s.replace(",", "")
        else:
            s = s.replace(",", "")
    elif decimal_comma and re.fullmatch(r"[-+]?\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")              # "1.234" in a decimal-comma file is 1234
    try:
        v = float(s)
    except ValueError:
        return None
    return -abs(v) if neg else v


def _cell(row: list[str], header: list[str], col: str | None) -> str:
    if not col or col not in header:
        return ""
    i = header.index(col)
    return row[i] if i < len(row) else ""


# ------------------------------------------------------------------ parsing
def parse(content: bytes, mapping: dict, default_currency: str = "GBP") -> tuple[list[dict], dict]:
    """Rows -> [{ticker, name, isin, quantity, buy_price, cost_currency,
    price, price_currency}], report {rows, used, skipped}."""
    header, body, delim = read_table(content)
    missing = [c for f, c in mapping.items() if f in FIELDS and c and c not in header]
    if missing:
        raise ValueError(f"Column not found in the file: {', '.join(missing)}")
    if not mapping.get("quantity"):
        raise ValueError("Choose the column with the number of shares (quantity).")
    if not (mapping.get("symbol") or mapping.get("isin") or mapping.get("name")):
        raise ValueError("Choose a column that names the investment (symbol, ISIN or name).")
    dec_comma = _decimal_comma(body, delim)
    per_share = (mapping.get("cost_kind") or "per_share") == "per_share"
    default_currency = (default_currency or "GBP").strip()

    out, skipped = [], 0
    for row in body:
        qty = number(_cell(row, header, mapping.get("quantity")), dec_comma)
        symbol = _cell(row, header, mapping.get("symbol")).strip()
        isin = _cell(row, header, mapping.get("isin")).strip().upper()
        name = _cell(row, header, mapping.get("name")).strip()
        label = (name or symbol or isin).lower()
        if not qty or qty <= 0 or not (symbol or isin or name) or _TOTAL_ROW.match(label):
            skipped += 1
            continue
        if isin and not is_valid_isin(isin):
            # some files put the ISIN in a "Symbol/ISIN" column, others junk
            isin = isin if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin) else ""
        ccy = _cell(row, header, mapping.get("currency")).strip()
        value_cell = _cell(row, header, mapping.get("value"))
        prefix = re.match(r"\s*([A-Z]{3})\s", value_cell)   # DEGIRO: "USD 1.234,00"
        sign = next((_SYMBOLS[ch] for f in ("value", "cost", "price")
                     for ch in _cell(row, header, mapping.get(f)) if ch in _SYMBOLS), None)
        ccy = ccy if _CCY.match(ccy) else (prefix.group(1) if prefix else sign or default_currency)
        # Pence-quoted rows (GBX, GBp): per-share figures are in pence, totals
        # (value, total cost) in pounds -- the usual UK layout.
        pence = 0.01 if ccy in _PENCE or ccy.upper() == "GBX" else 1.0
        ccy = "GBP" if pence != 1.0 else ccy.upper()
        cost = number(_cell(row, header, mapping.get("cost")), dec_comma)
        buy_price = (cost * pence if per_share else cost / qty) if cost is not None else None
        value = number(_cell(row, header, mapping.get("value")), dec_comma)
        price = number(_cell(row, header, mapping.get("price")), dec_comma)
        # the market value is money (unambiguous); a price column may be in pence
        price = value / qty if value is not None else (price * pence if price is not None else None)
        if symbol and is_valid_isin(symbol.upper()):
            isin = isin or symbol.upper()
            symbol = ""
        ticker = ticker_for(isin, ccy) if isin else ""
        if not ticker and symbol:
            sym = symbol.upper().replace(" ", "-")
            # a bare symbol is only safe to price live for US listings; others
            # need the Yahoo suffix (VWRL.L) or they keep the file's own price
            if "." in sym.strip(".") or ccy == "USD":
                ticker = sym
        out.append({
            "ticker": ticker, "name": name or symbol or isin, "isin": isin,
            "quantity": qty,
            # no cost in the file: count today's value as cost (no fake profit)
            "buy_price": round(buy_price if buy_price is not None else (price or 0.0), 6),
            "cost_currency": ccy,
            "price": round(price, 6) if price is not None else None,
            # live quotes come in the listing's currency; the file's own price in its currency
            "price_currency": price_currency(ticker) if ticker and ccy != "USD" else ccy,
        })
    return out, {"rows": len(body), "used": len(out), "skipped": skipped}


def preview(content: bytes, mapping: dict | None = None, default_currency: str = "GBP") -> dict:
    """What the Settings page shows before saving: the columns, the guessed
    (or given) mapping, the first raw rows and how they would be read."""
    header, body, _ = read_table(content)
    guess = detect(header)
    if mapping:
        guess.update({k: (v or None) for k, v in mapping.items() if k in guess})
    try:
        holdings, report = parse(content, guess, default_currency)
        error = ""
    except ValueError as e:
        holdings, report, error = [], {"rows": len(body), "used": 0, "skipped": len(body)}, str(e)
    return {"columns": header, "mapping": guess, "sample": body[:5],
            "holdings": holdings[:8], "report": report, "error": error}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m backend.importers.generic_csv positions.csv")
    data = open(sys.argv[1], "rb").read()
    print(json.dumps(preview(data), indent=1, ensure_ascii=False))
