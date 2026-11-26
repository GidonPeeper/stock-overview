"""Numbers over time, risk figures and what Claude is told: trend.py,
analytics.py, reports.py, insights.py, mcp_tools.py and the chart's range
arithmetic in index.html.

Every test works out the right answer by hand in its comments. No network:
prices, exchange rates and Yahoo calls are small made-up tables.
"""

import importlib.util
import json
import shutil
import subprocess
import tempfile
import sys
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import pandas as pd

from backend import analytics, cache, clock, fx, insights, reports, sources, trend
from backend.positions import Trade
from tests.test_trend import DAYS, frame

ROOT = Path(__file__).resolve().parents[1]


def rates(**per_unit):
    """fx._live_rates replacement: EUR per unit of each currency."""
    return mock.patch.object(fx, "_live_rates", return_value={"EUR": 1.0, **per_unit})


class Clean(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)


# ------------------------------------------------------------- analytics
class RiskFiguresTest(Clean):
    def test_beta_volatility_and_sharpe_by_hand(self):
        # Six complete months; the portfolio moves exactly twice as much as the S&P 500.
        #   S&P 500 monthly returns  s = +1%, +3%, -1%, +2%, 0%, +1%   (mean 1%)
        #   portfolio monthly returns r = 2s = +2%, +6%, -2%, +4%, 0%, +2%   (mean 2%)
        # beta = cov(r, s) / var(s) = 2 exactly.
        # The old code paired each return with the other series' mean:
        #   sum (r - mean_s)(s - mean_r) = cov - n (mean_r - mean_s)^2 = 0.002 - 6 x 0.0001
        #   = 0.0014, over var 0.001 -> beta 1.4.
        # volatility: deviations from 2%: 0, +4, -4, +2, -2, 0 (in %); squares 0.004 in
        #   total; sample variance 0.004 / 5 = 0.0008; sd 2.828%; x sqrt(12) = 9.8%.
        # Sharpe: (2% - 2%/12) / 2.828% x sqrt(12) = 2.25.
        s = [0.01, 0.03, -0.01, 0.02, 0.0, 0.01]
        months = [f"2024-0{m}" for m in range(1, 7)]
        hist = [{"day": "2023-12-29", "total_value": 10000.0, "total_profit": 0.0}]
        profit = 0.0
        for m, x in zip(months, s):
            profit += 2 * x * 10000        # a return of 2s on a steady 10,000
            hist.append({"day": m + "-28", "total_value": 10000.0, "total_profit": profit})
        hist.append({"day": "2024-07-10", "total_value": 10000.0, "total_profit": profit})  # month not over
        sp, level = {"2023-12": 100.0}, 100.0
        for m, x in zip(months, s):
            level *= 1 + x
            sp[m] = level
        with mock.patch.object(analytics.market, "benchmark", return_value=sp):
            out = analytics.summary([], hist)
        self.assertEqual(out["months"], 6)
        self.assertEqual(out["beta"], 2.0)
        self.assertEqual(out["volatility_pct"], 9.8)
        self.assertEqual(out["sharpe"], 2.25)

    def test_too_little_history_gives_no_risk_figures(self):
        # five complete months: not enough for volatility, Sharpe or beta
        hist = [{"day": f"2024-0{m}-28", "total_value": 10000.0, "total_profit": 100.0 * m}
                for m in range(1, 8)]
        out = analytics.summary([], hist)
        self.assertEqual(out["months"], 5)
        for k in ("volatility_pct", "sharpe", "beta"):
            self.assertNotIn(k, out)


