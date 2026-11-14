"""Ways to save (advisors.py) on synthetic numbers (no network).

1 GBP = 1.25 EUR here, so every amount below is written in pounds and
converted with eur().
"""

import unittest
from datetime import date
from unittest import mock

from backend import advisors as adv
from backend import fx, uktax

RATES = {"EUR": 1.0, "GBP": 1.25, "USD": 0.9}
TODAY = date(2026, 11, 14)

VOO = "US9229083632"     # Vanguard S&P 500 ETF (US-domiciled)
AAPL = "US0378331005"
CSPX = "IE00B5BMR087"    # iShares Core S&P 500 UCITS (Irish)
INDIA = "IE00BZCQB185"   # iShares MSCI India, 0.65%
GOLD = "IE00B4ND3602"    # iShares Physical Gold ETC


def eur(gbp):
    return gbp * 1.25


def row(name, value, profit=0.0, isin="", ticker="", wrapper="", qty=10.0, broker="Broker"):
    return {"broker": broker, "ticker": ticker or name.split()[0].upper(), "isin": isin,
            "name": name, "quantity": qty, "currency": "GBP",
            "value_eur": eur(value), "cost_eur": eur(value - profit), "profit_eur": eur(profit),
            "wrapper": wrapper}


def tax(exempt_left=3000.0, isa_left=None, net_gain=0.0):
    return {"tax_year": "2026/27", "exempt_left_gbp": exempt_left, "net_gain_gbp": net_gain,
            "isa": {"tracked": isa_left is not None, "left_gbp": isa_left or 0.0}}


def fin(income=None, fixed=None, loans=(), payments=0.0, sample=False):
    items = []
    if income is not None:
        f = income if fixed is None else fixed
        items.append({"name": "Salary", "monthly_eur": eur(f) / 12, "fixed": True})
        if income > f:
            items.append({"name": "Commission", "monthly_eur": eur(income - f) / 12, "fixed": False})
    return {"sample": sample, "income": items, "expenses": [],
            "loans": [{"name": n, "rate": r, "balance_eur": eur(b), "currency": "GBP"}
                      for n, r, b in loans],
            "totals": {"monthly_payments": eur(payments)}}


def cash(*accounts):
    accs = [{"name": n, "type": "savings", "balance_eur": eur(b), "rate": r, "currency": "GBP"}
            for n, b, r in accounts]
    return {"sample": False, "accounts": accs,
            "interest_eur_yr": sum(a["balance_eur"] * a["rate"] / 100 for a in accs)}


def no_divs():
    return {"holdings": []}


def run(rows=(), **kw):
    kw.setdefault("dividends", no_divs())
    return adv.build(adv.Inputs(rows=list(rows), today=TODAY, **kw))


def tip(out, tid):
    return next((t for t in out["tips"] if t["id"] == tid), None)


def prompt_ids(out):
    return {p["id"] for p in out["prompts"]}


class Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(fx, "_live_rates", return_value=RATES)
        p.start()
        self.addCleanup(p.stop)


class ConstantsTest(Base):
    def test_same_allowances_as_uk_tax_card(self):
        self.assertEqual(adv.ISA_ALLOWANCE, uktax.ISA_ALLOWANCE_GBP)
        self.assertEqual(adv.CGT_EXEMPT, uktax.CGT_ANNUAL_EXEMPT_GBP)
        self.assertEqual(adv.DIVIDEND_ALLOWANCE, uktax.DIVIDEND_ALLOWANCE_GBP)

    def test_bands(self):
        self.assertEqual([adv._band(x) for x in (None, 40_000, 80_000, 130_000)],
                         ["basic", "basic", "higher", "additional"])
        self.assertEqual(adv._personal_allowance(110_000), 7_570)
        self.assertEqual(adv._personal_allowance(130_000), 0)


