"""Price lookup for any share (backend/lookup.py; /api/quote, /api/search?kinds=all).

No network: Yahoo is a fake (in the spirit of test_cache.fake_yf, with the
chart metadata, company info, fast_info and symbol search the lookup reads),
exchange rates are fixed and "now" is pinned to noon today. Every name,
ticker and number here is made up.
"""

import sys
import time
import types
import unittest
from datetime import datetime, timedelta
from datetime import time as dtime
from unittest import mock

import pandas as pd

from backend import cache, clock, closes, fx, lookup, watch
from tests.test_cache import wait_for

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

RATES = {"EUR": 1.0, "USD": 0.9, "GBP": 1.2, "CHF": 1.05}   # EUR per unit


def noon() -> datetime:
    return datetime.combine(clock.today(), dtime(12, 0), tzinfo=clock.tz())


def inst(price, prev, ccy="USD", kind="EQUITY", traded=0, period=(8, 0, 16, 30), period_day=None,
         name="Example Holdings plc", exchange="LSE", day=None, year=None, hint=2, closes_=None,
         days=None, intraday=None, at=None, prepost=None):
    """One instrument for the fake. `traded`: days since its last trade (0 =
    today at 11:55). `period`: the regular session (start h, m, end h, m) on
    `period_day` (default today), or None when Yahoo does not say. `at`:
    (h, m) of the regular price instead. `prepost`: [(h, m, price)] trades
    today outside regular hours (a US-style market); None = Yahoo has none."""
    return dict(price=price, prev=prev, ccy=ccy, kind=kind, traded=traded, period=period,
                period_day=period_day, name=name, exchange=exchange, day=day, year=year, hint=hint,
                closes=closes_, days=days, intraday=intraday, at=at, prepost=prepost)


def fake_yahoo(spec, *, info=None, search=None, shares=None, delay=0.0, info_delay=0.0):
    """(module, calls). spec: {symbol: inst(...), None (Yahoo answers with
    nothing) or an Exception}. info: {symbol: dict}; any other symbol's
    company info is refused, as on a hosted server. search: a list of
    Yahoo quote dicts, a function of the query, or an Exception."""
    calls: list = []
    now = noon()
    tz = clock.tz()
    mod = types.ModuleType("yfinance")

    class Ticker:
        def __init__(self, sym):
            self.sym = sym
            self._meta = None

        @property
        def history_metadata(self):
            if self._meta is None:
                raise TypeError("no metadata")        # yfinance raises after a failed request
            return self._meta

        def history(self, period="1mo", interval="1d", prepost=False, **kw):
            calls.append(("history", self.sym, period, interval) + (("prepost",) if prepost else ()))
            time.sleep(delay)
            s = spec.get(self.sym)
            if isinstance(s, Exception):
                raise s
            if s is None:
                return pd.DataFrame({"Close": []})
            last_day = now.date() - timedelta(days=s["traded"])
            when = now - timedelta(minutes=5) if s["traded"] == 0 else datetime.combine(last_day, dtime(21, 0), tz)
            if s["at"]:
                when = datetime.combine(last_day, dtime(*s["at"]), tz)
            meta = {"currency": s["ccy"], "instrumentType": s["kind"], "regularMarketPrice": s["price"],
                    "chartPreviousClose": s["prev"], "regularMarketTime": pd.Timestamp(when),
                    "longName": s["name"], "fullExchangeName": s["exchange"], "priceHint": s["hint"]}
            if s["period"]:
                h1, m1, h2, m2 = s["period"]
                pday = s["period_day"] or now.date()
                meta["currentTradingPeriod"] = {"regular": {
                    "start": pd.Timestamp(datetime.combine(pday, dtime(h1, m1), tz)),
                    "end": pd.Timestamp(datetime.combine(pday, dtime(h2, m2), tz))}}
            if s["prepost"] is not None:
                meta["hasPrePostMarketData"] = True
            if s["day"]:
                meta["regularMarketDayLow"], meta["regularMarketDayHigh"] = s["day"]
            if s["year"]:
                meta["fiftyTwoWeekLow"], meta["fiftyTwoWeekHigh"] = s["year"]
            self._meta = meta
            if interval.endswith("m"):
                vals = s["intraday"] or [s["prev"], s["price"]]
                start = datetime.combine(last_day, dtime(9, 0), tz)
                index = [start + timedelta(minutes=30 * i) for i in range(len(vals))]
                if prepost and s["prepost"]:
                    extra = sorted((datetime.combine(now.date(), dtime(h, m), tz), v) for h, m, v in s["prepost"])
                    both = sorted(list(zip(index, vals)) + extra)
                    index, vals = [b[0] for b in both], [b[1] for b in both]
            elif period == "1d":
                vals, index = [s["price"]], [datetime.combine(last_day, dtime(0, 0), tz)]
            else:
                vals = s["closes"] or [s["prev"], s["price"]]
                step = timedelta(weeks=1) if interval == "1wk" else timedelta(days=1)
                ds = s["days"] or [last_day - step * i for i in range(len(vals))][::-1]
                index = [datetime.combine(d, dtime(0, 0), tz) for d in ds]
            return pd.DataFrame({"Close": vals}, index=pd.DatetimeIndex(index))

        def get_info(self):
            calls.append(("info", self.sym))
            time.sleep(info_delay)
            if not info or self.sym not in info:
                raise RuntimeError("401 Unauthorized")
            return dict(info[self.sym])

        @property
        def fast_info(self):
            calls.append(("fast_info", self.sym))
            if shares and self.sym in shares:
                return {"shares": shares[self.sym]}
            raise RuntimeError("no share count")

    class Search:
        def __init__(self, query, max_results=8, news_count=8, timeout=30, **kw):
            calls.append(("search", query))
            if isinstance(search, Exception):
                raise search
            self.quotes = list(search(query) if callable(search) else (search or []))

    mod.Ticker, mod.Search = Ticker, Search
    return mod, calls