# ----------------------------------------------------------------- trend
class TrendMoneyTest(Clean):
    def build(self, accts, close, today=date(2024, 1, 9)):
        with mock.patch.object(trend, "_fetch", return_value=(close, None)), \
                mock.patch.object(trend, "quote_scale", return_value=1.0):
            return trend.build(accts, today=today, income_events=[])

    def test_sp500_comparison_is_in_euros(self):
        # EUR 1,000 into a US share on day 1. The share and the S&P 500 both stay
        # at $100. EUR/USD is 1.00 on days 1-2 and 1.25 from day 3 (dollar assets
        # lose 20% in euros).
        # Same money in the S&P 500: EUR 1,000 = $1,000 = 10 units; from day 3
        # 10 x $100 = $1,000 = EUR 800, exactly like the share (also EUR 800).
        # Old code: units bought with euros at a dollar price, never converted
        # back -> EUR 1,000 throughout.
        trades = {"A": [Trade("2024-01-02", "US0000000001", "Share", "BUY", 10, 1000.0, ticker="XX")]}
        close = frame({"XX": [100.0] * 5, "EURUSD=X": [1.0, 1.0, 1.25, 1.25, 1.25],
                       "^GSPC": [100.0] * 5}, DAYS)
        pts = self.build(trades, close)
        self.assertEqual([p["total_value"] for p in pts], [1000.0, 1000.0, 800.0, 800.0, 800.0])
        with mock.patch.object(trend, "_accounts", return_value=trades):
            bench = trend.benchmark(pts)
        self.assertEqual(list(bench.values()), [1000.0, 1000.0, 800.0, 800.0, 800.0])

    def test_sp500_comparison_without_us_holdings_still_converts(self):
        # Only euro shares, so no dollar holding asks for EUR/USD; the index still
        # needs it. EUR 1,000 on day 1 at S&P $100 and EUR/USD 1.00 = 10 units;
        # day 5: S&P $110, EUR/USD 1.10 -> 10 x 110 / 1.10 = EUR 1,000.
        trades = {"A": [Trade("2024-01-02", "NL0000000001", "Euro share", "BUY", 10, 1000.0,
                              ticker="EU.AS")]}
        close = frame({"EU.AS": [100.0] * 5, "EURUSD=X": [1.0, 1.0, 1.0, 1.0, 1.1],
                       "^GSPC": [100.0, 100.0, 100.0, 100.0, 110.0]}, DAYS)
        fetched = []

        def fetch(tickers, start, threads):
            fetched.extend(tickers)
            return close, None
        with mock.patch.object(trend, "_fetch", side_effect=fetch), \
                mock.patch.object(trend, "quote_scale", return_value=1.0):
            pts = trend.build(trades, today=date(2024, 1, 9), income_events=[])
        self.assertIn("EURUSD=X", fetched)
        with mock.patch.object(trend, "_accounts", return_value=trades):
            self.assertEqual(trend.benchmark(pts)["2024-01-08"], 1000.0)

    def test_money_in_before_the_first_index_level_is_kept(self):
        # EUR 1,000 goes in on 2 Jan, a day with no S&P 500 level; the first level
        # (EUR 100) is on 3 Jan, so the money buys 10 units there: worth 1,000.
        # Old code dropped the trade: 0.
        hist = [{"day": "2024-01-02", "sp": None}, {"day": "2024-01-03", "sp": 100.0},
                {"day": "2024-01-04", "sp": 110.0}]
        with mock.patch.object(trend, "_accounts", return_value={
                "A": [Trade("2024-01-02", "X", "X", "BUY", 1, 1000.0)]}):
            self.assertEqual(trend.benchmark(hist), {"2024-01-03": 1000.0, "2024-01-04": 1100.0})

    def test_pound_account_buy_day_has_no_profit(self):
        # A pound account buys 100 London shares at GBP 10 (GBP 1,000) on day 1.
        # Price GBP 10 on days 1-2, GBP 11 from day 3. GBP 1 = EUR 1.25 on days
        # 1-2 (EURGBP=X 0.8) and EUR 1.20 from day 3 and today.
        # Right answer (the dashboard's rule: gains in the account's currency,
        # shown in euros at the day's rate):
        #   day 1-2: value 100 x 10 x 1.25 = 1,250; cost 1,000 x 1.25 = 1,250; profit 0
        #   day 3-5: value 100 x 11 x 1.20 = 1,320; cost 1,000 x 1.20 = 1,200; profit 120
        # Today's live point: 1,320 - 1,200 = 120, the same as day 5: no jump.
        # Old code: cost always at today's 1.20 = 1,200, so buying at the market
        # price "made" EUR 50 on day 1.
        gbp = [Trade("2024-01-02", "GB0000000001", "London share", "BUY", 100, 1000.0,
                     currency="GBP", ticker="XX.L")]
        close = frame({"XX.L": [10.0, 10.0, 11.0, 11.0, 11.0],
                       "EURGBP=X": [0.8, 0.8, 1 / 1.2, 1 / 1.2, 1 / 1.2],
                       "EURUSD=X": [1.0] * 5, "^GSPC": [100.0] * 5}, DAYS)
        with rates(GBP=1.2), mock.patch.object(
                sources, "trade_sources", return_value={"T212 ISA": ("trading212", lambda: gbp)}):
            pts = self.build(trend._accounts(), close)
        self.assertEqual([p["total_value"] for p in pts], [1250.0, 1250.0, 1320.0, 1320.0, 1320.0])
        self.assertEqual([p["total_cost"] for p in pts], [1250.0, 1250.0, 1200.0, 1200.0, 1200.0])
        self.assertEqual([p["total_profit"] for p in pts], [0.0, 0.0, 120.0, 120.0, 120.0])

    def test_sp500_comparison_takes_a_pound_account_s_money_at_the_day_s_rate(self):
        # The pound account puts GBP 1,000 in on day 1, when GBP 1 = EUR 1.25: the
        # value line rises by EUR 1,250 that day. The same money in the S&P 500
        # (EUR 100 a unit all week: $100, EUR/USD 1.00) buys 12.5 units = EUR 1,250.
        # Old code converted the GBP 1,000 at today's EUR 1.20: EUR 1,200 (12 units).
        gbp = [Trade("2024-01-02", "GB0000000001", "London share", "BUY", 100, 1000.0,
                     currency="GBP", ticker="XX.L")]
        close = frame({"XX.L": [10.0] * 5, "EURGBP=X": [0.8, 0.8, 1 / 1.2, 1 / 1.2, 1 / 1.2],
                       "EURUSD=X": [1.0] * 5, "^GSPC": [100.0] * 5}, DAYS)
        with rates(GBP=1.2), mock.patch.object(
                sources, "trade_sources", return_value={"T212 ISA": ("trading212", lambda: gbp)}):
            pts = self.build(trend._accounts(), close)
            bench = trend.benchmark(pts)
        self.assertEqual(pts[0]["total_value"], 1250.0)
        self.assertEqual(list(bench.values()), [1250.0] * 5)

    def test_pound_account_sale_moves_no_profit(self):
        # Same account; it sells all 100 shares at GBP 11 (GBP 1,100) on day 3
        # (GBP 1 = EUR 1.20). Realized = GBP 100 = EUR 120; value falls to 0.
        # Profit: day 1-2 0, day 3-5 120 (selling moves no profit).
        # Old code: day 1-2 +50 (cost at today's rate, see the test above).
        gbp = [Trade("2024-01-02", "GB0000000001", "London share", "BUY", 100, 1000.0,
                     currency="GBP", ticker="XX.L"),
               Trade("2024-01-04", "GB0000000001", "London share", "SELL", 100, 1100.0,
                     currency="GBP", ticker="XX.L")]
        close = frame({"XX.L": [10.0, 10.0, 11.0, 11.0, 11.0],
                       "EURGBP=X": [0.8, 0.8, 1 / 1.2, 1 / 1.2, 1 / 1.2],
                       "EURUSD=X": [1.0] * 5, "^GSPC": [100.0] * 5}, DAYS)
        with rates(GBP=1.2), mock.patch.object(
                sources, "trade_sources", return_value={"T212 ISA": ("trading212", lambda: gbp)}):
            pts = self.build(trend._accounts(), close)
        self.assertEqual([p["total_profit"] for p in pts], [0.0, 0.0, 120.0, 120.0, 120.0])
        self.assertEqual([p["realized"] for p in pts], [0.0, 0.0, 120.0, 120.0, 120.0])
        self.assertEqual(pts[-1]["total_value"], 0.0)


