"""DeGiro connector — derived from the account statement CSV.

DeGiro has no safe API, so we parse the exported account statement
(`data/degiro_account.csv`), which contains every buy/sell back to account
opening. From those trades we derive current holdings and closed positions.

Accuracy note: each trade is grouped by its Order Id and valued by its **actual
EUR cash flow** — the statement records the real EUR amounts DeGiro converted at
(it auto-converts every foreign trade) including transaction fees. So cost basis
and realized P/L are exact EUR, not a flat-rate approximation. Orders fill in
multiple pieces sharing one Order Id; we sum those fills.

Statements in any language in backend/degiro_lang.py are read (English, Dutch,
German, ...); an upload in another one is refused by `check_statement`.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from pathlib import Path

from .base import Holding
from .. import degiro_lang as lang
from ..degiro_lang import Numbers
from ..fx import to_eur
from ..instruments import TICKERS, prefetch, price_currency, ticker_for
from ..positions import Trade, compute_positions

from ..datafiles import resolve as _resolve_data

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _csv_file() -> Path:
    """Real statement wherever it's mounted (data/, root, /etc/secrets), else sample."""
    return _resolve_data("degiro_account.csv")[0]

# ISIN -> yfinance ticker (shared table in backend/instruments.py). "" => no live price.
TICKER = TICKERS

# Row wording (buys/sells, corporate actions) and number formats live in
# backend/degiro_lang.py, for every statement language the app reads.
# Corporate actions that change share count but aren't plain trades (Dutch
# wording; the other languages' equivalents are in degiro_lang):
#   "DELISTING: Verkoop N @ 0"      -> shares removed (often worthless)
#   "SPLIT AANPASSING: N NAME @ P"  -> reverse/forward split (old ISIN out, new in)
#   "PRODUCTWIJZIGING : Koop/Verkoop N @ P" -> ISIN/product change
#   "WIJZIGING ISIN: Verkoop N @ P" + "WIJZIGING ISIN: Koop N @ P" -> pure ISIN
#     change (e.g. Exxon's 2026 holding-company reorg): the position is re-keyed
#     to the new ISIN with its cost basis intact — nothing was sold.


def _rows(csv_file: Path) -> list[list[str]]:
    """Statement rows (header dropped). A long order id can wrap onto a row of
    its own (",,,,,,,,,,,b017-a61a6d03c3e7"): it is joined back onto its row.
    A fill without an order id gets one from its minute and ISIN, shared with
    the conversion and fee rows booked with it (as the PDF importer does)."""
    with csv_file.open(encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh) if len(r) >= 12][1:]
    out: list[list[str]] = []
    for r in rows:
        if out and r[11] and not any(r[:11]):
            out[-1] = out[-1][:11] + [out[-1][11] + r[11]] + out[-1][12:]
        else:
            out.append(r)
    groups: dict[tuple, list[list[str]]] = defaultdict(list)
    for r in out:
        if not r[11] and r[4] and (lang.fill(r[5]) or r[5].startswith(lang.ORDER_ROWS)):
            groups[(r[0], r[1], r[4])].append(r)
    for (date, time, isin), members in groups.items():
        if any(lang.fill(m[5]) for m in members):
            for m in members:
                m[11] = f"{date} {time} {isin}"
    return out


def _price_currency(ticker: str) -> str:
    return price_currency(ticker)


