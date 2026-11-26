"""Realized P/L and closed positions across all brokers.

Realized P/L is profit already locked in by selling — distinct from the
unrealized P/L on holdings you still own. We compute it from each broker's full
trade history:

  * Trading 212 — order history API per account (exact amount per fill)
  * DeGiro       — account statement CSV (trade currency -> EUR via static FX)
  * Trade Republic — transcribed statement trades (exact EUR)

A "closed position" is one whose quantity has returned to zero. Positions you
still hold but have partially sold also carry some realized P/L ("trims"); those
are summed into the broker total but listed separately.

A sale of more shares than the history shows were bought (shares bought
before the statement began, or transferred in) counts only for the shares
with a known cost; the rest are listed under "incomplete" so the gap can be
shown rather than hidden in the total.
"""

from __future__ import annotations

from . import cache, sources
from .positions import compute_positions


# Cached (backend/cache.py): recomputed after uploads/edits, otherwise
# fresh for 5 min and then refreshed in the background.
@cache.cached(ttl=300, stale=3600, copy=True)
def summary() -> dict:
    closed: list[dict] = []
    incomplete: list[dict] = []
    by_broker: dict[str, float] = {}
    closed_total = trims_total = 0.0

    for broker, (_, get_trades) in sources.trade_sources().items():
        try:
            positions = compute_positions(sources.in_eur(get_trades()))
        except Exception:
            positions = {}
        broker_total = 0.0
        for p in positions.values():
            if p.unmatched:
                incomplete.append({"broker": broker, "name": p.name,
                                   "shares_without_purchase": round(p.unmatched, 6)})
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
        "incomplete": incomplete,
    }
