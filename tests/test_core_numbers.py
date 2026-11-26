"""The core portfolio numbers, checked the way an accountant would: average
cost and realized profit (backend/positions.py), holding rows, merging and
totals (backend/main.py), today's and the calendar-period changes
(main._move_eur, backend/periods.py), currency conversion (backend/fx.py) and
quotes (backend/prices.py).

Every expected number is worked out by hand in the comment next to it.
Made-up instruments and amounts only. No network: Yahoo is faked and the FX
rates are fixed (EUR per unit: USD 0.8, GBP 1.25).
"""

import math
import sys
import tempfile
import types
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from backend import cache, fx, periods, prices
from backend.connectors.base import Holding
from backend.positions import Trade, compute_positions, realized_events

RATES = {"EUR": 1.0, "USD": 0.8, "GBP": 1.25, "CHF": 1.0}


def T(when, side, qty, amount, key="X"):
    return Trade(when=when, key=key, name=key, side=side, quantity=qty, amount=amount)


# ------------------------------------------------------------ average cost
class AverageCostTest(unittest.TestCase):
    def test_partial_sell_full_exit_and_reentry(self):
        # buy 10 for 1000 (avg 100); sell 4 for 480: 480 - 4 x 100 = +80;
        # sell 6 for 540: 540 - 6 x 100 = -60. Realized 80 - 60 = +20, flat.
        # Buy 2 for 300: a new round at avg 150, realized unchanged.
        trades = [T("2024-01-02 10:00", "BUY", 10, 1000.0), T("2024-02-01 10:00", "SELL", 4, 480.0),
                  T("2024-03-01 10:00", "SELL", 6, 540.0), T("2024-04-01 10:00", "BUY", 2, 300.0)]
        p = compute_positions(trades)["X"]
        self.assertAlmostEqual(p.realized, 20.0)
        self.assertAlmostEqual(p.quantity, 2.0)
        self.assertAlmostEqual(p.cost, 300.0)
        self.assertAlmostEqual(p.avg_cost, 150.0)
        self.assertEqual([round(e["realized"], 2) for e in realized_events(trades)], [80.0, -60.0])

    def test_fractional_exit_leaves_dust_that_counts_as_closed(self):
        # 0.1 + 0.2 shares in floats is 0.30000000000000004; selling 0.3
        # leaves 4e-17 shares: closed, with all of the cost (10 + 20) matched.
        trades = [T("2024-01-02", "BUY", 0.1, 10.0), T("2024-01-03", "BUY", 0.2, 20.0),
                  T("2024-01-04", "SELL", 0.3, 36.0)]
        p = compute_positions(trades)["X"]
        self.assertTrue(p.is_closed)
        self.assertFalse(p.is_open)
        self.assertAlmostEqual(p.realized, 6.0)   # 36 - 30

    def test_sale_listed_before_its_buy_in_the_same_minute(self):
        # A DeGiro statement is newest first, so a buy and a sell in the same
        # minute arrive sell-first. No shares are left; cash went 1000 out
        # and 1500 in, so realized must be 1500 - 1000 = +500 and quantity 0.
        # (Taken as listed, the sale met an empty position and was dropped,
        # then the buy left 0 shares carrying a cost of 1000: realized 0.)
        trades = [T("2024-06-03 10:00", "SELL", 10, 1500.0), T("2024-06-03 10:00", "BUY", 10, 1000.0)]
        p = compute_positions(trades)["X"]
        self.assertAlmostEqual(p.realized, 500.0)
        self.assertAlmostEqual(p.quantity, 0.0)
        self.assertAlmostEqual(p.cost, 0.0)
        self.assertEqual([round(e["realized"], 2) for e in realized_events(trades)], [500.0])

    def test_iso_and_spaced_times_sort_by_time(self):
        # The UK tax pool mixes accounts: Trading 212 writes "2024-03-05T09:00:00Z",
        # DeGiro "2024-03-05 15:00". The 09:00 buy comes before the 15:00 sale:
        # pool 10 for 1000 + 10 for 2000 = 20 for 3000 (avg 150); sell 10 for
        # 1800: 1800 - 10 x 150 = +300. (Comparing the raw text puts " " before
        # "T", so the 15:00 sale went first: 1800 - 10 x 100 = +800.)
        trades = [T("2024-03-01 10:00", "BUY", 10, 1000.0),
                  T("2024-03-05T09:00:00Z", "BUY", 10, 2000.0),
                  T("2024-03-05 15:00", "SELL", 10, 1800.0)]
        self.assertEqual([round(e["realized"], 2) for e in realized_events(trades)], [300.0])
        self.assertAlmostEqual(compute_positions(trades)["X"].realized, 300.0)

    def test_sale_larger_than_holding_matches_only_held_shares(self):
        # Hold 10 bought for 100; sell 12 for 144 (12.00 each). Only 10 shares
        # have a known cost; their proceeds are 144 x 10/12 = 120, so realized
        # = 120 - 100 = +20. The other 2 have no recorded purchase, so no
        # profit can be measured on them. Check: the same sale booked as two
        # orders (10 for 120, then 2 for 24 from an empty position) gives
        # 20 + 0 = 20. (Before: 144 - 100 = +44 in one order, +20 in two.)
        one = [T("2024-01-02", "BUY", 10, 100.0), T("2024-02-01", "SELL", 12, 144.0)]
        two = [T("2024-01-02", "BUY", 10, 100.0), T("2024-02-01", "SELL", 10, 120.0),
               T("2024-02-02", "SELL", 2, 24.0)]
        self.assertAlmostEqual(compute_positions(one)["X"].realized, 20.0)
        self.assertAlmostEqual(compute_positions(two)["X"].realized, 20.0)
        ev = realized_events(one)[0]
        self.assertAlmostEqual(ev["quantity"], 10.0)
        self.assertAlmostEqual(ev["proceeds"], 120.0)   # the proceeds of those 10 shares
        self.assertAlmostEqual(ev["realized"], 20.0)

    def test_sale_of_twice_the_known_shares(self):
        # (The tax-audit case.) Buy 5 for 500, sell 10 for 1200: the 5
        # known shares fetch 5/10 x 1200 = 600, cost 500, realized +100
        # (before: 1200 - 500 = +700). The 5 others are flagged, not priced.
        trades = [T("2024-01-02", "BUY", 5, 500.0), T("2024-02-01", "SELL", 10, 1200.0)]
        p = compute_positions(trades)["X"]
        self.assertAlmostEqual(p.realized, 100.0)
        self.assertAlmostEqual(p.unmatched, 5.0)
        ev = realized_events(trades)[0]
        self.assertEqual((round(ev["realized"], 2), ev["quantity"], ev["unmatched"]), (100.0, 5.0, 5.0))
        # rounding of fractional shares is not a gap in the history
        drift = [T("2024-01-02", "BUY", 0.1, 10.0), T("2024-01-03", "BUY", 0.2, 20.0),
                 T("2024-01-04", "SELL", 0.3000001, 36.0)]
        self.assertEqual(compute_positions(drift)["X"].unmatched, 0.0)

    def test_realized_summary_lists_the_incomplete_history(self):
        # Per account: 'Acct A' sold 4 shares it has no purchase for.
        from backend import realized, sources
        trades = [T("2024-01-02", "SELL", 4, 40.0), T("2024-02-01", "BUY", 10, 1000.0),
                  T("2024-03-01", "SELL", 5, 600.0)]
        cache.clear()
        self.addCleanup(cache.clear)
        with mock.patch.object(sources, "trade_sources",
                               return_value={"Acct A": (False, lambda: trades)}):
            out = realized.summary()
        self.assertEqual(out["total_realized_eur"], 100.0)   # 600 - 5 x 100
        self.assertEqual(out["incomplete"],
                         [{"broker": "Acct A", "name": "X", "shares_without_purchase": 4.0}])

    def test_unmatched_sale_does_not_distort_a_later_round(self):
        # A statement that starts after an earlier purchase: its first sale
        # (4 for 40) has no buy in the file. Then buy 10 for 1000 (avg 100)
        # and sell 5 for 600: 600 - 5 x 100 = +100, and 5 shares remain at a
        # cost of 500 (the broker holds 5). Carrying -4 shares forward gave
        # 6 shares at avg 166.67 after the buy, realized 600 - 5 x 166.67 =
        # -233.33 and 1 share left.
        trades = [T("2024-01-02", "SELL", 4, 40.0), T("2024-02-01", "BUY", 10, 1000.0),
                  T("2024-03-01", "SELL", 5, 600.0)]
        p = compute_positions(trades)["X"]
        self.assertAlmostEqual(p.realized, 100.0)
        self.assertAlmostEqual(p.quantity, 5.0)
        self.assertAlmostEqual(p.cost, 500.0)
        self.assertEqual([round(e["realized"], 2) for e in realized_events(trades)], [100.0])


