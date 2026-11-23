"""Cache layer (backend/cache.py) and its users: TTL, stale-while-revalidate,
invalidation, single-flight, failure keeps the last good value; parallel
quotes with pence handling; the Trading 212 path through sources (canned API).

No network: yfinance is replaced by a fake module where needed.
"""

import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from backend import cache, datafiles, fx, prices, sources
from backend.connectors import trading212 as t212


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class Counter:
    def __init__(self, values=None, delay=0.0):
        self.calls = 0
        self.values = values
        self.delay = delay
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.calls += 1
            n = self.calls
        if self.delay:
            time.sleep(self.delay)
        v = self.values[n - 1] if self.values else n
        if isinstance(v, Exception):
            raise v
        return v


class CacheTest(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_fresh_value_is_reused_then_recomputed_after_ttl(self):
        fn = Counter()
        self.assertEqual(cache.get("k", fn, ttl=0.1), 1)
        self.assertEqual(cache.get("k", fn, ttl=0.1), 1)
        time.sleep(0.12)
        self.assertEqual(cache.get("k", fn, ttl=0.1), 2)  # no stale window: waits
        self.assertEqual(fn.calls, 2)

    def test_stale_while_revalidate(self):
        fn = Counter(delay=0.2)
        self.assertEqual(cache.get("k", fn, ttl=0.05, stale=10), 1)
        time.sleep(0.06)
        t = time.perf_counter()
        self.assertEqual(cache.get("k", fn, ttl=0.05, stale=10), 1)  # old value at once…
        self.assertLess(time.perf_counter() - t, 0.1)
        self.assertTrue(wait_for(lambda: fn.calls == 2 and cache.get("k", fn, ttl=5, stale=10) == 2))
        self.assertEqual(fn.calls, 2)  # …and exactly one background refresh

    def test_past_stale_window_waits_for_fresh_value(self):
        fn = Counter()
        cache.get("k", fn, ttl=0.02, stale=0.02)
        time.sleep(0.05)
        self.assertEqual(cache.get("k", fn, ttl=0.02, stale=0.02), 2)

    def test_invalidate_recomputes_data_but_not_market_values(self):
        data, market = Counter(), Counter()
        cache.get("d", data, ttl=60, stale=600)
        cache.get("m", market, ttl=60, stale=600, data=False)
        cache.invalidate()
        self.assertEqual(cache.get("d", data, ttl=60, stale=600), 2)  # synchronous, never stale
        self.assertEqual(cache.get("m", market, ttl=60, stale=600, data=False), 1)

    def test_failure_keeps_last_good_value(self):
        fn = Counter(values=[1, RuntimeError("down"), 3])
        self.assertEqual(cache.get("k", fn, ttl=0.01), 1)
        time.sleep(0.02)
        self.assertEqual(cache.get("k", fn, ttl=0.01), 1)  # recompute failed -> last good
        time.sleep(0.02)
        self.assertEqual(cache.get("k", fn, ttl=0.01), 3)

    def test_failure_in_background_refresh_keeps_value(self):
        fn = Counter(values=[1, RuntimeError("down")])
        cache.get("k", fn, ttl=0.01, stale=10)
        time.sleep(0.02)
        self.assertEqual(cache.get("k", fn, ttl=0.01, stale=10), 1)
        self.assertTrue(wait_for(lambda: fn.calls == 2))
        time.sleep(0.02)
        self.assertEqual(cache.peek("k")[0], True)
        self.assertEqual(cache.get("k", lambda: 99, ttl=0.01, stale=10), 1)  # still served

    def test_failure_without_previous_value_raises(self):
        with self.assertRaises(RuntimeError):
            cache.get("k", Counter(values=[RuntimeError("down")]), ttl=1)

    def test_single_flight(self):
        fn = Counter(delay=0.2)
        results = []
        threads = [threading.Thread(target=lambda: results.append(cache.get("k", fn, ttl=60)))
                   for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual((fn.calls, results), (1, [1] * 8))

    def test_copy_protects_cached_value(self):
        out = cache.get("k", lambda: {"rows": [1, 2]}, ttl=60, copy=True)
        out["rows"].append(3)
        self.assertEqual(cache.get("k", lambda: None, ttl=60, copy=True), {"rows": [1, 2]})

    def test_decorator_keys_by_arguments(self):
        calls = []

        @cache.cached(ttl=60)
        def square(x):
            calls.append(x)
            return x * x

        self.assertEqual((square(3), square(3), square(4)), (9, 9, 16))
        self.assertEqual(calls, [3, 4])

    def test_changed_data_file_changes_version(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "degiro_account.csv"
            f.write_text("a")
            with mock.patch.object(cache, "_data_files", return_value=["degiro_account.csv"]), \
                 mock.patch.object(datafiles, "resolve", return_value=(f, True)), \
                 mock.patch.object(cache, "_FP_EVERY", 0):
                v1 = cache.data_version()
                fn = Counter()
                cache.get("d", fn, ttl=60)
                os.utime(f, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
                self.assertNotEqual(cache.data_version(), v1)
                self.assertEqual(cache.get("d", fn, ttl=60), 2)


class WriteHookTest(unittest.TestCase):
    """main.py calls cache.after_request() for every request: any successful
    write under /api/ marks data caches out of date."""

    def test_writes_invalidate_reads_do_not(self):
        v0 = cache._counter
        cache.after_request("GET", "/api/portfolio", 200)
        self.assertEqual(cache._counter, v0)
        cache.after_request("POST", "/api/cash", 200)
        cache.after_request("DELETE", "/api/connections/x", 200)  # endpoints added later too
        self.assertEqual(cache._counter, v0 + 2)
        cache.after_request("POST", "/api/cash", 400)             # failed write: no change
        cache.after_request("POST", "/login", 303)                # not an API write
        self.assertEqual(cache._counter, v0 + 2)

    def test_api_reads_count_as_activity_pings_do_not(self):
        cache.last_activity = 0.0
        cache.after_request("GET", "/healthz", 200)
        self.assertEqual(cache.last_activity, 0.0)
        cache.after_request("GET", "/api/portfolio", 200)
        self.assertGreater(cache.last_activity, 0.0)


# ------------------------------------------------------------ quotes
def fake_yf(quotes, delay=0.0, calls=None, traded_today=True):
    """quotes: {symbol: (price now, last close before today, currency) or an
    Exception}. Yesterday's bar closes at the second value; when the market
    traded today there is a bar for today at the first value."""
    import pandas as pd
    from datetime import datetime, timedelta

    from backend import clock

    mod = types.ModuleType("yfinance")

    class Ticker:
        def __init__(self, sym):
            if calls is not None:
                calls.append(sym)
            self.sym = sym
            self.history_metadata = {}

        def history(self, **kw):
            time.sleep(delay)
            q = quotes[self.sym]
            if isinstance(q, Exception):
                raise q
            last, prev, ccy = q
            today = clock.today()
            days = [today - timedelta(days=1)] + ([today] if traded_today else [])
            closes = [prev] + ([last] if traded_today else [])
            index = pd.DatetimeIndex([datetime(d.year, d.month, d.day) for d in days]).tz_localize(clock.tz())
            when = datetime.now(clock.tz()) if traded_today else datetime.now(clock.tz()) - timedelta(days=1)
            self.history_metadata = {"regularMarketPrice": last, "currency": ccy,
                                     "regularMarketTime": pd.Timestamp(when)}
            if last is None:
                self.history_metadata = {}
                return pd.DataFrame({"Close": []})
            return pd.DataFrame({"Close": closes}, index=index)

    mod.Ticker = Ticker
    return mod


class QuotesTest(unittest.TestCase):
    def setUp(self):
        for d in (prices._CACHE, prices._FAILED, prices._SCALE):
            d.clear()
        self.addCleanup(lambda: [d.clear() for d in (prices._CACHE, prices._FAILED, prices._SCALE)])

    def test_parallel_and_pence(self):
        quotes = {f"T{i}": (10.0 + i, 9.0, "USD") for i in range(8)}
        quotes["RMV.L"] = (600.0, 590.0, "GBp")  # London share quoted in pence
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf(quotes, delay=0.1)}):
            t = time.perf_counter()
            out = prices.fetch_quotes(list(quotes))
            took = time.perf_counter() - t
        self.assertLess(took, 0.6)  # 9 tickers x 0.2 s each would be 1.8 s one by one
        self.assertEqual(out["RMV.L"], {"price": 6.0, "prev_close": 5.9, "currency": "GBP"})
        self.assertEqual(out["T3"]["price"], 13.0)
        self.assertEqual(prices.quote_scale("RMV.L"), 0.01)

    def test_stale_quote_served_while_refreshing(self):
        quotes = {"AAA": (100.0, 99.0, "USD")}
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf(quotes, delay=0.1)}):
            prices.fetch_quotes(["AAA"])
            p, pc, at, *rest = prices._CACHE["AAA"]
            prices._CACHE["AAA"] = (p, pc, at - prices._TTL - 1, *rest)  # a minute old
            quotes["AAA"] = (105.0, 99.0, "USD")
            t = time.perf_counter()
            self.assertEqual(prices.fetch_quotes(["AAA"])["AAA"]["price"], 100.0)
            self.assertLess(time.perf_counter() - t, 0.1)
            self.assertTrue(prices.quotes_status(["AAA"])["refreshing"])
            self.assertTrue(wait_for(lambda: prices._CACHE["AAA"][0] == 105.0))
            self.assertFalse(prices.quotes_status(["AAA"])["refreshing"])

    def test_failed_refresh_keeps_last_quote(self):
        quotes = {"AAA": (100.0, 99.0, "USD")}
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf(quotes)}):
            prices.fetch_quotes(["AAA"])
            p, pc, at, *rest = prices._CACHE["AAA"]
            prices._CACHE["AAA"] = (p, pc, at - prices._TTL - prices._STALE - 1, *rest)  # too old to show
            quotes["AAA"] = RuntimeError("rate limited")
            self.assertEqual(prices.fetch_quotes(["AAA"])["AAA"]["price"], 100.0)

    def test_unpriceable_ticker_is_not_refetched_every_request(self):
        calls = []
        quotes = {"GONE": (None, None, "USD"), "AAA": (1.0, 1.0, "USD")}
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf(quotes, calls=calls)}):
            prices.fetch_quotes(["GONE", "AAA"])
            prices.fetch_quotes(["GONE", "AAA"])
            prices.fetch_quotes(["GONE"])
        self.assertEqual(calls.count("GONE"), 1)

    def test_today_is_the_calendar_day(self):
        """A market that has not traded yet today shows no change today,
        instead of the previous session's move."""
        quotes = {"NVDA": (125.0, 146.0, "USD")}  # last session fell 14%
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf(quotes, traded_today=False)}):
            out = prices.fetch_quotes(["NVDA"])
        self.assertEqual(out["NVDA"]["price"], 125.0)
        self.assertEqual(out["NVDA"]["prev_close"], 125.0)
        for d in (prices._CACHE, prices._FAILED):
            d.clear()
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf(quotes, traded_today=True)}):
            out = prices.fetch_quotes(["NVDA"])
        self.assertEqual(out["NVDA"]["prev_close"], 146.0)

    def test_yesterdays_quote_is_fetched_again(self):
        quotes = {"AAA": (100.0, 99.0, "USD")}
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf(quotes)}):
            prices.fetch_quotes(["AAA"])
            p, pc, at, ccy, day = prices._CACHE["AAA"]
            prices._CACHE["AAA"] = (p, pc, at, ccy, day.replace(year=day.year - 1))
            quotes["AAA"] = (101.0, 100.0, "USD")
            self.assertEqual(prices.fetch_quotes(["AAA"])["AAA"]["price"], 101.0)


