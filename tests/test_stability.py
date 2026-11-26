"""Stability under faults: each test injects one failure and checks what the
dashboard does with it.

  * one failing source (a broker API, a damaged statement, Yahoo) never takes
    the page down, and the dashboard says what is left out (`problems`);
  * a damaged data file gives an error that names the file, never a 0, and is
    never saved over; every write is atomic (temp file, then rename);
  * every unexpected /api/ error is JSON in plain words, no stack trace;
  * callers waiting on a source that is down share its error (no pile-up of
    slow retries), and in-memory caches stay bounded;
  * a refresh that fails never turns into a wrong number (yesterday's move as
    today's, a Trading 212 account counted twice, Freedom Day from 0);
  * two saves at once are both kept; run.sh says when its port is taken;
    Ctrl+C stops the server cleanly.

No network: Yahoo is a fake (tests/test_cache.fake_yf), broker APIs are
mocks, files live in temporary folders.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import requests

from backend import cache, clock, datafiles, market, prices
from tests.test_cache import Counter, fake_yf, wait_for
from tests.test_mcp import sample_world

try:
    from starlette.testclient import TestClient
except Exception:  # pragma: no cover - the test client needs httpx
    TestClient = None

ROOT = Path(__file__).resolve().parents[1]
DAMAGED = {
    "empty": b"",
    "cut off": b'{"accounts": [{"name": "Main", "bal',
    "binary": b"\x00\xff\xfe junk",
    "a list": b"[]",
    "null": b"null",
    "rows not objects": b'{"accounts": [1, "x"], "loans": "x", "tickers": [null]}',
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------- data files
class SafeFilesTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_missing_file_is_the_default(self):
        d = {"accounts": []}
        got = datafiles.read_json(self.dir / "none.json", d)
        self.assertEqual(got, d)
        self.assertIsNot(got, d)

    def test_every_kind_of_damage_is_a_plain_error_naming_the_file(self):
        for name, raw in DAMAGED.items():
            with self.subTest(name):
                p = self.dir / "cash_accounts.json"
                p.write_bytes(raw)
                with self.assertRaises(datafiles.DataFileError) as cm:
                    datafiles.read_json(p, {}, lists=("accounts", "loans", "tickers"))
                self.assertIn("cash_accounts.json", str(cm.exception))
                self.assertIn("Restore it from a backup", str(cm.exception))

    def test_atomic_write_never_leaves_half_a_file(self):
        p = self.dir / "finances.json"
        p.write_text('{"loans": []}')
        with mock.patch("os.replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                datafiles.write_atomic(p, b'{"loans": [{"name": "new"}]}')
        self.assertEqual(p.read_text(), '{"loans": []}')            # the old file, whole
        self.assertEqual([x.name for x in self.dir.iterdir()], ["finances.json"])   # no temp left
        datafiles.write_atomic(p, '{"loans": [1]}')
        self.assertEqual(p.read_text(), '{"loans": [1]}')

    def test_save_file_is_atomic(self):
        with mock.patch.object(datafiles, "DATA_DIR", self.dir), \
                mock.patch.object(datafiles, "write_atomic", wraps=datafiles.write_atomic) as w:
            datafiles.save_file("degiro_account.csv", b"Koop")
        w.assert_called_once()
        self.assertEqual((self.dir / "degiro_account.csv").read_bytes(), b"Koop")


class DamagedFilesTest(unittest.TestCase):
    """The user's own cash / budget / watchlist file is damaged."""

    @classmethod
    def setUpClass(cls):
        sample_world(cls)
        from backend import cash, finances, main, watch
        cls.main, cls.mods = main, {"cash_accounts.json": cash, "finances.json": finances,
                                    "watchlist.json": watch}

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in [mock.patch.object(datafiles, "_SEARCH_DIRS", [self.dir])] + \
                 [mock.patch.object(m, "DATA_DIR", self.dir) for m in self.mods.values()]:
            p.start()
            self.addCleanup(p.stop)
        datafiles.refresh()
        cache.clear()
        self.addCleanup(datafiles.refresh)

    def test_damaged_file_is_an_error_naming_it_never_an_empty_zero(self):
        endpoints = {"cash_accounts.json": self.main.cash_get, "finances.json": self.main.finances_get,
                     "watchlist.json": self.main.watchlist_get}
        for fname, endpoint in endpoints.items():
            for kind, raw in DAMAGED.items():
                if kind == "rows not objects" and fname == "finances.json":
                    raw = b'{"loans": "x"}'
                with self.subTest(file=fname, damage=kind):
                    (self.dir / fname).write_bytes(raw)
                    datafiles.refresh()
                    with self.assertRaises(datafiles.DataFileError) as cm:
                        endpoint()
                    self.assertIn(fname, str(cm.exception))
            (self.dir / fname).unlink()

    def test_damaged_file_is_never_saved_over(self):
        raw = b'{"accounts": [{"name": "Main", "balance": 1200}, {"name": "Sav'
        (self.dir / "cash_accounts.json").write_bytes(raw)
        datafiles.refresh()
        with self.assertRaises(datafiles.DataFileError):
            self.main.cash_upsert(name="New", institution="", balance=5.0, currency="EUR", type="cash", rate=0)
        self.assertEqual((self.dir / "cash_accounts.json").read_bytes(), raw)
        (self.dir / "finances.json").write_bytes(b"[]")
        datafiles.refresh()
        with self.assertRaises(datafiles.DataFileError):
            self.main.finances_upsert(kind="loans", name="Car", balance=100.0, rate=0, monthly_payment=10,
                                      monthly_eur=0, currency="EUR", fixed=True, tax="", pension_pct=0,
                                      pension_type="sacrifice", employer_pct=0, started="", value=0,
                                      category="other", paid=0, bought="")
        self.assertEqual((self.dir / "finances.json").read_bytes(), b"[]")

    def test_the_rest_of_the_dashboard_works_with_damaged_files(self):
        shutil.copy(ROOT / "data" / "degiro_account.sample.csv", self.dir / "degiro_account.csv")
        for fname in self.mods:
            (self.dir / fname).write_bytes(b'{"cut')
        datafiles.refresh()
        out = self.main.portfolio()
        self.assertGreater(out["totals"]["value"], 0)
        self.main.datastatus()
        self.main.savings()      # reads cash and budget; skips them when damaged

    def test_no_data_folder_at_all(self):
        gone = self.dir / "missing"
        with mock.patch.object(datafiles, "_SEARCH_DIRS", [gone]), \
                mock.patch.object(datafiles, "DATA_DIR", gone):
            datafiles.refresh()
            cache.clear()
            for endpoint in (self.main.portfolio, self.main.closed, self.main.cash_get,
                             self.main.finances_get, self.main.watchlist_get, self.main.datastatus):
                with self.subTest(endpoint.__name__):
                    endpoint()
        self.assertFalse(gone.exists())        # reading never creates it

    def test_freedom_day_is_left_out_when_investments_cannot_be_read(self):
        datafiles.refresh()       # nothing of mine: the sample budget, which has payments
        self.assertIn("freedom", self.main.finances_get())
        with mock.patch.object(self.main, "_open_holdings_rows", side_effect=RuntimeError("Yahoo down")):
            out = self.main.finances_get()
        self.assertNotIn("freedom", out)     # before: worked out with investments = 0

    def test_lookup_caches_of_the_wrong_shape_are_ignored(self):
        from backend import instruments, sectors
        (self.dir / "sectors.json").write_text('["Technology"]')
        (self.dir / "tickers_found.json").write_text('{"US0000000001": "AAPL", "X": [1]}')
        with mock.patch.object(sectors, "CACHE_FILE", self.dir / "sectors.json"), \
                mock.patch.object(sectors, "_memory", None), \
                mock.patch.object(instruments, "_FOUND_FILE", self.dir / "tickers_found.json"), \
                mock.patch.object(instruments, "_found", None), \
                mock.patch.dict(sys.modules, {"yfinance": fake_yf({})}):
            self.assertEqual(sectors._load_cache(), {})
            self.assertEqual(instruments.price_currency("ABC.AS"), "EUR")


