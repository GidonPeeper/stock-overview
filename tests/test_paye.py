"""Take-home pay (paye.py) and how finances.py applies it. No network."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from backend import finances, fx, paye

RATES = {"EUR": 1.0, "GBP": 1.0 / 0.86, "USD": 0.9}   # 1 GBP = 1.1628 EUR


class AnnualTest(unittest.TestCase):
    def test_60k_salary(self):
        t = paye.annual([{"gross": 60_000}])["total"]
        self.assertAlmostEqual(t["income_tax"], 11_432, places=2)
        self.assertAlmostEqual(t["ni"], 3_210.60, places=2)
        self.assertAlmostEqual(t["net"] / 12, 3_779.78, places=2)

    def test_below_allowance_pays_nothing(self):
        t = paye.annual([{"gross": 12_000}])["total"]
        self.assertEqual((t["income_tax"], t["ni"]), (0, 0))

    def test_salary_sacrifice_cuts_tax_and_ni(self):
        t = paye.annual([{"gross": 60_000, "pension_pct": 10, "pension_type": "sacrifice"}])["total"]
        self.assertAlmostEqual(t["pension"], 6_000, places=2)
        self.assertAlmostEqual(t["net"] / 12, 3_489.78, places=2)

    def test_net_pay_cuts_tax_not_ni(self):
        t = paye.annual([{"gross": 60_000, "pension_pct": 10, "pension_type": "net_pay"}])["total"]
        self.assertAlmostEqual(t["ni"], 3_210.60, places=2)
        self.assertAlmostEqual(t["income_tax"], 11_432 - 6_000 * 0.4, places=2)

    def test_relief_at_source_comes_off_take_home_at_80pct(self):
        t = paye.annual([{"gross": 30_000, "pension_pct": 5, "pension_type": "relief_at_source"}])["total"]
        plain = paye.annual([{"gross": 30_000}])["total"]
        self.assertAlmostEqual(plain["net"] - t["net"], 1_500 * 0.8, places=2)

    def test_commission_is_taxed_on_top_of_salary(self):
        items = paye.annual([{"gross": 60_000}, {"gross": 50_000}])["items"]
        # £40,000 at 40% up to £100,000, then £10,000 at 60% (allowance taper); NI 2%
        self.assertAlmostEqual(items[1]["income_tax"], 22_000, places=2)
        self.assertAlmostEqual(items[1]["ni"], 1_000, places=2)
        self.assertEqual(items[1]["top_rate"], 62.0)

    def test_additional_rate(self):
        tax = paye.income_tax(200_000)
        expected = 37_700 * 0.2 + (125_140 - 37_700) * 0.4 + (200_000 - 125_140) * 0.45
        self.assertAlmostEqual(tax, expected, places=2)


class PayslipTest(unittest.TestCase):
    def test_started_in_october(self):
        slips = paye.payslips(60_000, "2026-10", today=date(2026, 11, 20))
        self.assertEqual([s["month"] for s in slips],
                         ["2026-10", "2026-11", "2026-12", "2027-01", "2027-02", "2027-03"])
        self.assertEqual(slips[0]["income_tax"], 0)              # allowance for Apr-Oct unused
        self.assertAlmostEqual(slips[0]["net"], 4_732.45, places=2)
        self.assertAlmostEqual(slips[1]["net"], 4_408.45, places=2)
        self.assertAlmostEqual(slips[-1]["net"], 3_941.95, places=2)

    def test_nothing_special_for_a_full_year(self):
        self.assertIsNone(paye.payslips(60_000, "2026-04", today=date(2026, 11, 20)))
        self.assertIsNone(paye.payslips(60_000, "", today=date(2026, 11, 20)))
        self.assertIsNone(paye.payslips(60_000, "2025-10", today=date(2026, 11, 20)))


class FinancesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(finances, "DATA_DIR", self.dir),
            mock.patch.object(finances, "resolve",
                              lambda name: (self.dir / name, (self.dir / name).exists())),
            mock.patch.object(fx, "_live_rates", return_value=RATES),
            mock.patch.object(finances, "_residence", return_value="UK"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write(self, data):
        (self.dir / "finances.json").write_text(json.dumps(data))

    def test_old_salary_row_is_taxed(self):
        self.write({"income": [{"name": "Salary", "amount": 5000, "currency": "GBP", "fixed": True}],
                    "expenses": [{"name": "Rent", "amount": 1000, "currency": "GBP"}]})
        f = finances.load()
        row = f["income"][0]
        gbp = lambda eur: fx.from_eur(eur, "GBP")
        self.assertEqual(row["tax"], "before")
        self.assertAlmostEqual(gbp(row["net_monthly_eur"]), 3_779.78, delta=0.05)
        self.assertAlmostEqual(gbp(row["monthly_eur"]), 5_000, delta=0.05)  # advisors read gross
        self.assertAlmostEqual(gbp(f["totals"]["free_cashflow"]), 3_779.78 - 1_000, delta=0.05)
        self.assertAlmostEqual(gbp(f["fixed"]["income_total"]), 3_779.78, delta=0.05)
        self.assertTrue(f["fixed"]["income"][0]["taxed"])

    def test_after_tax_row_is_left_alone(self):
        self.write({"income": [{"name": "Rent from room", "amount": 500, "currency": "GBP",
                                "tax": "after"}]})
        row = finances.load()["income"][0]
        self.assertAlmostEqual(row["net_monthly_eur"], row["monthly_eur"])

    def test_not_uk_resident_no_tax(self):
        self.write({"income": [{"name": "Salary", "amount": 4000, "currency": "EUR"}]})
        with mock.patch.object(finances, "_residence", return_value="NL"):
            f = finances.load()
        self.assertEqual(f["income"][0]["tax"], "after")
        self.assertFalse(f["tax"]["applied"])

    def test_payslips_for_salary_started_this_tax_year(self):
        self.write({"income": [{"name": "Salary", "amount": 5000, "currency": "GBP",
                                "started": "2026-10"}]})
        with mock.patch.object(paye, "date") as d:
            d.today.return_value = date(2026, 11, 20)
            d.side_effect = lambda *a, **k: date(*a, **k)
            row = finances.load()["income"][0]
        self.assertAlmostEqual(row["payslips"][0]["net"], 4_732.45, places=2)

    def test_assets_count_and_show_change(self):
        finances.upsert("assets", "Watch", {"value": 4000, "currency": "GBP", "category": "watch",
                                            "paid": 5000, "bought": "2026-12-01"})
        f = finances.upsert("assets", "watch", {"value": 4200, "currency": "GBP", "category": "watch",
                                                "paid": 5000, "bought": "2026-12-01"})
        self.assertEqual(len(f["assets"]), 1)
        a = f["assets"][0]
        self.assertAlmostEqual(a["value_eur"], round(4200 / 0.86, 2), places=2)
        self.assertAlmostEqual(a["change_eur"], round(4200 / 0.86, 2) - round(5000 / 0.86, 2), places=2)
        self.assertAlmostEqual(f["totals"]["assets_eur"], a["value_eur"])
        f = finances.delete("assets", "Watch")
        self.assertEqual(f["assets"], [])
        self.assertEqual(f["totals"]["assets_eur"], 0)

    def test_unknown_category_becomes_other(self):
        f = finances.upsert("assets", "Bike", {"value": 300, "currency": "GBP", "category": "boat"})
        self.assertEqual(f["assets"][0]["category"], "other")


if __name__ == "__main__":
    unittest.main()
