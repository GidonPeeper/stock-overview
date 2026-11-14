"""Tax-loss harvesting on synthetic holdings (no network)."""

import json
import os
import unittest
from datetime import date
from unittest import mock

from backend import fx, harvest, sources
from backend.positions import Trade

RATES = {"EUR": 1.0, "GBP": 1.25, "USD": 0.8}   # £1 = €1.25, $1 = €0.80
TODAY = date(2026, 11, 14)


def row(name, value, cost, broker="T212 Invest", isin="", ticker="", qty=10.0, wrapper=""):
    """A holdings row as main._open_holdings_rows() returns it (EUR)."""
    return {"broker": broker, "name": name, "isin": isin, "ticker": ticker, "quantity": qty,
            "value_eur": value, "cost_eur": cost, "profit_eur": value - cost,
            "priced_live": True, "wrapper": wrapper}


def uk(net=0.0, isa_tracked=True, isa_left=5000.0):
    return {"tax_year": "2026/27", "to": "2027-04-05", "gains_gbp": max(net, 0.0), "net_gain_gbp": net,
            "isa": {"tracked": isa_tracked, "left_gbp": isa_left, "allowance_gbp": 20000}}


class Base(unittest.TestCase):
    def setUp(self):
        # fixed FX, and none of the user's harvest settings from the environment
        for p in (mock.patch.object(fx, "_live_rates", return_value=RATES),
                  mock.patch.dict("os.environ")):
            p.start()
            self.addCleanup(p.stop)
        for k in ("TAX_RESIDENCE", "CGT_RATE", "HARVEST_MIN_LOSS_PCT", "HARVEST_MIN_LOSS",
                  "CGT_LOSSES_BROUGHT_FORWARD"):
            os.environ.pop(k, None)

    def run_uk(self, rows, **kw):
        kw.setdefault("uk", uk())
        return harvest.suggest(rows, today=TODAY, res="UK", corr=False, sector_map={}, **kw)


class ThresholdTest(Base):
    def test_defaults_ten_percent_and_250_pounds(self):
        rows = [
            row("Small %", 9100, 10000, isin="X1"),    # -9%: too small a share
            row("Small £", 250, 500, isin="X2"),       # -50% but only £200
            row("Keeps", 3000, 4000, isin="X3"),       # -25%, £800
        ]
        out = self.run_uk(rows)
        self.assertEqual([c["name"] for c in out["candidates"]], ["Keeps"])
        c = out["candidates"][0]
        self.assertEqual((c["loss"], c["loss_pct"]), (800.0, 25.0))

    def test_thresholds_can_be_lowered(self):
        rows = [row("Small %", 9100, 10000, isin="X1"), row("Small £", 250, 500, isin="X2")]
        out = self.run_uk(rows, min_pct=5, min_loss=100)
        self.assertEqual(len(out["candidates"]), 2)

    def test_env_thresholds(self):
        with mock.patch.dict("os.environ", {"HARVEST_MIN_LOSS_PCT": "5", "HARVEST_MIN_LOSS": "50"}):
            out = self.run_uk([row("Small %", 9100, 10000, isin="X1")])
        self.assertEqual(out["thresholds"], {"min_loss_pct": 5.0, "min_loss": 50.0})
        self.assertEqual(len(out["candidates"]), 1)

    def test_nothing_qualifies_message(self):
        out = self.run_uk([row("Up", 5000, 4000, isin="X1")])
        self.assertEqual(out["candidates"], [])
        self.assertIn("Nothing to harvest", out["message"])


class IsaTest(Base):
    def test_isa_rows_left_out(self):
        rows = [row("In ISA", 1000, 4000, broker="T212 ISA", isin="X1", wrapper="ISA"),
                row("ISA by name", 1000, 4000, broker="Some ISA", isin="X2"),
                row("Pension", 1000, 4000, broker="My SIPP", isin="X3"),
                row("Taxable", 1000, 4000, broker="DeGiro", isin="X4")]
        out = self.run_uk(rows)
        self.assertEqual([c["name"] for c in out["candidates"]], ["Taxable"])

    def test_pooled_cost_across_taxable_accounts(self):
        # Down 30% at one broker, up 180% at another: HMRC pools them -> a gain.
        rows = [row("Alphabet", 1400, 2000, broker="T212 Invest", isin="US02079K3059", qty=5),
                row("Alphabet", 2800, 1000, broker="DeGiro", isin="US02079K3059", qty=10)]
        out = self.run_uk(rows)
        self.assertEqual(out["candidates"], [])
        (s,) = out["skipped"]
        self.assertEqual((s["account"], s["pooled_with"]), ("T212 Invest", ["DeGiro"]))

    def test_pool_ignores_isa_copy(self):
        rows = [row("Alphabet", 1400, 2000, broker="T212 Invest", isin="US02079K3059", qty=5),
                row("Alphabet", 2800, 1000, broker="T212 ISA", isin="US02079K3059", qty=10, wrapper="ISA")]
        (c,) = self.run_uk(rows)["candidates"]
        self.assertEqual((c["account"], c["pooled_with"], c["loss_eur"]), ("T212 Invest", [], 600.0))