class DamagedConnectionsFileTest(unittest.TestCase):
    def setUp(self):
        from backend import connections
        self.c = connections
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.object(connections, "_file", lambda name: self.dir / name)
        p.start()
        self.addCleanup(p.stop)
        for d in (connections._read_cache, connections.damaged):
            d.clear()
        self.addCleanup(connections.damaged.clear)

    def test_wrong_shape_runs_without_it_and_keeps_it_aside_before_saving(self):
        (self.dir / "connections.json").write_text('[{"id": "c_1"}]')
        self.assertEqual(self.c.load_all(), [])       # before: AttributeError on every page
        self.assertIn("connections.json", self.c.damaged)
        self.c.add(self.c.new({"id": "csv"}, "upload", "generic_csv", "My CSV", "", {}))
        kept = [p for p in self.dir.iterdir() if ".damaged-" in p.name]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(), '[{"id": "c_1"}]')
        self.assertEqual([c["label"] for c in self.c.load_all()], ["My CSV"])

    def test_entries_without_a_label_get_one(self):
        (self.dir / "connections.json").write_text(json.dumps(
            {"connections": [{"id": "c_1", "institution_name": "Bank"}, {"id": 5}, None, "x"]}))
        self.assertEqual([(c["id"], c["label"]) for c in self.c.load_all()], [("c_1", "Bank")])


