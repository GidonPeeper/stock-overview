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


_DUST = 1e-9  # shares: a position this small has nothing left to sell


def _time_key(when: str) -> str:
    """Sortable time. Trading 212 writes "2024-03-05T09:00:00Z", DeGiro
    "2024-03-05 15:00"; compared as text "T" sorts after " ", which put a
    15:00 sale before a 09:00 buy when accounts are pooled."""
    return when.replace("T", " ", 1)


def _chronological(trades: list[Trade]) -> list[Trade]:
    """Trades oldest first. Trades of one instrument at the same moment keep
    the order given, unless that order sells shares before the buy that
    brings them (a statement listed newest first, a date-only file): then
    that moment's buys of the instrument go first, so no sale meets an empty
    position that the next line fills."""
    ordered = sorted(trades, key=lambda t: _time_key(t.when))
    out: list[Trade] = []
    held: dict[str, float] = {}
    i = 0
    while i < len(ordered):
        moment = _time_key(ordered[i].when)
        j = i + 1
        while j < len(ordered) and _time_key(ordered[j].when) == moment:
            j += 1
        group = ordered[i:j]
        if len(group) > 1:
            flip = set()
            for key in {t.key for t in group}:
                mine = [t for t in group if t.key == key]
                q, short = held.get(key, 0.0), False
                for t in mine:
                    q = q + t.quantity if t.side == "BUY" else q - t.quantity
                    short = short or q < -_DUST
                if short and any(t.side == "BUY" for t in mine):
                    flip.add(key)
            if flip:
                group.sort(key=lambda t: t.key in flip and t.side != "BUY")
        for t in group:
            q = held.get(t.key, 0.0)
            held[t.key] = q + t.quantity if t.side == "BUY" else max(q - t.quantity, 0.0)
        out.extend(group)
        i = j
    return out


def _match(quantity: float, cost: float, t: Trade) -> tuple[float, float, float]:
    """The part of sale `t` that meets the shares held: (shares, their
    proceeds, their average cost). Shares sold beyond the holding have no
    recorded purchase (bought before the statement began, transferred in),
    so their part of the proceeds is left out instead of counted as profit:
    one sale of 12 from 10 held gives the same result as a sale of 10 and
    then one of 2 from nothing."""
    if quantity <= _DUST:
        return 0.0, 0.0, 0.0
    sold = max(min(t.quantity, quantity), 0.0)
    share = sold / t.quantity if t.quantity > 0 else 1.0
    matched_cost = cost if sold >= quantity else cost / quantity * sold
    return sold, t.amount * share, matched_cost


def compute_positions(trades: list[Trade]) -> dict[str, Position]:
    positions: dict[str, Position] = {}
    for t in _chronological(trades):
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
            _, proceeds, cost = _match(p.quantity, p.cost, t)
            p.realized += proceeds - cost
            p.cost -= cost
            # never below zero: a sale beyond the holding leaves nothing, so
            # a later round trip is booked in full
            p.quantity = max(p.quantity - t.quantity, 0.0)
            p.sells += 1
    return positions


def realized_events(trades: list[Trade], accounts: dict[int, str] | None = None) -> list[dict]:
    """One entry per sell: the P/L it locked in against the running average
    cost, with its date. Same method as compute_positions, kept per event so
    gains can be grouped by (tax) year. Expects one currency per call.
    `accounts` ({id(trade): account name}) lets trades from several accounts
    share one pool and still says which account each sale came from."""
    pools: dict[str, list[float]] = {}  # key -> [quantity, cost]
    out: list[dict] = []
    for t in _chronological(trades):
        q, c = pools.setdefault(t.key, [0.0, 0.0])
        if t.side == "BUY":
            pools[t.key] = [q + t.quantity, c + t.amount]
            continue
        sold, proceeds, cost = _match(q, c, t)
        pools[t.key] = [max(q - t.quantity, 0.0), c - cost]
        if q <= _DUST:
            continue
        ev = {"when": t.when, "key": t.key, "name": t.name,
              "quantity": sold, "proceeds": proceeds,
              "realized": proceeds - cost, "currency": t.currency}
        if accounts is not None:
            ev["account"] = accounts.get(id(t), "")
        out.append(ev)
    return out
