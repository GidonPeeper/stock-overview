"""The trendline for the sample portfolio (demo mode only).

Real data gets its chart from the trades themselves (trend.py). The sample
holdings have no full trade history, so the demo shows a made-up past instead:
a gentle climb from what was paid to today's value. It is rebuilt from today's
totals on every request, so it always ends on the numbers in the header and
nothing is written to disk.
"""

from __future__ import annotations

import math
from datetime import timedelta

from . import clock


def demo_history(total_value: float, total_cost: float, other_profit: float = 0.0,
                 days: int = 60) -> list[dict]:
    """`days` daily points ending today. `other_profit` is what was earned
    besides price gains (profit from selling, dividends, interest, fees): it
    builds up evenly, so the last point equals the dashboard's total return."""
    today = clock.today()
    out = []
    for i in range(days, -1, -1):
        progress = (days - i) / days  # 0 -> 1 toward today
        value = total_cost + (total_value - total_cost) * progress \
            + math.sin(i / 5.0) * total_value * 0.01          # a wave; 0 today
        unrealized = value - total_cost
        realized = other_profit * progress
        out.append({"day": (today - timedelta(days=i)).isoformat(),
                    "total_value": round(value, 2), "total_cost": round(total_cost, 2),
                    "unrealized": round(unrealized, 2), "realized": round(realized, 2),
                    "total_profit": round(unrealized + realized, 2)})
    return out
