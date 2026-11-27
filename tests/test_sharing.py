"""What a friend's own copy needs: no sample data mixed into real data,
automatic ISIN lookup, a password on any host, and tax cards that can be off.

No network: Yahoo is replaced with small fakes.
"""

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import datafiles, harvest, instruments


class SampleDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        (self.data / "degiro_account.sample.csv").write_text("sample")
        for p in (mock.patch.object(datafiles, "_SEARCH_DIRS", [self.data]),
                  mock.patch.object(datafiles, "DATA_DIR", self.data),
                  mock.patch.dict(os.environ, {k: "" for k in os.environ if k.startswith("T212_")})):
            p.start()
            self.addCleanup(p.stop)
        datafiles.refresh()
        self.addCleanup(datafiles.refresh)

    def test_samples_only_until_you_add_your_own(self):
        path, real = datafiles.resolve("degiro_account.csv")
        self.assertEqual((path.name, real), ("degiro_account.sample.csv", False))
        self.assertTrue(datafiles.demo())
        # only a Trading 212 statement of your own: no sample DeGiro next to it
        datafiles.save_file("trading212_statement.json", b"{}")
        path, real = datafiles.resolve("degiro_account.csv")
        self.assertFalse(real)
        self.assertFalse(path.exists())
        self.assertFalse(datafiles.demo())

    def test_a_broker_key_alone_ends_demo_mode(self):
        with mock.patch.dict(os.environ, {"T212_ISA_API_KEY": "k"}):
            datafiles.refresh()
            self.assertFalse(datafiles.demo())


class TickerLookupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (mock.patch.object(instruments, "_FOUND_FILE", Path(self.tmp.name) / "found.json"),
                  mock.patch.object(instruments, "_found", None),
                  mock.patch.object(instruments, "_failed", {}),
                  mock.patch.object(instruments, "_ccy_cache", [None, None]),
                  mock.patch.dict(os.environ, {"TICKER_LOOKUP": "1"})):
            p.start()
            self.addCleanup(p.stop)

    def fake_yahoo(self, listings: dict[str, str | None]):
        """Yahoo search returns `listings` (symbol -> quote currency or None)."""
        def search(isin, currency):
            return instruments._pick(list(listings), currency, lambda s: listings[s])
        return mock.patch.object(instruments, "_search", side_effect=search)

    def test_known_isin_needs_no_lookup(self):
        with mock.patch.object(instruments, "_search") as s:
            self.assertEqual(instruments.ticker_for("US0378331005"), "AAPL")
            self.assertEqual(instruments.ticker_for("US34619R1023"), "")   # known: no price
        s.assert_not_called()

    def test_unknown_isin_is_looked_up_once_and_its_currency_kept(self):
        isin = "IE00B4L5Y983"          # iShares Core MSCI World
        with self.fake_yahoo({"IWDA.L": "USD", "IWDA.AS": "EUR"}) as s:
            self.assertEqual(instruments.ticker_for(isin), "IWDA.L")
            self.assertEqual(instruments.ticker_for(isin), "IWDA.L")
        self.assertEqual(s.call_count, 1)
        self.assertEqual(instruments.price_currency("IWDA.L"), "USD")   # not GBP from ".L"
        # kept on disk for the next start
        instruments._found = None
        self.assertEqual(instruments.ticker_for(isin), "IWDA.L")

    def test_traded_currency_picks_the_listing(self):
        with self.fake_yahoo({"IWDA.L": "USD", "IWDA.AS": "EUR"}):
            self.assertEqual(instruments.ticker_for("IE00B4L5Y983", "EUR"), "IWDA.AS")

    def test_nothing_found_is_not_retried_at_once(self):
        with self.fake_yahoo({"XYZ.SG": "SEK"}) as s:     # a currency the app cannot convert
            self.assertEqual(instruments.ticker_for("IE00B4L5Y983"), "")
            self.assertEqual(instruments.ticker_for("IE00B4L5Y983"), "")
        self.assertEqual(s.call_count, 1)

    def test_off_switch_and_invalid_isins(self):
        with self.fake_yahoo({"X": "USD"}) as s:
            self.assertEqual(instruments.ticker_for("NOTANISIN123"), "")
            with mock.patch.dict(os.environ, {"TICKER_LOOKUP": "0"}):
                self.assertEqual(instruments.ticker_for("IE00B4L5Y983"), "")
        s.assert_not_called()


def request(host="127.0.0.1", headers=None):
    return SimpleNamespace(client=SimpleNamespace(host=host), headers=headers or {})


