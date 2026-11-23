"""Every account the dashboard reads, in one place.

Trading 212 can contribute several accounts (e.g. "T212 ISA" and "T212 Invest");
DeGiro and Trade Republic contribute one each. Realized P/L is computed per
account, so the same share held in an ISA and in a normal account stays in two
separate cost pools, as HMRC treats them.
"""

from __future__ import annotations

from typing import Callable

from . import connections
from .connectors import degiro, traderepublic, trading212
from .connectors.base import Holding
from .fx import to_eur
from .positions import Trade


def holdings() -> list[Holding]:
    return (trading212.get_holdings() + degiro.get_holdings() + traderepublic.get_holdings()
            + connections.holdings())  # accounts added in Settings (IBKR, CSV, ...)


def trade_sources() -> dict[str, tuple[bool, Callable[[], list[Trade]]]]:
    """{account label: (is_isa, get_trades)}."""
    out = dict(trading212.trade_sources())
    out["DeGiro"] = (False, degiro.get_trades)
    out["Trade Republic"] = (False, traderepublic.get_trades)
    out.update(connections.trade_sources())
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


# ------------------------------------------------------------------ caching
# Everything above is wrapped here, so a connector added to `holdings()` or
# `trade_sources()` is cached with no extra code (see backend/cache.py):
#   holdings     fresh 30 s, then served while a background refresh runs (1 h)
#   trade lists  per account, fresh 5 min, then served while refreshing (1 h)
# Any upload or edit (a write under /api/, or a changed data file) makes them
# recompute before they are served again. A failing broker keeps its last
# good result. Trade lists are shared: treat them as read-only.
import functools as _functools

from . import cache as _cache

_uncached_holdings = holdings
_uncached_trade_sources = trade_sources


@_functools.wraps(_uncached_holdings)
def holdings() -> list[Holding]:  # noqa: F811 - cached wrapper of the function above
    return _cache.get("sources.holdings", _uncached_holdings, ttl=30, stale=3600, copy=True)


def _cached_trades(label: str, get_trades: Callable[[], list[Trade]]) -> list[Trade]:
    return list(_cache.get(f"sources.trades:{label}", get_trades, ttl=300, stale=3600))


@_functools.wraps(_uncached_trade_sources)
def trade_sources() -> dict[str, tuple[bool, Callable[[], list[Trade]]]]:  # noqa: F811
    return {label: (is_isa, _functools.partial(_cached_trades, label, get))
            for label, (is_isa, get) in _uncached_trade_sources().items()}