# --------------------------------------------------------- one source fails
class SourceFailureTest(unittest.TestCase):
    """A broker or statement that fails leaves the others and says so."""

    @classmethod
    def setUpClass(cls):
        sample_world(cls)
        from backend import main, sources
        cls.main, cls.sources = main, sources

    def setUp(self):
        cache.clear()
        self.sources._problems.clear()
        self.sources._last_good.clear()
        self.addCleanup(self.sources._problems.clear)

    def brokers(self, out):
        return {b for h in out["holdings"] for b in h["brokers"]}

    def test_failing_source_is_left_out_and_reported(self):
        with mock.patch.object(self.sources.degiro, "get_holdings", side_effect=RuntimeError("bad file")):
            out = self.main.portfolio()
        self.assertNotIn("DeGiro", self.brokers(out))
        self.assertTrue(self.brokers(out))                      # the others are still there
        (p,) = [p for p in out["problems"] if p["source"] == "DeGiro"]
        self.assertIn("left out of the totals", p["message"])

    def test_failing_source_keeps_its_last_good_holdings(self):
        first = self.main.portfolio()
        self.assertIn("DeGiro", self.brokers(first))
        cache.clear()
        with mock.patch.object(self.sources.degiro, "get_holdings", side_effect=RuntimeError("down")):
            out = self.main.portfolio()
        self.assertEqual(out["totals"]["value"], first["totals"]["value"])
        (p,) = [p for p in out["problems"] if p["source"] == "DeGiro"]
        self.assertIn("as read at", p["message"])
        cache.clear()
        self.assertEqual(self.main.portfolio()["problems"], [])   # recovered: the note goes

    def test_damaged_statement_message_names_the_file(self):
        err = datafiles.DataFileError(Path("trades_trade_republic.json"), "it is empty or cut off")
        with mock.patch.object(self.sources.traderepublic, "get_holdings", side_effect=err):
            out = self.main.portfolio()
        msgs = [p["message"] for p in out["problems"]]
        self.assertTrue(any("trades_trade_republic.json" in m for m in msgs), msgs)

    def test_failing_trade_history_is_reported(self):
        with mock.patch.object(self.sources.degiro, "get_trades", side_effect=RuntimeError("bad file")):
            self.main.closed()                                  # still answers
            msgs = {p["source"]: p["message"] for p in self.sources.problems()}
        self.assertIn("Profit from selling", msgs["DeGiro"])

    def test_broken_keys_and_connections_are_reported_without_waiting_for_them(self):
        bad_key = [{"label": "T212 ISA", "ok": False, "error": "HTTP 401 — key/secret wrong"}]
        gate = threading.Event()

        def slow_status():                     # Trading 212 is slow to answer
            gate.wait(2)
            return bad_key
        conns = [{"id": "c_1", "label": "Wise", "status": "error", "error": "Wise rejected the token.",
                  "last_sync": None},
                 {"id": "c_2", "label": "Bank", "status": "error", "error": "The bank link expired.",
                  "last_sync": "2026-11-20T08:00:00"}]
        with mock.patch.object(self.sources.trading212, "status", slow_status), \
                mock.patch.object(self.sources.connections, "load_all", return_value=conns):
            t = time.perf_counter()
            first = {p["source"]: p["message"] for p in self.sources.problems()}
            self.assertLess(time.perf_counter() - t, 0.5)      # never waits for the API
            gate.set()
            self.assertTrue(wait_for(lambda: any(p["source"] == "T212 ISA" for p in self.sources.problems())))
            msgs = {p["source"]: p["message"] for p in self.sources.problems()}
        self.assertNotIn("T212 ISA", first)
        self.assertIn("left out of the totals", msgs["T212 ISA"])
        self.assertIn("left out of the totals", msgs["Wise"])
        self.assertIn("Showing the numbers read at 2026-11-20 08:00", msgs["Bank"])

    def test_connector_that_cannot_list_accounts_leaves_the_others(self):
        with mock.patch.object(self.sources.traderepublic, "trade_sources", side_effect=TypeError("x")):
            labels = set(self.sources.trade_sources())
        self.assertIn("DeGiro", labels)
        self.assertIn("Trade Republic", {p["source"] for p in self.sources.problems()})

    def test_unreadable_income_is_a_dash_not_a_zero(self):
        normal = self.main.portfolio()["totals"]
        with mock.patch.object(self.main.income, "summary", side_effect=ValueError("bad statement")):
            out = self.main.portfolio()
        t = out["totals"]
        self.assertEqual((t["dividends"], t["income"], t["total_return"]), (None, None, None))
        self.assertEqual(t["value"], normal["value"])            # the rest is unchanged
        self.assertIn("Dividends and interest", {p["source"] for p in out["problems"]})
        from backend import insights
        with mock.patch.object(insights, "headlines", return_value=[]):
            insights.daily_brief(out, {})                       # the brief copes with it

    def test_no_live_price_and_fallback_rates_are_flagged(self):
        from backend import fx
        with mock.patch.dict(fx._cache, {"USD": 0.9, "GBP": 1.2, "CHF": 1.05}, clear=True):
            quotes = self.main.portfolio()["quotes"]
        self.assertEqual((quotes["unpriced"], quotes["fx_live"]), (0, True))
        cache.clear()
        with mock.patch.object(self.main, "fetch_quotes", return_value={}), \
                mock.patch.dict(fx._cache, {}, clear=True):      # no live rate yet: the fallback
            q = self.main.portfolio()["quotes"]
        self.assertGreater(q["unpriced"], 0)
        self.assertIs(q["fx_live"], False)