class LoginTest(unittest.TestCase):
    def setUp(self):
        from backend import main
        self.main = main
        main._fails.clear()
        self.addCleanup(main._fails.clear)

    def test_password_required_on_a_host(self):
        m = self.main
        with mock.patch.object(m, "_DASH_PASS", None), \
                mock.patch.dict(os.environ, {"RENDER": "", "ALLOW_NO_PASSWORD": ""}):
            self.assertFalse(m._needs_password(request()))                      # your own computer
            self.assertTrue(m._needs_password(request("10.0.0.5")))             # another device
            self.assertTrue(m._needs_password(request(headers={"x-forwarded-for": "1.2.3.4"})))
            with mock.patch.dict(os.environ, {"RENDER": "true"}):
                self.assertTrue(m._needs_password(request()))
                with mock.patch.dict(os.environ, {"ALLOW_NO_PASSWORD": "1"}):
                    self.assertFalse(m._needs_password(request()))
        with mock.patch.object(m, "_DASH_PASS", "pw"):
            self.assertFalse(m._needs_password(request("10.0.0.5")))

    def test_wrong_passwords_are_limited(self):
        m = self.main
        for _ in range(m._FAIL_PER_CLIENT):
            self.assertFalse(m._login_blocked("a", 1000.0))
            m._fails.setdefault("a", []).append(1000.0)
        self.assertTrue(m._login_blocked("a", 1001.0))
        self.assertFalse(m._login_blocked("b", 1001.0))              # someone else
        self.assertFalse(m._login_blocked("a", 1000.0 + m._FAIL_WINDOW))   # after 15 minutes
        # many visitors together hit the total cap
        for i in range(m._FAIL_TOTAL):
            m._fails.setdefault(f"v{i}", []).append(2000.0)
        self.assertTrue(m._login_blocked("new", 2001.0))

    def test_forwarded_address_identifies_the_visitor(self):
        r = request("10.0.0.1", {"x-forwarded-for": "1.2.3.4, 10.0.0.1"})
        self.assertEqual(self.main._client_key(r), "1.2.3.4")


class OtherWebsitesTest(unittest.TestCase):
    """Requests another website makes through the user's browser."""

    def refused(self, method="GET", password=None, **headers):
        from backend import main as m
        req = SimpleNamespace(method=method, headers=headers)
        with mock.patch.object(m, "_DASH_PASS", password), \
                mock.patch.dict(os.environ, {"ALLOWED_HOSTS": "my.example.org"}):
            return m._refused(req)

    def test_a_change_from_another_site_is_refused(self):
        self.assertIsNone(self.refused("POST", host="127.0.0.1:8000", origin="http://127.0.0.1:8000"))
        self.assertIsNone(self.refused("POST", host="127.0.0.1:8000"))            # curl, scripts
        self.assertIsNotNone(self.refused("POST", host="127.0.0.1:8000", origin="https://evil.example"))
        self.assertIsNotNone(self.refused("DELETE", host="127.0.0.1:8000", origin="null"))
        self.assertIsNone(self.refused("GET", host="127.0.0.1:8000", origin="https://evil.example"))
        # behind a proxy that passes the public name along
        self.assertIsNone(self.refused("POST", "pw", host="10.0.0.2:10000", origin="https://d.onrender.com",
                                       **{"x-forwarded-host": "d.onrender.com"}))
        self.assertIsNotNone(self.refused("POST", "pw", host="d.onrender.com", origin="https://evil.example"))

    def test_without_a_password_only_local_addresses_are_answered(self):
        for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "192.168.1.20:8000",
                     "nas:8000", "nas.local", "my.example.org"):
            self.assertIsNone(self.refused(host=host), host)
        self.assertIsNotNone(self.refused(host="evil.example:8000"))
        self.assertIsNotNone(self.refused(host="127.0.0.1.evil.example"))
        self.assertIsNone(self.refused(password="pw", host="d.onrender.com"))    # a password: any name


class TaxResidenceTest(unittest.TestCase):
    def test_other_countries_turn_the_tax_cards_off(self):
        from backend import main
        for value, want in (("", "UK"), ("uk", "UK"), ("GB", "UK"), ("NL", "NL"),
                            ("NONE", "OTHER"), ("DE", "OTHER")):
            with mock.patch.dict(os.environ, {"TAX_RESIDENCE": value}):
                self.assertEqual(harvest.residence(), want)
        with mock.patch.dict(os.environ, {"TAX_RESIDENCE": "NONE"}):
            self.assertFalse(harvest.suggest([])["applies"])
            self.assertEqual(main.uk_tax(), {"applies": False})




class WithoutTheConnectorPackageTest(unittest.TestCase):
    """The dashboard does not depend on Claude: without the optional `mcp`
    package it still starts, only the connector is off."""

    def test_the_app_starts_without_mcp(self):
        import subprocess
        import sys
        from pathlib import Path
        code = ("import sys; sys.modules['mcp'] = None\n"
                "from backend import main\n"
                "assert main.mcp_server is None\n"
                "assert not any(getattr(r, 'path', '') == '/mcp' for r in main.app.routes)\n"
                "print('ok', len(main.app.routes))\n")
        env = {**os.environ, "WARMUP": "0", "KEEPALIVE_SELF": "0", "TICKER_LOOKUP": "0"}
        out = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                             env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-1500:])
        self.assertTrue(out.stdout.strip().startswith("ok"), out.stdout)


if __name__ == "__main__":
    unittest.main()