class ArithmeticTest(unittest.TestCase):
    def test_gains_above_exempt(self):
        v = harvest.uk_value(5000, 1000, 0.24)
        self.assertEqual((v["saved_now"], v["carried_forward"], v["wasted"]), (240.0, 0.0, 0.0))

    def test_wastes_exempt_amount(self):
        # £4,000 of gains: only £1,000 is taxable, but a £3,000 loss comes off in full.
        v = harvest.uk_value(4000, 3000, 0.24)
        self.assertEqual((v["saved_now"], v["carried_forward"], v["wasted"]), (240.0, 0.0, 2000.0))

    def test_gains_below_exempt_waste_everything(self):
        v = harvest.uk_value(2000, 1000, 0.24)
        self.assertEqual((v["saved_now"], v["carried_forward"], v["wasted"], v["tax_value"]),
                         (0.0, 0.0, 1000.0, 0.0))

    def test_net_loss_carries_forward(self):
        v = harvest.uk_value(-500, 1000, 0.24)
        self.assertEqual((v["saved_now"], v["carried_forward"], v["wasted"], v["tax_value"]),
                         (0.0, 1000.0, 0.0, 240.0))

    def test_big_loss_wastes_exempt_then_carries(self):
        v = harvest.uk_value(5000, 6000, 0.24)
        self.assertEqual((v["saved_now"], v["carried_forward"], v["wasted"]), (480.0, 1000.0, 3000.0))

    def test_brought_forward_losses_only_down_to_exempt(self):
        # £1,500 brought forward: without harvesting £1,500 is used and £500 taxed.
        # A £1,000 loss now clears the £500 and leaves £500 of old losses unused.
        v = harvest.uk_value(5000, 1000, 0.24, brought_forward=1500)
        self.assertEqual((v["taxable_before"], v["taxable_after"]), (500.0, 0.0))
        self.assertEqual((v["saved_now"], v["carried_forward"], v["wasted"]), (120.0, 500.0, 0.0))

    def test_us_offsets_gains_then_3000_of_income(self):
        v = harvest.us_value(1000, 6000, 0.15)
        self.assertEqual((v["saved_now"], v["carried_forward"]), (600.0, 2000.0))


class CardTest(Base):
    def test_headline_and_tip(self):
        out = self.run_uk([row("Keeps", 2500, 6250, isin="X3")], uk=uk(net=4000))   # £3,000 loss
        self.assertIn("You've realised £4,000 of gains this tax year; £3,000 is tax-free anyway", out["headline"])
        self.assertIn("saves about £240 this year and carries £0 forward", out["headline"])
        self.assertIn("£2,000 of these losses would only cancel gains that are already tax-free", out["tip"])
        self.assertEqual(out["claim_by"], "2031-04-05")

    def test_rebuy_date_is_day_31(self):
        self.assertEqual(harvest.rebuy_from(date(2026, 11, 14)), date(2026, 12, 15))
        (c,) = self.run_uk([row("Keeps", 3000, 4000, isin="X3")])["candidates"]
        self.assertEqual((c["rebuy_from"], c["sell_by"]), ("2026-12-15", "2027-04-05"))

    def test_ranked_by_tax_value(self):
        rows = [row("Small", 3000, 4000, isin="X1"), row("Big", 3000, 8000, isin="X2")]
        out = self.run_uk(rows, uk=uk(net=10000))
        self.assertEqual([c["name"] for c in out["candidates"]], ["Big", "Small"])

    def test_lines_share_this_years_gains_and_add_up(self):
        # £4,000 of gains; losses of £3,000 and £1,000. The bigger one uses the
        # £1,000 of taxable gain; the second only cancels tax-free gains.
        rows = [row("Small", 2500, 3750, isin="X1"), row("Big", 2500, 6250, isin="X2")]
        out = self.run_uk(rows, uk=uk(net=4000))
        big, small = out["candidates"]
        self.assertEqual((big["saved_now"], big["wasted"]), (240.0, 2000.0))
        self.assertEqual((small["saved_now"], small["wasted"]), (0.0, 1000.0))
        for k in ("saved_now", "carried_forward", "wasted"):
            self.assertAlmostEqual(big[k] + small[k], out["totals"][k])
        self.assertIn("Saves nothing this year; £1,000 of it only cancels gains", small["effect"])

    def test_bed_and_isa_first_when_allowance_left(self):
        (c,) = self.run_uk([row("Keeps", 3000, 4000, isin="X3")], uk=uk(isa_left=5000))["candidates"]
        self.assertEqual(c["options"][0]["kind"], "bed_and_isa")
        self.assertTrue(c["options"][0]["fits"])

    def test_no_bed_and_isa_when_allowance_used(self):
        (c,) = self.run_uk([row("Keeps", 3000, 4000, isin="X3")], uk=uk(isa_left=0))["candidates"]
        self.assertNotIn("bed_and_isa", [o["kind"] for o in c["options"]])

    def test_partial_bed_and_isa(self):
        (c,) = self.run_uk([row("Keeps", 3000, 4000, isin="X3")], uk=uk(isa_left=1200))["candidates"]
        self.assertFalse(c["options"][0]["fits"])
        self.assertIn("about 50%", c["options"][0]["reason"])   # £1,200 of £2,400

    def test_netherlands_explains_instead(self):
        out = harvest.suggest([row("Keeps", 3000, 4000, isin="X3")], today=TODAY, res="NL", corr=False)
        self.assertFalse(out["applies"])
        self.assertEqual(out["candidates"], [])
        self.assertIn("box 3", out["message"])


