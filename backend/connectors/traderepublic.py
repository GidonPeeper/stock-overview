"""Trade Republic connector — derived from transcribed statement trades.

Trade Republic has no API; `data/trades_trade_republic.json` (full history) is
generated from the app's "Transaction export" CSV by
`backend/importers/traderepublic_csv.py` (or transcribed by hand). We derive
both current holdings and closed positions from it. TR books cash in EUR, so
trade amounts are exact EUR; live prices come from each instrument's listing
(USD for US stocks, EUR for Xetra/Euronext-listed ETFs and ASML).
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import Holding
from ..instruments import prefetch, price_currency, ticker_for
from ..positions import Trade, compute_positions

from ..datafiles import resolve as _resolve_data

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _data_file() -> Path:
    return _resolve_data("trades_trade_republic.json")[0]


def get_trades() -> list[Trade]:
    f = _data_file()
    if not f.exists():
        return []
    raw = json.loads(f.read_text()).get("trades", [])
    prefetch({t["isin"]: "EUR" for t in raw if not t.get("ticker")})
    return [
        Trade(
            when=t["date"],
            key=t["isin"],
            name=t["name"],
            side=t["side"],
            quantity=float(t["quantity"]),
            amount=float(t["amount_eur"]),   # exact EUR
            currency="EUR",
            ticker=t.get("ticker") or ticker_for(t["isin"], "EUR"),
        )
        for t in raw
    ]


def get_holdings() -> list[Holding]:
    holdings: list[Holding] = []
    for p in compute_positions(get_trades()).values():
        if not p.is_open:
            continue
        holdings.append(
            Holding(
                broker="Trade Republic",
                ticker=p.ticker,
                name=p.name,
                isin=p.key,
                quantity=round(p.quantity, 6),
                buy_price=round(p.avg_cost, 4),  # EUR cost per share
                currency=price_currency(p.ticker),  # USD for US listings, EUR for Xetra/Euronext
                buy_currency="EUR",
            )
        )
    return holdings
