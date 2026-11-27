"""Back up and restore (backend/backup.py): a backup, wiped data and a
restore give back identical files; every bad file is refused before anything
changes; a restore that fails half-way puts the old files back. Made-up data
in temporary folders only."""

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock

from backend import appstate, backup, cash, datafiles, github_sync, prefs

ROOT = Path(__file__).resolve().parents[1]


def own_files() -> dict[str, bytes]:
    return {
        "degiro_account.csv": (ROOT / "data" / "degiro_account.sample.csv").read_bytes(),
        "cash_accounts.json": json.dumps({"accounts": [{"name": "Test saver", "balance": 1234.5,
                                                        "currency": "EUR", "type": "savings", "rate": 2}]}).encode(),
        "settings.json": json.dumps({"currency": "USD"}).encode(),
        "connections.json": json.dumps({"connections": [{"id": "c_1", "label": "Example bank",
                                                         "credentials": {"token": "made-up"}}]}).encode(),
        "tickers_found.json": json.dumps({"XX0000000001": {"ticker": "XMPL", "currency": "EUR"}}).encode(),
    }


def make_zip(entries: dict[str, bytes], manifest: dict | None | bool = True, **zkw) -> bytes:
    """A zip by hand. manifest True = a correct one for the entries."""
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            if manifest is True:
                manifest = {"app": "stock-overview", "format": 1, "created": "2026-11-27T10:00:00",
                            "files": {n: {"size": len(b), "sha256": hashlib.sha256(b).hexdigest()}
                                      for n, b in entries.items() if "/" not in n}}
            if manifest:
                zf.writestr(backup.MANIFEST, json.dumps(manifest))
            for n, b in entries.items():
                zf.writestr(n, b)
    return buf.getvalue()