# ------------------------------------------------------------ holding rows
class _MainCase(unittest.TestCase):
    def setUp(self):
        from backend import main
        self.main = main
        cache.clear()
        self.addCleanup(cache.clear)
        p = mock.patch.object(fx, "_live_rates", return_value=dict(RATES))
        p.start()
        self.addCleanup(p.stop)

    def patch_world(self, holdings, quotes):
        """sources.holdings() and fetch_quotes() return these."""
        for p in (mock.patch.object(self.main.sources, "holdings", return_value=holdings),
                  mock.patch.object(self.main, "fetch_quotes", return_value=quotes)):
            p.start()
            self.addCleanup(p.stop)
        cache.clear()

    def rows(self, holdings, quotes):
        self.patch_world(holdings, quotes)
        return self.main._open_holdings_rows()


class HoldingRowsTest(_MainCase):
    def test_unpriced_holding_is_valued_at_cost_in_its_cost_currency(self):
        # A DeGiro share quoted in dollars, bought for EUR 50.00 a share (its
        # cost is in EUR), and Yahoo has no quote: valued at cost, 10 x 50 =
        # EUR 500.00, profit 0.00, flagged as not priced live. Its price shown
        # in dollars: 50 / 0.8 = 62.50. (Reading the EUR 50 as USD 50 gave
        # 10 x 50 x 0.8 = 400.00 and a made-up -100.00 loss.)
        h = Holding("DeGiro", "XYZ", "Xyz Corp", 10, 50.0, "USD", isin="US0000000001",
                    buy_currency="EUR")
        rows, value, cost, by_broker = self.rows([h], {})
        r = rows[0]
        self.assertEqual((r["value_eur"], r["cost_eur"], r["profit_eur"]), (500.0, 500.0, 0.0))
        self.assertFalse(r["priced_live"])
        self.assertEqual(r["price"], 62.5)
        self.assertIsNone(r["daily_eur"])
        self.assertAlmostEqual(value, 500.0)
        self.assertAlmostEqual(cost, 500.0)

    def test_market_price_is_valued_in_the_quote_currency(self):
        # A Madrid share in an imported statement: price_currency() knows only
        # some exchange suffixes, so "XMPL.MC" is labelled USD, but Yahoo
        # quotes it in EUR. Value = 100 x 4.00 EUR = 400.00 (not 100 x 4 x 0.8
        # = 320.00); cost 100 x 3.00 = 300.00; profit +100.00; today
        # (4.00 - 3.90) x 100 = +10.00 EUR (not 8.00).
        from backend.instruments import price_currency
        self.assertEqual(price_currency("XMPL.MC"), "USD")   # the mislabel this guards against
        h = Holding("Trading212", "XMPL.MC", "Example SA", 100, 3.0, price_currency("XMPL.MC"),
                    isin="ES0000000001", buy_currency="EUR")
        rows, value, *_ = self.rows([h], {"XMPL.MC": {"price": 4.0, "prev_close": 3.9,
                                                      "currency": "EUR"}})
        r = rows[0]
        self.assertEqual((r["value_eur"], r["cost_eur"], r["profit_eur"]), (400.0, 300.0, 100.0))
        self.assertEqual(r["daily_eur"], 10.0)
        self.assertEqual(r["currency"], "EUR")

    def test_closed_market_shows_no_change_for_a_broker_priced_holding(self):
        # Saturday: prices.py reports the previous close equal to the last
        # price (200.00) because the market has not traded today. Trading
        # 212's own price is 201.00 (after-hours). Today's change must be
        # 0.00, not (201 - 200) x 10 x 0.8 = 8.00. The value stays the
        # broker's: 10 x 201 x 0.8 = 1608.00; cost = value - broker P/L 408 = 1200.00.
        h = Holding("T212 ISA", "XMPL", "Example Inc", 10, 150.0, "USD", isin="US0000000002",
                    price=201.0, broker_profit_eur=408.0)
        rows, *_ = self.rows([h], {"XMPL": {"price": 200.0, "prev_close": 200.0, "currency": "USD"}})
        r = rows[0]
        self.assertEqual((r["value_eur"], r["cost_eur"], r["profit_eur"]), (1608.0, 1200.0, 408.0))
        self.assertEqual(r["daily_eur"], 0.0)

    def test_open_market_broker_price_in_the_same_currency(self):
        # While the market trades the broker's live price counts:
        # (205 - 200) x 10 x 0.8 = 40.00 (unchanged rule).
        h = Holding("T212 ISA", "XMPL", "Example Inc", 10, 150.0, "USD", price=205.0,
                    broker_profit_eur=440.0)
        rows, *_ = self.rows([h], {"XMPL": {"price": 204.0, "prev_close": 200.0, "currency": "USD"}})
        self.assertEqual(rows[0]["daily_eur"], 40.0)


