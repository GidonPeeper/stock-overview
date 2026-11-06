"""Every account the dashboard reads, in one place.

Trading 212 can contribute several accounts (e.g. "T212 ISA" and "T212 Invest");
DeGiro and Trade Republic contribute one each. Realized P/L is computed per
account, so the same share held in an ISA and in a normal account stays in two
separate cost pools, as HMRC treats them.
"""

from __future__ import annotations

from typing import Callable

from .connectors import degiro, traderepublic, trading212
from .connectors.base import Holding
from .fx import to_eur
from .positions import Trade


def holdings() -> list[Holding]:
    return trading212.get_holdings() + degiro.get_holdings() + traderepublic.get_holdings()


def trade_sources() -> dict[str, tuple[bool, Callable[[], list[Trade]]]]:
    """{account label: (is_isa, get_trades)}."""
    out = dict(trading212.trade_sources())
    out["DeGiro"] = (False, degiro.get_trades)
    out["Trade Republic"] = (False, traderepublic.get_trades)
    return out


def all_trades() -> list[Trade]:
    out: list[Trade] = []
    for _, get in trade_sources().values():
        try:
            out.extend(get())
        except Exception:
            continue
    return out


def in_eur(trades: list[Trade]) -> list[Trade]:
    """Same trades with every cash amount converted to EUR (today's rate for
    non-EUR accounts, e.g. a GBP Trading 212 account)."""
    return [
        t if t.currency == "EUR" else Trade(
            when=t.when, key=t.key, name=t.name, side=t.side, quantity=t.quantity,
            amount=to_eur(t.amount, t.currency), currency="EUR", ticker=t.ticker)
        for t in trades
    ]


def all_trades_eur() -> list[Trade]:
    return in_eur(all_trades())
