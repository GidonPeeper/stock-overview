"""The check-up (backend/checkup.py) and the notices for out-of-date rules
(backend/rules.py).

Every finding is tested with the condition that triggers it and without it.
Made-up names and figures only; no network (Yahoo, GitHub and brokers are
never asked: the check-up reads what the app already holds)."""

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from backend import (appstate, checkup, clock, connections, datafiles, fx, github_sync, prices, rules,
                     sources, version)
from backend.datafiles import DataFileError

TODAY = date(2026, 11, 27)
# The day before and the day a later UK tax year starts than the one the
# figures are for: these tests keep working after a roll-over (MAINTAINING.md).
_Y = int(rules.UK_TAX_YEAR[:4])
LAST_DAY, NEW_YEAR = date(_Y + 1, 4, 5), date(_Y + 1, 4, 6)
RULES, NEXT = rules.UK_TAX_YEAR, f"{_Y + 1}/{str(_Y + 2)[2:]}"


_no_dismissals = mock.patch("backend.prefs.dismissed", return_value={})


def setUpModule():        # warnings dismissed in a copy that holds your own data change nothing here
    _no_dismissals.start()


def tearDownModule():
    _no_dismissals.stop()


def ctx(**inputs):
    return checkup.Context(today=inputs.pop("today", TODAY), vault_state=inputs.pop("vault_state", None),
                           inputs=inputs)


def ids(findings):
    return {f["id"] for f in findings}


def row(**kw):
    out = {"broker": "Broker A", "ticker": "AAA", "name": "Alpha plc", "currency": "EUR", "priced_live": True}
    out.update(kw)
    return out


class FindingShapeTest(unittest.TestCase):
    def test_every_finding_says_what_to_do(self):
        f = checkup._finding("x", "attention", "Title", "Why", "Do this", "/settings#backup", "Backup")
        self.assertEqual(set(f), {"id", "level", "title", "why", "action", "target", "target_label"})
        with self.assertRaises(AssertionError):
            checkup._finding("x", "urgent", "t", "w", "a")

    def test_order_and_counts(self):
        found = [checkup._finding("i", "info", "t", "w", "a"), checkup._finding("p", "problem", "t", "w", "a"),
                 checkup._finding("a", "attention", "t", "w", "a")]
        with mock.patch.object(checkup, "CHECKS", [lambda c: found, lambda c: 1 / 0]):
            out = checkup.run(ctx())
        self.assertEqual([f["level"] for f in out["findings"]], ["problem", "attention", "info"])
        self.assertEqual(out["counts"], {"problem": 1, "attention": 1, "info": 1})
        self.assertFalse(out["ok"])
        with mock.patch.object(checkup, "CHECKS", [lambda c: [found[0]]]):
            self.assertTrue(checkup.run(ctx())["ok"])       # good to know only: all is well

    def test_dismissed_findings_leave_the_counts_and_problems_come_back(self):
        found = [checkup._finding("p", "problem", "t", "w", "a"), checkup._finding("a", "attention", "t", "w", "a"),
                 checkup._finding("i", "info", "t", "w", "a")]
        hidden = {"a": "2026-01-01", "i": "2026-11-27", "p": (TODAY - timedelta(days=6)).isoformat()}
        with mock.patch.object(checkup, "CHECKS", [lambda c: [dict(f) for f in found]]), \
                mock.patch("backend.prefs.dismissed", return_value=hidden):
            out = checkup.run(ctx())
            self.assertEqual(out["counts"], {"problem": 0, "attention": 0, "info": 0})
            self.assertEqual((out["dismissed"], out["ok"]), (3, True))
            self.assertTrue(all(f["dismissed"] for f in out["findings"]))   # still listed, as dismissed
            # a week on, the problem is back if it is still there; the rest stay hidden
            later = checkup.run(ctx(today=TODAY + timedelta(days=1)))
        self.assertEqual(later["counts"], {"problem": 1, "attention": 0, "info": 0})
        self.assertEqual([f["id"] for f in later["findings"] if not f["dismissed"]], ["p"])

    def test_slow_input_is_skipped_not_waited_for(self):
        def slow():
            time.sleep(5)
            return [], 0, 0, {}
        with mock.patch.object(checkup, "SLOW_INPUT_SECONDS", 0.2), \
                mock.patch("backend.realized.summary", return_value={"incomplete": []}), \
                mock.patch("backend.cash.load", return_value={"accounts": []}):
            t = time.monotonic()
            c = checkup.Context(holdings_rows=slow)
            self.assertLess(time.monotonic() - t, 1.5)
        self.assertIsNone(c.get("rows"))
        self.assertEqual(checkup._check_prices(c), [])      # cannot be checked: says nothing