# -------------------------------------------------------------- reports
class HindsightTest(Clean):
    def run_hindsight(self, trades, quotes, splits):
        with rates(USD=0.9), \
                mock.patch.object(sources, "all_trades", return_value=trades), \
                mock.patch("backend.prices.fetch_quotes", return_value=quotes), \
                mock.patch.object(reports, "_splits", side_effect=lambda tk: splits.get(tk, []),
                                  create=True):
            return reports.hindsight()

    def test_split_after_the_sale_and_split_bookings(self):
        # 1) Sold 10 shares for EUR 5,000 in March 2024; a 10-for-1 split followed
        #    in June. Those 10 shares are 100 shares now, at $150 (EUR 0.90 per $):
        #    100 x 150 x 0.90 = EUR 13,500 -> kept, they would be worth EUR 8,500 more.
        #    Old code: 10 x 150 x 0.90 = 1,350 -> "saved EUR 3,650 by selling".
        # 2) A split booked by the broker as shares out and shares in at the same
        #    moment (DeGiro) is not a sale: nothing to list.
        #    Old code: 100 old shares "sold" for 1,500, worth 100 x 8 = 800 now.
        trades = [
            Trade("2024-01-02 10:00", "XX0000000001", "Chips", "BUY", 10, 4000.0, ticker="NVX"),
            Trade("2024-03-01 10:00", "XX0000000001", "Chips", "SELL", 10, 5000.0, ticker="NVX"),
            Trade("2024-01-02 10:00", "XX0000000002", "Splitter", "BUY", 100, 1000.0, ticker="SPL.AS"),
            Trade("2024-05-01 08:00", "XX0000000002", "Splitter", "SELL", 100, 1500.0, ticker="SPL.AS"),
            Trade("2024-05-01 08:00", "XX0000000002", "Splitter", "BUY", 200, 1500.0, ticker="SPL.AS"),
        ]
        quotes = {"NVX": {"price": 150.0}, "SPL.AS": {"price": 8.0}}
        splits = {"NVX": [("2024-06-10", 10.0)], "SPL.AS": [("2024-05-01", 2.0)]}
        out = self.run_hindsight(trades, quotes, splits)
        self.assertEqual(out["total_delta_eur"], 8500.0)
        self.assertEqual([(r["name"], r["worth_now_eur"]) for r in out["regrets"]], [("Chips", 13500.0)])
        self.assertEqual(out["good_exits"], [])

    def test_partial_sales_before_and_after_a_split(self):
        # 4 shares sold before a 2-for-1 split (EUR 400), 2 sold after it (EUR 110).
        # Now EUR 60 a share: 4 x 2 x 60 + 2 x 60 = 600; 600 - 510 = +90.
        trades = [
            Trade("2024-01-02", "XX0000000003", "Half", "BUY", 10, 900.0, ticker="HLF.AS"),
            Trade("2024-02-01", "XX0000000003", "Half", "SELL", 4, 400.0, ticker="HLF.AS"),
            Trade("2024-04-01", "XX0000000003", "Half", "SELL", 2, 110.0, ticker="HLF.AS"),
        ]
        out = self.run_hindsight(trades, {"HLF.AS": {"price": 60.0}},
                                 {"HLF.AS": [("2024-03-01", 2.0)]})
        self.assertEqual(out["regrets"][0]["worth_now_eur"], 600.0)
        self.assertEqual(out["total_delta_eur"], 90.0)


