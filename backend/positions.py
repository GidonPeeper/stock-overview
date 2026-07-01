"""Shared position math.

Given a chronological list of trades, compute per-instrument state using the
average-cost method: current quantity, remaining cost basis, and realized P/L
(profit already locked in by sells). Both open holdings and closed positions are
derived from the same trade history, so there is a single source of truth.

`amount` on each trade is the total cash value of that fill (cost for a buy,
proceeds for a sell). Whatever currency the caller expresses `amount` in is the
currency the resulting cost basis and realized P/L come out in.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Trade:
    when: str          # ISO date/datetime, used only for ordering
    key: str           # stable instrument id (ISIN preferred)
    name: str
    side: str          # "BUY" or "SELL"
    quantity: float
    amount: float      # total cash value of the fill, in `currency`
    currency: str = "EUR"
    ticker: str = ""   # market-data ticker for live pricing (open positions)


@dataclass
class Position:
    key: str
    name: str
    ticker: str = ""
    currency: str = "EUR"
    quantity: float = 0.0
    cost: float = 0.0          # remaining cost basis of open shares
    realized: float = 0.0      # P/L locked in by sells
    sells: int = 0

    @property
    def avg_cost(self) -> float:
        return self.cost / self.quantity if self.quantity > 1e-9 else 0.0

    @property
    def is_open(self) -> bool:
        return self.quantity > 1e-4

    @property
    def is_closed(self) -> bool:
        return self.sells > 0 and not self.is_open


def compute_positions(trades: list[Trade]) -> dict[str, Position]:
    positions: dict[str, Position] = {}
    for t in sorted(trades, key=lambda x: x.when):
        p = positions.get(t.key)
        if p is None:
            p = positions[t.key] = Position(key=t.key, name=t.name)
        p.name = t.name
        if t.ticker:
            p.ticker = t.ticker
        p.currency = t.currency
        if t.side == "BUY":
            p.quantity += t.quantity
            p.cost += t.amount
        else:  # SELL — realize against the running average cost
            if p.quantity > 1e-9:
                avg = p.cost / p.quantity
                sold = min(t.quantity, p.quantity)
                p.realized += t.amount - avg * sold
                p.cost -= avg * sold
            p.quantity -= t.quantity
            p.sells += 1
    return positions