class DataTest(unittest.TestCase):
    def test_demo(self):
        with mock.patch.object(datafiles, "demo", return_value=True):
            self.assertEqual(ids(checkup._check_demo(ctx())), {"demo"})
        with mock.patch.object(datafiles, "demo", return_value=False):
            self.assertEqual(checkup._check_demo(ctx()), [])

    def test_trading212_key_refused(self):
        bad = [{"label": "T212 Invest", "ok": False, "error": "HTTP 401 — key/secret wrong"},
               {"label": "T212 Old", "ok": False, "error": "HTTP 401", "note": "same account as the statement"},
               {"label": "T212 ISA", "ok": True}]
        with mock.patch.object(sources, "_t212_status", return_value=bad), \
                mock.patch("backend.connectors.trading212.accounts", return_value=[]):
            got = checkup._check_trading212_keys(ctx())
        self.assertEqual(ids(got), {"t212_key:T212 Invest"})
        self.assertIn("make a new read-only API key", got[0]["action"])
        with mock.patch.object(sources, "_t212_status", return_value=bad[2:]), \
                mock.patch("backend.connectors.trading212.accounts", return_value=[]):
            self.assertEqual(checkup._check_trading212_keys(ctx()), [])

    def _conn(self, **kw):
        c = {"id": "c_1", "label": "Bank B", "connector": "starling", "method": "api", "status": "ok",
             "last_sync": (TODAY - timedelta(days=1)).isoformat() + "T09:00:00"}
        c.update(kw)
        return c

    def _connections(self, conns):
        with mock.patch.object(connections, "load_all", return_value=conns), \
                mock.patch.object(connections, "damaged", {}):
            return checkup._check_connections(ctx())

    def test_connection_fine(self):
        self.assertEqual(self._connections([self._conn()]), [])

    def test_connection_error_and_sign_in(self):
        got = self._connections([self._conn(status="error", error="The token was refused."),
                                 self._conn(id="c_2", label="Broker T", status="signin")])
        self.assertEqual(ids(got), {"connection_error:c_1", "connection_signin:c_2"})
        self.assertTrue(all(f["level"] == "problem" for f in got))
        self.assertIn("The token was refused.", got[0]["why"])

    def test_bank_link_ending_and_ended(self):
        soon = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 5 * 86400))
        gone = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 86400))
        later = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 60 * 86400))
        got = self._connections([self._conn(consent_expires=soon), self._conn(id="c_2", consent_expires=gone),
                                 self._conn(id="c_3", consent_expires=later)])
        self.assertEqual(ids(got), {"bank_link_ends:c_1", "bank_link_expired:c_2"})

    def test_connection_not_updated_for_days(self):
        old = self._conn(last_sync=(TODAY - timedelta(days=9)).isoformat() + "T09:00:00")
        self.assertEqual(ids(self._connections([old])), {"connection_old:c_1"})

    def test_trading212_account_is_not_called_out_of_date(self):
        # Trading 212 accounts added in Settings are read by trading212.py, which
        # never refreshes this date: an old date here says nothing about the data
        old = self._conn(connector="trading212", last_sync=(TODAY - timedelta(days=9)).isoformat() + "T09:00:00")
        self.assertEqual(self._connections([old]), [])

    def test_position_file_old(self):
        csv = self._conn(connector="generic_csv", method="upload",
                         last_sync=(TODAY - timedelta(days=120)).isoformat() + "T09:00:00")
        self.assertEqual(ids(self._connections([csv])), {"csv_old:c_1"})
        csv["last_sync"] = (TODAY - timedelta(days=10)).isoformat() + "T09:00:00"
        self.assertEqual(self._connections([csv]), [])

    def test_damaged_connections_file(self):
        with mock.patch.object(connections, "load_all", return_value=[]), \
                mock.patch.object(connections, "damaged", {"connections.json": "ValueError"}):
            self.assertEqual(ids(checkup._check_connections(ctx())), {"damaged:connections.json"})

    def test_source_that_could_not_be_read(self):
        with mock.patch.object(sources, "_problems",
                               {"holdings:DeGiro": {"source": "DeGiro", "message": "Your file x.csv could not be read.",
                                                    "at": time.time()}}):
            got = checkup._check_source_problems(ctx())
        self.assertEqual(ids(got), {"source:holdings:DeGiro"})
        with mock.patch.object(sources, "_problems", {}):
            self.assertEqual(checkup._check_source_problems(ctx()), [])

    def test_damaged_cash_or_budget_file(self):
        err = DataFileError(Path("/x/cash_accounts.json"), "it is empty or cut off")
        with mock.patch("backend.finances._load_raw", return_value=({}, False)):
            self.assertEqual(ids(checkup._check_files(ctx(cash=err))), {"damaged:cash_accounts.json"})
            self.assertEqual(checkup._check_files(ctx(cash={"accounts": []})), [])
        with mock.patch("backend.finances._load_raw",
                        side_effect=DataFileError(Path("/x/finances.json"), "it is not valid JSON")):
            self.assertEqual(ids(checkup._check_files(ctx(cash={}))), {"damaged:finances.json"})

    def test_sales_without_a_purchase(self):
        inc = {"incomplete": [{"broker": "DeGiro", "name": "Example Oil A", "shares_without_purchase": 4}]}
        got = checkup._check_incomplete_history(ctx(realized=inc))
        self.assertEqual(ids(got), {"incomplete_history"})
        self.assertIn("Example Oil A (DeGiro)", got[0]["why"])
        self.assertEqual(checkup._check_incomplete_history(ctx(realized={"incomplete": []})), [])

    def test_interest_rate_not_set(self):
        c = {"rate_unknown_eur": 500.0, "accounts": [{"name": "Saver", "type": "savings", "rate_known": False}]}
        self.assertEqual(ids(checkup._check_cash_rates(ctx(cash=c))), {"cash_rate_unknown"})
        self.assertEqual(checkup._check_cash_rates(ctx(cash={**c, "rate_unknown_eur": 0})), [])