class IncomeProjectionTest(Clean):
    def project(self, holdings, dividends):
        def series(points):
            idx = pd.DatetimeIndex([datetime.fromisoformat(d) for d, _ in points]).tz_localize("America/New_York")
            return pd.Series([v for _, v in points], index=idx, dtype=float)

        yf = types.ModuleType("yfinance")
        yf.Ticker = lambda t: types.SimpleNamespace(dividends=series(dividends.get(t, [])))
        with rates(USD=0.9), mock.patch.dict(sys.modules, {"yfinance": yf}), \
                mock.patch.object(clock, "today", return_value=date(2026, 11, 26)):
            return reports.income_projection(holdings)

    def test_dividend_currency_is_the_share_s_not_the_broker_line_s(self):
        # 10 shares of a US company held on a euro line (EUR 180 each). Yahoo pays
        # $1 a quarter in the last year: $4 a share = EUR 3.60 at EUR 0.90 per $.
        # Next 12 months: 10 x 3.60 = EUR 36; yield 3.60 / 180 = 2.0%.
        # Old code read the $4 as EUR 4: EUR 40 and 2.22%.
        h = [{"ticker": "USCO", "name": "US Co", "quantity": 10.0, "currency": "EUR", "value_eur": 1800.0}]
        out = self.project(h, {"USCO": [("2026-02-10", 1.0), ("2026-05-10", 1.0),
                                        ("2026-08-10", 1.0), ("2026-11-10", 1.0)]})
        self.assertEqual(out["projected_annual_eur"], 36.0)
        self.assertEqual(out["holdings"][0]["yield_pct"], 2.0)

    def test_a_dividend_stopped_years_ago_is_not_expected(self):
        # Last paid in 2022 (four times $1). Today is 26 Nov 2026: nothing has
        # been paid for almost four years, so nothing is expected.
        # Old code summed the 12 months before the LAST payment: 4 x 10 x 0.9 = EUR 36.
        h = [{"ticker": "STOP", "name": "Stopped", "quantity": 10.0, "currency": "USD", "value_eur": 900.0}]
        out = self.project(h, {"STOP": [("2022-02-10", 1.0), ("2022-05-10", 1.0),
                                        ("2022-08-10", 1.0), ("2022-11-10", 1.0)]})
        self.assertEqual(out["projected_annual_eur"], 0)
        self.assertEqual(out["holdings"], [])

    def test_a_yearly_payer_a_few_days_late_still_counts(self):
        # One dividend a year: $2 on 20 Nov 2025; this year's is not paid yet
        # (26 Nov 2026). Expected: 10 x 2 x 0.9 = EUR 18 (unchanged rule).
        h = [{"ticker": "YEAR", "name": "Yearly", "quantity": 10.0, "currency": "USD", "value_eur": 900.0}]
        out = self.project(h, {"YEAR": [("2024-11-18", 2.0), ("2025-11-20", 2.0)]})
        self.assertEqual(out["projected_annual_eur"], 18.0)