class PortfolioTotalsTest(_MainCase):
    def portfolio(self, holdings, quotes):
        self.patch_world(holdings, quotes)
        inc = {"dividends": 0.0, "interest": 0.0, "total": 0.0, "fees": 0.0, "by_broker": {}}
        for p in (mock.patch.object(self.main.realized, "summary",
                                    return_value={"total_realized_eur": 0.0}),
                  mock.patch.object(self.main.income, "summary", return_value=inc),
                  mock.patch.object(self.main, "_demo", return_value=False),
                  mock.patch.object(self.main.sectors, "get_sectors",
                                    side_effect=lambda ts: {t: "Tech" for t in ts})):
            p.start()
            self.addCleanup(p.stop)
        return self.main.portfolio()

    def test_merged_holding_sector_and_total_are_rounded_once(self):
        # The same share in two accounts, each 1 share at 10.004 EUR, bought
        # for 9.00, previous close 10.00. Each row: value 10.004 (shows
        # 10.00), profit 1.004 (1.00), today 0.004 (0.00). Together: value
        # 20.008 -> 20.01, profit 2.008 -> 2.01 = 11.16% of 18.00, today
        # 0.008 -> 0.01. The merged holding, the sector and the total must all
        # say 20.01. (Summing the rounded rows gave 20.00, 2.00 = 11.11% and
        # 0.00 next to a 20.01 total.)
        hs = [Holding(b, "AAA", "Aaa NV", 1, 9.0, "EUR", isin="NL0000000001") for b in ("A", "B")]
        quotes = {"AAA": {"price": 10.004, "prev_close": 10.0, "currency": "EUR"}}
        out = self.portfolio(hs, quotes)
        m = out["holdings"][0]
        self.assertEqual(out["totals"]["value"], 20.01)
        self.assertEqual((m["value_eur"], m["profit_eur"], m["profit_pct"]), (20.01, 2.01, 11.16))
        self.assertEqual(out["by_sector"], [{"sector": "Tech", "value": 20.01}])
        self.assertEqual(m["daily_eur"], 0.01)
        self.assertEqual(out["totals"]["daily"], 0.01)
        # the Performance card's "today" is the same number
        with mock.patch.object(periods, "_closes", return_value={"AAA": [(date(2026, 11, 20), 10.0)]}):
            prof = self.main.profit()
        self.assertEqual(prof["periods"]["today"], 0.01)


