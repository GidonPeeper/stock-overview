"""Portfolio analytics computed from the value history and current holdings.

Everything is derived from data the app already has (the daily value and
profit history in trend.py, holdings, S&P benchmark). Metrics:

  * volatility  — annualized std-dev of monthly returns
  * beta        — regression slope of portfolio vs S&P 500 monthly returns
  * sharpe      — annualized mean excess return / volatility (rf ~ 2%)
  * max_drawdown — worst peak-to-trough of the return index (below)
  * concentration — top-3 holdings weight + effective number of positions (1/HHI)
  * currency / sector exposure — value-weighted breakdown

Returns are flow-adjusted (time-weighted): between two points the gain is the
change in total profit (unrealized + realized), divided by the value at the
start. Deposits, withdrawals and moving money between brokers change the value
but not the profit, so they never show up as gains or losses.
"""

from __future__ import annotations

import math

from . import market, trend

_RF_ANNUAL = 0.02  # assumed risk-free rate
MIN_STEP_VALUE = 500.0  # below this a step's return is noise (one trade dwarfs the move)


def _step_returns(hist: list[dict], min_value: float = MIN_STEP_VALUE) -> list[tuple[str, float]]:
    """(day, return since the previous point) for every point after the first:
    the change in total profit over the value at the start of the step (money
    added or taken out changes the value, not the profit)."""
    out = []
    for a, b in zip(hist, hist[1:]):
        base = a["total_value"]
        r = (b["total_profit"] - a["total_profit"]) / base if base > min_value else 0.0
        out.append((b["day"], max(-0.4, min(r, 0.4))))  # clip leftover data noise
    return out


def _monthly_returns(hist: list[dict]) -> list[tuple[str, float]]:
    """Time-weighted monthly returns (compounded from the points in between).
    Months that start below 5,000 are skipped: there a single trade dwarfs
    the market move."""
    start_value: dict[str, float] = {}
    growth: dict[str, float] = {}
    for (day, r), a in zip(_step_returns(hist), hist):
        m = day[:7]
        if m not in start_value:
            start_value[m] = a["total_value"]
        growth[m] = growth.get(m, 1.0) * (1 + r)
    months = sorted(growth)
    if months and months[-1] == hist[-1]["day"][:7] and len(months) > 1:
        months = months[:-1]  # the current month is not complete yet
    return [(m, growth[m] - 1) for m in months if start_value[m] > 5000]


def max_drawdown(hist: list[dict]) -> float:
    """Worst fall from a peak of the time-weighted return index (0 to -1)."""
    index = peak = 1.0
    mdd = 0.0
    for _, r in _step_returns(hist):
        index *= 1 + r
        peak = max(peak, index)
        mdd = min(mdd, index / peak - 1)
    return mdd


def summary(holdings: list[dict], hist: list[dict] | None = None) -> dict:
    hist = trend.series() if hist is None else hist
    rets = _monthly_returns(hist) if hist else []
    out: dict = {"months": len(rets)}

    if len(rets) >= 6:
        # volatility = sample standard deviation of the monthly returns
        #   (divided by n - 1) x sqrt(12), the usual annualisation of monthly data;
        # Sharpe = (mean monthly return - risk-free rate / 12) / monthly standard
        #   deviation x sqrt(12).
        vals = [r for _, r in rets]
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
        vol_m = math.sqrt(var)
        out["volatility_pct"] = round(vol_m * math.sqrt(12) * 100, 1)
        if vol_m > 0:
            out["sharpe"] = round((mean - _RF_ANNUAL / 12) / vol_m * math.sqrt(12), 2)

        # beta = cov(portfolio, S&P 500) / var(S&P 500), over the same months
        # (the slope of a least-squares line through the monthly returns)
        try:
            sp = market.benchmark(hist[0]["day"][:7] + "-01")
            sp_ret = {}
            ms = sorted(sp)
            for p, c in zip(ms, ms[1:]):
                if sp[p]:
                    sp_ret[c] = sp[c] / sp[p] - 1
            pairs = [(r, sp_ret[m]) for m, r in rets if m in sp_ret]   # (portfolio, S&P)
            if len(pairs) >= 6:
                mp = sum(p for p, _ in pairs) / len(pairs)
                ms_ = sum(s for _, s in pairs) / len(pairs)
                cov = sum((p - mp) * (s - ms_) for p, s in pairs)
                var_s = sum((s - ms_) ** 2 for _, s in pairs)
                if var_s > 0:
                    out["beta"] = round(cov / var_s, 2)
        except Exception:
            pass

    # max drawdown over the whole curve, deposits and withdrawals taken out
    mdd = max_drawdown(hist) if hist else 0.0
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
