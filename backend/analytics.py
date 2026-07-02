"""Portfolio analytics computed from the snapshot history and current holdings.

Everything is derived from data the app already has (monthly value history,
holdings, S&P benchmark), so it works offline and in demo mode. Metrics:

  * volatility  — annualized std-dev of monthly returns
  * beta        — regression slope of portfolio vs S&P 500 monthly returns
  * sharpe      — annualized mean excess return / volatility (rf ~ 2%)
  * max_drawdown — worst peak-to-trough of the value curve
  * concentration — top-3 holdings weight + effective number of positions (1/HHI)
  * currency / sector exposure — value-weighted breakdown

Monthly returns are computed flow-adjusted: r = (V_t - V_{t-1} - net_flow) / V_{t-1},
using cost-basis deltas as the flow proxy, so deposits don't masquerade as gains.
"""

from __future__ import annotations

import math

from . import market, store

_RF_ANNUAL = 0.02  # assumed risk-free rate


def _monthly_returns(hist: list[dict]) -> list[tuple[str, float]]:
    """Flow-adjusted monthly returns from the snapshot history."""
    # last snapshot of each month
    by_month: dict[str, dict] = {}
    for s in hist:
        by_month[s["day"][:7]] = s
    months = sorted(by_month)
    out: list[tuple[str, float]] = []
    for prev, cur in zip(months, months[1:]):
        a, b = by_month[prev], by_month[cur]
        flow = b["total_cost"] - a["total_cost"]  # new money in (or out)
        base = a["total_value"]
        if base > 5000:  # skip the tiny-portfolio start, where flows dwarf value
            r = (b["total_value"] - a["total_value"] - flow) / base
            out.append((cur, max(-0.4, min(r, 0.4))))  # winsorize residual flow noise
    return out


def summary(holdings: list[dict]) -> dict:
    hist = store.get_history()
    rets = _monthly_returns(hist)
    out: dict = {"months": len(rets)}

    if len(rets) >= 6:
        vals = [r for _, r in rets]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
        vol_m = math.sqrt(var)
        out["volatility_pct"] = round(vol_m * math.sqrt(12) * 100, 1)
        if vol_m > 0:
            out["sharpe"] = round((mean - _RF_ANNUAL / 12) / vol_m * math.sqrt(12), 2)

        # beta vs S&P 500 (monthly closes)
        try:
            sp = market.benchmark(hist[0]["day"][:7] + "-01")
            sp_ret = {}
            ms = sorted(sp)
            for p, c in zip(ms, ms[1:]):
                if sp[p]:
                    sp_ret[c] = sp[c] / sp[p] - 1
            pairs = [(r, sp_ret[m]) for m, r in rets if m in sp_ret]
            if len(pairs) >= 6:
                py = [p[0] for p in pairs]
                px = [p[1] for p in pairs]
                mx, my = sum(px) / len(px), sum(py) / len(py)
                cov = sum((x - mx) * (y - my) for x, y in pairs)
                varx = sum((x - mx) ** 2 for x in px)
                if varx > 0:
                    out["beta"] = round(cov / varx, 2)
        except Exception:
            pass

    # max drawdown over the whole curve
    peak, mdd = 0.0, 0.0
    for s in hist:
        v = s["total_value"]
        peak = max(peak, v)
        if peak > 500:
            mdd = min(mdd, (v - peak) / peak)
    if mdd < 0:
        out["max_drawdown_pct"] = round(mdd * 100, 1)

    # concentration + exposures from current holdings
    total = sum(h["value_eur"] for h in holdings) or 1.0
    weights = sorted((h["value_eur"] / total for h in holdings), reverse=True)
    out["top3_pct"] = round(sum(weights[:3]) * 100, 1)
    hhi = sum(w * w for w in weights)
    if hhi > 0:
        out["effective_positions"] = round(1 / hhi, 1)

    ccy: dict[str, float] = {}
    for h in holdings:
        ccy[h["currency"]] = ccy.get(h["currency"], 0.0) + h["value_eur"]
    out["currency_exposure"] = [
        {"currency": c, "pct": round(v / total * 100, 1)}
        for c, v in sorted(ccy.items(), key=lambda kv: -kv[1])
    ]
    return out