def hit(symbol, kind="EQUITY", name=None, exch="London"):
    return {"symbol": symbol, "quoteType": kind, "longname": name or f"{symbol} Example", "exchDisp": exch,
            "exchange": "LSE"}


class LookupCase(unittest.TestCase):
    """Fixed FX, now = noon today, clean caches; `use(...)` installs a fake Yahoo."""

    def setUp(self):
        for p in (mock.patch.object(fx, "_live_rates", return_value=dict(RATES)),
                  mock.patch.object(lookup, "_now", side_effect=noon)):
            p.start()
            self.addCleanup(p.stop)
        self.clean()
        self.addCleanup(self.clean)

    def clean(self):
        wait_for(lambda: not lookup._jobs, timeout=5)    # facts still being fetched by an earlier test
        cache.clear()
        lookup.reset()
        watch._search_cache.clear()

    def use(self, spec, **kw):
        mod, calls = fake_yahoo(spec, **kw)
        p = mock.patch.dict(sys.modules, {"yfinance": mod})
        p.start()
        self.addCleanup(p.stop)
        return calls


class QuoteTest(LookupCase):
    def test_london_share_in_pence_is_shown_in_pounds_once(self):
        self.use({"EXPL.L": inst(547.0, 533.0, ccy="GBp", day=(533.8, 547.0), year=(391.4, 612.4),
                                 closes_=[520.0, 530.0, 547.0], intraday=[536.0, 540.0, 545.0])},
                 info={"EXPL.L": {"quoteType": "EQUITY", "marketCap": 3_900_000_000, "trailingPE": 18.86,
                                  "dividendYield": 2.02}})
        q = lookup.quote("expl.l")
        self.assertEqual((q["ticker"], q["currency"], q["kind"]), ("EXPL.L", "GBP", "share"))
        self.assertEqual(q["price"], 5.47)
        self.assertEqual((q["change"], q["change_pct"], q["previous_close"]), (0.14, 2.63, 5.33))
        self.assertEqual(q["facts"]["day_range"], [5.338, 5.47])
        self.assertEqual(q["facts"]["year_range"], [3.914, 6.124])
        self.assertEqual(q["decimals"], 4)                       # 547.4p = £5.474
        self.assertAlmostEqual(q["price_eur"], 5.47 * 1.2, places=4)
        self.assertEqual(q["eur_per_unit"], 1.2)
        if q["facts_pending"]:                                   # company facts come a moment later
            self.assertTrue(wait_for(lambda: not lookup.quote("EXPL.L")["facts_pending"]))
            q = lookup.quote("EXPL.L")
        self.assertEqual(q["facts"]["market_cap"], 3_900_000_000)   # Yahoo's is in pounds already
        self.assertEqual((q["facts"]["pe"], q["facts"]["dividend_yield_pct"]), (18.86, 2.02))

        month = lookup.chart("EXPL.L", "1mo")
        self.assertEqual([p[1] for p in month["points"]], [5.2, 5.3, 5.47])
        self.assertEqual((month["currency"], month["change"], month["change_pct"]), ("GBP", 0.27, 5.19))
        day = lookup.chart("EXPL.L", "1d")
        self.assertEqual([p[1] for p in day["points"]], [5.36, 5.4, 5.45, 5.47])   # ends at the quoted price
        self.assertEqual((day["base"], day["change"]), (5.33, 0.14))              # same as today's change

    def test_market_value_from_the_share_count_when_company_info_is_refused(self):
        self.use({"EXPL.L": inst(500.0, 490.0, ccy="GBp")}, shares={"EXPL.L": 1_000_000})
        self.assertTrue(wait_for(lambda: "market_cap" in lookup.quote("EXPL.L")["facts"]))
        self.assertEqual(lookup.quote("EXPL.L")["facts"]["market_cap"], 5_000_000)   # 1m x £5.00, not x 500p

    def test_market_that_has_not_traded_today(self):
        self.use({"EXMP": inst(149.3, 155.38, traded=1, period=(14, 30, 21, 0),
                               period_day=clock.today() - timedelta(days=1), exchange="NasdaqGS")})
        q = lookup.quote("EXMP")
        self.assertIs(q["traded_today"], False)
        self.assertIsNone(q["change"])                           # nothing changed today
        self.assertIsNone(q["previous_close"])
        self.assertEqual(q["last_session"], {"day": (clock.today() - timedelta(days=1)).isoformat(),
                                             "change": -6.08, "change_pct": -3.91})
        self.assertEqual(q["price_time_text"], "at yesterday's close")
        self.assertEqual((q["market_open"], q["market_note"]), (False, "Market closed today."))

    def test_older_close_and_a_market_that_opens_later_today(self):
        self.use({"OLD": inst(10.0, 9.0, traded=3, period=None),
                  "LATER": inst(20.0, 19.0, traded=1, period=(14, 30, 21, 0))})
        old = lookup.quote("OLD")
        day = clock.today() - timedelta(days=3)
        self.assertEqual(old["price_time_text"], f"at {day:%A}'s close")
        self.assertEqual((old["market_open"], old["market_note"]), (None, None))   # Yahoo did not say
        later = lookup.quote("LATER")
        self.assertEqual((later["market_open"], later["market_note"]), (False, "Market closed. It opens at 14:30."))

    def test_open_market_and_crypto(self):
        self.use({"OPEN.L": inst(100.0, 99.0, ccy="GBP"),
                  "COIN-EUR": inst(50000.0, 49000.0, ccy="EUR", kind="CRYPTOCURRENCY", period=None)})
        q = lookup.quote("OPEN.L")
        self.assertEqual((q["market_open"], q["market_note"], q["price_time_text"]), (True, None, "at 11:55"))
        c = lookup.quote("COIN-EUR")
        self.assertEqual((c["kind"], c["market_open"], c["eur_per_unit"]), ("crypto", True, 1.0))

    def test_after_hours_price_once_the_market_has_closed(self):
        # regular session 06:00-11:00 closed at 100.00; trades after hours at 11:30 and 11:58
        calls = self.use({"EXAH": inst(100.0, 98.0, period=(6, 0, 11, 0), at=(11, 0), exchange="NasdaqGS",
                                       intraday=[99.0, 100.0], prepost=[(11, 30, 101.0), (11, 58, 101.5)])})
        q = lookup.quote("EXAH")
        self.assertEqual((q["market_open"], q["change"]), (False, 2.0))     # today's change stays the regular one
        today = clock.today()
        self.assertEqual(q["extended"], {"session": "post", "price": 101.5, "change": 1.5, "change_pct": 1.5,
                                         "time": datetime.combine(today, dtime(11, 59), clock.tz()).isoformat(timespec="minutes"),
                                         "time_text": "at 11:59"})
        self.assertIn(("history", "EXAH", "1d", "1m", "prepost"), calls)

        day = lookup.chart("EXAH", "1d")
        self.assertEqual([p[1] for p in day["points"]], [99.0, 100.0, 100.0, 101.0, 101.5])
        self.assertEqual(day["sessions"], [None, None, None, "post", "post"])
        self.assertEqual((day["change"], day["day"]), (2.0, today.isoformat()))   # the regular session's change

    def test_pre_market_price_before_the_market_opens(self):
        self.use({"EXPM": inst(149.3, 155.38, traded=1, period=(14, 30, 21, 0), exchange="NasdaqGS",
                               prepost=[(9, 0, 148.0), (11, 30, 150.793)])})
        q = lookup.quote("EXPM")
        self.assertEqual(q["market_note"], "Market closed. It opens at 14:30.")
        ext = q["extended"]
        self.assertEqual((ext["session"], ext["price"], ext["change"], ext["change_pct"], ext["time_text"]),
                         ("pre", 150.793, 1.493, 1.0, "at 11:31"))          # against the last close

    def test_no_extended_hours_while_open_or_where_the_market_has_none(self):
        calls = self.use({"EXOP": inst(100.0, 99.0, period=(8, 0, 16, 30), prepost=[(7, 0, 98.0)]),
                          "EXLN.L": inst(500.0, 490.0, ccy="GBP", period=(8, 0, 11, 0), at=(11, 0))})
        self.assertEqual((lookup.quote("EXOP")["market_open"], lookup.quote("EXOP")["extended"]), (True, None))
        self.assertEqual((lookup.quote("EXLN.L")["market_open"], lookup.quote("EXLN.L")["extended"]), (False, None))
        self.assertFalse([c for c in calls if c[0] == "history" and c[3] == "1m"])             # no extra request for either
        self.assertNotIn("sessions", lookup.chart("EXLN.L", "1d"))

    def test_ticker_yahoo_does_not_know(self):
        calls = self.use({"NOPE1": None}, search=[hit("NOPE10"), hit("XNOPE1")])
        with self.assertRaises(lookup.UnknownTicker) as ctx:
            lookup.quote("NOPE1")
        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("does not know the ticker NOPE1", str(ctx.exception))
        self.assertIn(("search", "NOPE1"), calls)

    def test_known_ticker_without_a_price(self):
        self.use({"GONE.L": None}, search=[hit("GONE.L", name="Gone Example plc")])
        q = lookup.quote("GONE.L")
        self.assertIsNone(q["price"])
        self.assertEqual((q["name"], q["kind"]), ("Gone Example plc", "share"))
        self.assertIn("no price", q["message"])
        self.assertEqual(q["facts"], {})

    def test_yahoo_down_gives_a_plain_error(self):
        self.use({"DOWN": RuntimeError("connection reset")})
        with self.assertRaises(lookup.YahooUnavailable) as ctx:
            lookup.quote("DOWN")
        self.assertEqual(ctx.exception.status, 503)
        self.assertIn("not answering", str(ctx.exception))
        self.use({"EMPTY": None}, search=RuntimeError("timed out"))   # nothing from the chart, search down too
        with self.assertRaises(lookup.YahooUnavailable):
            lookup.quote("EMPTY")

    def test_slow_yahoo_never_hangs_the_page(self):
        self.use({"SLOW": inst(1.0, 1.0)}, delay=1.0)
        with mock.patch.object(lookup, "_WAIT", 0.2):
            t = time.perf_counter()
            with self.assertRaises(lookup.YahooUnavailable) as ctx:
                lookup.quote("SLOW")
            self.assertLess(time.perf_counter() - t, 0.8)
        self.assertIn("too long", str(ctx.exception))

    def test_company_info_failing_while_the_chart_works(self):
        calls = self.use({f"T{i}": inst(10.0 + i, 10.0) for i in range(5)})
        q = lookup.quote("T0")
        self.assertEqual(q["price"], 10.0)
        self.assertTrue(wait_for(lambda: not lookup._jobs))
        q = lookup.quote("T0")
        self.assertFalse(q["facts_pending"])
        self.assertEqual(set(q["facts"]), set())                 # no ranges in this fake; never a 0
        for t in ("T1", "T2", "T3", "T4"):
            lookup.quote(t)
            self.assertTrue(wait_for(lambda: not lookup._jobs))
        # three refusals in a row: not asked again for an hour (a host Yahoo turns away)
        self.assertEqual(sum(1 for c in calls if c[0] == "info"), 3)

    def test_slow_company_info_does_not_delay_the_price(self):
        self.use({"EXMP": inst(150.0, 148.0)}, info={"EXMP": {"quoteType": "EQUITY", "marketCap": 9e11}},
                 info_delay=1.0)
        t = time.perf_counter()
        q = lookup.quote("EXMP")
        self.assertLess(time.perf_counter() - t, 0.8)
        self.assertEqual(q["price"], 150.0)
        self.assertIs(q["facts_pending"], True)
        self.assertTrue(wait_for(lambda: "market_cap" in lookup.quote("EXMP")["facts"], timeout=4))

    def test_repeated_lookup_does_not_ask_yahoo_again(self):
        calls = self.use({"EXMP": inst(150.0, 148.0)})
        lookup.quote("EXMP")
        lookup.quote("exmp")
        lookup.chart("EXMP", "1y")
        lookup.chart("EXMP", "1y")
        self.assertEqual([c for c in calls if c[0] == "history"],
                         [("history", "EXMP", "1d", "1d"), ("history", "EXMP", "1y", "1d")])

    def test_bad_input_is_refused_before_asking_yahoo(self):
        calls = self.use({})
        for bad in ("", "  ", "A B", "X" * 30, "../etc", "<b>", "AAPL;rm", "^", ".L"):
            with self.subTest(bad=bad), self.assertRaises(lookup.BadInput) as ctx:
                lookup.quote(bad)
            self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(lookup.BadInput):
            lookup.chart("EXMP", "2w")
        with self.assertRaises(lookup.BadInput):
            lookup.search("x" * 65)
        self.assertEqual(calls, [])

    def test_what_makes_no_sense_for_a_kind_is_left_out(self):
        rich = {"quoteType": "X", "marketCap": 5e10, "trailingPE": 20.0, "dividendYield": 1.5,
                "netExpenseRatio": 0.0945}
        self.use({"^EXIDX": inst(7000.0, 6990.0, kind="INDEX", day=(6950.0, 7010.0)),
                  "EURXYZ=X": inst(1.1, 1.09, kind="CURRENCY", hint=4),
                  "EXETF": inst(500.0, 499.0, kind="ETF"),
                  "EQQQ.L": inst(400.0, 398.0, ccy="GBP", kind="ETF"),
                  "COIN-USD": inst(60000.0, 59000.0, kind="CRYPTOCURRENCY", period=None)},
                 info={t: rich for t in ("^EXIDX", "EURXYZ=X", "EXETF", "COIN-USD")})

        def facts(t):
            lookup.quote(t)
            self.assertTrue(wait_for(lambda: not lookup._jobs))
            return lookup.quote(t)

        idx = facts("^EXIDX")
        self.assertEqual(set(idx["facts"]), {"day_range"})       # no market value, P/E or yield
        self.assertIsNone(idx["eur_per_unit"])                   # points, not money
        ccy = facts("EURXYZ=X")
        self.assertEqual((ccy["kind"], ccy["decimals"], ccy["eur_per_unit"], ccy["facts"]), ("currency", 4, None, {}))
        etf = facts("EXETF")
        self.assertEqual(etf["facts"], {"pe": 20.0, "dividend_yield_pct": 1.5, "yearly_cost_pct": 0.0945})
        listed = facts("EQQQ.L")                                 # refused info: the checked fund table
        self.assertEqual(listed["facts"], {"yearly_cost_pct": 0.3})
        coin = facts("COIN-USD")
        self.assertEqual(coin["facts"], {"market_cap": 50_000_000_000})

    def test_daily_holes_are_filled_and_scaled_once(self):
        end = clock.today()
        days = [end - timedelta(days=n) for n in (10, 9, 3, 2, 1, 0)]          # five days missing
        self.use({"HOLE.L": inst(600.0, 598.0, ccy="GBp", closes_=[590.0, 591.0, 595.0, 596.0, 598.0, 600.0],
                                 days=days)})
        filled = lambda t, wanted, today=None: {d: 593.0 for d in wanted}       # raw Yahoo: pence
        with mock.patch.object(closes, "hourly_closes", side_effect=filled):
            c = lookup.chart("HOLE.L", "6mo")
        values = [p[1] for p in c["points"]]
        weekdays_missing = sum(1 for n in range(4, 9) if (end - timedelta(days=n)).weekday() < 5)
        self.assertGreaterEqual(weekdays_missing, 3)
        self.assertEqual(len(values), 6 + weekdays_missing)
        self.assertIn(5.93, values)
        self.assertTrue(all(5 < v < 7 for v in values), values)