class IsaAllowanceTest(Base):
    ROWS = [row("Small gain", 4000, 100), row("Big gain", 4000, 2000),
            row("Inside ISA", 9000, 500, wrapper="ISA")]

    def test_bed_and_isa(self):
        out = run(self.ROWS, tax=tax(isa_left=5000))
        t = tip(out, "isa_allowance")
        self.assertAlmostEqual(t["saving_gbp"], 5000 * 0.05 * 0.18)   # no dividends, basic rate
        self.assertEqual(t["period"], "year")
        self.assertEqual(t["deadline"], "2027-04-05")
        self.assertEqual(t["days_left"], 142)
        # smallest gain per pound first; second holding only up to the £5,000 left
        self.assertEqual([i["label"] for i in t["items"]], ["Small gain (Broker)", "Big gain (Broker)"])
        self.assertEqual([i["amount_eur"] for i in t["items"]], [eur(4000), eur(1000)])

    def test_dividend_yield_counts(self):
        divs = {"holdings": [{"ticker": "SMALL", "annual_eur": eur(200)}]}
        t = tip(run(self.ROWS, tax=tax(isa_left=5000), dividends=divs), "isa_allowance")
        self.assertAlmostEqual(t["saving_gbp"], 5000 * (200 / 8000 * 0.1075 + 0.05 * 0.18), 2)

    def test_untracked_isa_prompts(self):
        out = run(self.ROWS, tax=tax(isa_left=None))
        self.assertIsNone(tip(out, "isa_allowance"))
        self.assertIn("isa", prompt_ids(out))

    def test_allowance_used(self):
        out = run(self.ROWS, tax=tax(isa_left=0))
        self.assertIsNone(tip(out, "isa_allowance"))
        self.assertTrue(any(c.startswith("ISA allowance") for c in out["all_clear"]))


class CgtAllowanceTest(Base):
    ROWS = [row("Alpha", 5000, 2000), row("Beta", 3000, 1500), row("Gamma", 2000, -300),
            row("Sheltered", 9000, 5000, wrapper="ISA")]

    def test_realise_up_to_allowance(self):
        t = tip(run(self.ROWS, tax=tax(exempt_left=3000)), "cgt_allowance")
        self.assertAlmostEqual(t["saving_gbp"], 3000 * 0.18)
        self.assertEqual(t["period"], "once")
        self.assertEqual([i["amount_eur"] for i in t["items"]], [eur(5000), eur(2000)])

    def test_higher_rate(self):
        t = tip(run(self.ROWS, tax=tax(exempt_left=1000), fin=fin(80_000)), "cgt_allowance")
        self.assertAlmostEqual(t["saving_gbp"], 1000 * 0.24)

    def test_nothing_to_do(self):
        self.assertIsNone(tip(run(self.ROWS, tax=tax(exempt_left=0)), "cgt_allowance"))
        out = run([row("Gamma", 2000, -300)], tax=tax())
        self.assertIsNone(tip(out, "cgt_allowance"))
        self.assertTrue(any("no gains" in c for c in out["all_clear"]))

    def test_no_tax_summary_skips(self):
        self.assertIsNone(tip(run(self.ROWS, tax=None), "cgt_allowance"))


class DividendTest(Base):
    def rows(self):
        return [row("Payer", 30_000, ticker="PAY", qty=30), row("Payer", 10_000, ticker="PAY",
                                                               qty=10, wrapper="ISA")]

    DIVS = {"holdings": [{"ticker": "PAY", "annual_eur": eur(1000)}]}  # all 40 shares

    def test_tax_above_allowance(self):
        t = tip(run(self.rows(), tax=tax(), dividends=self.DIVS), "dividend_allowance")
        self.assertAlmostEqual(t["saving_gbp"], (750 - 500) * 0.1075, delta=0.01)  # 30/40 of £1,000

    def test_limited_by_isa_allowance(self):
        t = tip(run(self.rows(), tax=tax(isa_left=6000), dividends=self.DIVS), "dividend_allowance")
        # 6,000 of 30,000 moved = £150 of dividends; £600 stays, £100 taxed
        self.assertAlmostEqual(t["saving_gbp"], (250 - 100) * 0.1075, delta=0.01)

    def test_within_allowance(self):
        divs = {"holdings": [{"ticker": "PAY", "annual_eur": eur(600)}]}
        out = run(self.rows(), tax=tax(), dividends=divs)
        self.assertIsNone(tip(out, "dividend_allowance"))
        self.assertTrue(any(c.startswith("Dividend allowance") for c in out["all_clear"]))

    def test_forecast_not_ready_prompts(self):
        out = run(self.rows(), tax=tax(), dividends=None)
        self.assertIn("dividends", prompt_ids(out))