# ------------------------------------------------------------ periods
class PeriodsMoveTest(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for p in (mock.patch.object(fx, "_live_rates", return_value=dict(RATES)),
                  mock.patch.object(periods, "quote_scale", return_value=1.0)):
            p.start()
            self.addCleanup(p.stop)

    def test_row_without_a_market_price_has_no_period_change(self):
        # A DeGiro row whose live quote failed is valued at cost (500.00)
        # while the daily history still downloads (Friday close 60.00 USD:
        # 10 x 60 x 0.8 = 480.00). 500 - 480 = +20.00 compares cost with a
        # price; it is not a move, so the week has no figure (None).
        row = {"ticker": "XYZ", "name": "Xyz", "quantity": 10.0, "currency": "USD",
               "value_eur": 500.0, "daily_eur": None,
               "_mkt": {"price": None, "prev": None, "currency": "", "own_price": False}}
        with mock.patch.object(periods, "_closes", return_value={"XYZ": [(date(2026, 11, 20), 60.0)]}):
            out = periods.compute([row], today=date(2026, 11, 25))
        self.assertIsNone(out["periods"]["week"])

    def test_monday_week_equals_today_for_a_broker_priced_holding(self):
        # Monday before the open: Yahoo's last price is Friday's close 200.00
        # (= its previous close), the broker shows 201.00. Today = 0.00, and
        # this week, which began at midnight, has had no session either:
        # 0.00, not (201 - 200) x 10 x 0.8 = 8.00. This month counts the
        # market's move from the October close 180.00: (200 - 180) x 10 x 0.8
        # = 160.00 (the broker's extra 1.00 is not a session's move: 168.00 before).
        row = {"ticker": "XMPL", "name": "Example", "quantity": 10.0, "currency": "USD",
               "value_eur": 1608.0, "daily_eur": 0.0,
               "_mkt": {"price": 200.0, "prev": 200.0, "currency": "USD", "own_price": True}}
        closes = {"XMPL": [(date(2026, 10, 30), 180.0), (date(2026, 11, 20), 200.0)]}
        with mock.patch.object(periods, "_closes", return_value=closes):
            out = periods.compute([row], today=date(2026, 11, 23))
        self.assertEqual(out["periods"]["today"], 0.0)
        self.assertEqual(out["periods"]["week"], 0.0)
        self.assertEqual(out["periods"]["month"], 160.0)


# ------------------------------------------------------------ FX
def fake_fx(rates):
    mod = types.ModuleType("yfinance")

    class Ticker:
        def __init__(self, sym):
            if sym not in rates:
                raise KeyError(sym)
            self.fast_info = {"last_price": rates[sym]}

    mod.Ticker = Ticker
    return mod


class FxTest(unittest.TestCase):
    def setUp(self):
        cache.clear()
        fx._cache.clear()
        self.addCleanup(cache.clear)
        self.addCleanup(fx._cache.clear)
        if hasattr(fx, "_other"):
            fx._other.clear()
            self.addCleanup(fx._other.clear)

    def test_a_currency_outside_the_usual_four_is_converted(self):
        # EURSEK=X 11.00 means 1 EUR = 11 SEK, so SEK 110 = EUR 10.00 and
        # EUR 10 = SEK 110. (Before: no SEK rate, so 1 SEK was taken as 1 EUR.)
        yf = fake_fx({"EURUSD=X": 1.25, "EURGBP=X": 0.8, "EURCHF=X": 1.0, "EURSEK=X": 11.0})
        with mock.patch.dict(sys.modules, {"yfinance": yf}):
            self.assertAlmostEqual(fx.to_eur(110, "SEK"), 10.0)
            self.assertAlmostEqual(fx.from_eur(10, "SEK"), 110.0)

    def test_a_nan_rate_is_not_used(self):
        # Yahoo answering NaN for EURUSD=X must leave the fallback rate
        # (0.88 EUR per USD): USD 100 = EUR 88.00, not NaN.
        yf = fake_fx({"EURUSD=X": float("nan"), "EURGBP=X": 0.8, "EURCHF=X": 1.0})
        with mock.patch.dict(sys.modules, {"yfinance": yf}):
            self.assertAlmostEqual(fx.to_eur(100, "USD"), 88.0)

    def test_pence_in_lower_case(self):
        # 600 pence = GBP 6.00 = EUR 7.50 at 1.25, however the code is written.
        with mock.patch.object(fx, "_live_rates", return_value=dict(RATES)):
            for code in ("GBX", "GBp", "gbx"):
                self.assertAlmostEqual(fx.to_eur(600, code), 7.5, msg=code)


# ------------------------------------------------------------ quotes
class QuoteDayTest(unittest.TestCase):
    def setUp(self):
        for d in (prices._CACHE, prices._FAILED, prices._SCALE):
            d.clear()
        self.addCleanup(lambda: [d.clear() for d in (prices._CACHE, prices._FAILED, prices._SCALE)])

    def test_quote_from_an_earlier_day_shows_no_change_today(self):
        # Tuesday's quote (100.00, previous close 99.00) is still cached on
        # Wednesday and Yahoo fails. Its +1.00 is Tuesday's move, so today's
        # reference is the price itself (no change known today), not 99.00.
        from tests.test_cache import fake_yf
        quotes = {"AAA": (100.0, 99.0, "USD")}
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf(quotes)}):
            prices.fetch_quotes(["AAA"])
            p, pc, at, ccy, day = prices._CACHE["AAA"]
            prices._CACHE["AAA"] = (p, pc, at, ccy, date.fromordinal(day.toordinal() - 1))
            quotes["AAA"] = RuntimeError("Yahoo is down")
            out = prices.fetch_quotes(["AAA"])["AAA"]
        self.assertEqual((out["price"], out["prev_close"]), (100.0, 100.0))

    def test_nan_from_yahoo_is_no_price(self):
        # A NaN price is no quote at all; a NaN previous close is a missing one.
        from tests.test_cache import fake_yf
        quotes = {"NANP": (float("nan"), 99.0, "USD"), "NANC": (100.0, float("nan"), "USD")}
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf(quotes)}):
            out = prices.fetch_quotes(list(quotes))
        self.assertNotIn("NANP", out)
        self.assertEqual(out["NANC"]["price"], 100.0)
        self.assertIsNone(out["NANC"]["prev_close"])


