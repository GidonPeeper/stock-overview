"""Trade Republic connector — derived from transcribed statement trades.

Trade Republic has no API, so trades are transcribed from the account statement
PDF into `data/trades_trade_republic.json` (full history). We derive both
current holdings and closed positions from them. TR books cash in EUR, so trade
amounts are exact EUR; live prices come from the US listings (USD).
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import Holding
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
    return [
        Trade(
            when=t["date"],
            key=t["isin"],
            name=t["name"],
            side=t["side"],
            quantity=float(t["quantity"]),
            amount=float(t["amount_eur"]),   # exact EUR
            currency="EUR",
            ticker=t.get("ticker", ""),
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
                currency="USD",                  # US listings price in USD
                buy_currency="EUR",
            )
        )
    return holdings