class ProjectionTest(Clean):
    def test_median_contribution_and_start_month(self):
        # Cost at each month end: Aug 1,000, Sep 1,100, Oct 1,400 -> monthly
        # additions 100 and 300. The median of two values is their average: 200.
        # Old code took the upper one: 300.
        # Today (the app's clock) is 15 Jan 2030; the first quarterly point is
        # three months on: 2030-04.
        hist = [{"day": d, "total_value": 2000.0, "total_cost": c}
                for d, c in (("2026-08-31", 1000.0), ("2026-09-30", 1100.0), ("2026-10-30", 1400.0))]
        with mock.patch.object(trend, "series", return_value=hist), \
                mock.patch.object(clock, "today", return_value=date(2030, 1, 15)):
            out = reports.projection(2000.0)
        self.assertEqual(out["monthly_contribution"], 200.0)
        self.assertEqual(out["scenarios"]["expected"]["points"][0]["ym"], "2030-04")
        # month by month at 6% a year: v = v x 1.005 + 200, three times from 2,000
        v = 2000.0
        for _ in range(3):
            v = v * 1.005 + 200
        self.assertEqual(out["scenarios"]["expected"]["points"][0]["value"], round(v))


# ------------------------------------------------------------ mcp_tools
class McpIncomeByYearTest(unittest.TestCase):
    def test_by_year_counts_what_the_dashboard_counts(self):
        # What the dashboard's income total (income.summary) counts:
        #   DeGiro: dividend 10 (Jan 2025), exchange fee -1 (Feb 2025)
        #   Trade Republic, signed in: dividend 4 (Mar 2025), tax withheld -2 (Apr 2025)
        #   the uploaded Trade Republic file holds the same dividend 4 but is not
        #   counted: the sign-in covers that account (income._tr_rows gives nothing).
        # Right answer for 2025: dividends 14, interest 0, fees -3.
        # Old code: read the file anyway (dividends 18) and left out the sign-in's
        # tax (fees -1).
        from backend import connections, datafiles, income, mcp_tools
        from backend.connectors import trading212
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        f = tmp / "income_trade_republic.json"
        f.write_text(json.dumps({"income": [{"date": "2025-03-05", "type": "dividend", "amount_eur": 4.0}]}))
        tr = {"account": "Trade Republic"}
        patches = [
            mock.patch.object(income, "_degiro_rows", return_value=[
                {"date": "2025-01-10", "kind": "dividends", "amount_eur": 10.0},
                {"date": "2025-02-01", "kind": "fees", "amount_eur": -1.0}]),
            mock.patch.object(income, "_tr_rows", return_value=("Trade Republic", [])),
            mock.patch.object(datafiles, "resolve", return_value=(f, True)),
            mock.patch.object(trading212, "get_dividends", return_value=[]),
            mock.patch.object(trading212, "get_interest", return_value=[]),
            mock.patch.object(trading212, "get_fees", return_value=[]),
            mock.patch.object(connections, "get_dividends",
                              return_value=[{**tr, "date": "2025-03-05", "amount_eur": 4.0}]),
            mock.patch.object(connections, "get_interest", return_value=[]),
            mock.patch.object(connections, "get_fees",
                              return_value=[{**tr, "date": "2025-04-01", "amount_eur": -2.0}]),
        ]
        for p in patches:
            self.enterContext(p)
        self.assertEqual(mcp_tools._income_by_year(),
                         {"2025": {"dividends": 14.0, "interest": 0.0, "fees": -3.0}})