# ------------------------------------------------------------ DeGiro split (not in my files)
class DegiroSplitTest(unittest.TestCase):
    def test_a_split_is_not_a_sale(self):
        # Bought 300 shares for EUR 500. A 1-for-30 reverse split books 300
        # old shares out ("SPLIT AANPASSING", cash +USD 300) and 10 new shares
        # in (cash -USD 300). Nothing was sold: realized must be 0.00 and the
        # 10 new shares must carry the EUR 500 cost (avg 50.00). The
        # connector used to feed the split as a SELL for 300 x 0.8 = EUR 240 and
        # a BUY for EUR 240: realized 240 - 500 = -260.00, new shares at 240.
        from backend.connectors import degiro
        csv = (
            "Date,Time,Value date,Product,ISIN,Description,FX,Change,,Balance,,Order Id\n"
            '05-01-2024,09:00,05-01-2024,EXAMPLE NEW,US00000000N2,SPLIT AANPASSING: 10 EXAMPLE @ 30 USD,,USD,"-300,00",USD,"0,00",\n'
            '05-01-2024,09:00,05-01-2024,EXAMPLE OLD,US00000000O1,SPLIT AANPASSING: 300 EXAMPLE @ 1 USD,,USD,"300,00",USD,"300,00",\n'
            '02-01-2024,10:00,02-01-2024,EXAMPLE OLD,US00000000O1,Koop 300 @ 2 USD,,EUR,"-500,00",EUR,"0,00",ord1\n')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "degiro_account.csv"
            path.write_text(csv)
            with mock.patch.object(degiro, "_csv_file", return_value=path), \
                    mock.patch.object(fx, "_live_rates", return_value=dict(RATES)):
                pos = compute_positions(degiro.get_trades())
        self.assertAlmostEqual(sum(p.realized for p in pos.values()), 0.0)
        self.assertAlmostEqual(pos["US00000000N2"].cost, 500.0)


    def test_a_split_under_the_same_isin_then_a_sale(self):
        # 10 shares for EUR 900, a 3-for-1 split (10 out, 30 in, same ISIN),
        # then all 30 sold for EUR 1,200: the only real result is
        # 1,200 - 900 = +300.00, and it is realized on the day of the sale.
        from backend.connectors import degiro
        csv = (
            "Date,Time,Value date,Product,ISIN,Description,FX,Change,,Balance,,Order Id\n"
            '02-09-2024,10:00,02-09-2024,EXAMPLE INC,US00000000S3,Verkoop 30 @ 40 EUR,,EUR,"1200,00",EUR,"0,00",ord2\n'
            '29-08-2024,13:29,29-08-2024,EXAMPLE INC,US00000000S3,SPLIT AANPASSING: 30 Example Inc @ 35 EUR,,EUR,"-1050,00",EUR,"0,00",\n'
            '29-08-2024,13:29,29-08-2024,EXAMPLE INC,US00000000S3,SPLIT AANPASSING: 10 Example Inc @ 105 EUR,,EUR,"1050,00",EUR,"0,00",\n'
            '02-01-2024,10:00,02-01-2024,EXAMPLE INC,US00000000S3,Koop 10 @ 90 EUR,,EUR,"-900,00",EUR,"0,00",ord1\n')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "degiro_account.csv"
            path.write_text(csv)
            with mock.patch.object(degiro, "_csv_file", return_value=path), \
                    mock.patch.object(fx, "_live_rates", return_value=dict(RATES)):
                trades = sorted(degiro.get_trades(), key=lambda t: t.when)
        before_sale = compute_positions([t for t in trades if t.when < "2024-09-01"])["US00000000S3"]
        self.assertAlmostEqual(before_sale.realized, 0.0)        # the split itself: nothing
        self.assertAlmostEqual(before_sale.quantity, 30.0)
        self.assertAlmostEqual(before_sale.cost, 900.0)           # EUR 30 a share
        after = compute_positions(trades)["US00000000S3"]
        self.assertAlmostEqual(after.realized, 300.0)
        self.assertAlmostEqual(after.quantity, 0.0)


class DegiroIncomeCurrencyTest(unittest.TestCase):
    def test_a_dividend_in_swedish_kronor(self):
        # A DeGiro dividend of SEK 110.00 at 11 SEK per EUR is EUR 10.00.
        # income._degiro_rows converts only currencies in fx.FX_TO_EUR
        # (EUR/USD/GBP/CHF) and booked any other as euros: EUR 110.00.
        from backend import income
        csv = ("Date,Time,Value date,Product,ISIN,Description,FX,Change,,Balance,,Order Id\n"
               '15-05-2024,07:00,14-05-2024,EXAMPLE AB,SE0000000001,Dividend,,SEK,"110,00",SEK,"110,00",\n')
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "degiro_account.csv"
            path.write_text(csv)
            with mock.patch.object(income, "_resolve_data", return_value=(path, True)), \
                    mock.patch.object(fx, "_live_rates", return_value={**RATES, "SEK": 1 / 11}):
                rows = income._degiro_rows()
        self.assertAlmostEqual(rows[0]["amount_eur"], 10.0)


if __name__ == "__main__":
    unittest.main()