class SearchTest(LookupCase):
    QUOTES = [hit("EXMP", name="Example Corp", exch="NASDAQ"), hit("EXMP=F", "FUTURE"),
              hit("EXMPX", "ETF", name="Example 2x ETF"), hit("^EXMP", "INDEX"),
              hit("EXMP-USD", "CRYPTOCURRENCY"), hit("EXMP.L", "MUTUALFUND"), hit("EXMP", name="duplicate")]

    def test_every_kind_but_futures(self):
        self.use({}, search=self.QUOTES)
        out = lookup.search("example")
        self.assertEqual([(r["ticker"], r["kind"]) for r in out],
                         [("EXMP", "share"), ("EXMPX", "etf"), ("^EXMP", "index"), ("EXMP-USD", "crypto"),
                          ("EXMP.L", "fund")])
        self.assertEqual(out[0], {"ticker": "EXMP", "name": "Example Corp", "exchange": "NASDAQ", "kind": "share"})

    def test_watchlist_search_is_unchanged(self):
        self.use({}, search=self.QUOTES)
        self.assertEqual([r["ticker"] for r in watch.search("example")], ["EXMP", "EXMPX", "EXMP"])
        watch._search_cache.clear()
        self.use({}, search=RuntimeError("down"))
        self.assertEqual(watch.search("example"), [])            # as before: empty, no error
        with self.assertRaises(lookup.YahooUnavailable):
            lookup.search("example")                             # the lookup says so instead

    def test_isin_finds_your_own_listing_first(self):
        isin = "GB00B0000001"
        self.use({}, search=[hit("EXPLD.L", "ETF")])
        mine = types.SimpleNamespace(ticker="EXPL.L", isin=isin, name="Example World Fund")
        with mock.patch("backend.sources.holdings", return_value=[mine]):
            out = lookup.search(isin.lower())
        self.assertEqual([(r["ticker"], r["kind"], r["isin"]) for r in out],
                         [("EXPL.L", "etf", isin), ("EXPLD.L", "etf", isin)])
        self.assertIs(out[0]["held"], True)

    def test_your_listing_by_ticker_and_held_marks(self):
        mine = [types.SimpleNamespace(ticker="EXPL.L", isin="GB00B0000001", name="Example World Fund"),
                types.SimpleNamespace(ticker="EXCO", isin=None, name="Example Corp")]
        self.use({}, search=[hit("EXPL.AS", "ETF", exch="Amsterdam"), hit("EXCO", name="Example Corp")])
        with mock.patch("backend.sources.holdings", return_value=mine):
            out = lookup.search("expl")
        self.assertEqual([(r["ticker"], r["kind"], r.get("held")) for r in out],
                         [("EXPL.L", "etf", True), ("EXPL.AS", "etf", None), ("EXCO", "share", True)])
        self.assertEqual(out[0]["isin"], "GB00B0000001")         # so the page can match it by ISIN too
        watch._search_cache.clear()
        self.use({}, search=[hit("EXPL.AS", "ETF"), hit("EXPL.SW", "ETF"), hit("EXPL.L", "ETF")])
        with mock.patch("backend.sources.holdings", return_value=mine):
            out = lookup.search("EXPL")
        self.assertEqual([r["ticker"] for r in out], ["EXPL.L", "EXPL.AS", "EXPL.SW"])   # Yahoo had it third
        self.assertIs(out[0]["held"], True)