class ReplacementTest(Base):
    def test_sp500_uk_gets_other_provider_not_same_fund(self):
        (c,) = self.run_uk([row("Vanguard S&P 500", 3000, 4000, isin="IE00B3XXRP09", ticker="VUSA.L")])["candidates"]
        tickers = [o["ticker"] for o in c["options"] if o["kind"] == "similar"]
        # not VUAG.L (same fund, other share class), not IUSA.L (second iShares
        # pick); then a different index. UCITS listings only.
        self.assertEqual(tickers, ["CSP1.L", "SPXP.L", "CSUS.L"])

    def test_held_listing_never_offered(self):
        opts = harvest.replacement_options({"isin": "IE00B5BMR087", "ticker": "SXR8.DE"}, "UK")
        self.assertNotIn("CSP1.L", [o["ticker"] for o in opts])

    def test_us_resident_gets_different_index_only(self):
        opts = harvest.replacement_options({"isin": "US9229083632", "ticker": "VOO"}, "US")
        self.assertEqual([o["ticker"] for o in opts], ["VTI", "SCHX"])

    def test_stock_peer_and_sector_etf(self):
        opts = harvest.replacement_options({"isin": "NL0010273215", "ticker": "ASML.AS"}, "UK")
        self.assertEqual([o["ticker"] for o in opts], ["ASM.AS", "SEMI.L"])

    def test_unknown_stock_falls_back_to_sector(self):
        opts = harvest.replacement_options({"isin": "XX0000000000", "ticker": "ZZZ"}, "UK", "Healthcare")
        self.assertEqual(opts[0]["ticker"], "XDWH.L")
        opts = harvest.replacement_options({"isin": "", "ticker": "ZZZ"}, "UK", None)
        self.assertEqual(opts[0]["ticker"], "VWRP.L")

    def test_library_is_consistent(self):
        lib = json.loads(harvest._LIB_FILE.read_text())
        seen = set()
        for g in lib["funds"]:
            for m in g["members"]:
                for k in harvest._member_keys(m):
                    self.assertNotIn(k, seen, k)
                    seen.add(k)
                self.assertTrue(m["index"] and m["name"] and m["provider"])
        for s in lib["stocks"]:
            self.assertTrue(set(s["match"]).isdisjoint(seen), s["name"])
            seen.update(s["match"])
            self.assertIn(s.get("etf") or s["sector"], lib["sector_etfs"])
            for p in s["peers"]:
                self.assertTrue(p["ticker"] and p["reason"])
        for v in lib["sector_etfs"].values():
            self.assertTrue(v["uk"]["ticker"] and v["us"]["ticker"])


class UsTest(Base):
    def test_wash_sale_warning_and_no_bed_and_isa(self):
        trades = [Trade(when="2026-11-01", key="US0000000001", name="Co", side="BUY", quantity=1,
                        amount=100, currency="USD", ticker="CO")]
        with mock.patch.object(sources, "trade_sources", return_value={"Broker": (False, lambda: trades)}):
            out = harvest.suggest([row("Co", 3000, 4000, broker="Broker", isin="US0000000001", ticker="CO")],
                                  today=TODAY, res="US", corr=False, sector_map={})
        (c,) = out["candidates"]
        self.assertEqual(out["currency"], "USD")
        self.assertNotIn("bed_and_isa", [o["kind"] for o in c["options"]])
        self.assertIn("2026-12-02", c["warning"])
        self.assertEqual(c["loss"], 1250.0)     # €1,000 at $1 = €0.80


if __name__ == "__main__":
    unittest.main()
