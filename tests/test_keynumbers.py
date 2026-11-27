"""Key numbers of a share or fund (backend/keynumbers.py; /api/keynumbers).

No network: Yahoo is the fake from test_lookup (chart metadata and company
info); the reported accounts are a fake of keynumbers._fetch_series.
Exchange rates are fixed (test_lookup.RATES: 1 USD = 0.9 EUR, 1 GBP = 1.2
EUR). Every name, ticker and number here is made up, and every expected
value is worked out by hand in the comments.
"""

import unittest
from unittest import mock

from backend import fx, keynumbers, lookup
from tests.test_lookup import LookupCase, TestClient, inst


def accounts(ccy="USD", **over):
    """A made-up company's accounts, in the shape _fetch_series returns."""
    s = {
        "trailingTotalRevenue": [["2026-06-30", 10e9, ccy]],
        "trailingGrossProfit": [["2026-06-30", 4e9, ccy]],
        "trailingOperatingIncome": [["2026-06-30", 2e9, ccy]],
        "trailingEBITDA": [["2026-06-30", 3e9, ccy]],
        "trailingNetIncomeCommonStockholders": [["2026-06-30", 2.5e9, ccy]],
        "trailingDilutedEPS": [["2026-06-30", 2.5, ccy]],
        "trailingFreeCashFlow": [["2026-06-30", 1.8e9, ccy]],
        "trailingCashDividendsPaid": [["2026-06-30", -1e9, ccy]],
        "quarterlyTotalRevenue": [["2025-06-30", 2.0e9, ccy], ["2026-03-31", 2.4e9, ccy], ["2026-06-30", 2.5e9, ccy]],
        "quarterlyDilutedEPS": [["2025-06-30", 0.5, ccy], ["2026-06-30", 0.6, ccy]],
        "annualStockholdersEquity": [["2025-12-31", 18e9, ccy]],
        "quarterlyStockholdersEquity": [["2026-06-30", 20e9, ccy]],      # newer: this one counts
        "quarterlyTotalDebt": [["2026-06-30", 5e9, ccy]],
        "quarterlyCashCashEquivalentsAndShortTermInvestments": [["2026-06-30", 3e9, ccy]],
        "quarterlyCurrentAssets": [["2026-06-30", 8e9, ccy]],
        "quarterlyCurrentLiabilities": [["2026-06-30", 4e9, ccy]],
        "quarterlyInventory": [["2026-06-30", 2e9, ccy]],
        "quarterlyTotalAssets": [["2026-06-30", 50e9, ccy]],
        "quarterlyOrdinarySharesNumber": [["2026-06-30", 1e9, ccy]],
    }
    s.update(over)
    return {k: v for k, v in s.items() if v is not None}


class KeyNumbersCase(LookupCase):
    def series(self, data):
        """Install fake accounts: {ticker: dict or Exception}. Returns the calls."""
        calls = []

        def fetch(t):
            calls.append(t)
            v = data.get(t, {})
            if isinstance(v, Exception):
                raise v
            return v
        p = mock.patch.object(keynumbers, "_fetch_series", side_effect=fetch)
        p.start()
        self.addCleanup(p.stop)
        return calls