class ReportingFundTest(Base):
    def test_us_etf_flagged_others_not(self):
        rows = [row("Vanguard S&P 500 ETF", 10_000, 2000, isin=VOO),
                row("iShares Core S&P 500 UCITS ETF", 5000, 1000, isin=CSPX),
                row("Apple Inc", 5000, 1000, isin=AAPL),
                row("iShares Physical Gold ETC", 5000, 1000, isin=GOLD),
                row("SPDR S&P 500 ETF Trust", 5000, 1000, isin="US78462F1030", wrapper="ISA")]
        out = run(rows, tax=tax(), fin=fin(80_000))
        t = tip(out, "reporting_funds")
        self.assertEqual([i["label"] for i in t["items"]], ["Vanguard S&P 500 ETF (Broker)"])
        self.assertAlmostEqual(t["saving_gbp"], 10_000 * 0.05 * (0.40 - 0.24))
        self.assertIn("{{%.2f}}" % eur(2000 * 0.16), t["explanation"])  # extra tax on today's gain
        self.assertTrue(t["link"].startswith("https://www.gov.uk/"))
        self.assertTrue(any("Irish/Luxembourg" in c for c in out["all_clear"]))

    def test_nothing_flagged(self):
        out = run([row("Apple Inc", 5000, 1000, isin=AAPL)], tax=tax())
        self.assertIsNone(tip(out, "reporting_funds"))


class FundFeeTest(Base):
    def test_cheaper_fund(self):
        rows = [row("iShares MSCI India", 10_000, isin=INDIA),
                row("iShares Core MSCI World", 10_000, ticker="SWDA.L"),   # matched by ticker
                row("iShares Core S&P 500", 10_000, isin=CSPX)]            # 0.02% gap: too small
        t = tip(run(rows, tax=tax()), "fund_fees")
        self.assertAlmostEqual(t["saving_gbp"], 10_000 * (0.46 + 0.08) / 100)
        self.assertEqual(len(t["items"]), 2)  # India and World only
        self.assertIn("FRIN.L", t["items"][0]["detail"])

    def test_table_is_consistent(self):
        groups = adv._fund_table()
        self.assertTrue(groups)
        for g in groups:
            for f in g["funds"]:
                self.assertGreater(f["ter_pct"], g["cheaper"]["ter_pct"], f["name"])
                self.assertTrue(adv.is_valid_isin(f["isin"]), f["isin"])
            self.assertTrue(adv.is_valid_isin(g["cheaper"]["isin"]))
            self.assertTrue(g["cheaper"]["yahoo"])


class PensionTest(Base):
    def test_needs_income(self):
        out = run(tax=tax())
        self.assertIsNone(tip(out, "pension_relief"))
        self.assertIn("income", prompt_ids(out))

    def test_basic_rate_nothing_extra(self):
        out = run(tax=tax(), fin=fin(40_000))
        self.assertIsNone(tip(out, "pension_relief"))
        self.assertNotIn("income", prompt_ids(out))

    def test_higher_rate(self):
        t = tip(run(tax=tax(), fin=fin(80_000)), "pension_relief")
        self.assertAlmostEqual(t["saving_gbp"], (80_000 - 50_270) * 0.40, 1)

    def test_taper_band_and_variable_income(self):
        t = tip(run(tax=tax(), fin=fin(110_000, fixed=90_000)), "pension_relief")
        self.assertAlmostEqual(t["saving_gbp"], 10_000 * 0.60, 1)
        self.assertIn("variable income", t["explanation"])
        self.assertIn("Scotland", t["explanation"])

    def test_additional_rate_and_annual_allowance(self):
        t = tip(run(tax=tax(), fin=fin(150_000)), "pension_relief")
        self.assertAlmostEqual(t["saving_gbp"], 24_860 * 0.45 + 25_140 * 0.60, 1)
        t = tip(run(tax=tax(), fin=fin(200_000)), "pension_relief")
        self.assertAlmostEqual(t["saving_gbp"], 60_000 * 0.45, 1)   # capped at £60,000

    def test_capped_by_spare_cash_flow(self):
        f = fin(80_000)
        f["totals"].update(monthly_payments=eur(5000), free_cashflow=eur(80_000 / 12 - 5000))
        t = tip(run(tax=tax(), fin=f), "pension_relief")
        self.assertAlmostEqual(t["saving_gbp"], 20_000 * 0.40, 0)   # £20,000 spare < £29,730 slice
        self.assertIn("spare cash flow", t["how"])

    def test_sample_finances_ignored_outside_demo(self):
        sample = fin(80_000, sample=True)
        self.assertIsNone(tip(run(tax=tax(), fin=sample), "pension_relief"))
        self.assertIsNotNone(tip(run(tax=tax(), fin=sample, demo=True), "pension_relief"))