class PricesTest(unittest.TestCase):
    def setUp(self):
        for d in (prices._CACHE, prices._FAILED):
            p = mock.patch.dict(d, clear=True)
            p.start()
            self.addCleanup(p.stop)

    def test_holding_without_a_price(self):
        got = checkup._check_prices(ctx(rows=[row(priced_live=False)]))
        self.assertEqual(ids(got), {"unpriced"})
        self.assertEqual(checkup._check_prices(ctx(rows=[row()])), [])

    def test_yahoo_not_answering(self):
        now = time.time()
        prices._CACHE["AAA"] = (10.0, 9.0, now - 7200, "EUR", TODAY)
        prices._FAILED["AAA"] = now - 60
        self.assertEqual(ids(checkup._check_prices(ctx(rows=[row()]))), {"prices_down"})

    def test_some_prices_old(self):
        now = time.time()
        prices._CACHE.update({"AAA": (10.0, 9.0, now - 7200, "EUR", TODAY), "BBB": (5.0, 5.0, now, "EUR", TODAY)})
        prices._FAILED["AAA"] = now - 60
        got = checkup._check_prices(ctx(rows=[row(), row(ticker="BBB", name="Beta")]))
        self.assertEqual(ids(got), {"prices_old"})

    def test_a_failure_followed_by_a_good_price_is_fine(self):
        now = time.time()
        prices._FAILED["AAA"] = now - 600
        prices._CACHE["AAA"] = (10.0, 9.0, now - 60, "EUR", TODAY)
        self.assertEqual(checkup._check_prices(ctx(rows=[row()])), [])

    def test_exchange_rates(self):
        with mock.patch.dict(fx._cache, {}, clear=True), mock.patch.dict(fx._other, {"SEK": (None, time.time())}):
            got = checkup._check_rates(ctx(rows=[row(currency="USD"), row(currency="SEK")]))
        self.assertEqual(ids(got), {"rates_fixed", "rates_none"})
        with mock.patch.dict(fx._cache, {"USD": 0.9}), mock.patch.dict(fx._other, {"SEK": (0.09, time.time())}):
            self.assertEqual(checkup._check_rates(ctx(rows=[row(currency="USD"), row(currency="SEK")])), [])


class StatementTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.object(datafiles, "_SEARCH_DIRS", [self.dir])
        p.start()
        self.addCleanup(p.stop)
        datafiles.refresh()
        self.addCleanup(datafiles.refresh)

    def _t212(self, end):
        (self.dir / "trading212_statement.json").write_text(json.dumps({
            "overview": {"period": ["2025-01-01", end]}, "trades": [{"date": "2025-02-01 10:00"}],
            "positions": [{"symbol": "AAA"}], "_generated": "2026-07-01 10:00"}))

    def test_old_statement_of_an_open_account(self):
        self._t212("2026-06-30")
        got = checkup._check_statements(ctx(rows=[row(broker="Trading212")]))
        self.assertEqual(ids(got), {"statement_old:trading212_statement.json"})
        self.assertIn("30 Jun 2026", got[0]["title"])
        self.assertIn("uploaded on 1 Jul 2026", got[0]["why"])

    def test_recent_statement_or_closed_account(self):
        self._t212("2026-11-20")
        self.assertEqual(checkup._check_statements(ctx(rows=[row(broker="Trading212")])), [])
        self._t212("2026-01-31")
        self.assertEqual(checkup._check_statements(ctx(rows=[row(broker="Broker A")])), [])   # holds nothing there

    def test_degiro_csv_dates(self):
        sample = (Path(__file__).resolve().parents[1] / "data" / "degiro_account.sample.csv").read_text()
        (self.dir / "degiro_account.csv").write_text(sample)
        got = checkup._check_statements(ctx(rows=[row(broker="DeGiro")]))
        self.assertEqual(ids(got), {"statement_old:degiro_account.csv"})   # the sample ends in 2024


class SavingTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.object(datafiles, "DATA_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)

    def test_host_without_cloud_backup(self):
        with mock.patch.dict(os.environ, {"RENDER": "true", "GITHUB_TOKEN": "", "GITHUB_REPO": ""}):
            self.assertEqual(ids(checkup._check_saving(ctx())), {"cloud_backup_off"})

    def test_host_with_failing_cloud_backup(self):
        env = {"RENDER": "true", "GITHUB_TOKEN": "t", "GITHUB_REPO": "someone/data"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(github_sync, "last_result", {"enabled": True, "ok": False, "error": "HTTP 401"}):
            got = checkup._check_saving(ctx())
        self.assertEqual(ids(got), {"cloud_backup_failing"})
        self.assertIn("HTTP 401", got[0]["why"])
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(github_sync, "last_result", {"enabled": True, "ok": True}):
            self.assertEqual(checkup._check_saving(ctx()), [])

    def test_own_computer_backup_reminder(self):
        with mock.patch.dict(os.environ, {"RENDER": ""}), \
                mock.patch.object(datafiles, "has_own_data", return_value=True):
            self.assertEqual(ids(checkup._check_saving(ctx())), {"no_backup"})
            appstate.put("backup_downloaded_at", (TODAY - timedelta(days=200)).isoformat())
            self.assertEqual(ids(checkup._check_saving(ctx())), {"backup_old"})
            appstate.put("backup_downloaded_at", (TODAY - timedelta(days=3)).isoformat())
            self.assertEqual(checkup._check_saving(ctx()), [])
        with mock.patch.dict(os.environ, {"RENDER": ""}), \
                mock.patch.object(datafiles, "has_own_data", return_value=False):
            appstate.put("backup_downloaded_at", None)
            self.assertEqual(checkup._check_saving(ctx()), [])      # nothing of yours to back up yet

    def test_saved_copy_cannot_be_opened(self):
        with mock.patch.dict(os.environ, {"RENDER": ""}), \
                mock.patch.object(datafiles, "has_own_data", return_value=False):
            got = checkup._check_saving(ctx(vault_state={"present": True, "error": "InvalidToken"}))
            self.assertEqual(ids(got), {"vault_unreadable"})
            none = checkup._check_saving(ctx(vault_state={"present": True, "error": "no key material (x)"}))
            self.assertEqual(none, [])


class SafetyTest(unittest.TestCase):
    BASE = {"DASHBOARD_PASSWORD": "", "DASHBOARD_SECRET": "", "DATA_KEY": "", "ALLOW_NO_PASSWORD": "",
            "MCP_TOKEN": "", "RENDER": ""}

    def check(self, argv=("uvicorn",), **env):
        with mock.patch.dict(os.environ, {**self.BASE, **env}), mock.patch.object(checkup.sys, "argv", list(argv)):
            return ids(checkup._check_safety(ctx()))

    def test_all_fine(self):
        self.assertEqual(self.check(), set())
        self.assertEqual(self.check(DASHBOARD_PASSWORD="a long enough password", DASHBOARD_SECRET="s" * 40,
                                    MCP_TOKEN="t" * 40, RENDER="true"), set())

    def test_no_password(self):
        self.assertEqual(self.check(ALLOW_NO_PASSWORD="1"), {"no_password"})
        self.assertEqual(self.check(argv=["uvicorn", "--host", "0.0.0.0"]), {"network_no_password"})
        self.assertEqual(self.check(argv=["uvicorn", "--host", "127.0.0.1"]), set())

    def test_short_secrets(self):
        self.assertEqual(self.check(DASHBOARD_PASSWORD="short", RENDER="true"), {"short_password"})
        self.assertEqual(self.check(DASHBOARD_PASSWORD="short"), set())          # on your own computer
        self.assertEqual(self.check(DASHBOARD_SECRET="tooshort"), {"short_secret"})
        self.assertEqual(self.check(DATA_KEY="tooshort", DASHBOARD_SECRET="s" * 40), {"short_data_key"})
        self.assertEqual(self.check(MCP_TOKEN="abc"), {"short_mcp_token"})


class RulesTest(unittest.TestCase):
    def test_uk_notice_turns_on_on_6_april(self):
        with mock.patch.object(clock, "today", return_value=LAST_DAY):
            self.assertIsNone(rules.uk_notice())
        with mock.patch.object(clock, "today", return_value=NEW_YEAR):
            n = rules.uk_notice()
        self.assertEqual((n["rules_year"], n["current_year"]), (RULES, NEXT))
        self.assertEqual(n["text"], f"These figures use the {RULES} rules. The {NEXT} tax year has started: "
                                    "update the app, and check gov.uk for changes.")
        with mock.patch.object(rules, "UK_TAX_YEAR", "2026/27"):     # the wording the owner asked for
            self.assertEqual(rules.uk_notice(date(2027, 4, 6))["text"],
                             "These figures use the 2026/27 rules. The 2027/28 tax year has started: "
                             "update the app, and check gov.uk for changes.")

    def test_one_constant_for_the_uk_tax_year(self):
        from backend import advisors, paye
        self.assertEqual(advisors.TAX_YEAR, rules.UK_TAX_YEAR)
        self.assertIn(rules.UK_TAX_YEAR, paye.RULES_NOTE)
        with mock.patch.object(rules, "UK_TAX_YEAR", NEXT):
            self.assertIsNone(rules.uk_notice(NEW_YEAR))                # updated: no notice

    def test_country_page(self):
        page = {"code": "NL", "checked_on": "2026-11-26",
                "tax_year": {"label": "2026", "starts": "01-01", "ends": "12-31"}}
        self.assertIsNone(rules.country_notice(page, date(2026, 12, 31)))
        n = rules.country_notice(page, date(2027, 1, 1))
        self.assertTrue(n["ended"])
        self.assertIn("rules for 2026 and was checked on 26 Nov 2026; a new tax year has started", n["text"])
        uk = {"checked_on": "2026-11-26", "tax_year": {"label": "2026/27", "starts": "04-06", "ends": "04-05"}}
        self.assertIsNone(rules.country_notice(uk, date(2027, 4, 5)))
        self.assertTrue(rules.country_notice(uk, date(2027, 4, 6))["ended"])
        au = {"checked_on": "2026-11-27", "tax_year": {"label": "2026-27", "starts": "07-01", "ends": "06-30"}}
        self.assertIsNone(rules.country_notice(au, date(2027, 6, 30)))
        long_ago = {"checked_on": "2025-01-01", "tax_year": {"label": "2026", "starts": "01-01", "ends": "12-31"}}
        self.assertEqual(rules.country_notice(long_ago, date(2026, 3, 1))["old"], True)

    def test_broker_steps(self):
        self.assertIsNone(rules.broker_steps_notice("2026-11-26", date(2027, 11, 26)))
        self.assertIn("more than 18 months ago", rules.broker_steps_notice("2026-11-26", date(2028, 6, 1))["text"])
        self.assertIsNone(rules.broker_steps_notice(None, date(2030, 1, 1)))

    def _rules(self, country, today):
        with mock.patch("backend.prefs.value", side_effect=lambda k: country if k == "country" else ""), \
                mock.patch("backend.harvest.residence", return_value="UK" if country == "GB" else "OTHER"):
            return ids(checkup._check_rules(ctx(today=today)))

    def test_check_up_lists_only_your_own_country(self):
        from backend import catalog, countries
        gb, nl = countries.get("GB"), countries.get("NL")
        gb_end, nl_end = rules.tax_year_end(gb["tax_year"]), rules.tax_year_end(nl["tax_year"])
        steps_ok = rules.parse_day(catalog.raw()["researched_on"])
        with mock.patch.object(rules, "UK_TAX_YEAR", rules.uk_tax_year(gb_end)):   # figures and page agree
            self.assertEqual(self._rules("GB", gb_end), set())
            self.assertEqual(self._rules("GB", gb_end + timedelta(days=1)), {"uk_rules_old", "country_page_old:GB"})
        self.assertEqual(self._rules("NL", nl_end + timedelta(days=1)), {"country_page_old:NL"})
        self.assertEqual(self._rules("NL", nl_end), set())
        self.assertEqual(self._rules("", nl_end + timedelta(days=1)), set())
        self.assertNotIn("broker_steps_old", self._rules("", steps_ok + timedelta(days=30)))
        self.assertIn("broker_steps_old", self._rules("", steps_ok + timedelta(days=600)))

    def test_update_available(self):
        base = {"newer": True, "latest_date": "2027-01-10", "date": "2026-11-27", "how_to_update": "Run it.",
                "check_on": True, "error": None}
        with mock.patch.object(version, "status", return_value=base):
            got = checkup._check_version(ctx())
        self.assertEqual(ids(got), {"update_available"})
        self.assertEqual(got[0]["action"], "Run it.")
        with mock.patch.object(version, "status", return_value={**base, "newer": False}):
            self.assertEqual(checkup._check_version(ctx()), [])
        with mock.patch.object(version, "status", return_value={**base, "newer": None, "error": "offline"}):
            self.assertEqual(ids(checkup._check_version(ctx())), {"update_check_failed"})


class TaxCardNoticeTest(unittest.TestCase):
    """The cards that use the UK figures carry the notice once a later tax year started."""

    def test_uk_tax_year_card(self):
        from backend import uktax
        with mock.patch.object(sources, "trade_sources", return_value={}), \
                mock.patch("backend.connectors.trading212.get_dividends", return_value=[]), \
                mock.patch("backend.connectors.trading212.get_interest", return_value=[]), \
                mock.patch("backend.connectors.trading212.get_transactions", return_value=[]), \
                mock.patch.object(connections, "get_dividends", return_value=[]), \
                mock.patch.object(connections, "get_interest", return_value=[]):
            before = uktax.summary.uncached(LAST_DAY)
            after = uktax.summary.uncached(NEW_YEAR)
        self.assertIsNone(before["outdated"])
        self.assertEqual(after["tax_year"], NEXT)
        self.assertEqual(after["outdated"]["rules_year"], RULES)
        self.assertEqual(after["annual_exempt_gbp"], before["annual_exempt_gbp"])    # numbers unchanged

    def test_ways_to_save(self):
        from backend import advisors
        before = advisors.build(advisors.Inputs(rows=[], today=LAST_DAY))
        after = advisors.build(advisors.Inputs(rows=[], today=NEW_YEAR))
        self.assertIsNone(before["outdated"])
        self.assertEqual(after["outdated"]["current_year"], NEXT)
        self.assertEqual(after["rules_year"], RULES)
        other = advisors.build(advisors.Inputs(rows=[], today=NEW_YEAR, residence="NL"))
        self.assertIsNone(other["outdated"])                      # no UK figures in use

    def test_tax_loss_harvesting(self):
        from backend import harvest
        uk = {"net_gain_gbp": 0, "gains_gbp": 0, "to": f"{_Y + 2}-04-05", "tax_year": NEXT, "isa": None}
        out = harvest.suggest([], today=NEW_YEAR, res="UK", uk=uk, corr=False, sector_map={})
        self.assertEqual(out["outdated"]["rules_year"], RULES)
        out = harvest.suggest([], today=LAST_DAY, res="UK", uk=uk, corr=False, sector_map={})
        self.assertIsNone(out["outdated"])

    def test_take_home_pay(self):
        from backend import finances
        rows = [{"name": "Salary", "amount": 3000, "currency": "GBP", "monthly_eur": 3500, "tax": "before"}]
        with mock.patch.object(finances, "_residence", return_value="UK"):
            with mock.patch.object(clock, "today", return_value=NEW_YEAR):
                after = finances._apply_tax([dict(r) for r in rows], sample=False)
            with mock.patch.object(clock, "today", return_value=LAST_DAY):
                before = finances._apply_tax([dict(r) for r in rows], sample=False)
        self.assertEqual(after["outdated"]["current_year"], NEXT)
        self.assertIsNone(before["outdated"])
        self.assertEqual(after["yearly_eur"], before["yearly_eur"])

    def test_country_page_card(self):
        from backend import countries
        end = rules.tax_year_end(countries.get("GB")["tax_year"])
        with mock.patch.object(clock, "today", return_value=end + timedelta(days=1)):
            self.assertIsNotNone(countries.overview("GB")["outdated"])
        with mock.patch.object(clock, "today", return_value=end):
            self.assertIsNone(countries.overview("GB")["outdated"])

    def test_broker_steps_in_the_catalog(self):
        from backend import catalog
        checked = rules.parse_day(catalog.raw()["researched_on"])
        with mock.patch.object(clock, "today", return_value=checked + timedelta(days=30)):
            self.assertIsNone(catalog.view()["steps_outdated"])
        with mock.patch.object(clock, "today", return_value=checked + timedelta(days=600)):
            self.assertIsNotNone(catalog.view()["steps_outdated"])


try:
    from starlette.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None


@unittest.skipIf(TestClient is None, "starlette's test client is not available")
class EndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_mcp import sample_world
        sample_world(cls)
        from backend import main
        cls.main = main
        cls.client = TestClient(main.app, base_url="http://localhost:8000")

    def test_check_up_answers_quickly_with_sample_data(self):
        self.client.get("/api/portfolio")
        t = time.monotonic()
        r = self.client.get("/api/checkup")
        self.assertLess(time.monotonic() - t, 3)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("demo", ids(body["findings"]))
        self.assertTrue(body["help"].endswith("TROUBLESHOOTING.md"))
        for f in body["findings"]:
            self.assertTrue(f["title"] and f["why"] and f["action"], f)

    def test_uk_tax_card_after_6_april(self):
        from backend import cache
        cache.clear()
        with mock.patch.object(clock, "today", return_value=NEW_YEAR):
            body = self.client.get("/api/uk_tax").json()
        self.assertIn(f"These figures use the {RULES} rules", body["outdated"]["text"])
        cache.clear()


if __name__ == "__main__":
    unittest.main()