def get_trades() -> list[Trade]:
    csv_file = _csv_file()
    if not csv_file.exists():
        return []

    all_rows = _rows(csv_file)
    num = Numbers(all_rows)

    # Group ALL rows of an order together (fills + the EUR Valuta-conversion rows
    # + fees) so the actual EUR cash flow is captured. Rows with no order id, or
    # order groups that are corporate actions (reverse split / delisting / product
    # change) rather than plain trades, are handled per-row afterwards.
    orders: dict[str, list[list[str]]] = defaultdict(list)
    corporate: list[list[str]] = []
    for row in all_rows:
        if row[11]:
            orders[row[11]].append(row)
        elif lang.corporate(row[5]):
            corporate.append(row)

    trades: list[Trade] = []
    traded_in: dict[str, str] = {}     # ISIN -> currency of its first fill

    def _when(r):
        return f"{r[0][6:]}-{r[0][3:5]}-{r[0][0:2]} {r[1]}"  # "YYYY-MM-DD HH:MM"

    for rows in orders.values():
        fills = [(r, f) for r in rows if (f := lang.fill(r[5]))]
        if not fills:
            corporate.extend(rows)  # e.g. a delisting/split booked under an order id
            continue
        row0, first = fills[0]
        qty = sum(num.text(f.qty) for _, f in fills)
        isin, name = row0[4], row0[3]
        if first.ccy:
            traded_in.setdefault(isin, first.ccy)
        # Actual net EUR cash flow = sum of EUR-currency Change rows (real rate + fees).
        eur = abs(sum(num.amount(r[8]) for r in rows if r[7] == "EUR"))
        if eur < 1e-6 and not any(r[7] == "EUR" for r in rows):
            # A GBP (or CHF) account books the order's real cash flow, fees
            # included, in its own currency; foreign legs net to zero against
            # their FX rows.
            net: dict[str, float] = defaultdict(float)
            for r in rows:
                if r[7]:
                    net[r[7]] += num.amount(r[8])
            eur = abs(sum(to_eur(v, c) for c, v in net.items() if abs(v) > 0.005))
        if eur < 1e-6:
            eur = abs(qty * num.text(first.price) * to_eur(1.0, first.ccy or "EUR"))
        trades.append(Trade(
            when=_when(row0), key=isin, name=name.title(), side=first.side,
            quantity=qty, amount=round(eur, 2), currency="EUR",
        ))

    # ISIN changes: pair the sell (old ISIN) with the buy (new ISIN) row
    # booked at the same moment and remember old -> new (name from the new row).
    renames: dict[str, tuple[str, str]] = {}
    changes = [(r, lang.find_fill(r[5])) for r in corporate if lang.corporate(r[5]) == "isin_change"]
    for out, f in changes:
        if not f or f.side != "SELL":
            continue
        new = next((r for r, g in changes if g and g.side == "BUY" and r[0] == out[0]
                    and r[1] == out[1] and r[4] != out[4]), None)
        if new is not None:
            renames[out[4]] = (new[4], new[3].title())
    corporate = [r for r in corporate if lang.corporate(r[5]) != "isin_change"]

    def eur_of(change: float, ccy: str) -> float:
        return abs(to_eur(change, ccy)) if ccy in ("USD", "EUR", "GBP") else abs(change)

    # A split is booked as two rows at the same moment: the old shares out
    # (cash +) and the new shares in (cash -), often under a new ISIN. Nothing
    # is sold: such a pair is kept apart and booked below at the position's
    # own cost, so it realizes nothing and the new shares carry the cost.
    moments: dict[tuple, list] = defaultdict(list)
    for r in corporate:
        if not lang.find_fill(r[5]) and lang.split_qty(r[5]):
            moments[(r[0], r[1])].append(r)
    pairs = []
    for rs in moments.values():
        outs = [r for r in rs if num.amount(r[8]) > 0]
        ins = [r for r in rs if num.amount(r[8]) < 0]
        if len(rs) == 2 and len(outs) == 1 and len(ins) == 1:
            pairs.append((outs[0], ins[0]))
            if outs[0][4] != ins[0][4] and outs[0][4] not in renames:
                renames[outs[0][4]] = (ins[0][4], ins[0][3].title())   # one position, new ISIN
    paired = {id(r) for pair in pairs for r in pair}

    for r in corporate:
        if id(r) in paired:
            continue
        desc, ccy, change = r[5], r[7], num.amount(r[8])
        f = lang.find_fill(desc)
        if f:                                   # DELISTING / PRODUCTWIJZIGING
            side, qty = f.side, num.text(f.qty)
        else:                                   # a split row without its other half
            sq = lang.split_qty(desc)
            if not sq:
                continue
            qty = num.text(sq)
            side = "SELL" if change > 0 else "BUY"  # shares leaving vs arriving
        trades.append(Trade(
            when=_when(r), key=r[4], name=r[3].title(), side=side,
            quantity=qty, amount=round(eur_of(change, ccy), 2), currency="EUR",
        ))

    def current(key: str) -> tuple[str, str | None]:
        """Follow ISIN changes (possibly chained) to the current ISIN."""
        name, seen = None, set()
        while key in renames and key not in seen:
            seen.add(key)
            key, name = renames[key]
        return key, name

    for t in trades:
        key, name = current(t.key)
        if name is not None:
            t.key, t.name = key, name

    # the split pairs, oldest first: out and in at what the shares had cost
    for out, new in sorted(pairs, key=lambda p: _when(p[0])):
        when, key = _when(out), current(new[4])[0]
        q_out, q_in = num.text(lang.split_qty(out[5])), num.text(lang.split_qty(new[5]))
        held = compute_positions(sorted((t for t in trades if t.key == key and t.when <= when),
                                        key=lambda t: t.when)).get(key)
        if held is not None and held.quantity > 1e-9 and q_out > 0:
            cost = held.cost * min(1.0, q_out / held.quantity)
            amounts = (cost, cost)
        else:   # no purchase on record: as before, at the value DEGIRO booked
            amounts = (eur_of(num.amount(out[8]), out[7]), eur_of(num.amount(new[8]), new[7]))
        name = new[3].title()
        trades.append(Trade(when=when, key=key, name=name, side="SELL", quantity=q_out,
                            amount=round(amounts[0], 2), currency="EUR"))
        trades.append(Trade(when=when, key=key, name=name, side="BUY", quantity=q_in,
                            amount=round(amounts[1], 2), currency="EUR"))

    # the ISIN table, else a Yahoo lookup (instruments.py); "" = no live price
    prefetch({t.key: traded_in.get(t.key) for t in trades})
    for t in trades:
        t.ticker = ticker_for(t.key, traded_in.get(t.key))
    return trades