class LoanCashTest(Base):
    def test_pay_off_expensive_loan(self):
        out = run(tax=tax(), fin=fin(loans=[("Car loan", 6.0, 10_000)], payments=1000),
                  cash=cash(("Saver", 20_000, 4.0)))
        t = tip(out, "loan_payoff:Car loan")
        self.assertAlmostEqual(t["saving_gbp"], 10_000 * (6 - 4) / 100)

    def test_interest_tax_lowers_cash_rate(self):
        # £800 interest > £500 higher-rate allowance: cash earns 4% × 0.6 = 2.4%
        out = run(tax=tax(), fin=fin(80_000, loans=[("Car loan", 6.0, 10_000)], payments=1000),
                  cash=cash(("Saver", 20_000, 4.0)))
        self.assertAlmostEqual(tip(out, "loan_payoff:Car loan")["saving_gbp"], 10_000 * 3.6 / 100)

    def test_keep_cheap_loan_links_cash_optimizer(self):
        out = run(tax=tax(), fin=fin(loans=[("Student loan", 1.0, 10_000)]),
                  cash=cash(("Saver", 20_000, 4.0), ("Current", 2000, 0.0)))
        t = tip(out, "loan_keep:Student loan")
        self.assertAlmostEqual(t["saving_gbp"], 10_000 * 3 / 100)
        self.assertEqual(t["link"], "/#cashopt")
        self.assertFalse(t["in_total"])          # not doing something isn't a saving
        self.assertEqual(out["total_per_year_eur"], 0)

    def test_missing_cash_prompts_and_no_loans_is_silent(self):
        out = run(tax=tax(), fin=fin(loans=[("Car loan", 6.0, 10_000)]))
        self.assertIn("cash", prompt_ids(out))
        out = run(tax=tax(), fin=fin(), cash=cash(("Saver", 1000, 1.0)))
        self.assertFalse([t for t in out["tips"] if t["id"].startswith("loan")])


class SavingsInterestTest(Base):
    def test_interest_above_allowance(self):
        out = run(tax=tax(), fin=fin(80_000), cash=cash(("Saver", 50_000, 4.0)))
        t = tip(out, "savings_interest")
        self.assertAlmostEqual(t["saving_gbp"], (2000 - 500) * 0.40)

    def test_limited_by_isa_allowance(self):
        out = run(tax=tax(isa_left=20_000), fin=fin(80_000), cash=cash(("Saver", 50_000, 4.0)))
        # £37,500 would need to move; only £20,000 fits
        self.assertAlmostEqual(tip(out, "savings_interest")["saving_gbp"], 600 * 20_000 / 37_500)

    def test_pension_pots_are_not_savings(self):
        c = cash(("Saver", 10_000, 4.0))
        c["accounts"].append({"name": "Pension", "type": "retirement", "balance_eur": eur(500_000),
                              "rate": 5.0, "currency": "GBP"})
        out = run(tax=tax(), cash=c)
        self.assertIsNone(tip(out, "savings_interest"))

    def test_within_allowance(self):
        out = run(tax=tax(), cash=cash(("Saver", 10_000, 4.0)))
        self.assertIsNone(tip(out, "savings_interest"))
        self.assertTrue(any(c.startswith("Savings interest") for c in out["all_clear"]))


class FigRegimeTest(Base):
    ROWS = [row("Apple Inc", 30_000, 10_000, isin=AAPL)]

    def test_worth_claiming_without_personal_allowance(self):
        out = run(self.ROWS, tax=tax(net_gain=5000), fin=fin(150_000),
                  resident_from=date(2026, 9, 1), foreign_gains_gbp=5000)
        t = tip(out, "fig_regime")
        # 5,000 × 24% relieved − 3,000 × 24% CGT allowance lost (no personal allowance left)
        self.assertAlmostEqual(t["saving_gbp"], 5000 * 0.24 - 3000 * 0.24)

    def test_not_worth_it_with_personal_allowance(self):
        out = run(self.ROWS, tax=tax(net_gain=5000), fin=fin(60_000),
                  resident_from=date(2026, 9, 1), foreign_gains_gbp=5000)
        self.assertIsNone(tip(out, "fig_regime"))
        self.assertTrue(any("foreign income and gains" in c.lower() for c in out["all_clear"]))

    def test_uk_gains_lose_cgt_allowance_unless_reset(self):
        # £1,000 foreign of £5,000 gains: the claim costs the £3,000 allowance on the
        # £4,000 UK gain, so it only pays if the £10,000 unrealised foreign gain is reset too
        out = run(self.ROWS, tax=tax(net_gain=5000), fin=fin(150_000),
                  resident_from=date(2026, 9, 1), foreign_gains_gbp=1000)
        t = tip(out, "fig_regime")
        self.assertAlmostEqual(t["saving_gbp"], (2000 - 4000) * 0.24 + 10_000 * 0.24)
        self.assertIn("resets their cost", t["explanation"])

    def test_needs_income(self):
        out = run(self.ROWS, tax=tax(), resident_from=date(2026, 9, 1), foreign_gains_gbp=5000)
        self.assertIsNone(tip(out, "fig_regime"))
        self.assertIn("income", prompt_ids(out))

    def test_only_first_four_years(self):
        for arrived in (None, date(2021, 1, 1)):
            out = run(self.ROWS, tax=tax(), fin=fin(150_000), resident_from=arrived,
                      foreign_gains_gbp=50_000)
            self.assertIsNone(tip(out, "fig_regime"))


