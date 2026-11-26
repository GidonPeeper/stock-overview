"""Holes in Yahoo's daily price history are filled from hourly prices
(backend/closes.py). No network: the hourly source is a made-up table."""

import unittest
from datetime import date, datetime

import pandas as pd

from backend import closes


def d(day: int) -> date:
    return date(2026, 11, day)


class MissingDaysTest(unittest.TestCase):
    def test_a_weekend_or_a_holiday_is_not_a_hole(self):
        # Fri 6, Mon 9: weekend. Tue 10 to Fri 13 with Wed + Thu missing: 2 weekdays, a holiday
        self.assertEqual(closes.missing_days([d(6), d(9), d(10), d(13)]), [])

    def test_three_weekdays_in_a_row_is_a_hole(self):
        # closes on Tue 10 and Thu 26: every weekday in between is missing
        hole = closes.missing_days([d(10), d(26)])
        self.assertEqual(hole[0], d(11))
        self.assertEqual(hole[-1], d(25))
        self.assertEqual(len(hole), 11)                      # 11-13, 16-20, 23-25
        self.assertNotIn(d(14), hole)                        # Saturday

    def test_the_stretch_up_to_yesterday(self):
        self.assertEqual(closes.missing_days([d(9), d(10)], until=d(13)), [d(11), d(12), d(13)])
        self.assertEqual(closes.missing_days([d(9), d(10)], until=d(11)), [])


class FillTest(unittest.TestCase):
    def hourly(self, table):
        calls = []

        def fetch(ticker, wanted):
            calls.append((ticker, list(wanted)))
            return {day: v for day, v in table.get(ticker, {}).items() if day in wanted}
        return fetch, calls

    def test_list_gets_the_missing_days_and_keeps_its_own_closes(self):
        fetch, calls = self.hourly({"ETF.L": {d(20): 10.7, d(25): 10.6, d(10): 99.0}})
        out = closes.fill_list("ETF.L", [(d(10), 11.2), (d(26), 10.9)], fetch=fetch)
        self.assertEqual(out, [(d(10), 11.2), (d(20), 10.7), (d(25), 10.6), (d(26), 10.9)])
        # the close before Monday the 23rd is now Friday's, not the one of the 10th
        self.assertEqual([c for day, c in out if day < d(23)][-1], 10.7)

    def test_no_hole_means_no_request(self):
        fetch, calls = self.hourly({})
        days = [(d(n), 1.0) for n in (9, 10, 11, 12, 13)]
        self.assertEqual(closes.fill_list("X", days, fetch=fetch), days)
        self.assertEqual(calls, [])

    def test_nothing_found_leaves_the_list_alone(self):
        fetch, _ = self.hourly({})
        days = [(d(10), 11.2), (d(26), 10.9)]
        self.assertEqual(closes.fill_list("X", days, fetch=fetch), days)

    def test_frame_fills_one_ticker_and_leaves_the_other(self):
        days = [n for n in range(10, 27) if d(n).weekday() < 5 and n != 25]   # the 25th: no row at all
        idx = pd.DatetimeIndex([datetime(2026, 11, n) for n in days])
        frame = pd.DataFrame({"ETF.L": [11.2] + [None] * (len(days) - 2) + [10.9],
                              "AAA": [1.0 + n / 100 for n in days]}, index=idx)
        fetch, calls = self.hourly({"ETF.L": {d(12): 11.5, d(17): 11.0, d(25): 10.6}})
        out, filled = closes.fill_frame(frame, fetch=fetch)
        self.assertEqual(filled, ["ETF.L"])
        self.assertEqual([t for t, _ in calls], ["ETF.L"])            # AAA has no hole: not asked for
        self.assertEqual(out.loc[pd.Timestamp(d(12)), "ETF.L"], 11.5)
        self.assertEqual(out.loc[pd.Timestamp(d(25)), "ETF.L"], 10.6)  # a day that had no row
        self.assertTrue(out.index.is_monotonic_increasing)
        # carried forward as the chart does: the 24th shows the 17th's price, not the 10th's
        self.assertEqual(out.ffill().loc[pd.Timestamp(d(24)), "ETF.L"], 11.0)

    def test_a_share_that_stopped_trading_is_not_asked_for(self):
        fetch, calls = self.hourly({})
        closes.fill_list("GONE", [(date(2026, 1, 5), 1.0), (date(2026, 1, 6), 1.0)],
                         until=d(25), fetch=fetch)
        self.assertEqual(calls, [])

    def test_hourly_source_is_off_in_tests(self):
        self.assertEqual(closes.hourly_closes("ETF.L", [d(12)], today=d(26)), {})


if __name__ == "__main__":
    unittest.main()
