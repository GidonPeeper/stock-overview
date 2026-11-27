"""What a first-time user runs into, found by following the documents on a
fresh copy (made-up files only). Each test names the step that went wrong."""

import fnmatch
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class WrongFileUploadTest(unittest.TestCase):
    """Settings → Statements: the wrong file in a broker's slot. Before, a
    CSV in the Trade Republic or Trading 212 slot answered "Not valid JSON —
    see the .sample file for the format": the user never chose JSON and has
    no idea what the .sample file is."""

    def setUp(self):
        from starlette.testclient import TestClient

        from backend import datafiles, github_sync, main

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        for p in (mock.patch.object(datafiles, "_SEARCH_DIRS", [self.data]),
                  mock.patch.object(datafiles, "DATA_DIR", self.data),
                  mock.patch.object(github_sync, "push_vault_async", lambda *a, **k: None),
                  mock.patch.object(main, "_DASH_PASS", "")):
            p.start()
            self.addCleanup(p.stop)
        datafiles.refresh()
        self.addCleanup(datafiles.refresh)
        self.client = TestClient(main.app, base_url="http://localhost:8000")

    def upload(self, kind: str, name: str, body: bytes):
        return self.client.post(f"/api/upload/{kind}", files={"file": (name, body, "text/csv")})

    DEGIRO_CSV = (b"Datum,Tijd,Valutadatum,Product,ISIN,Omschrijving,FX,Mutatie,,Saldo,,Order Id\n"
                  b'02-01-2025,09:30,02-01-2025,,,iDEAL storting,,EUR,"2000,00",EUR,"2000,00",\n')

    def test_a_degiro_file_in_the_trade_republic_slot_says_what_to_upload(self):
        r = self.upload("trade_republic", "Account.csv", self.DEGIRO_CSV)
        self.assertEqual(r.status_code, 400)
        detail = r.json()["detail"]
        self.assertNotIn("JSON", detail)
        self.assertNotIn(".sample", detail)
        self.assertIn("transaction export (CSV)", detail)
        self.assertIn("Profile → Account statements", detail)
        self.assertFalse((self.data / "trades_trade_republic.json").exists())

    def test_a_degiro_file_in_the_trading_212_slot_says_what_to_upload(self):
        r = self.upload("t212_statement", "Account.csv", self.DEGIRO_CSV)
        self.assertEqual(r.status_code, 400)
        detail = r.json()["detail"]
        self.assertNotIn("JSON", detail)
        self.assertIn("History export (CSV)", detail)
        self.assertFalse((self.data / "trading212_statement.json").exists())

    def test_an_empty_file_is_called_empty(self):
        for kind in ("degiro", "trade_republic", "t212_statement"):
            r = self.upload(kind, "empty.csv", b"")
            self.assertEqual(r.status_code, 400, kind)
            self.assertIn("empty", r.json()["detail"], kind)

    def test_the_apps_own_json_still_works(self):
        r = self.upload("trade_republic", "trades.json",
                        (ROOT / "data" / "trades_trade_republic.sample.json").read_bytes())
        self.assertEqual(r.status_code, 200, r.text)


class PlainMessagesTest(unittest.TestCase):
    def test_a_wrong_trading_212_key_says_it_once(self):
        # Before: "... (HTTP 401). Check you copied all of it, or make a new
        # one. Check you copied the key and the secret (the secret is shown once)."
        from backend.connectors.live import http_message
        msg = http_message("Trading 212", 401, {401: "Check you copied both the key and the secret"})
        self.assertEqual(msg.count("Check you copied"), 1, msg)
        self.assertIn("or make a new one", msg)
        self.assertIn("did not accept the key", http_message("Wise", 401))

    def test_the_password_page_helps_someone_on_their_own_computer(self):
        # Opened from a phone on the same Wi-Fi without a password set. Before,
        # it only said "In your host's settings (on Render: ...)".
        from backend import main
        self.assertIn(".env", main._SETUP_PAGE)
        self.assertIn("Render", main._SETUP_PAGE)

    def test_saved_keys_are_described_as_they_are_stored(self):
        # Locally the key sits in data/connections.json; it is only encrypted in
        # the cloud backup. Before: "kept on your own server (encrypted in the
        # backup vault)", also on a laptop with no backup at all.
        for f in ("frontend/accounts.js", "backend/broker_catalog.json"):
            self.assertNotIn("backup vault", (ROOT / f).read_text(), f)

    def test_removing_a_positions_file_does_not_talk_about_a_key(self):
        js = (ROOT / "frontend" / "accounts.js").read_text()
        self.assertIn("data-csv", js)
        self.assertIn("Its positions are removed from this app", js)


class DataFolderIsIgnoredTest(unittest.TestCase):
    """README: "Everything you add is saved in the data folder, which git
    ignores." Before, data/settings.json (country, currency, time zone,
    colours) was not ignored, so `git status` listed it and `git add .`
    would commit it."""

    def ignored(self, path: str) -> bool:
        out = False
        for line in (ROOT / ".gitignore").read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            neg = line.startswith("!")
            pat = line[1:] if neg else line
            if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(path, pat.rstrip("/") + "/*"):
                out = not neg
        return out

    def test_every_file_the_app_writes_is_ignored_and_the_samples_are_not(self):
        written = ["settings.json", "cash_accounts.json", "connections.json", "manual_holdings.json",
                   "finances.json", "watchlist.json", "degiro_account.csv", "trades_trade_republic.json",
                   "income_trade_republic.json", "trading212_statement.json", "vault.enc",
                   "bank_link_states.json", "sectors.json", "t212_instruments.json", "tickers_found.json",
                   ".degiro_account.csv.123.tmp"]
        for name in written:
            self.assertTrue(self.ignored("data/" + name), name)
        for sample in sorted(p.name for p in (ROOT / "data").glob("*.sample.*")):
            self.assertFalse(self.ignored("data/" + sample), sample)


if __name__ == "__main__":
    unittest.main()