class McpPeriodTest(unittest.TestCase):
    def performance(self, period, today):
        # one point a day for two years; profit grows by 1 a day, value stays 10,000
        from backend import datafiles, mcp_tools
        start = date(today.year - 2, 1, 1)
        hist = [{"day": date.fromordinal(o).isoformat(), "total_value": 10000.0,
                 "total_profit": float(o - start.toordinal())}
                for o in range(start.toordinal(), today.toordinal() + 1)]
        app = types.SimpleNamespace(_history=lambda: hist, profit=lambda: {"periods": {}, "starts": {}})
        with mock.patch.object(mcp_tools, "_app", return_value=app), \
                mock.patch.object(clock, "today", return_value=today), \
                mock.patch.object(datafiles, "demo", return_value=False):
            return mcp_tools.get_performance(period=period, points=5, currency="EUR")

    def test_rolling_windows_are_measured_from_exactly_that_long_ago(self):
        # Today 26 Nov 2026. "Last 30 days" = from the close of 27 Oct (30 days
        # ago) to today: gain 30 (1 a day). "Last 12 months" = from the close of
        # 26 Nov 2025: 365 days, gain 365. Old code measured from the day before
        # each (26 Oct, 25 Nov): 31 and 366 days.
        out = self.performance("last_30_days", date(2026, 11, 26))
        self.assertEqual((out["measured_from"], out["gain"]), ("2026-10-27", 30.0))
        out = self.performance("last_12_months", date(2026, 11, 26))
        self.assertEqual((out["measured_from"], out["gain"]), ("2025-11-26", 365.0))
        # across a 29 February: 1 Mar 2028 back to 1 Mar 2027 is 366 days
        out = self.performance("last_12_months", date(2028, 3, 1))
        self.assertEqual((out["measured_from"], out["gain"]), ("2027-03-01", 366.0))

    def test_calendar_periods_and_thinning(self):
        # "month" on 26 Nov: from the close of 31 Oct, 26 days. "today": from
        # yesterday's close, 1 day. The thinned series keeps the first and last point.
        out = self.performance("month", date(2026, 11, 26))
        self.assertEqual((out["measured_from"], out["gain"]), ("2026-10-31", 26.0))
        out = self.performance("today", date(2026, 11, 26))
        self.assertEqual((out["measured_from"], out["gain"]), ("2026-11-25", 1.0))
        self.assertEqual(out["return_pct"], 0.01)          # 1 on 10,000
        from backend import mcp_tools
        pts = list(range(23))
        thin = mcp_tools._thin(pts, 5)
        self.assertEqual((thin[0], thin[-1], len(thin)), (0, 22, 5))
        self.assertEqual(mcp_tools._thin(pts, 2), [0, 22])