class _DataFolder(unittest.TestCase):
    """A temporary data folder the whole app reads and writes."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.object(datafiles, "DATA_DIR", self.dir),
                  mock.patch.object(datafiles, "_SEARCH_DIRS", [self.dir]),
                  mock.patch.object(prefs, "DATA_DIR", self.dir),
                  mock.patch.object(prefs, "_cache", [None, None]),
                  mock.patch.object(cash, "DATA_DIR", self.dir),
                  mock.patch.object(github_sync, "push_vault_async")):
            self.pushed = p.start()
            self.addCleanup(p.stop)
        datafiles.refresh()
        self.addCleanup(datafiles.refresh)

    def write(self, files):
        for n, b in files.items():
            (self.dir / n).write_bytes(b)
        datafiles.refresh()

    def snapshot(self):
        return {p.name: p.read_bytes() for p in self.dir.iterdir() if p.is_file()}


class BackupTest(_DataFolder):
    # ----------------------------------------------------------- round trip
    def test_backup_wipe_restore_gives_identical_files(self):
        self.write(own_files())
        (self.dir / "degiro_account.sample.csv").write_text("sample")       # never in a backup
        (self.dir / "vault.enc").write_text("ciphertext")                   # nor the cloud copy
        blob, names = backup.make()
        self.assertEqual(set(names), set(own_files()))
        before = {n: (self.dir / n).read_bytes() for n in names}
        for n in names:
            (self.dir / n).unlink()
        datafiles.refresh()
        out = backup.restore(blob)
        self.assertEqual({n: (self.dir / n).read_bytes() for n in names}, before)
        self.assertEqual(out["restored"], sorted(names))
        self.assertIsNone(out["safety_copy"])          # nothing was there to set aside
        self.pushed.assert_called()                    # on a host: the cloud copy is saved again

    def test_restore_keeps_the_replaced_files_and_reloads_them(self):
        self.write(own_files())
        blob, _ = backup.make()
        self.write({"cash_accounts.json": b'{"accounts": []}', "settings.json": b'{"currency": "GBP"}',
                    "finances.json": b'{"loans": []}'})
        self.assertEqual(prefs.value("currency"), "GBP")
        out = backup.restore(blob)
        kept = self.dir / out["safety_copy"].split("/", 1)[1]
        self.assertEqual((kept / "cash_accounts.json").read_bytes(), b'{"accounts": []}')
        self.assertEqual((kept / "finances.json").read_bytes(), b'{"loans": []}')
        self.assertFalse((self.dir / "finances.json").exists())       # not in the backup: set aside
        self.assertEqual(out["set_aside"], ["finances.json"])
        self.assertEqual(prefs.value("currency"), "USD")                # read again, not from memory
        self.assertEqual(cash.load()["accounts"][0]["name"], "Test saver")
        self.assertEqual([p.name for p in self.dir.iterdir() if p.name.startswith(".restoring")], [])

    def test_preview_changes_nothing(self):
        self.write(own_files())
        blob, _ = backup.make()
        self.write({"finances.json": b'{"loans": []}'})
        before = self.snapshot()
        p = backup.preview(blob)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("Connected accounts, with their keys", [f["label"] for f in p["files"]])
        self.assertTrue(all(f["replaces"] for f in p["files"]))
        self.assertEqual([f["name"] for f in p["set_aside"]], ["finances.json"])

    def test_nothing_to_back_up_in_demo_mode(self):
        with self.assertRaises(backup.BackupError):
            backup.make()

    def test_info_and_download_time(self):
        self.write(own_files())
        self.assertIsNone(backup.info()["last_downloaded"])
        backup.mark_downloaded()
        self.assertTrue(appstate.get("backup_downloaded_at"))
        self.assertIn("connections.json", [f["name"] for f in backup.info()["files"]])

    # ------------------------------------------------------------ refused
    def refused(self, blob, words):
        self.write(own_files())
        before = self.snapshot()
        with self.assertRaises(backup.BackupError) as cm:
            backup.restore(blob)
        self.assertIn(words, str(cm.exception))
        self.assertEqual(self.snapshot(), before, "a refused restore changed a file")
        self.assertEqual([p for p in self.dir.iterdir() if p.is_dir()], [])

    def test_not_a_zip(self):
        self.refused(b"this is not a zip", "not a zip file")

    def test_too_large(self):
        with mock.patch.object(backup, "MAX_ZIP", 100):
            self.refused(make_zip(own_files()), "larger than")

    def test_no_manifest(self):
        self.refused(make_zip(own_files(), manifest=None), "stock-overview-backup.json is missing")

    def test_unknown_file(self):
        self.refused(make_zip({"cash_accounts.json": b"{}", "evil.sh": b"rm -rf /"}), "does not know")

    def test_paths_outside_the_data_folder(self):
        for name in ("../cash_accounts.json", "data/cash_accounts.json", "/etc/cash_accounts.json",
                     "..\\cash_accounts.json", "C:cash_accounts.json", ".env"):
            with self.subTest(name):
                self.refused(make_zip({"cash_accounts.json": b"{}", name: b"{}"}), "")

    def test_duplicate_names(self):
        buf = io.BytesIO()
        body = b'{"accounts": []}'
        manifest = {"app": "stock-overview", "format": 1, "created": "x",
                    "files": {"cash_accounts.json": {"size": len(body), "sha256": hashlib.sha256(body).hexdigest()}}}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr(backup.MANIFEST, json.dumps(manifest))
                zf.writestr("cash_accounts.json", body)
                zf.writestr("cash_accounts.json", b'{"accounts": [{"name": "sneaky"}]}')
        self.refused(buf.getvalue(), "twice")

    def test_password_protected(self):
        blob = bytearray(make_zip({"cash_accounts.json": b"{}"}))
        cd = blob.rfind(b"PK\x01\x02")          # the last central-directory entry: set "encrypted"
        blob[cd + 8] |= 0x1
        self.refused(bytes(blob), "password-protected")

    def test_oversized_and_zip_bomb(self):
        big = b"{" + b" " * 5000 + b"}"           # compresses to almost nothing
        with mock.patch.object(backup, "MAX_FILE", 1000):
            self.refused(make_zip({"settings.json": big}), "too large")

    def test_checksum_and_list_mismatch(self):
        files = {"cash_accounts.json": b'{"accounts": []}'}
        bad = {"app": "stock-overview", "format": 1, "created": "x",
               "files": {"cash_accounts.json": {"size": 16, "sha256": "0" * 64}}}
        self.refused(make_zip(files, manifest=bad), "checksum")
        short = {"app": "stock-overview", "format": 1, "created": "x", "files": {}}
        self.refused(make_zip(files, manifest=short), "incomplete")

    def test_damaged_or_wrong_shape_content(self):
        self.refused(make_zip({"cash_accounts.json": b'{"accounts": [1, 2'}), "not valid JSON")
        self.refused(make_zip({"cash_accounts.json": b"[]"}), "not what the app writes")
        self.refused(make_zip({"cash_accounts.json": b'{"accounts": "x"}'}), "not a list of entries")
        self.refused(make_zip({"degiro_account.csv": b"\xff\xfe\x00junk"}), "not readable text")
        self.refused(make_zip({"degiro_account.csv": b"just,three,columns\n"}), "not a DEGIRO account statement")

    def test_wrong_manifest(self):
        self.refused(make_zip({"settings.json": b"{}"}, manifest={"app": "other", "files": {}}), "not right")
        newer = {"app": "stock-overview", "format": 99, "created": "x",
                 "files": {"settings.json": {"size": 2, "sha256": hashlib.sha256(b"{}").hexdigest()}}}
        self.refused(make_zip({"settings.json": b"{}"}, manifest=newer), "newer version")
        self.refused(make_zip({}), "no data files")

    def test_half_a_restore_is_put_back(self):
        self.write(own_files())
        blob, _ = backup.make()
        self.write({"cash_accounts.json": b'{"accounts": []}'})
        before = self.snapshot()
        real = os.replace
        calls = []

        def flaky(src, dst):
            calls.append(dst)
            if len(calls) == 8:                       # half-way through moving the new files in
                raise OSError(28, "No space left on device")
            return real(src, dst)
        with mock.patch.object(backup.os, "replace", flaky):
            with self.assertRaises(backup.BackupError) as cm:
                backup.restore(blob)
        self.assertIn("put back as they were", str(cm.exception))
        self.assertEqual(self.snapshot(), before)


try:
    from starlette.testclient import TestClient
except Exception:  # pragma: no cover
    TestClient = None


@unittest.skipIf(TestClient is None, "starlette's test client is not available")
class BackupApiTest(_DataFolder):
    """The endpoints: behind the same checks as every other write."""

    @classmethod
    def setUpClass(cls):
        from tests.test_mcp import sample_world
        sample_world(cls)
        from backend import main
        cls.client = TestClient(main.app, base_url="http://localhost:8000")

    def test_download_check_and_restore(self):
        r = self.client.get("/api/backup")
        self.assertEqual(r.status_code, 404)                       # example data only
        self.assertIn("nothing of yours", r.json()["detail"])
        self.write(own_files())
        r = self.client.get("/api/backup")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/zip")
        self.assertIn("attachment", r.headers["content-disposition"])
        self.assertTrue(self.client.get("/api/backup/info").json()["last_downloaded"])
        blob = r.content
        origin = {"Origin": "http://localhost:8000"}
        r = self.client.post("/api/backup/check", files={"file": ("b.zip", blob, "application/zip")}, headers=origin)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["files"]), len(own_files()))
        r = self.client.post("/api/backup/restore", files={"file": ("b.zip", blob, "application/zip")}, headers=origin)
        self.assertEqual(r.status_code, 400)                       # not confirmed
        r = self.client.post("/api/backup/restore", files={"file": ("b.zip", blob, "application/zip")},
                             data={"confirm": "yes"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)                       # from another website
        r = self.client.post("/api/backup/restore", files={"file": ("b.zip", b"junk", "application/zip")},
                             data={"confirm": "yes"}, headers=origin)
        self.assertEqual(r.status_code, 400)
        self.assertIn("not a zip", r.json()["detail"])
        r = self.client.post("/api/backup/restore", files={"file": ("b.zip", blob, "application/zip")},
                             data={"confirm": "yes"}, headers=origin)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["safety_copy"].startswith("data/before-restore-"))

    def test_backup_needs_the_login_when_there_is_a_password(self):
        from backend import main
        with mock.patch.object(main, "_DASH_PASS", "a password"):
            client = TestClient(main.app, base_url="http://localhost:8000")
            self.assertEqual(client.get("/api/backup").status_code, 401)
            r = client.post("/api/backup/restore", files={"file": ("b.zip", b"x", "application/zip")},
                            data={"confirm": "yes"}, headers={"Origin": "http://localhost:8000"})
            self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
