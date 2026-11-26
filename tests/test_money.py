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




class AutoAccountRateTest(unittest.TestCase):
    """An account that updates itself (Trading 212 cash, a linked bank) has no
    interest rate until one is typed in: unknown, not 0%."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from backend import cash, fx
        self.cash = cash
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        auto = [{"name": "Bank Easy Saver", "institution": "Bank", "balance": 10000.0, "currency": "EUR",
                 "type": "cash", "rate": 0.0, "auto": True},
                {"name": "Bank Personal", "institution": "Bank", "balance": 1000.0, "currency": "EUR",
                 "type": "cash", "rate": 0.0, "auto": True}]
        for p in (mock.patch.object(cash, "DATA_DIR", d),
                  mock.patch.object(cash, "resolve", side_effect=lambda n: (d / n, (d / n).exists())),
                  mock.patch.object(cash, "_broker_cash", return_value=[]),
                  mock.patch.object(cash, "_connection_cash", side_effect=lambda: [dict(a) for a in auto]),
                  mock.patch.object(fx, "_live_rates", return_value={"EUR": 1.0})):
            p.start()
            self.addCleanup(p.stop)

    def test_unknown_rate_is_not_zero_percent(self):
        out = self.cash.load()
        self.assertEqual(out["interest_eur_yr"], 0.0)
        self.assertEqual(out["rate_unknown_eur"], 11000.0)       # nothing is claimed about this money
        self.assertEqual(out["blended_rate_pct"], 0)
        saver = out["accounts"][0]
        self.assertFalse(saver["rate_known"])
        self.assertEqual(saver["type"], "savings")               # from its name

    def test_a_typed_in_rate_counts_and_survives_other_saves(self):
        # 10,000 at 4% = 400 a year; the 1,000 current account is set to 0% (known)
        self.cash.set_auto("Bank Easy Saver", 4.0, "savings")
        out = self.cash.set_auto("bank  personal", 0.0, "cash")   # name matched loosely
        self.assertEqual(out["interest_eur_yr"], 400.0)
        self.assertEqual(out["rate_unknown_eur"], 0.0)
        self.assertEqual(out["blended_rate_pct"], round(400 / 11000 * 100, 2))   # 3.64
        # adding and removing a typed-in account keeps the rates
        self.cash.upsert("Old Bank", "Old", 500.0, "EUR", "cash", 1.0)            # 5 a year
        out = self.cash.load()
        self.assertEqual(out["interest_eur_yr"], 405.0)
        out = self.cash.delete("Old Bank")
        self.assertEqual(out["interest_eur_yr"], 400.0)
        # a blank rate means not known again
        out = self.cash.set_auto("Bank Easy Saver", None)
        self.assertEqual((out["interest_eur_yr"], out["rate_unknown_eur"]), (0.0, 10000.0))


if __name__ == "__main__":
    unittest.main()