# ------------------------------------------------------------ cache faults
class SharedFailureTest(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_callers_waiting_on_a_failing_source_share_its_error(self):
        fn = Counter(values=[RuntimeError("down")] * 10, delay=0.2)
        errors = []

        def call():
            try:
                cache.get("k", fn, ttl=60)
            except RuntimeError as e:
                errors.append(e)
        t = time.perf_counter()
        threads = [threading.Thread(target=call) for _ in range(6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual((fn.calls, len(errors)), (1, 6))      # before: 6 calls, one after another
        self.assertLess(time.perf_counter() - t, 0.6)

    def test_tried_again_after_the_hold_or_after_an_upload(self):
        fn = Counter(values=[RuntimeError("down"), RuntimeError("down"), 7])
        with mock.patch.object(cache, "_ERROR_HOLD", 0.05):
            with self.assertRaises(RuntimeError):
                cache.get("k", fn, ttl=60)
            with self.assertRaises(RuntimeError):
                cache.get("k", fn, ttl=60)                     # within the hold: no new attempt
            self.assertEqual(fn.calls, 1)
            time.sleep(0.06)
            with self.assertRaises(RuntimeError):
                cache.get("k", fn, ttl=60)
            self.assertEqual(fn.calls, 2)
            cache.invalidate()                                 # an upload: try at once
            self.assertEqual(cache.get("k", fn, ttl=60), 7)

    def test_last_good_value_at_once_while_the_source_is_down(self):
        fn = Counter(values=[1] + [RuntimeError("down")] * 5, delay=0.1)
        self.assertEqual(cache.get("k", fn, ttl=0.15), 1)
        time.sleep(0.16)
        self.assertEqual(cache.get("k", fn, ttl=0.15), 1)      # tried once, failed
        t = time.perf_counter()
        self.assertEqual(cache.get("k", fn, ttl=0.15), 1)      # no second wait
        self.assertLess(time.perf_counter() - t, 0.05)
        self.assertEqual(fn.calls, 2)
        self.assertIsInstance(cache.last_error("k"), RuntimeError)

    def test_background_refresh_failure_is_visible(self):
        fn = Counter(values=[1, RuntimeError("down")])
        cache.get("k", fn, ttl=0.01, stale=60)
        time.sleep(0.02)
        cache.get("k", fn, ttl=0.01, stale=60)
        self.assertTrue(wait_for(lambda: cache.last_error("k") is not None))

    def test_idle_entries_of_one_family_are_dropped(self):
        for i in range(30):
            cache.get(f"market.sparklines:{i}", lambda: [0] * 1000, ttl=60, data=False)
        for e in cache._entries.values():
            e.used -= 2 * cache._IDLE
        cache.get("market.sparklines:new", lambda: [], ttl=60, data=False)
        fam = [k for k in cache._entries if k.startswith("market.sparklines:")]
        self.assertLessEqual(len(fam), cache._FAMILY_MAX + 1)
        self.assertIn("market.sparklines:new", fam)

    def test_entries_in_use_are_kept(self):
        for i in range(30):
            cache.get(f"sources.trades:acct{i}", lambda: [], ttl=60)
        cache.get("sources.trades:one more", lambda: [], ttl=60)
        self.assertEqual(len([k for k in cache._entries if k.startswith("sources.trades:")]), 31)

    def test_trim(self):
        d = {i: i for i in range(10)}
        cache.trim(d, 3)
        self.assertEqual(list(d), [7, 8, 9])


class BoundedCachesTest(unittest.TestCase):
    def setUp(self):
        for d in (market._news_cache, market._hist_cache):
            d.clear()
        self.addCleanup(market._news_cache.clear)
        self.addCleanup(market._hist_cache.clear)

    def test_news_cache_is_bounded_and_failures_are_not_kept(self):
        yf = fake_yf({})

        class T:
            def __init__(self, sym):
                self.news = [{"content": {"title": sym}}, None, 5]   # odd items are skipped
        yf.Ticker = T
        with mock.patch.dict(sys.modules, {"yfinance": yf}):
            for i in range(market._MAX_KEYS + 50):
                self.assertEqual(market.news(f"T{i}"), [{"title": f"T{i}", "publisher": "", "url": ""}])
        self.assertEqual(len(market._news_cache), market._MAX_KEYS)

        def down(sym):
            raise RuntimeError("Yahoo down")
        yf.Ticker = down
        with mock.patch.dict(sys.modules, {"yfinance": yf}):
            self.assertEqual(market.news("NEW"), [])
            self.assertNotIn("NEW", market._news_cache)            # asked again next time
            self.assertEqual(market.news("T349")[0]["title"], "T349")   # the old ones stay


# ------------------------------------------------- failures as wrong numbers
class StaleQuoteTest(unittest.TestCase):
    def setUp(self):
        for d in (prices._CACHE, prices._FAILED):
            d.clear()
        self.addCleanup(prices._CACHE.clear)
        self.addCleanup(prices._FAILED.clear)

    def test_yesterdays_quote_is_not_todays_move(self):
        now = time.time()
        yesterday = clock.today() - timedelta(days=1)
        prices._CACHE["ACME"] = (110.0, 100.0, now - 3600, "USD", yesterday)
        prices._FAILED["ACME"] = now                     # today's fetch failed a moment ago
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf({"ACME": RuntimeError("down")})}):
            q = prices.fetch_quotes(["ACME"])["ACME"]
        self.assertEqual(q["price"], 110.0)              # the last known price…
        self.assertIsNone(q["prev_close"])               # …but no "today" change (was +10%)

    def test_todays_quote_keeps_its_move(self):
        prices._CACHE["ACME"] = (110.0, 100.0, time.time(), "USD", clock.today())
        self.assertEqual(prices.fetch_quotes(["ACME"])["ACME"]["prev_close"], 100.0)


class Trading212OutageTest(unittest.TestCase):
    """A short Trading 212 outage must not count one account twice."""

    def test_key_and_statement_of_one_account_count_once_after_an_outage(self):
        from backend.connectors import trading212 as t212
        from tests.test_trading212 import (DIVIDENDS, POSITIONS, SAME_ORDERS, SAME_STATEMENT, TRANSACTIONS,
                                           clear_caches, fake_get)
        from backend import fx
        clear_caches()
        self.addCleanup(clear_caches)
        rates = mock.patch.object(fx, "_live_rates", return_value={"EUR": 1.0, "USD": 0.9, "GBP": 1.2})
        rates.start()
        self.addCleanup(rates.stop)
        acct = t212.Account("Trading212", "k", "s")
        down = {"/equity/positions": POSITIONS, "/equity/history/orders": requests.ConnectionError("down"),
                "/equity/account/info": {"currencyCode": "EUR"}}
        up = {"/equity/positions": POSITIONS, "/equity/history/orders": SAME_ORDERS,
              "/equity/history/dividends": DIVIDENDS, "/equity/history/transactions": TRANSACTIONS,
              "/equity/account/info": {"currencyCode": "EUR"}}
        with mock.patch.object(t212, "_statement", return_value=SAME_STATEMENT), \
                mock.patch.object(t212, "accounts", return_value=[acct]), \
                mock.patch.object(t212, "_get", fake_get(down)):
            t212.trade_sources()                         # during the outage
        with mock.patch.object(t212, "_statement", return_value=SAME_STATEMENT), \
                mock.patch.object(t212, "accounts", return_value=[acct]), \
                mock.patch.object(t212, "_get", fake_get(up)):
            srcs = t212.trade_sources()
            trades = sum(len(get()) for _, get in srcs.values())
        self.assertEqual(set(srcs), {"Trading212"})      # before: also "Trading212 (statement)"
        self.assertEqual(trades, len(SAME_ORDERS["items"]))   # before: 6, every trade twice

    def test_closed_account_answer_is_still_remembered(self):
        from backend.connectors import trading212 as t212
        from tests.test_trading212 import http_error
        self.assertFalse(t212._transient(http_error(403)))
        self.assertTrue(t212._transient(http_error(503)))
        self.assertTrue(t212._transient(http_error(429)))
        self.assertTrue(t212._transient(requests.ConnectionError("x")))


# ---------------------------------------------------------- slow feeds
class SlowFeedsTest(unittest.TestCase):
    def setUp(self):
        from backend import fx
        cache.clear()
        self.addCleanup(cache.clear)
        p = mock.patch.object(fx, "_live_rates", return_value={"EUR": 1.0, "USD": 0.9, "GBP": 1.2})
        p.start()
        self.addCleanup(p.stop)

    def test_news_feeds_are_read_at_the_same_time(self):
        from backend import insights

        def slow(name, url, limit=10):
            time.sleep(0.3)
            return [{"title": f"{name} story", "url": "", "source": name, "published": ""}]
        with mock.patch.object(insights, "_fetch_rss", slow):
            t = time.perf_counter()
            items = insights.headlines()
        self.assertEqual(len(items), len(insights.RSS_FEEDS))
        self.assertLess(time.perf_counter() - t, 0.6)     # before: 0.9 (one after another)

    def test_brief_never_waits_for_headlines(self):
        from backend import insights
        port = {"totals": {"daily": None, "total_return": 10.0, "all_time": 5.0, "income": 5.0, "value": 100.0},
                "holdings": []}
        gate = threading.Event()
        with mock.patch.object(insights, "_fetch_rss", lambda *a, **k: gate.wait(2) and []):
            t = time.perf_counter()
            out = insights.daily_brief(port, {})
            self.assertLess(time.perf_counter() - t, 0.2)
            gate.set()
        self.assertEqual(out["headlines"], [])


# ---------------------------------------------------------------- the API
@unittest.skipIf(TestClient is None, "starlette's test client is not available")
class ApiErrorTest(unittest.TestCase):
    """Unexpected errors reach the browser as JSON in plain words."""

    @classmethod
    def setUpClass(cls):
        sample_world(cls)
        from backend import main
        cls.main = main
        cls.client = TestClient(main.app, base_url="http://localhost:8000", raise_server_exceptions=False)

    def setUp(self):
        cache.clear()

    def test_unexpected_error_is_json_without_a_trace(self):
        with mock.patch.object(self.main.market, "indices", side_effect=KeyError("regularMarketPrice")):
            r = self.client.get("/api/indices")
        self.assertEqual(r.status_code, 500)
        self.assertIn("could not be worked out just now", r.json()["detail"])
        self.assertNotIn("Traceback", r.text)
        self.assertNotIn("regularMarketPrice", r.text)

    def test_damaged_file_message_reaches_the_card(self):
        err = datafiles.DataFileError(Path("/x/cash_accounts.json"), "it is empty or cut off")
        with mock.patch.object(self.main.cash, "load", side_effect=err):
            r = self.client.get("/api/cash")
        self.assertEqual(r.status_code, 500)
        self.assertIn("cash_accounts.json could not be read", r.json()["detail"])

    def test_pages_are_not_json(self):
        with mock.patch.object(self.main, "FileResponse", side_effect=RuntimeError("disk")):
            r = self.client.get("/settings")
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("Traceback", r.text)

    def test_every_dashboard_endpoint_answers_with_yahoo_down(self):
        down = fake_yf({})

        def broken(*a, **k):
            raise RuntimeError("Yahoo is down")
        down.Ticker = broken
        down.download = broken
        with mock.patch.dict(sys.modules, {"yfinance": down}):
            for d in (prices._CACHE, prices._FAILED, market._spark_cache):
                d.clear()
            for path in ("/api/portfolio", "/api/closed", "/api/profit", "/api/history", "/api/indices",
                         "/api/sparklines", "/api/cash", "/api/finances", "/api/analytics", "/api/health",
                         "/api/watchlist", "/api/uk_tax", "/api/harvest", "/api/savings", "/api/datastatus",
                         "/api/news/AAPL", "/api/ticker_history/AAPL", "/api/fx"):
                with self.subTest(path):
                    r = self.client.get(path)
                    self.assertIn(r.status_code, (200, 500))
                    body = r.json()
                    if r.status_code == 500:
                        self.assertIsInstance(body["detail"], str)
        for d in (prices._CACHE, prices._FAILED, market._spark_cache):
            d.clear()


@unittest.skipIf(TestClient is None, "starlette's test client is not available")
class ConcurrentSavesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sample_world(cls)
        from backend import cash, main
        cls.main, cls.cash = main, cash

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.object(datafiles, "_SEARCH_DIRS", [self.dir]),
                  mock.patch.object(self.cash, "DATA_DIR", self.dir)):
            p.start()
            self.addCleanup(p.stop)
        (self.dir / "cash_accounts.json").write_text('{"accounts": []}')
        datafiles.refresh()
        self.addCleanup(datafiles.refresh)

    def test_two_saves_at_once_are_both_kept(self):
        real_load = self.cash.load

        def slow_load():                 # widen the read-change-write window
            out = real_load()
            time.sleep(0.15)
            return out
        client = TestClient(self.main.app, base_url="http://localhost:8000")
        codes = []

        def save(name):
            r = client.post("/api/cash", data={"name": name, "balance": "10", "currency": "EUR"},
                            headers={"Origin": "http://localhost:8000"})
            codes.append(r.status_code)
        with mock.patch.object(self.cash, "load", slow_load):
            threads = [threading.Thread(target=save, args=(n,)) for n in ("Alpha", "Beta")]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        self.assertEqual(codes, [200, 200])
        names = {a["name"] for a in json.loads((self.dir / "cash_accounts.json").read_text())["accounts"]}
        self.assertEqual(names, {"Alpha", "Beta"})        # before: one of them was lost

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                     "folder permissions do not apply")
    def test_read_only_data_folder_is_a_clear_error(self):
        self.dir.chmod(0o500)
        self.addCleanup(self.dir.chmod, 0o700)
        client = TestClient(self.main.app, base_url="http://localhost:8000", raise_server_exceptions=False)
        r = client.post("/api/cash", data={"name": "Alpha", "balance": "10", "currency": "EUR"},
                        headers={"Origin": "http://localhost:8000"})
        self.assertEqual(r.status_code, 500)
        self.assertIn("can be written to", r.json()["detail"])
        self.assertEqual((self.dir / "cash_accounts.json").read_text(), '{"accounts": []}')

    def test_full_disk_during_an_upload_keeps_the_old_statement(self):
        old = (ROOT / "data" / "degiro_account.sample.csv").read_bytes()
        (self.dir / "degiro_account.csv").write_bytes(old)
        real_open = open

        def full_disk(file, mode="r", *a, **k):
            if "w" in mode and ".degiro_account.csv." in str(file):
                raise OSError(28, "No space left on device")
            return real_open(file, mode, *a, **k)
        with mock.patch.object(datafiles, "DATA_DIR", self.dir), \
                mock.patch("builtins.open", full_disk):
            client = TestClient(self.main.app, base_url="http://localhost:8000", raise_server_exceptions=False)
            r = client.post("/api/upload/degiro", files={"file": ("d.csv", old + b"\n", "text/csv")},
                            headers={"Origin": "http://localhost:8000"})
        self.assertEqual(r.status_code, 500)
        self.assertIn("disk is not full", r.json()["detail"])
        self.assertEqual((self.dir / "degiro_account.csv").read_bytes(), old)

    def test_statement_in_another_encoding_is_saved_readable(self):
        sample = (ROOT / "data" / "degiro_account.sample.csv").read_text().replace("FERRARI NV", "FERRARI NV é")
        raw = sample.encode("cp1252")
        with mock.patch.object(datafiles, "DATA_DIR", self.dir), \
                mock.patch.object(self.main.github_sync, "push_vault_async", lambda: None):
            client = TestClient(self.main.app, base_url="http://localhost:8000")
            r = client.post("/api/upload/degiro", files={"file": ("degiro.csv", raw, "text/csv")},
                            headers={"Origin": "http://localhost:8000"})
        self.assertEqual(r.status_code, 200, r.text)
        saved = (self.dir / "degiro_account.csv").read_bytes()
        self.assertIn("FERRARI NV é", saved.decode("utf-8"))   # before: raw cp1252, unreadable later


