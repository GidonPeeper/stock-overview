"""DEGIRO account-statement wording per language, kept in this one place.

DEGIRO writes each statement row in the account's language ("Koop 3 @ 600 EUR",
"Buy 3 Apple Inc@150.5 USD (US0378331005)"), but the column headers, and the
number format of the amount columns, follow the website's language. The two can
differ: a Dutch account exported from the English site has English headers,
Dutch rows and "-77.30" amounts. So rows are matched against every language at
once, and numbers are read from evidence in the file itself (`Numbers`), never
from the header.

Only wording seen in real statements, or in open-source DEGIRO parsers built on
real exports, is listed (sources at the end of this file). A statement whose
buys and sells match none of it is refused at upload with a clear message
(connectors/degiro.check_statement), not imported as empty.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, NamedTuple

# Per language. Row-text lists are matched case-sensitively from the start of
# the description, except the income lists (lower case): "dividends" are
# prefixes, "interest" and "fees" are found anywhere in the row text.
#   buy / sell    first word of a fill ("Buy 3 Apple Inc@150.5 USD")
#   decimal       decimal mark of the numbers inside the row text
#   header        the CSV export's header row (the PDF prints the same titles)
#   order_rows    rows booked with a fill: currency conversion and order fees
#   fx_debit / fx_credit   the conversion rows, by direction (PDF checks only)
#   isin_change   corporate action that only re-keys a holding to a new ISIN
#   split         split rows without a buy/sell word: "SPLIT AANPASSING: 30 X @ 2 EUR"
#   corporate     corporate actions written as a buy/sell ("DELISTING: Verkoop 5 @ 0 EUR")
#   money_market  cash-fund sweeps; never trades
LANGUAGES: dict[str, dict] = {
    "en": {
        "name": "English", "buy": "Buy", "sell": "Sell", "decimal": ".",
        "header": ("Date", "Time", "Value date", "Product", "ISIN", "Description", "FX",
                   "Change", "", "Balance", "", "Order Id"),
        "order_rows": ("FX Debit", "FX Credit", "DEGIRO Transaction"),
        "fx_debit": ("FX Debit",), "fx_credit": ("FX Credit",),
        "isin_change": ("ISIN CHANGE",), "split": ("SPLIT ADJUSTMENT",),
        "corporate": ("DELISTING", "PRODUCT CHANGE", "STOCK SPLIT", "MERGER"),
        "money_market": ("Money Market fund conversion",),
        "dividends": ("dividend", "fund distribution"),
        "interest": ("interest", "money market fund compensation"),
        "fees": ("connection fee", "adr/gdr pass-through fee", "transaction tax", "stamp duty"),
    },
    "nl": {
        "name": "Dutch", "buy": "Koop", "sell": "Verkoop", "decimal": ",",
        "header": ("Datum", "Tijd", "Valutadatum", "Product", "ISIN", "Omschrijving", "FX",
                   "Mutatie", "", "Saldo", "", "Order Id"),
        "order_rows": ("Valuta Debitering", "Valuta Creditering", "DEGIRO Transactiekosten"),
        "fx_debit": ("Valuta Debitering",), "fx_credit": ("Valuta Creditering",),
        "isin_change": ("WIJZIGING ISIN",), "split": ("SPLIT AANPASSING",),
        "corporate": ("DELISTING", "PRODUCTWIJZIGING"),
        "money_market": ("Conversie geldmarktfonds",),
        "dividends": ("dividend",),                       # Dividend, Dividendbelasting
        "interest": ("rente", "securities lending", "geldmarktfondsen compensatie"),
        "fees": ("aansluitingskosten", "adr/gdr externe kosten", "transactiebelasting"),
    },
    "de": {
        "name": "German", "buy": "Kauf", "sell": "Verkauf", "decimal": ",",   # "Kauf 3 zu je 64,91 EUR"
        "header": ("Datum", "Uhrzeit", "Valutadatum", "Produkt", "ISIN", "Beschreibung", "FX",
                   "Änderung", "", "Saldo", "", "Order-ID"),
        "order_rows": ("Währungswechsel", "FX-Gutschrift", "FX-Belastung",
                       "DEGIRO Transaktionsgebühr", "Transaktionsgebühr"),
        "fx_debit": ("Währungswechsel (Ausbuchung)", "FX-Belastung"),
        "fx_credit": ("Währungswechsel (Einbuchung)", "FX-Gutschrift"),
        "isin_change": ("ISIN-ÄNDERUNG",), "split": (),
        "corporate": ("AKTIENSPLIT", "FUSION:"),
        "money_market": ("Geldmarktfonds Umwandlung",),
        "dividends": ("dividend", "fondsausschüttung"),    # Dividende, Dividendensteuer
        "interest": ("geldmarktfonds kompensation", "zinsen"),
        "fees": ("einrichtung von handelsmodalitäten", "adr/gdr weitergabegebühr",
                 "gebühr für kapitalmaßnahme"),
    },
    "fr": {
        "name": "French", "buy": "Achat", "sell": "Vente", "decimal": ",",
        "header": None,    # only English headers seen over French rows
        "order_rows": ("Opération de change", "Operation de change", "Frais DEGIRO de courtage"),
        "fx_debit": ("Opération de change - Débit", "Operation de change - Débit"),
        "fx_credit": ("Opération de change - Crédit", "Operation de change - Crédit"),
        "isin_change": (), "split": (), "corporate": (),
        "money_market": ("Conversion Fonds Monétaires",),
        "dividends": ("dividend", "impôts sur dividende", "impôt sur les dividendes"),
        "interest": ("intérêts",),
        "fees": ("frais de connexion",),
    },
    "es": {
        "name": "Spanish", "buy": "Compra", "sell": "Venta", "decimal": ",",
        "header": ("Fecha", "Hora", "Fecha valor", "Producto", "ISIN", "Descripción", "Tipo",
                   "Variación", "", "Saldo", "", "ID Orden"),
        "order_rows": ("Ingreso Cambio de Divisa", "Retirada Cambio de Divisa", "Costes de transacción"),
        "fx_debit": ("Retirada Cambio de Divisa",), "fx_credit": ("Ingreso Cambio de Divisa",),
        "isin_change": ("CAMBIO DE ISIN",), "split": (),
        "corporate": ("DESLISTAMIENTO", "EMISIÓN DE DERECHOS", "DIVIDENDO FLEXIBLE",
                      "DIVIDENDO OPCIONAL", "DIVIDENDO EN ACCIONES", "OFERTA DE SUSCRIPCIÓN"),
        "money_market": ("Conversión fondos del mercado monetario",),
        "dividends": ("dividend", "retención del dividendo"),
        "interest": (),
        "fees": ("comisión de conectividad",),
    },
    "it": {
        "name": "Italian", "buy": "Acquisto", "sell": "Vendita", "decimal": ",",
        "header": ("Data", "Ora", "Data Valore", "Prodotto", "ISIN", "Descrizione", "Borsa",
                   "Variazioni", "", "Saldo", "", "ID Ordine"),
        "order_rows": ("Credito FX", "Prelievo FX", "DEGIRO Commissioni di transazione",
                       "DEGIRO costi di transazione"),
        "fx_debit": ("Prelievo FX",), "fx_credit": ("Credito FX",),
        "isin_change": (), "split": (), "corporate": ("CAMBIO DI PRODOTTO",),
        "money_market": ("Conversione Fondo Comune Monetario",),
        "dividends": ("dividend", "ritenuta sul dividendo", "imposta sui dividendi"),
        "interest": ("interesse",),
        "fees": ("costi di connessione",),
    },
    "pt": {
        "name": "Portuguese", "buy": "Compra", "sell": "Venda", "decimal": ",",
        "header": ("Data", "Hora", "Data Valor", "Produto", "ISIN", "Descrição", "T.",
                   "Mudança", "", "Saldo", "", "ID da Ordem"),
        "order_rows": ("Crédito de divisa", "Levantamento de divisa", "Comissões de transação"),
        "fx_debit": ("Levantamento de divisa",), "fx_credit": ("Crédito de divisa",),
        "isin_change": (), "split": (), "corporate": (), "money_market": (),
        "dividends": ("dividend", "imposto sobre dividendo"),
        "interest": (),
        "fees": ("custo de conectividade",),
    },
    "cs": {
        "name": "Czech", "buy": "Nákup", "sell": "Prodej", "decimal": ",",
        "header": ("Datum", "Čas", "Datum", "Produkt", "ISIN", "Popis", "Kurz",
                   "Pohyb", "", "Zůstatek", "", "ID objednávky"),
        "order_rows": ("FX vyučtování konverze měny",),
        "fx_debit": (), "fx_credit": (),     # one wording for both directions
        "isin_change": (), "split": (), "corporate": (),
        "money_market": ("Konverze Peněžního Fondu",),
        "dividends": ("dividend", "daň z dividendy"),
        "interest": (),
        "fees": ("poplatek za obchodování na zahraničních burzách",),
    },
    "pl": {
        "name": "Polish", "buy": "Kupno", "sell": "Sprzedaż", "decimal": ",",
        "header": ("Data", "Czas", "Data", "Produkt", "ISIN", "Opis", "Kurs",
                   "Zmiana", "", "Saldo", "", "Identyfikator zlecenia"),
        "order_rows": (), "fx_debit": (), "fx_credit": (),
        "isin_change": (), "split": (), "corporate": (), "money_market": (),
        "dividends": ("dywidend", "podatek dywidendowy"),
        "interest": (), "fees": (),
    },
}

# Languages that group thousands with a space ("Vente 1 046,3825 @ ...").
_SPACE_THOUSANDS = ("fr", "cs", "pl")


def _all(key: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(w for lang in LANGUAGES.values() for w in lang[key]))


NAMES = ", ".join(lang["name"] for lang in LANGUAGES.values())
ORDER_ROWS = _all("order_rows")
FX_DEBIT, FX_CREDIT = _all("fx_debit"), _all("fx_credit")
ISIN_CHANGE, SPLIT, CORPORATE = _all("isin_change"), _all("split"), _all("corporate")
MONEY_MARKET = _all("money_market")
DIVIDENDS, INTEREST, FEES = _all("dividends"), _all("interest"), _all("fees")
# PDF column titles: the CSV header without its unnamed currency columns and the order id
PDF_HEADERS = {code: tuple(lang["header"][i] for i in (0, 1, 2, 3, 4, 5, 6, 7, 9))
               for code, lang in LANGUAGES.items() if lang["header"]}

# ---- buy/sell rows ----------------------------------------------------------

_SIDE: dict[str, tuple[str, str]] = {}          # word -> (BUY/SELL, language)
for _code, _lang in LANGUAGES.items():
    _SIDE.setdefault(_lang["buy"], ("BUY", _code))
    _SIDE.setdefault(_lang["sell"], ("SELL", _code))

_PRICE = r"\d(?:[\d.,'’\u00a0\u202f]|\s(?=\d))*"
_QTY = r"\d[\d.,'’\u00a0\u202f]*"    # no plain space: a name may start with digits ("Buy 5 3M CO@...")
_QTY_SPACED = rf"\d{{1,3}}(?:[ \u00a0\u202f]\d{{3}})+(?:,\d+)?|{_QTY}"


def _fill_re(words: list[str], qty: str) -> re.Pattern:
    alt = "|".join(sorted(map(re.escape, words), key=len, reverse=True))
    # side, quantity, then (optional) "<name>@<price> <CCY>" or German "zu je <price> <CCY>"
    return re.compile(rf"(?<!\w)(?P<side>{alt})\s+(?P<qty>{qty})"
                      rf"(?:\s+(?:(?P<name>.*?)\s*@|zu\s+je)\s*(?P<price>{_PRICE})\s+(?P<ccy>[A-Z]{{3}})\b)?")


_SPACED = [w for w, (_, code) in _SIDE.items() if code in _SPACE_THOUSANDS]
_FILL_RES = [_fill_re(_SPACED, _QTY_SPACED), _fill_re([w for w in _SIDE if w not in _SPACED], _QTY)]
_SPLIT_RE = re.compile(rf"(?:{'|'.join(map(re.escape, SPLIT))})\s*:\s+({_QTY})\s") if SPLIT else None
# the shape of a buy/sell in any language: "<word> <qty> ...@<price> <CCY>"
_FILL_SHAPE = re.compile(r"^\S+\s+\d[\d.,'’]*\s.*@\s*\d[\d.,'’]*\s+[A-Z]{3}\b")


class Fill(NamedTuple):
    side: str            # "BUY" / "SELL"
    qty: str             # as written, read it with Numbers.text()
    price: str | None    # None when the row text was cut short (a PDF line lost)
    ccy: str | None
    lang: str


def _fill(m: re.Match | None) -> Fill | None:
    if m is None:
        return None
    side, lang = _SIDE[m["side"]]
    return Fill(side, m["qty"], m["price"], m["ccy"], lang)


def fill(desc: str) -> Fill | None:
    """The buy/sell a row's text starts with, in any known language, else None."""
    return next((f for f in (_fill(r.match(desc)) for r in _FILL_RES) if f), None)


