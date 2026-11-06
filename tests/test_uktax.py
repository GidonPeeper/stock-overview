"""UK tax-year summary on canned trades (no network)."""

import unittest
from datetime import date
from unittest import mock

from backend import fx, sources, uktax
from backend.connectors import trading212
from backend.positions import Trade

RATES = {"EUR": 1.0, "GBP": 1.2, "USD": 0.9}
GOOGL = "US02079K3059"


def t(when, side, qty, amount, key=GOOGL, ccy="GBP"):
    return Trade(when=when, key=key, name="Alphabet", side=side, quantity=qty, amount=amount, currency=ccy)


INVEST = [
    t("2025-06-01", "BUY", 10, 1000), t("2026-03-01", "SELL", 5, 700),     # last tax year: +200
    t("2026-05-01", "BUY", 5, 600), t("2026-11-05T15:00:00Z", "SELL", 10, 880),  # pool 10 @ 110 -> -220
]
ISA = [t("2026-11-05T15:30:00Z", "BUY", 10, 890), t("2026-11-06", "SELL", 1, 95)]


class TaxYearTest(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(uktax.tax_year(date(2026, 4, 5))[2], "2025/26")
        self.assertEqual(uktax.tax_year(date(2026, 4, 6))[2], "2026/27")
        self.assertEqual(uktax.tax_year(date(2027, 1, 1))[:2], (date(2026, 4, 6), date(2027, 4, 5)))


class SummaryTest(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(fx, "_live_rates", return_value=RATES),
            mock.patch.object(sources, "trade_sources", return_value={
                "T212 Invest": (False, lambda: INVEST), "T212 ISA": (True, lambda: ISA)}),
            mock.patch.object(trading212, "get_dividends", return_value=[
                {"isa": False, "date": "2026-09-20", "amount_eur": 12.0},
                {"isa": True, "date": "2026-09-20", "amount_eur": 50.0}]),
            mock.patch.object(trading212, "get_interest", return_value=[]),
            mock.patch.object(trading212, "get_transactions", return_value=[
                {"isa": True, "type": "DEPOSIT", "date": "2026-10-20", "amount_eur": 18000.0},
                {"isa": True, "type": "DEPOSIT", "date": "2026-03-01", "amount_eur": 6000.0}]),
            mock.patch.object(trading212, "is_live", return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_summary(self):
        s = uktax.summary(today=date(2026, 11, 6))
        self.assertEqual(s["tax_year"], "2026/27")
        self.assertEqual((s["gains_gbp"], s["losses_gbp"], s["disposals"]), (0, -220.0, 1))  # ISA left out
        self.assertEqual(s["exempt_left_gbp"], 3000)
        self.assertEqual(s["dividends_gbp"], 10.0)                       # €12 at 1.2 = £10, ISA one ignored
        self.assertEqual((s["isa"]["paid_in_gbp"], s["isa"]["left_gbp"]), (15000.0, 5000.0))
        (sale,) = s["recent_sales"]
        self.assertEqual((sale["sold_on"], sale["rebuy_from"], sale["rebought"]),
                         ("2026-11-05", "2026-12-06", False))

    def test_rebuy_at_another_taxable_broker_counts(self):
        degiro = [t("2026-11-20", "BUY", 1, 90)]
        with mock.patch.object(sources, "trade_sources", return_value={
                "T212 Invest": (False, lambda: INVEST), "DeGiro": (False, lambda: degiro)}):
            s = uktax.summary(today=date(2026, 11, 21))
        self.assertTrue(s["recent_sales"][0]["rebought"])

    def test_resident_from_trims_window(self):
        with mock.patch.dict("os.environ", {"UK_RESIDENT_FROM": "2026-11-06"}):
            s = uktax.summary(today=date(2026, 11, 6))
        self.assertEqual((s["from"], s["disposals"]), ("2026-11-06", 0))


if __name__ == "__main__":
    unittest.main()