class FxTest(unittest.TestCase):
    def setUp(self):
        cache.clear()
        fx._cache.clear()
        self.addCleanup(cache.clear)
        self.addCleanup(fx._cache.clear)

    def test_rates_cached_and_failed_pair_keeps_last_rate(self):
        mod = types.ModuleType("yfinance")
        rates = {"EURUSD=X": 1.25, "EURGBP=X": 0.8, "EURCHF=X": 1.0}

        class Ticker:
            def __init__(self, sym):
                v = rates[sym]
                if isinstance(v, Exception):
                    raise v
                self.fast_info = {"last_price": v}

        mod.Ticker = Ticker
        with mock.patch.dict(sys.modules, {"yfinance": mod}):
            self.assertAlmostEqual(fx.to_eur(10, "USD"), 8.0)
            rates["EURUSD=X"] = RuntimeError("down")
            cache.clear()  # force a refetch
            self.assertAlmostEqual(fx.to_eur(10, "USD"), 8.0)   # last live rate, not the fallback
            self.assertAlmostEqual(fx.to_eur(600, "GBX"), 7.5)  # pence -> pounds -> euros


# ------------------------------------------- Trading 212 through sources
POSITIONS = [{"instrument": {"ticker": "AAPL_US_EQ", "name": "Apple", "isin": "US0378331005",
                             "currency": "USD"},
              "quantity": 2, "averagePricePaid": 150.0, "currentPrice": 200.0,
              "walletImpact": {"currency": "EUR", "unrealizedProfitLoss": 90.0}}]