@unittest.skipIf(importlib.util.find_spec("mcp") is None, "the MCP SDK is not installed")
class McpMatchesDashboardTest(unittest.TestCase):
    """Every figure Claude is given equals the dashboard's figure for the same
    thing, and every amount follows the requested currency (sample data)."""

    @classmethod
    def setUpClass(cls):
        from tests.test_mcp import sample_world
        sample_world(cls)
        from backend import main, mcp_tools
        cls.main, cls.t = main, mcp_tools

    def test_totals_equal_the_dashboard(self):
        port = self.main.portfolio()
        tot = port["totals"]
        s = self.t.get_portfolio_summary(currency="EUR")
        for mine, theirs in (("value", "value"), ("cost", "cost"), ("unrealized_gain", "unrealized"),
                             ("realized_gain", "realized"), ("dividends", "dividends"),
                             ("interest", "interest"), ("fees", "fees"), ("total_return", "total_return"),
                             ("today_change", "daily")):
            self.assertAlmostEqual(s[mine], tot[theirs], delta=0.01, msg=mine)
        self.assertAlmostEqual(s["total_return"], s["unrealized_gain"] + s["realized_gain"] + s["dividends"]
                               + s["interest"] + s["fees"], delta=0.03)

        h = self.t.list_holdings(limit=200, currency="EUR")
        self.assertAlmostEqual(sum(x["value"] for x in h["holdings"]), tot["value"], delta=0.05)
        self.assertAlmostEqual(sum(x["unrealized_gain"] for x in h["holdings"]), tot["unrealized"], delta=0.05)
        self.assertAlmostEqual(h["matches_value"], tot["value"], delta=0.01)
        for b in port["by_broker"]:
            acct = self.t.list_holdings(account=b["broker"], limit=200, currency="EUR")
            self.assertAlmostEqual(sum(x["value"] for x in acct["holdings"]), b["value"], delta=0.05)

        perf = self.t.get_performance(period="all", currency="EUR")
        hist = self.main._history()
        self.assertAlmostEqual(perf["end_value"], tot["value"], delta=0.01)
        self.assertAlmostEqual(hist[-1]["total_profit"], tot["total_return"], delta=0.01)
        self.assertAlmostEqual(perf["gain"], hist[-1]["total_profit"] - hist[0]["total_profit"], delta=0.01)

        r = self.t.get_realized_gains(currency="EUR")
        closed = self.main.closed()
        self.assertAlmostEqual(r["total_realized"], closed["total_realized_eur"], delta=0.01)
        self.assertAlmostEqual(r["total_realized"], tot["realized"], delta=0.01)
        self.assertAlmostEqual(sum(y["realized"] for y in r["by_year"]), r["total_realized"], delta=0.05)
        self.assertAlmostEqual(sum(a["realized"] for a in r["by_account"]), r["total_realized"], delta=0.05)

        inc = self.t.get_income(currency="EUR")
        for k in ("dividends", "interest", "fees"):
            self.assertAlmostEqual(inc["received_all_time"][k], tot[k], delta=0.01, msg=k)
            self.assertAlmostEqual(sum(y[k] for y in inc["by_year"]), tot[k], delta=0.05, msg=k)

        self.assertAlmostEqual(self.t.get_net_worth(currency="EUR")["investments"], tot["value"], delta=0.01)

        risk = self.t.get_risk_analysis(currency="EUR")
        a, health, stress = self.main.analytics_endpoint(), self.main.health(), self.main.stress()
        self.assertEqual(risk["metrics"]["top3_weight_pct"], a.get("top3_pct"))
        self.assertEqual(risk["metrics"]["effective_positions"], a.get("effective_positions"))
        self.assertEqual(risk["health_score"]["score"], health["score"])
        self.assertEqual([w["impact"] for w in risk["what_if"]],
                         [float(x["impact_eur"]) for x in stress["scenarios"]])

    # numbers that are not amounts of money, so they stay the same in every currency
    NOT_MONEY = ("pct", "quantity", "price", "count", "shown", "total_matches", "months", "beta",
                 "sharpe", "effective_positions", "score", "points", "max", "annual_rate", "per")

    def walk(self, a, b, path=""):
        """Yield (path, value in EUR, value in GBP) for every number."""
        if isinstance(a, dict):
            for k in a:
                yield from self.walk(a[k], b.get(k), f"{path}.{k}")
        elif isinstance(a, list):
            self.assertEqual(len(a), len(b), path)
            for i, (x, y) in enumerate(zip(a, b)):
                yield from self.walk(x, y, f"{path}[{i}]")
        elif isinstance(a, (int, float)) and not isinstance(a, bool):
            yield path, a, b

    def test_every_amount_follows_the_currency(self):
        # GBP 1 = EUR 1.20 in the sample world: every amount in pounds is the euro
        # amount / 1.2; percentages and counts stay as they are.
        for fn in (self.t.get_portfolio_summary, self.t.list_holdings, self.t.get_performance,
                   self.t.get_realized_gains, self.t.get_income, self.t.get_net_worth,
                   self.t.get_risk_analysis):
            with self.subTest(tool=fn.__name__):
                eur, gbp = fn(currency="EUR"), fn(currency="GBP")
                eur.pop("as_of", None), gbp.pop("as_of", None)
                for path, e, g in self.walk(eur, gbp):
                    key = path.rsplit(".", 1)[-1].split("[")[0]
                    if any(w in key for w in self.NOT_MONEY):
                        self.assertEqual(e, g, path)
                    else:
                        self.assertAlmostEqual(g, e / 1.2, delta=0.02, msg=path)


