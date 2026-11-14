"""Cloud persistence: each upload has to be made only once.

GitHub is replaced by an in-memory fake; threads and sleeps run inline.
"""

import base64
import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from backend import github_sync, vault


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.github.com", code, "err", {}, io.BytesIO(b"{}"))


class FakeGitHub:
    """Just enough of the GitHub API for github_sync."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, str] = {}
        self.commits: dict[str, dict] = {}
        self.branch: str | None = None      # commit sha of data-sync
        self.down = False                   # every call fails with a network error
        self.calls: list[tuple[str, str]] = []
        self._n = 0

    def _sha(self) -> str:
        self._n += 1
        return f"sha{self._n}"

    def stored(self) -> bytes | None:
        if self.branch is None:
            return None
        return self.blobs[self.trees[self.commits[self.branch]["tree"]]]

    def __call__(self, path, method="GET", body=None):
        self.calls.append((method, path))
        if self.down:
            raise urllib.error.URLError("offline")
        repo = "/repos/" + github_sync._repo()
        if method == "GET" and path.startswith(repo + "/contents/data/vault.enc"):
            if self.branch is None:
                raise http_error(404)
            blob_sha = self.trees[self.commits[self.branch]["tree"]]
            data = self.blobs[blob_sha]
            big = len(data) > 1_000_000
            return {"sha": blob_sha, "content": "" if big else base64.b64encode(data).decode()}
        if method == "GET" and path.startswith(repo + "/git/blobs/"):
            return {"content": base64.b64encode(self.blobs[path.rsplit("/", 1)[1]]).decode()}
        if method == "POST" and path == repo + "/git/blobs":
            sha = self._sha()
            self.blobs[sha] = base64.b64decode(body["content"])
            return {"sha": sha}
        if method == "POST" and path == repo + "/git/trees":
            sha = self._sha()
            self.trees[sha] = body["tree"][0]["sha"]
            return {"sha": sha}
        if method == "POST" and path == repo + "/git/commits":
            assert body["parents"] == []    # one commit only: the repo does not grow
            sha = self._sha()
            self.commits[sha] = body
            return {"sha": sha}
        if method == "PATCH" and path == repo + "/git/refs/heads/data-sync":
            if self.branch is None:
                raise http_error(422)
            self.branch = body["sha"]
            return {}
        if method == "POST" and path == repo + "/git/refs":
            self.branch = body["sha"]
            return {}
        raise AssertionError(f"unexpected call {method} {path}")


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.gh = FakeGitHub()
        self.spawned = []
        patches = [
            mock.patch.object(vault, "DATA_DIR", self.data),
            mock.patch.object(vault, "VAULT", self.data / "vault.enc"),
            mock.patch.object(vault, "_ITERATIONS", 1000),
            mock.patch.object(github_sync, "_req", self.gh),
            mock.patch.object(github_sync, "_spawn", self.spawned.append),
            mock.patch.object(github_sync, "_sleep", lambda s: None),
            mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t", "DATA_KEY": "k"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        github_sync._dirty = github_sync._running = False
        github_sync._pulled = None
        github_sync._restored_at = 0.0
        github_sync.last_result = {"enabled": False}
        self.addCleanup(self.tmp.cleanup)

    def run_spawned(self):
        while self.spawned:
            self.spawned.pop(0)()

    def write(self, name, text):
        (self.data / name).write_text(text)

    def stored_files(self) -> dict:
        """Decrypt what GitHub holds, as {name: text}."""
        blob = json.loads(self.gh.stored())
        f = vault._fernet(base64.b64decode(blob["salt"]))
        return {n: f.decrypt(base64.b64decode(v)).decode() for n, v in blob["files"].items()}

    def reboot(self):
        """A fresh disk with only the (older) vault from main, then boot."""
        for p in self.data.iterdir():
            p.unlink()
        github_sync._pulled = None
        return github_sync.restore()

    def test_upload_survives_restart(self):
        github_sync.restore()
        self.write("cash_accounts.json", '{"accounts": [1]}')
        github_sync.push_vault_async()
        self.run_spawned()
        self.assertTrue(github_sync.last_result["ok"])
        state = self.reboot()
        self.assertTrue(state["cloud"])
        self.assertEqual((self.data / "cash_accounts.json").read_text(), '{"accounts": [1]}')

    def test_edits_during_a_save_are_saved_too(self):
        github_sync.restore()
        self.write("cash_accounts.json", "a")
        github_sync.push_vault_async()
        self.write("watchlist.json", "b")     # before the worker ran
        github_sync.push_vault_async()
        self.assertEqual(len(self.spawned), 1)   # one worker, not two racing
        self.run_spawned()
        self.assertEqual(self.stored_files(), {"cash_accounts.json": "a", "watchlist.json": "b"})
        self.assertFalse(github_sync.pending())

    def test_saving_replaces_the_single_commit(self):
        github_sync.restore()
        for i in range(3):
            self.write("cash_accounts.json", str(i))
            github_sync.push_vault_async()
            self.run_spawned()
        self.assertEqual(self.stored_files(), {"cash_accounts.json": "2"})
        self.assertEqual(self.gh.commits[self.gh.branch]["parents"], [])

    def test_vault_over_one_megabyte_is_read_back(self):
        github_sync.restore()
        big = os.urandom(800_000).hex()     # ~1.6 MB once encrypted
        self.write("finances.json", big)
        github_sync.push_vault_async()
        self.run_spawned()
        self.reboot()
        self.assertEqual((self.data / "finances.json").read_text(), big)

    def test_unreadable_cloud_copy_pauses_saving(self):
        # an earlier save is in the cloud
        github_sync.restore()
        self.write("cash_accounts.json", "cloud")
        self.write("watchlist.json", "cloud")
        github_sync.push_vault_async()
        self.run_spawned()
        # main's older vault is on disk; GitHub is unreachable at boot
        self.write("cash_accounts.json", "old")
        self.write("watchlist.json", "old")
        vault.encrypt()
        older_vault = vault.VAULT.read_bytes()
        for p in self.data.iterdir():
            p.unlink()
        vault.VAULT.write_bytes(older_vault)
        self.gh.down = True
        github_sync._pulled = None
        github_sync.restore()
        self.assertEqual((self.data / "cash_accounts.json").read_text(), "old")
        self.assertTrue(github_sync.last_result["paused"])
        # an edit now must not overwrite the newer cloud copy
        time.sleep(0.01)
        self.write("watchlist.json", "edited")
        github_sync.push_vault_async()
        retry = self.spawned.pop(0)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.stored_files()["cash_accounts.json"], "cloud")
        # GitHub is back: cloud copy wins for untouched files, the edit is kept, then saved
        self.gh.down = False
        retry()
        self.run_spawned()
        self.assertEqual((self.data / "cash_accounts.json").read_text(), "cloud")
        self.assertEqual((self.data / "watchlist.json").read_text(), "edited")
        self.assertEqual(self.stored_files(),
                         {"cash_accounts.json": "cloud", "watchlist.json": "edited"})

    def test_failed_save_reports_and_retries(self):
        github_sync.restore()
        self.write("cash_accounts.json", "a")
        self.gh.down = True
        github_sync.push_vault_async()
        self.run_spawned()
        self.assertFalse(github_sync.last_result["ok"])
        self.assertTrue(github_sync.pending())      # still queued for the next edit
        self.gh.down = False
        github_sync.push_vault_async()
        self.run_spawned()
        self.assertTrue(github_sync.last_result["ok"])
        self.assertFalse(github_sync.pending())

    def test_bad_token_is_explained(self):
        self.gh.down = False
        with mock.patch.object(github_sync, "_req", side_effect=http_error(401)):
            github_sync.restore()
        self.assertIn("rejected the token", github_sync.last_result["error"])

    def test_no_token_changes_nothing(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": ""}):
            github_sync.restore()
            self.write("cash_accounts.json", "a")
            github_sync.push_vault_async()
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.gh.calls, [])
        self.assertFalse(github_sync.last_result["enabled"])


if __name__ == "__main__":
    unittest.main()