ORDERS = {"items": [{"order": {"status": "FILLED", "side": "BUY", "ticker": "AAPL_US_EQ",
                               "instrument": {"ticker": "AAPL_US_EQ", "isin": "US0378331005",
                                              "name": "Apple"}},
                     "fill": {"filledAt": "2026-01-02T10:00:00Z", "quantity": 2,
                              "walletImpact": {"currency": "EUR", "netValue": 300.0}}}],
          "nextPagePath": None}


class Trading212ThroughSourcesTest(unittest.TestCase):
    """The live Trading 212 path, with a slow canned API: after the first call
    a dashboard request never waits on it, and an upload still takes effect."""

    def setUp(self):
        cache.clear()
        for fn in (t212.account_currency, t212.account_summary, t212.account_holdings,
                   t212.account_trades, t212.account_dividends, t212.account_transactions):
            fn.cache_clear()
        self.calls = []

        def slow_get(acct, path):
            self.calls.append(path)
            time.sleep(0.15)  # a rate-limited broker API
            if path.startswith("/equity/positions"):
                return POSITIONS
            if path.startswith("/equity/history/orders"):
                return ORDERS
            return {"currency": "EUR"}

        acct = t212.Account("T212 Invest", "k", "s")
        for p in (mock.patch.object(t212, "accounts", return_value=[acct]),
                  mock.patch.object(t212, "_statement", return_value=None),
                  mock.patch.object(t212, "_get", slow_get),
                  mock.patch.object(fx, "_live_rates", return_value={"EUR": 1.0, "USD": 0.9}),
                  mock.patch.object(sources.degiro, "get_holdings", return_value=[]),
                  mock.patch.object(sources.traderepublic, "get_holdings", return_value=[]),
                  mock.patch.object(sources.degiro, "get_trades", return_value=[]),
                  mock.patch.object(sources.traderepublic, "get_trades", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(cache.clear)

    def test_holdings_and_trades_cached_and_refreshed_in_background(self):
        (h,) = sources.holdings()
        self.assertEqual((h.broker, h.ticker, h.price), ("T212 Invest", "AAPL", 200.0))
        self.assertEqual(len(sources.all_trades()), 1)
        n = len(self.calls)
        t = time.perf_counter()
        for _ in range(5):  # a page load asks many times
            sources.holdings()
            sources.all_trades()
        self.assertLess(time.perf_counter() - t, 0.05)
        self.assertEqual(len(self.calls), n)

        # 40 s later: served at once, refreshed once in the background
        cache._entries["sources.holdings"].at -= 40
        t212.account_holdings.cache_clear()
        t = time.perf_counter()
        sources.holdings()
        self.assertLess(time.perf_counter() - t, 0.05)
        self.assertTrue(wait_for(lambda: self.calls.count("/equity/positions") == 2))

    def test_upload_invalidates(self):
        sources.holdings()
        cache.invalidate()  # what main.py does after POST /api/upload/...
        with mock.patch.object(sources.degiro, "get_holdings", return_value=["new"]):
            self.assertIn("new", sources.holdings())


if __name__ == "__main__":
    unittest.main()