def find_fill(desc: str) -> Fill | None:
    """A complete buy/sell anywhere in the text, e.g. after a corporate-action
    prefix ("PRODUCTWIJZIGING : Koop 500 @ 0,039 EUR")."""
    for r in _FILL_RES:
        f = _fill(r.search(desc))
        if f and f.ccy:
            return f
    return None


def looks_like_fill(desc: str) -> bool:
    """A buy/sell-shaped row ("<word> 10 ACME@5,5 EUR") whatever its language."""
    return bool(_FILL_SHAPE.match(desc))


def split_qty(desc: str) -> str | None:
    """Share count of a split row without a buy/sell word ("SPLIT AANPASSING: 30 X @ 2 EUR")."""
    m = _SPLIT_RE.search(desc) if _SPLIT_RE else None
    return m.group(1) if m else None


def corporate(desc: str) -> str | None:
    """"isin_change", "split" or "trade" for a corporate-action row, else None."""
    if desc.startswith(ISIN_CHANGE):
        return "isin_change"
    if SPLIT and desc.startswith(SPLIT):
        return "split"
    if desc.startswith(CORPORATE):
        return "trade"
    return None


def income_kind(desc: str) -> str | None:
    """"dividends" (withholding tax included, negative), "interest" or "fees"
    for a row outside orders, else None."""
    if corporate(desc):
        return None              # "DIVIDENDO FLEXIBLE: Venta ..." is a corporate action
    d = desc.lower()
    if d.startswith(DIVIDENDS):
        return "dividends"
    if any(w in d for w in INTEREST):
        return "interest"
    if any(w in d for w in FEES):
        return "fees"
    return None