# ------------------------------------------------------------- frontend
NODE = shutil.which("node")


@unittest.skipIf(NODE is None, "node is not installed")
class ChartRangeTest(unittest.TestCase):
    """The Overview chart's range chips (frontend/index.html), run in node."""

    def run_js(self, now: str, rng: str, days: list[str]) -> dict:
        html = (ROOT / "frontend" / "index.html").read_text()
        start = html.index("/* chart-range:start")
        end = html.index("/* chart-range:end */")
        js = html[start:end] + f"""
            const H={json.dumps([{"day": d, "total_profit": i} for i, d in enumerate(days)])};
            const w=chartWindow(H, rangeStart({json.dumps(rng)}, new Date({json.dumps(now)})));
            console.log(JSON.stringify({{from: rangeStart({json.dumps(rng)}, new Date({json.dumps(now)})),
                                         first: w.h[0].day, n: w.h.length, all: w.all}}));"""
        out = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30,
                             env={"TZ": "Europe/Amsterdam", "PATH": ""})
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_ranges_start_at_the_close_exactly_that_long_ago(self):
        # One point per day, 1 Jan 2026 - Wed 26 Nov 2026.
        # 1W on Wed 26 Nov: from the close of Wed 19 Nov (7 days: 20..26 Nov).
        #   Old code started a day earlier (18 Nov): 8 days of change under "1W".
        # This year: from the close of 31 Dec 2025 - before the history starts, so
        #   the whole history counts (all = true).
        # 1M: from the close of 26 Oct.
        days = [date.fromordinal(o).isoformat()
                for o in range(date(2026, 1, 1).toordinal(), date(2026, 11, 26).toordinal() + 1)]
        w = self.run_js("2026-11-26T12:00:00", "1w", days)
        self.assertEqual((w["from"], w["first"], w["n"], w["all"]), ("2026-11-19", "2026-11-19", 8, False))
        w = self.run_js("2026-11-26T12:00:00", "1m", days)
        self.assertEqual((w["first"], w["all"]), ("2026-10-26", False))
        w = self.run_js("2026-11-26T12:00:00", "ytd", days)
        self.assertEqual((w["first"], w["all"]), ("2026-01-01", True))
        w = self.run_js("2026-11-26T12:00:00", "all", days)
        self.assertEqual((w["first"], w["all"]), ("2026-01-01", True))

    def test_just_after_midnight_uses_the_local_date(self):
        # 00:30 in Amsterdam on 26 Nov is still 25 Nov in UTC. 1W must count from
        # 19 Nov (the local date a week ago), not 18 Nov.
        days = [date.fromordinal(o).isoformat()
                for o in range(date(2026, 11, 1).toordinal(), date(2026, 11, 26).toordinal() + 1)]
        w = self.run_js("2026-11-26T00:30:00", "1w", days)
        self.assertEqual(w["from"], "2026-11-19")


# -------------------------------------------------------------- insights
class BriefTest(unittest.TestCase):
    def test_total_return_parts_add_up(self):
        # total return 1,150 = 1,000 price gains + 200 dividends and interest - 50 fees.
        # Old text: "(EUR 1,000 capital gains + EUR 200 dividends & interest)" = 1,200.
        port = {"totals": {"value": 10000.0, "daily": None, "all_time": 1000.0, "income": 200.0,
                           "fees": -50.0, "total_return": 1150.0}, "holdings": []}
        with mock.patch.object(insights, "headlines", return_value=[]), rates():
            text = " ".join(insights.daily_brief(port, {}, "EUR")["summary"])
        self.assertIn("€1,150", text)
        self.assertIn("€1,000 capital gains + €200 dividends & interest − €50 fees", text)


if __name__ == "__main__":
    unittest.main()
