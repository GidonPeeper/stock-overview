"""Realized P/L and closed positions across all brokers.

Realized P/L is profit already locked in by selling — distinct from the
unrealized P/L on holdings you still own. We compute it from each broker's full
trade history:

  * Trading 212 — order history API (exact EUR per fill)
  * DeGiro       — account statement CSV (trade currency -> EUR via static FX)
  * Trade Republic — transcribed statement trades (exact EUR)

A "closed position" is one whose quantity has returned to zero. Positions you
still hold but have partially sold also carry some realized P/L ("trims"); those
are summed into the broker total but listed separately.
"""

from __future__ import annotations

from .connectors import degiro, traderepublic, trading212
from .fx import to_eur
from .positions import Trade, compute_positions

_SOURCES = {
    "Trading212": trading212.get_trades,
    "DeGiro": degiro.get_trades,
    "Trade Republic": traderepublic.get_trades,
}


def _in_eur(trades: list[Trade]) -> list[Trade]:
    """Convert each trade's cash amount to EUR up front, so realized P/L is
    computed in EUR throughout (correct even if a position was traded in more
    than one currency). T212/TR are already EUR (no-op)."""
    return [
        Trade(
            when=t.when, key=t.key, name=t.name, side=t.side,
            quantity=t.quantity, amount=to_eur(t.amount, t.currency),
            currency="EUR", ticker=t.ticker,
        )
        for t in trades
    ]


def summary() -> dict:
    closed: list[dict] = []
    by_broker: dict[str, float] = {}
    closed_total = trims_total = 0.0

    for broker, get_trades in _SOURCES.items():
        try:
            positions = compute_positions(_in_eur(get_trades()))
        except Exception:
            positions = {}
        broker_total = 0.0
        for p in positions.values():
            if p.sells == 0:
                continue
            broker_total += p.realized
            if p.is_closed:
                closed_total += p.realized
                closed.append(
                    {
                        "broker": broker,
                        "name": p.name,
                        "realized_eur": round(p.realized, 2),
                    }
                )
            else:  # realized on a position still partly held ("trim")
                trims_total += p.realized
        by_broker[broker] = round(broker_total, 2)

    closed.sort(key=lambda c: c["realized_eur"], reverse=True)
    return {
        "closed": closed,
        "closed_total_eur": round(closed_total, 2),
        "trims_total_eur": round(trims_total, 2),
        "by_broker": by_broker,
        "total_realized_eur": round(closed_total + trims_total, 2),
    }