class AccountsTest(KeyNumbersCase):
    """Company info refused (as on a hosted server): worked out from the accounts."""

    def test_ratios_with_a_price_in_another_currency(self):
        # Trades at EUR 30, reports in USD: the price in USD is 30 / 0.9 = 33.333.
        self.use({"EXMP.AS": inst(30.0, 29.0, ccy="EUR")})
        self.series({"EXMP.AS": accounts("USD")})
        d = keynumbers.get("EXMP.AS")
        n = d["numbers"]
        self.assertEqual((d["source"], d["currency"], d["report_currency"], d["as_of"]),
                         ("reported accounts", "EUR", "USD", "2026-06-30"))
        self.assertAlmostEqual(n["market_cap"], 30e9, delta=1)              # 1e9 shares x EUR 30
        self.assertAlmostEqual(n["pe"], 13.33, places=2)                     # 33.333 / 2.5
        self.assertAlmostEqual(n["price_to_book"], 1.6667, places=4)         # 33.33bn / 20bn (newest equity)
        self.assertAlmostEqual(n["price_to_sales"], 3.3333, places=4)        # 33.33bn / 10bn
        self.assertAlmostEqual(n["enterprise_value"], 31.8e9, delta=2)       # (33.33 + 5 - 3)bn USD = 31.8bn EUR
        self.assertAlmostEqual(n["ev_to_ebitda"], 11.78, places=2)           # 35.33bn / 3bn
        self.assertEqual((n["gross_margin"], n["operating_margin"], n["profit_margin"]), (40.0, 20.0, 25.0))
        self.assertEqual((n["roe"], n["roa"]), (12.5, 5.0))                  # 2.5 / 20, 2.5 / 50
        self.assertEqual((n["debt_to_equity"], n["current_ratio"], n["quick_ratio"]), (0.25, 2.0, 1.5))
        self.assertEqual(n["payout_ratio"], 40.0)                            # 1bn of 2.5bn
        self.assertAlmostEqual(n["dividend_yield"], 3.0, places=4)           # USD 1 a share / 33.333
        self.assertEqual((n["revenue_growth"], n["earnings_growth"]), (25.0, 20.0))   # 2.5/2.0, 0.6/0.5
        self.assertEqual((n["revenue"], n["ebitda"], n["eps"]), (10e9, 3e9, 2.5))     # as reported, in USD
        for k in ("forward_pe", "peg", "beta"):                              # need analysts / price history
            self.assertNotIn(k, n)

    def test_a_share_quoted_in_pence_uses_pounds_once(self):
        # 547p = GBP 5.47; the accounts are in GBP: P/E 5.47 / 0.25 = 21.88.
        self.use({"EXPL.L": inst(547.0, 540.0, ccy="GBp")})
        self.series({"EXPL.L": accounts("GBP", trailingDilutedEPS=[["2026-06-30", 0.25, "GBP"]],
                                        quarterlyOrdinarySharesNumber=[["2026-06-30", 1e8, "GBP"]])})
        n = keynumbers.get("EXPL.L")["numbers"]
        self.assertAlmostEqual(n["pe"], 21.88, places=2)
        self.assertAlmostEqual(n["market_cap"], 5.47e8, delta=1)            # pounds, not pence

    def test_a_loss_gives_no_pe_and_no_growth_from_a_loss(self):
        self.use({"LOSS": inst(10.0, 10.0, ccy="USD")})
        self.series({"LOSS": accounts("USD",
                                      trailingDilutedEPS=[["2026-06-30", -1.0, "USD"]],
                                      trailingEBITDA=[["2026-06-30", -5e8, "USD"]],
                                      trailingNetIncomeCommonStockholders=[["2026-06-30", -1e9, "USD"]],
                                      quarterlyDilutedEPS=[["2025-06-30", -0.2, "USD"], ["2026-06-30", 0.1, "USD"]])})
        n = keynumbers.get("LOSS")["numbers"]
        for k in ("pe", "ev_to_ebitda", "earnings_growth", "payout_ratio"):
            self.assertNotIn(k, n)
        self.assertEqual((n["eps"], n["ebitda"], n["profit_margin"]), (-1.0, -5e8, -10.0))

    def test_growth_falls_back_to_the_year_before(self):
        # No quarters: the yearly figures, 12 against 10 = +20%.
        self.use({"HALF.L": inst(500.0, 500.0, ccy="GBp")})
        self.series({"HALF.L": accounts("GBP", quarterlyTotalRevenue=None, quarterlyDilutedEPS=None,
                                        annualTotalRevenue=[["2024-12-31", 10e9, "GBP"], ["2025-12-31", 12e9, "GBP"]])})
        n = keynumbers.get("HALF.L")["numbers"]
        self.assertEqual(n["revenue_growth"], 20.0)
        self.assertNotIn("earnings_growth", n)                               # one EPS point: no year to compare

    def test_no_exchange_rate_means_no_price_ratios(self):
        # fx counts an unknown currency as 1 EUR; a P/E from that would be wrong, so none.
        self.use({"EXMP.XX": inst(30.0, 29.0, ccy="XYZ")})
        self.series({"EXMP.XX": accounts("USD")})
        with mock.patch.object(fx, "_other_rate", return_value=None):
            n = keynumbers.get("EXMP.XX")["numbers"]
        for k in ("pe", "market_cap", "price_to_book", "enterprise_value", "dividend_yield"):
            self.assertNotIn(k, n)
        self.assertEqual(n["gross_margin"], 40.0)                            # needs no price

    def test_yahoo_down_is_a_plain_error_and_not_kept(self):
        self.use({"EXMP": inst(30.0, 29.0, ccy="USD")})
        calls = self.series({"EXMP": RuntimeError("connection reset")})
        with self.assertRaises(lookup.QuoteError) as e:
            keynumbers.get("EXMP")
        self.assertEqual(e.exception.status, 503)
        self.assertIn("not answering", str(e.exception))
        self.series({"EXMP": accounts("USD")})
        keynumbers.cache.clear()                                             # past the short error hold
        self.assertIn("pe", keynumbers.get("EXMP")["numbers"])
        self.assertEqual(calls, ["EXMP"])