# ---------------------------------------------------- start and shutdown
FAKE_YF = '''
def __getattr__(name):
    raise RuntimeError("no network in tests")
'''


class StartStopTest(unittest.TestCase):
    def test_ctrl_c_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "yfinance.py").write_text(FAKE_YF)
            port = free_port()
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("T212_", "GITHUB_", "RENDER", "DASHBOARD_", "MCP_", "ENABLEBANKING"))}
            env.update({"PYTHONPATH": os.pathsep.join([tmp, str(ROOT)]), "WARMUP": "0", "KEEPALIVE_SELF": "0",
                        "TICKER_LOOKUP": "0", "NO_PROXY": "127.0.0.1,localhost"})
            proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app", "--port", str(port),
                                     "--log-level", "warning"], cwd=ROOT, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            try:
                self.assertTrue(wait_for(lambda: self._up(port), timeout=20), "server did not start")
                proc.send_signal(signal.SIGINT)
                out, _ = proc.communicate(timeout=20)
            finally:
                proc.kill()
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn(b"Traceback", out)

    @staticmethod
    def _up(port):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            return False

    def test_run_sh_says_when_the_port_is_taken(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen(1)
            port = busy.getsockname()[1]
            shutil.copy(ROOT / "run.sh", tmp)
            shutil.copy(ROOT / "requirements.txt", tmp)
            venv = Path(tmp) / ".venv"
            (venv / "bin").mkdir(parents=True)
            os.symlink(sys.executable, venv / "bin" / "python")
            with open(ROOT / "requirements.txt") as fh:
                want = subprocess.run(["cksum"], stdin=fh, capture_output=True, text=True).stdout.strip()
            (venv / ".requirements").write_text(want + "\n")      # no pip install
            r = subprocess.run(["sh", "run.sh"], cwd=tmp, capture_output=True, text=True, timeout=20,
                               env={**os.environ, "PORT": str(port), "NO_BROWSER": "1"})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn(f"Port {port} is already in use", r.stdout)
        self.assertIn(f"PORT={port + 1} ./run.sh", r.stdout)


if __name__ == "__main__":
    unittest.main()