class OverallTest(Base):
    def test_sorted_and_totalled(self):
        out = run([row("Alpha", 5000, 2000), row("iShares MSCI India", 10_000, isin=INDIA)],
                  tax=tax(), fin=fin(80_000))
        savings = [t["saving_gbp"] for t in out["tips"]]
        self.assertEqual(savings, sorted(savings, reverse=True))
        self.assertAlmostEqual(out["total_once_eur"],
                               sum(t["saving_eur"] for t in out["tips"] if t["period"] == "once"))
        self.assertIn("Not tax or financial advice", out["disclaimer"])
        for t in out["tips"]:
            self.assertAlmostEqual(t["saving_eur"], eur(t["saving_gbp"]), 1)
            for key in ("title", "explanation", "how", "action", "data_used"):
                self.assertTrue(t[key], (t["id"], key))

    def test_other_residence_skips_uk_checks(self):
        rows = [row("Alpha", 5000, 2000), row("iShares MSCI India", 10_000, isin=INDIA)]
        out = run(rows, residence="NL", fin=fin(80_000, loans=[("Car loan", 6.0, 10_000)]),
                  cash=cash(("Saver", 20_000, 4.0)))
        self.assertEqual({t["id"] for t in out["tips"]}, {"fund_fees", "loan_payoff:Car loan"})
        self.assertEqual(out["prompts"], [])
        self.assertIn("TAX_RESIDENCE", out["note"])
        # no UK tax on the cash rate: 6% − 4%
        self.assertAlmostEqual(tip(out, "loan_payoff:Car loan")["saving_gbp"], 200)

    def test_residence_env(self):
        with mock.patch.dict("os.environ", {"TAX_RESIDENCE": "nl"}):
            self.assertEqual(adv.residence(), "NL")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(adv.residence(), "UK")

    def test_a_failing_check_does_not_hide_the_rest(self):
        with mock.patch.object(adv, "check_cgt_allowance", side_effect=RuntimeError), \
                mock.patch.object(adv, "UK_CHECKS", (adv.check_cgt_allowance, adv.check_pension)):
            out = run(tax=tax(), fin=fin(80_000))
        self.assertIsNotNone(tip(out, "pension_relief"))


class CollectTest(Base):
    def test_collect_uses_cached_inputs(self):
        from backend import cash as cash_mod, finances, reports

        rows = [row("Payer", 30_000, 5000, ticker="PAY", qty=30)]
        with mock.patch.object(finances, "load", return_value=fin(80_000)), \
                mock.patch.object(cash_mod, "load", return_value=cash(("Saver", 1000, 1.0))), \
                mock.patch.object(uktax, "summary", return_value=tax(isa_left=10_000)), \
                mock.patch.object(reports, "income_projection",
                                  return_value={"holdings": [{"ticker": "PAY", "annual_eur": eur(900)}]}), \
                mock.patch.dict("os.environ", {"TAX_RESIDENCE": "UK", "UK_RESIDENT_FROM": ""}):
            out = adv.collect(rows, today=TODAY)
        ids = {t["id"] for t in out["tips"]}
        self.assertTrue({"isa_allowance", "cgt_allowance", "dividend_allowance",
                         "pension_relief"} <= ids, ids)
        self.assertNotIn("dividends", prompt_ids(out))


if __name__ == "__main__":
    unittest.main()