class CompanyInfoTest(KeyNumbersCase):
    INFO = {"quoteType": "EQUITY", "financialCurrency": "USD", "sector": "Technology", "industry": "Software",
            "mostRecentQuarter": 1782777600,                                 # 2026-06-30
            "marketCap": 5e11, "trailingPE": -5.0, "forwardPE": 25.0, "grossMargins": 0.488,
            "returnOnEquity": 0, "debtToEquity": 84.286, "dividendYield": 0.3, "payoutRatio": 0.118,
            "revenueGrowth": -0.05, "totalRevenue": 1e11, "beta": 1.2, "trailingEps": -0.5}

    def test_units_and_what_is_left_out(self):
        self.use({"EXMP": inst(100.0, 99.0, ccy="USD")}, info={"EXMP": self.INFO})
        calls = self.series({})
        d = keynumbers.get("EXMP")
        n = d["numbers"]
        self.assertEqual((d["source"], d["report_currency"], d["sector"], d["industry"], d["as_of"]),
                         ("company info", "USD", "Technology", "Software", "2026-06-30"))
        self.assertEqual(n["gross_margin"], 48.8)                            # a fraction becomes %
        self.assertEqual(n["debt_to_equity"], 0.8429)                        # Yahoo's % becomes a ratio
        self.assertEqual((n["dividend_yield"], n["payout_ratio"]), (0.3, 11.8))   # already %, fraction
        self.assertEqual((n["revenue_growth"], n["forward_pe"], n["beta"]), (-5.0, 25.0, 1.2))
        self.assertNotIn("pe", n)                                            # negative P/E means nothing
        self.assertNotIn("roe", n)                                           # 0 = not reported
        self.assertEqual(calls, [])                                          # info had the accounts

    def test_info_without_accounts_is_filled_from_the_accounts(self):
        info = {"quoteType": "EQUITY", "financialCurrency": "USD", "forwardPE": 20.0, "beta": 0.9}
        self.use({"EXMP": inst(30.0, 29.0, ccy="USD")}, info={"EXMP": info})
        self.series({"EXMP": accounts("USD")})
        d = keynumbers.get("EXMP")
        self.assertEqual(d["source"], "company info and reported accounts")
        self.assertEqual((d["numbers"]["forward_pe"], d["numbers"]["beta"], d["numbers"]["pe"]), (20.0, 0.9, 12.0))


class FundAndOtherKindsTest(KeyNumbersCase):
    def test_fund_numbers_from_info(self):
        info = {"quoteType": "ETF", "netExpenseRatio": 0.07, "trailingPE": 22.3, "yield": 0.0107,
                "threeYearAverageReturn": 0.178, "fiveYearAverageReturn": -0.02, "beta3Year": 0.97,
                "category": "Large Blend", "fundFamily": "Example Funds"}
        self.use({"EXFD": inst(500.0, 499.0, ccy="USD", kind="ETF")}, info={"EXFD": info})
        calls = self.series({})
        d = keynumbers.get("EXFD")
        self.assertEqual((d["kind"], d["category"], d["fund_family"]), ("etf", "Large Blend", "Example Funds"))
        self.assertEqual(d["numbers"], {"yearly_cost": 0.07, "pe": 22.3, "dividend_yield": 1.07,
                                        "return_3y": 17.8, "return_5y": -2.0, "beta": 0.97})
        self.assertEqual(calls, [])                                          # funds have no company accounts

    def test_fund_cost_from_the_checked_table_when_info_is_refused(self):
        self.use({"SWDA.L": inst(9000.0, 8990.0, ccy="GBp", kind="ETF")})
        self.series({})
        d = keynumbers.get("SWDA.L")
        self.assertEqual((d["numbers"], d["source"]), ({"yearly_cost": 0.2}, "fund fee table"))

    def test_index_has_no_key_numbers_and_asks_nothing_more(self):
        self.use({"^EXI": inst(5000.0, 4990.0, ccy="USD", kind="INDEX")})
        series = self.series({})
        d = keynumbers.get("^EXI")
        self.assertEqual((d["kind"], d["numbers"]), ("index", {}))
        self.assertIn("shares and funds", d["message"])
        self.assertFalse(keynumbers.cache.peek("keynumbers.raw:^EXI")[0])   # Yahoo was not asked for company numbers
        self.assertEqual(series, [])


@unittest.skipIf(TestClient is None, "starlette not installed")
class ApiTest(KeyNumbersCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_mcp import sample_world
        sample_world(cls)
        from backend import main
        cls.client = TestClient(main.app, base_url="http://localhost:8000", raise_server_exceptions=False)

    def test_answer_and_plain_errors(self):
        self.use({"EXMP": inst(30.0, 29.0, ccy="USD"), "NOPE1": None, "DOWN": inst(30.0, 29.0, ccy="USD")}, search=[])
        self.series({"EXMP": accounts("USD"), "DOWN": RuntimeError("reset")})
        r = self.client.get("/api/keynumbers/EXMP")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["numbers"]["pe"], 12.0)                    # 30 / 2.5
        for path, status in (("/api/keynumbers/NOPE1", 404), ("/api/keynumbers/DOWN", 503),
                             ("/api/keynumbers/%3Cb%3E", 400)):
            with self.subTest(path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, status)
                self.assertIsInstance(r.json()["detail"], str)
                self.assertNotIn("Traceback", r.text)


if __name__ == "__main__":
    unittest.main()
