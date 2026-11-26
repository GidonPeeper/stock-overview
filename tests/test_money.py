"""Money tab sums: interest on cash (cash.py) and loan payoff dates
(finances.py), on made-up numbers. No network."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from backend import cash, finances, fx

RATES = {"EUR": 1.0, "GBP": 1.25, "USD": 0.9}


class TempData(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        resolve = lambda name: (self.dir / name, (self.dir / name).exists())
        for p in (mock.patch.object(fx, "_live_rates", return_value=RATES),
                  mock.patch.object(cash, "resolve", resolve),
                  mock.patch.object(cash, "DATA_DIR", self.dir),
                  mock.patch.object(cash, "_broker_cash", return_value=[]),
                  mock.patch.object(cash, "_connection_cash", return_value=[]),
                  mock.patch.object(finances, "resolve", resolve),
                  mock.patch.object(finances, "DATA_DIR", self.dir),
                  mock.patch.object(finances, "_residence", return_value="UK")):
            p.start()
            self.addCleanup(p.stop)

    def write(self, name, data):
        (self.dir / name).write_text(json.dumps(data))


class CashInterestTest(TempData):
    def test_interest_counts_bank_and_savings_accounts_only(self):
        # Savings €10,000 at 4% = €400 a year. A €90,000 pension pot with a 5%
        # "rate" is not interest on cash: before, the card said €4,900 a year
        # at 4.9%. And a pension with no rate dragged the average down to 0.4%.
        self.write("cash_accounts.json", {"accounts": [
            {"name": "Saver", "balance": 10_000, "currency": "EUR", "type": "savings", "rate": 4},
            {"name": "Pension", "balance": 90_000, "currency": "EUR", "type": "retirement", "rate": 5}]})
        c = cash.load()
        self.assertEqual((c["interest_eur_yr"], c["blended_rate_pct"]), (400.0, 4.0))
        self.assertEqual(c["total_eur"], 100_000)            # net worth still counts the pot

    def test_average_is_weighted_by_balance(self):
        # €30,000 at 5% + €10,000 at 1% = €1,600 on €40,000 = 4%.
        self.write("cash_accounts.json", {"accounts": [
            {"name": "A", "balance": 30_000, "currency": "EUR", "type": "savings", "rate": 5},
            {"name": "B", "balance": 10_000, "currency": "EUR", "type": "cash", "rate": 1}]})
        c = cash.load()
        self.assertEqual((c["interest_eur_yr"], c["blended_rate_pct"]), (1600.0, 4.0))


class LoanPayoffTest(TempData):
    def test_payoff_month(self):
        # €1,200 at 0%, €100 a month: 12 payments, paid off 12 months from now.
        self.write("finances.json", {"loans": [
            {"name": "Car", "balance": 1200, "rate": 0, "monthly_payment": 100, "currency": "EUR"}]})
        loan = finances.load()["loans"][0]
        now = time.localtime()
        y, m = divmod(now.tm_year * 12 + now.tm_mon - 1 + 12, 12)
        self.assertEqual((loan["payoff_months"], loan["payoff"]), (12, f"{y}-{m + 1:02d}"))

    def test_no_payoff_date_when_not_repaid_within_50_years(self):
        # €100,000 at 6% (€500 interest a month) with €501 a month takes about
        # 104 years: before, it showed "paid off" in 50 years (the loop's cap).
        self.write("finances.json", {"loans": [
            {"name": "Mortgage", "balance": 100_000, "rate": 6, "monthly_payment": 501,
             "currency": "EUR"}]})
        loan = finances.load()["loans"][0]
        self.assertNotIn("payoff", loan)


if __name__ == "__main__":
    unittest.main()