# ---- numbers ----------------------------------------------------------------

_STRIP = str.maketrans("", "", " \u00a0\u202f'’")


def _own_mark(s: str) -> str | None:
    """The decimal mark a number's own form shows: "1,234.56", "21,31", "0.039",
    "1'105" and "1.234.567" are certain; "1.000" or "682.568" could be either."""
    if "'" in s or "’" in s:
        return "."                       # Swiss thousands: 1’667.31
    t = s.strip().lstrip("+-").translate(_STRIP)
    dots, commas = t.count("."), t.count(",")
    if dots and commas:
        return "." if t.rfind(".") > t.rfind(",") else ","
    if not (dots or commas):
        return None
    sep, other = (".", ",") if dots else (",", ".")
    if t.count(sep) > 1:
        return other                     # repeated: thousands separators
    head, tail = t.split(sep)
    if len(tail) != 3 or head in ("", "0") or len(head) > 3:
        return sep
    return None


def decimal_mark(numbers: Iterable[str], default: str) -> str:
    """The decimal mark most of these numbers show by their form; `default`
    when none shows it (or on a tie)."""
    votes = Counter(m for m in map(_own_mark, numbers) if m)
    if not votes:
        return default
    return max(votes, key=lambda k: (votes[k], k == default))


def to_float(s: str, mark: str) -> float:
    """A number as written. Its own form decides where it is certain, `mark`
    (the file's decimal mark) where it is not."""
    if not s or not s.strip():
        return 0.0
    m = _own_mark(s) or mark
    return float(s.strip().translate(_STRIP).replace("," if m == "." else ".", "").replace(m, "."))