@unittest.skipIf(TestClient is None, "starlette's test client is not available")
class ApiTest(LookupCase):
    """The endpoints: JSON answers, plain errors, never a stack trace."""

    @classmethod
    def setUpClass(cls):
        from tests.test_mcp import sample_world
        sample_world(cls)
        from backend import main
        cls.client = TestClient(main.app, base_url="http://localhost:8000", raise_server_exceptions=False)

    def test_quote_chart_and_search(self):
        self.use({"EXPL.L": inst(547.0, 533.0, ccy="GBp")}, search=[hit("EXPL.L")])
        r = self.client.get("/api/quote/EXPL.L")
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["price"], r.json()["currency"]), (5.47, "GBP"))
        r = self.client.get("/api/quote/EXPL.L/chart", params={"range": "5y"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["range"], "5y")
        r = self.client.get("/api/search", params={"q": "example", "kinds": "all"})
        self.assertEqual(r.json()[0]["kind"], "share")
        r = self.client.get("/api/ticker_history/EXPL.L", params={"range": "6mo"})   # the new ranges work there too
        self.assertEqual(r.status_code, 200)

    def test_errors_are_plain_json(self):
        self.use({"NOPE1": None, "DOWN": RuntimeError("reset")}, search=[])
        for path, status in (("/api/quote/NOPE1", 404), ("/api/quote/DOWN", 503), ("/api/quote/%3Cb%3E", 400),
                             ("/api/quote/EXMP/chart?range=2w", 400)):
            with self.subTest(path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, status)
                self.assertIsInstance(r.json()["detail"], str)
                self.assertNotIn("Traceback", r.text)
        self.use({}, search=RuntimeError("down"))
        watch._search_cache.clear()
        r = self.client.get("/api/search", params={"q": "example", "kinds": "all"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("not answering", r.json()["detail"])
        self.assertEqual(self.client.get("/api/search", params={"q": "example"}).json(), [])


if __name__ == "__main__":
    unittest.main()
