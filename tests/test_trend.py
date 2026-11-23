"""Performance chart (backend/trend.py), calendar periods (backend/periods.py),
the today change (main._move_eur) and flow-adjusted analytics.

No network: price downloads are replaced with small made-up tables.
"""

import unittest
from datetime import date, datetime
from unittest import mock

import pandas as pd

from backend import analytics, cache, clock, periods, trend
from backend.connectors.base import Holding
from backend.positions import Trade


def frame(rows: dict[str, list], days: list[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, index=pd.DatetimeIndex([datetime.fromisoformat(d) for d in days]))


DAYS = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"]


class TrendTest(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def build(self, accts, close, splits=None, income=()):
        with mock.patch.object(trend, "_fetch", return_value=(close, splits)), \
                mock.patch.object(trend, "quote_scale", return_value=1.0):
            return trend.build(accts, today=date(2024, 1, 9), income_events=list(income))

    def test_reverse_split_does_not_inflate_older_prices(self):
        # 300 shares at $1; a 1-for-30 reverse split on 5 Jan: the broker books
        # 300 old shares out and 10 new ones in. Yahoo's closes are adjusted:
        # $30 throughout, which would make the old 300 shares worth $9,000.
        accts = {"DeGiro": [
            Trade("2024-01-02 10:00", "OLD", "Wish", "BUY", 300, 300.0, ticker="LOGC"),
            Trade("2024-01-05 09:00", "OLD", "Wish", "SELL", 300, 300.0, ticker="LOGC"),
            Trade("2024-01-05 09:00", "NEW", "Wish", "BUY", 10, 300.0, ticker="LOGC"),
        ]}
        close = frame({"LOGC": [30.0] * 5, "EURUSD=X": [1.0] * 5, "^GSPC": [4700.0] * 5}, DAYS)
        splits = frame({"LOGC": [0, 0, 0, 1 / 30, 0], "EURUSD=X": [0] * 5, "^GSPC": [0] * 5}, DAYS)
        pts = self.build(accts, close, splits)
        self.assertEqual([p["total_value"] for p in pts], [300.0, 300.0, 300.0, 300.0, 300.0])
        self.assertEqual([p["total_profit"] for p in pts], [0.0] * 5)

    def test_profit_counts_realized_per_account_and_ignores_transfers(self):
        # Bought at 100 in two accounts; one sells at 150 (realized +50) and the
        # money leaves. Value drops, profit does not.
        accts = {
            "A": [Trade("2024-01-02", "X", "X", "BUY", 1, 100.0, ticker="XX"),
                  Trade("2024-01-04", "X", "X", "SELL", 1, 150.0, ticker="XX")],
            "B": [Trade("2024-01-03", "X", "X", "BUY", 1, 120.0, ticker="XX")],
        }
        close = frame({"XX": [100.0, 120.0, 150.0, 150.0, 150.0], "EURUSD=X": [1.0] * 5,
                       "^GSPC": [1.0] * 5}, DAYS)
        pts = self.build(accts, close)
        by_day = {p["day"]: p for p in pts}
        self.assertEqual(by_day["2024-01-03"]["total_value"], 240.0)
        self.assertEqual(by_day["2024-01-04"]["total_value"], 150.0)   # A sold
        self.assertEqual(by_day["2024-01-04"]["realized"], 50.0)
        self.assertEqual(by_day["2024-01-04"]["total_profit"], 80.0)   # 50 realized + 30 on B
        self.assertEqual(pts[-1]["day"], "2024-01-08")                 # never today

    def test_dividends_interest_and_fees_count_from_their_day(self):
        accts = {"A": [Trade("2024-01-02", "X", "X", "BUY", 1, 100.0, ticker="XX")]}
        close = frame({"XX": [100.0] * 5, "EURUSD=X": [1.0] * 5, "^GSPC": [1.0] * 5}, DAYS)
        pts = self.build(accts, close, income=[("2024-01-03", 5.0), ("2024-01-05", 2.5),
                                               ("2024-01-05", -1.0)])
        self.assertEqual([p["total_profit"] for p in pts], [0.0, 5.0, 5.0, 6.5, 6.5])
        h = trend.history.__wrapped__ if hasattr(trend.history, "__wrapped__") else trend.history
        with mock.patch.object(trend, "series", return_value=pts):
            live = h(100.0, 100.0, 0.0, 6.5)[-1]
        self.assertEqual(live["total_profit"], 6.5)   # today's point: same total

    def test_unpriced_holding_counts_at_cost(self):
        accts = {"A": [Trade("2024-01-02", "Z", "Gone SPAC", "BUY", 5, 50.0, ticker="")]}
        close = frame({"EURUSD=X": [1.0] * 5, "^GSPC": [1.0] * 5}, DAYS)
        pts = self.build(accts, close)
        self.assertEqual(pts[-1]["total_value"], 50.0)
        self.assertEqual(trend.last_build["unpriced"], ["Gone SPAC"])

    def test_history_ends_with_the_live_point(self):
        with mock.patch.object(trend, "series", return_value=[
                {"day": "2000-01-03", "total_value": 1.0, "total_cost": 1.0, "unrealized": 0.0,
                 "realized": 0.0, "total_profit": 0.0, "sp": 9.0}]):
            h = trend.history(110.0, 100.0, 5.0, 2.0)
        self.assertEqual(h[-1]["day"], clock.today().isoformat())
        self.assertEqual(h[-1]["total_profit"], 17.0)

    def test_benchmark_buys_index_units_on_trade_days(self):
        hist = [{"day": "2024-01-02", "sp": 100.0}, {"day": "2024-01-03", "sp": 110.0}]
        with mock.patch.object(trend, "_accounts", return_value={
                "A": [Trade("2024-01-02", "X", "X", "BUY", 1, 1000.0)]}):
            self.assertEqual(trend.benchmark(hist), {"2024-01-02": 1000.0, "2024-01-03": 1100.0})


class PeriodsTest(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_calendar_periods(self):
        today = date(2026, 11, 25)  # a Wednesday
        closes = {"AAA": [(date(2025, 12, 31), 50.0), (date(2026, 10, 30), 80.0),
                          (date(2026, 11, 20), 90.0), (date(2026, 11, 23), 95.0),
                          (date(2026, 11, 24), 98.0)]}
        row = {"ticker": "AAA", "name": "A", "quantity": 2.0, "currency": "EUR",
               "value_eur": 200.0, "daily_eur": 4.0, "_mkt": {"price": 100.0, "currency": "EUR",
                                                              "own_price": False}}
        with mock.patch.object(periods, "_closes", return_value=closes), \
                mock.patch.object(periods, "quote_scale", return_value=1.0):
            out = periods.compute([row], today=today)
        self.assertEqual(out["periods"], {"today": 4.0, "week": 20.0, "month": 40.0, "year": 100.0})
        self.assertEqual(out["starts"]["week"], "2026-11-23")

    def test_other_currency_listing_uses_the_market_move(self):
        # broker holds a euro line of a dollar share: apply the % move only
        row = {"quantity": 10.0, "currency": "EUR", "value_eur": 1100.0,
               "_mkt": {"price": 110.0, "currency": "USD", "own_price": True}}
        self.assertAlmostEqual(periods.move_eur(row, 100.0), 100.0)


class TodayMoveTest(unittest.TestCase):
    def test_same_currency_uses_price_difference(self):
        from backend import main
        h = Holding("T212", "NVDA", "Nvidia", 2, 100.0, "USD", price=160.0)
        with mock.patch.object(main, "to_eur", side_effect=lambda v, c: v):
            self.assertEqual(main._move_eur(320.0, 160.0, h, {"price": 125.0, "currency": "USD"}, 125.0), 70.0)
            # market closed today: reference = last price -> no change
            self.assertEqual(main._move_eur(250.0, 125.0, Holding("D", "NVDA", "Nvidia", 2, 100.0, "USD"),
                                            {"price": 125.0, "currency": "USD"}, 125.0), 0.0)

    def test_other_currency_uses_percentage(self):
        from backend import main
        h = Holding("T212", "ADBE", "Adobe", 10, 100.0, "EUR", price=200.0)
        out = main._move_eur(2000.0, 200.0, h, {"price": 220.0, "currency": "USD"}, 200.0)
        self.assertAlmostEqual(out, 2000.0 - 2000.0 / 1.1)


class IncomeTest(unittest.TestCase):
    def test_degiro_rows_outside_orders(self):
        from backend import income
        kinds = {d: income._degiro_kind(d) for d in (
            "Dividend", "Dividendbelasting", "Flatex Interest Income", "Rente",
            "Inkomsten uit Securities Lending - Mei", "DEGIRO Geldmarktfondsen Compensatie",
            "DEGIRO Aansluitingskosten 2024 (Nasdaq - NDQ)", "ADR/GDR Externe Kosten",
            "Transactiebelasting Frankrijk", "iDEAL Deposit", "Valuta Creditering")}
        self.assertEqual(kinds, {
            "Dividend": "dividends", "Dividendbelasting": "dividends",
            "Flatex Interest Income": "interest", "Rente": "interest",
            "Inkomsten uit Securities Lending - Mei": "interest",
            "DEGIRO Geldmarktfondsen Compensatie": "interest",
            "DEGIRO Aansluitingskosten 2024 (Nasdaq - NDQ)": "fees", "ADR/GDR Externe Kosten": "fees",
            "Transactiebelasting Frankrijk": "fees", "iDEAL Deposit": None, "Valuta Creditering": None})


class AnalyticsTest(unittest.TestCase):
    def test_withdrawal_is_not_a_drawdown(self):
        # value halves because money was taken out; profit stays the same
        hist = [{"day": f"2024-0{m}-28", "total_value": v, "total_profit": p}
                for m, v, p in [(1, 10000, 0), (2, 11000, 1000), (3, 5500, 1000), (4, 6050, 1550)]]
        self.assertEqual(analytics.max_drawdown(hist), 0.0)
        hist[2]["total_profit"] = 0.0  # a real loss of 1,000 on 11,000
        self.assertAlmostEqual(analytics.max_drawdown(hist), -1000 / 11000)


if __name__ == "__main__":
    unittest.main()