def plain(s: str, mark: str) -> str:
    """Thousands separators dropped, decimal mark kept: "-1.116,03" -> "-1116,03"
    (what DEGIRO's CSV export writes)."""
    if not s:
        return ""
    m = _own_mark(s) or mark
    return s.strip().translate(_STRIP).replace("," if m == "." else ".", "")


class Numbers:
    """How one statement writes its numbers. The amount columns and the row
    text are judged apart: an English-site export of a Dutch account has
    "-77.30" amounts next to "Koop 1 @ 77,3 EUR"."""

    def __init__(self, rows: list[list[str]]):
        fills = [f for f in (fill(r[5]) for r in rows if len(r) > 5) if f]
        langs = Counter(f.lang for f in fills)
        default = LANGUAGES[langs.most_common(1)[0][0]]["decimal"] if langs else ","
        self.text_mark = decimal_mark((x for f in fills for x in (f.qty, f.price) if x), default)
        self.amount_mark = decimal_mark((r[i] for r in rows for i in (6, 8, 10) if len(r) > i),
                                        self.text_mark)

    def amount(self, s: str) -> float:
        return to_float(s, self.amount_mark)

    def text(self, s: str | None) -> float:
        return to_float(s or "", self.text_mark)


# ---- sources ----------------------------------------------------------------
# Real statements (anonymised), Portfolio Performance's DEGIRO PDF tests:
#   github.com/portfolio-performance/portfolio/tree/master/name.abuchen.portfolio.tests/
#   src/name/abuchen/portfolio/datatransfer/pdf/degiro  (AccountStatement*, Rekeningoverzicht*,
#   Kontoauszug*, AccountStatement_french*, EstadoDeCuenta01, EstrattoConto*, Prehleductu01)
# CSV exports and parsers built on them:
#   en  VibeNL/GhostfolioSidekick Parsers.UnitTests/TestFiles/DeGiro/EN, pviotti/degiro-bookkeeper,
#       craicoverflow/taxman internal/ingest/degiro (SPLIT ADJUSTMENT)
#   nl  dickwolff/Export-To-Ghostfolio samples/degiro-export.csv, GhostfolioSidekick .../NL
#   de  vroonhof/opensteuerauszug (header, FX-Gutschrift/-Belastung, FUSION, Zinsen)
#   fr  vroonhof/opensteuerauszug tests/samples/import/degiro/fr/Account.csv
#   es  mxmhos/degiro estadoCuenta.csv
#   it  db10x/tracker import/Degiro Account.csv
#   pt  GhostfolioSidekick .../PT, Tomas-Silva-PT/maisvalias-tool data/mockdata/degiro/Account.csv
#   cs  marekskopal/fingather backend/tests/Fixtures/Import/File/degiro_export.csv
#   pl  mwarzynski/tax_stocks app/providers/degiro.py (parser only, no statement seen)