def check_statement(text: str, pdf: bool = False) -> dict:
    """Refuse an upload the app would read as empty, with a message that names
    the problem. Returns {"fills", "orders"} for the upload report."""
    how = "Export it from DEGIRO: Activity → Account statement (PDF or CSV), all dates."
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    head = rows[0] if rows else []
    if len(head) >= 15 and "ISIN" in head[:5]:
        raise ValueError("That is DEGIRO's Transactions export. Upload the Account statement "
                         "instead: Activity → Account statement (PDF or CSV), all dates.")
    if len(head) < 12 or head[4].strip() != "ISIN":
        raise ValueError("That doesn't look like a DEGIRO account statement. " + how)
    data = [r for r in rows[1:] if len(r) >= 12 and any(r[:11])]
    if not data:
        if pdf:
            raise ValueError("No statement table found in that PDF. If it is the DEGIRO account "
                             "statement, it may be in a language the app does not read yet "
                             f"(it reads {lang.NAMES}). Try the CSV export instead.")
        raise ValueError("That DEGIRO statement has no rows. " + how)
    fills = [r for r in data if lang.fill(r[5])]
    if not fills:
        odd = next((r[5] for r in data if lang.looks_like_fill(r[5]) and not lang.find_fill(r[5])), None)
        if odd:
            raise ValueError("This looks like a DEGIRO statement in a language the app does not "
                             f'read yet: it cannot read buys and sells such as "{odd[:40]}". '
                             f"It reads statements in {lang.NAMES}.")
        raise ValueError("No buys or sells found in that DEGIRO statement. " + how)
    return {"fills": len(fills), "orders": len({r[11] or (r[0], r[1], r[4]) for r in fills})}


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
