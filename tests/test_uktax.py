"""UK tax-year summary on canned trades (no network)."""

import unittest
from datetime import date
from unittest import mock

from backend import cache, fx, sources, uktax
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
        cache.clear()  # summaries are cached process-wide
        self.addCleanup(cache.clear)
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
            mock.patch.object(trading212, "accounts", return_value=[
                mock.Mock(label="T212 Invest", is_isa=False), mock.Mock(label="T212 ISA", is_isa=True)]),
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
        self.assertTrue(s["isa"]["tracked"])
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

    def test_one_pool_across_taxable_accounts(self):
        # HMRC pools the same share across brokers: per account this sale would
        # be a £200 gain, pooled with the dearer DeGiro shares it is a £300 loss.
        invest = [t("2026-05-01", "BUY", 10, 1000), t("2026-10-01", "SELL", 10, 1200)]
        degiro = [t("2026-06-01", "BUY", 10, 2000)]
        with mock.patch.object(sources, "trade_sources", return_value={
                "T212 Invest": (False, lambda: invest), "DeGiro": (False, lambda: degiro)}):
            s = uktax.summary(today=date(2026, 10, 10))
        self.assertEqual((s["gains_gbp"], s["losses_gbp"]), (0, -300.0))
        self.assertEqual(s["recent_sales"][0]["account"], "T212 Invest")

    def test_resident_from_trims_window(self):
        with mock.patch.dict("os.environ", {"UK_RESIDENT_FROM": "2026-11-06"}):
            s = uktax.summary(today=date(2026, 11, 6))
        self.assertEqual((s["from"], s["disposals"]), ("2026-11-06", 0))

    def summary_of(self, trades, today, env=None, **extra):
        with mock.patch.object(sources, "trade_sources", return_value={
                "T212 Invest": (False, lambda: trades), **extra}), \
                mock.patch.dict("os.environ", env or {"UK_RESIDENT_FROM": ""}):
            return uktax.summary(today=today)

    def test_thirty_day_rule_matches_the_rebuy(self):
        # HMRC CG51560 / TCGA92 s106A(5): the sale is matched with the shares
        # bought back within 30 days, not with the pool.
        # Sold 100 for £800, bought 100 back 14 days later for £820:
        #   loss = 800 - 820 = -£20 (pool-only would say 800 - 1,000 = -£200).
        trades = [t("2026-05-01", "BUY", 100, 1000), t("2026-10-01", "SELL", 100, 800),
                  t("2026-10-15", "BUY", 100, 820)]
        s = self.summary_of(trades, date(2026, 10, 20))
        self.assertEqual((s["gains_gbp"], s["losses_gbp"]), (0, -20.0))

    def test_same_day_rule_comes_first(self):
        # TCGA92 s105(1): shares bought on the day of the sale are matched first.
        # Pool 100 @ £10; same day buy 50 for £600, sell 50 for £550:
        #   loss = 550 - 600 = -£50 (pool-only: 550 - 50 x 1,600/150 = +£16.67).
        trades = [t("2026-05-01", "BUY", 100, 1000), t("2026-09-01T10:00:00Z", "BUY", 50, 600),
                  t("2026-09-01T15:00:00Z", "SELL", 50, 550)]
        s = self.summary_of(trades, date(2026, 9, 2))
        self.assertEqual((s["gains_gbp"], s["losses_gbp"]), (0, -50.0))

    def test_thirty_day_matches_earliest_first_then_pool(self):
        # Pool 100 @ £20. Sell 100 for £1,000 on 1 Jul; buy 60 for £540 on 6 Jul
        # and 60 for £600 on 21 Jul. s106A(5): earliest first ->
        #   60 @ £9 + 40 @ £10 = £940, gain +£60.
        # The pool keeps the 100 @ £2,000 plus the unmatched 20 @ £200.
        # Sell 120 for £1,800 on 10 Aug (pool 120 @ £2,200): loss -£400.
        trades = [t("2026-05-01", "BUY", 100, 2000), t("2026-07-01", "SELL", 100, 1000),
                  t("2026-07-06", "BUY", 60, 540), t("2026-07-21", "BUY", 60, 600),
                  t("2026-08-10", "SELL", 120, 1800)]
        s = self.summary_of(trades, date(2026, 9, 1))
        self.assertEqual((s["gains_gbp"], s["losses_gbp"], s["net_gain_gbp"]), (60.0, -400.0, -340.0))
        self.assertEqual(s["disposals"], 2)

    def test_no_thirty_day_match_for_buys_before_uk_residence(self):
        # s106A(5A): the 30-day rule ignores shares bought while not UK resident.
        # Pool 10 @ £100. Sell 10 for £500 on 25 May (not resident yet, pool:
        # not counted), rebuy 10 for £600 on 30 May (still not resident: joins
        # the pool), sell 10 for £900 on 1 Jul (resident): gain 900 - 600 = +£300.
        trades = [t("2026-01-10", "BUY", 10, 1000), t("2026-05-25", "SELL", 10, 500),
                  t("2026-05-30", "BUY", 10, 600), t("2026-07-01", "SELL", 10, 900)]
        s = self.summary_of(trades, date(2026, 7, 2), env={"UK_RESIDENT_FROM": "2026-06-01"})
        self.assertEqual((s["gains_gbp"], s["losses_gbp"]), (300.0, 0))

    def test_isa_not_tracked_when_another_isa_is_not_trading212(self):
        # Only Trading 212 ISA deposits are read. With a second ISA elsewhere,
        # "£15,000 left" would be wrong (money paid in there is unknown).
        s = self.summary_of(INVEST, date(2026, 11, 6), **{"T212 ISA": (True, lambda: ISA),
                                                          "Other ISA": (True, lambda: [])})
        self.assertFalse(s["isa"]["tracked"])


if __name__ == "__main__":
    unittest.main()
