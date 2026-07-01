"""DeGiro connector — derived from the account statement CSV.

DeGiro has no safe API, so we parse the exported account statement
(`data/degiro_account.csv`), which contains every buy/sell back to account
opening. From those trades we derive current holdings and closed positions.

Accuracy note: each trade is grouped by its Order Id and valued by its **actual
EUR cash flow** — the statement records the real EUR amounts DeGiro converted at
(it auto-converts every foreign trade) including transaction fees. So cost basis
and realized P/L are exact EUR, not a flat-rate approximation. Orders fill in
multiple pieces sharing one Order Id; we sum those fills.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from pathlib import Path

from .base import Holding
from ..fx import to_eur
from ..positions import Trade, compute_positions

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _csv_file() -> Path:
    """Real statement if present, otherwise the bundled sample (demo)."""
    real = DATA_DIR / "degiro_account.csv"
    return real if real.exists() else DATA_DIR / "degiro_account.sample.csv"

# ISIN -> yfinance ticker for currently-held names. "" => no live price.
# US listings price in USD; ".DE" in EUR. Extend as holdings change.
TICKER = {
    "US62914V1061": "NIO", "US67066G1040": "NVDA", "US02079K3059": "GOOGL",
    "US46222L1089": "IONQ", "IE00BZCQB185": "QDV5.DE", "US09075V1026": "BNTX",
    "NL0011585146": "RACE", "US0378331005": "AAPL", "US30231G1022": "XOM",
    "US36315X1019": "LKFT",  # Lakefront Biotherapeutics (ex-Galapagos, renamed 2026)
    "US30303M1027": "META",
}

_TRADE_RE = re.compile(r"(Koop|Verkoop)\s+([\d.,]+)\s+@\s+([\d.,]+)\s+(\w+)")
# Corporate actions that change share count but aren't plain trades:
#   "DELISTING: Verkoop N @ 0"      -> shares removed (often worthless)
#   "SPLIT AANPASSING: N NAME @ P"  -> reverse/forward split (old ISIN out, new in)
#   "PRODUCTWIJZIGING : Koop/Verkoop N @ P" -> ISIN/product change
_SPLIT_RE = re.compile(r"SPLIT AANPASSING:\s+([\d.,]+)\s")
_CORP_PREFIXES = ("DELISTING", "SPLIT AANPASSING", "PRODUCTWIJZIGING")


def _num(s: str) -> float:
    # Dutch formatting: "1.234,56" -> 1234.56
    return float(s.replace(".", "").replace(",", ".")) if s else 0.0


def _price_currency(ticker: str) -> str:
    if ticker.endswith((".DE", ".F", ".AS", ".PA", ".MI")):
        return "EUR"
    if ticker.endswith(".L"):
        return "GBP"
    return "USD"


def get_trades() -> list[Trade]:
    csv_file = _csv_file()
    if not csv_file.exists():
        return []

    with csv_file.open(encoding="utf-8") as fh:
        all_rows = [r for r in csv.reader(fh) if len(r) >= 12][1:]

    # Group ALL rows of an order together (fills + the EUR Valuta-conversion rows
    # + fees) so the actual EUR cash flow is captured. Rows with no order id, or
    # order groups that are corporate actions (reverse split / delisting / product
    # change) rather than plain trades, are handled per-row afterwards.
    orders: dict[str, list[list[str]]] = defaultdict(list)
    corporate: list[list[str]] = []
    for row in all_rows:
        if row[11]:
            orders[row[11]].append(row)
        elif row[5].startswith(_CORP_PREFIXES):
            corporate.append(row)

    trades: list[Trade] = []

    def _when(r):
        return f"{r[0][6:]}-{r[0][3:5]}-{r[0][0:2]} {r[1]}"  # "YYYY-MM-DD HH:MM"

    for rows in orders.values():
        fills = [r for r in rows if r[5].startswith(("Koop ", "Verkoop "))]
        if not fills:
            corporate.extend(rows)  # e.g. a delisting/split booked under an order id
            continue
        first = _TRADE_RE.match(fills[0][5])
        side = "BUY" if first.group(1) == "Koop" else "SELL"
        qty = sum(_num(_TRADE_RE.match(f[5]).group(2)) for f in fills)
        isin, name = fills[0][4], fills[0][3]
        # Actual net EUR cash flow = sum of EUR-currency Change rows (real rate + fees).
        eur = abs(sum(_num(r[8]) for r in rows if r[7] == "EUR"))
        if eur < 1e-6:
            eur = abs(qty * _num(first.group(3)) * to_eur(1.0, first.group(4)))
        trades.append(Trade(
            when=_when(fills[0]), key=isin, name=name.title(), side=side,
            quantity=qty, amount=round(eur, 2), currency="EUR",
            ticker=TICKER.get(isin, ""),
        ))

    for r in corporate:
        desc, ccy, change = r[5], r[7], _num(r[8])
        m = _TRADE_RE.search(desc)
        if m:                                   # DELISTING / PRODUCTWIJZIGING
            side = "BUY" if m.group(1) == "Koop" else "SELL"
            qty = _num(m.group(2))
        else:                                   # SPLIT AANPASSING
            sm = _SPLIT_RE.search(desc)
            if not sm:
                continue
            qty = _num(sm.group(1))
            side = "SELL" if change > 0 else "BUY"  # shares leaving vs arriving
        eur = abs(to_eur(change, ccy)) if ccy in ("USD", "EUR", "GBP") else abs(change)
        trades.append(Trade(
            when=_when(r), key=r[4], name=r[3].title(), side=side,
            quantity=qty, amount=round(eur, 2), currency="EUR",
            ticker=TICKER.get(r[4], ""),
        ))

    return trades


def get_holdings() -> list[Holding]:
    holdings: list[Holding] = []
    for p in compute_positions(get_trades()).values():
        if not p.is_open:
            continue
        holdings.append(
            Holding(
                broker="DeGiro",
                ticker=p.ticker,
                name=p.name,
                isin=p.key,
                quantity=round(p.quantity, 6),
                buy_price=round(p.avg_cost, 4),  # EUR cost per share (exact)
                currency=_price_currency(p.ticker) if p.ticker else "EUR",
                buy_currency="EUR",
            )
        )
    return holdings
