"""Updating without a programmer: the version read from .git (version.py),
the optional daily check for a newer version (a faked GitHub; never the
network), the switch in prefs, and `./run.sh update` on scratch git
repositories on this disk (no network either)."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import zlib
from pathlib import Path
from unittest import mock

from backend import appstate, datafiles, prefs, version

ROOT = Path(__file__).resolve().parents[1]
SHA_A = "a" * 40
SHA_B = "b" * 40


def fake_git(root: Path, sha: str = SHA_A, when: int = 1795737600, packed: bool = False,
             url: str = "git@github.com:Someone/stock-fork.git") -> None:
    """A minimal .git folder: HEAD on main, one commit object (loose)."""
    g = root / ".git"
    (g / "refs" / "heads").mkdir(parents=True)
    (g / "refs" / "remotes" / "origin").mkdir(parents=True)
    (g / "HEAD").write_text("ref: refs/heads/main\n")
    if packed:
        (g / "packed-refs").write_text(f"# pack-refs with: peeled\n{sha} refs/heads/main\n")
    else:
        (g / "refs" / "heads" / "main").write_text(sha + "\n")
    (g / "refs" / "remotes" / "origin" / "HEAD").write_text("ref: refs/remotes/origin/main\n")
    (g / "config").write_text(f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n')
    body = (f"tree {'c' * 40}\nauthor A <a@example.com> {when} +0000\n"
            f"committer A <a@example.com> {when} +0000\n\nA commit\n").encode()
    obj = g / "objects" / sha[:2] / sha[2:]
    obj.parent.mkdir(parents=True)
    obj.write_bytes(zlib.compress(b"commit %d\0" % len(body) + body))


class ReadVersionTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": "", "RENDER_GIT_REPO_SLUG": "", "RENDER_GIT_BRANCH": ""})
        p.start()
        self.addCleanup(p.stop)

    def test_commit_date_and_repo_from_the_git_folder(self):
        fake_git(self.dir)
        v = version.read(self.dir)
        self.assertEqual((v["commit"], v["short"], v["date"]), (SHA_A, "aaaaaaa", "2026-11-27"))
        self.assertEqual((v["repo"], v["default_branch"], v["branch"]), ("Someone/stock-fork", "main", "main"))

    def test_packed_ref_and_https_remote(self):
        fake_git(self.dir, packed=True, url="https://github.com/Someone/stock-overview.git")
        v = version.read(self.dir)
        self.assertEqual((v["commit"], v["repo"]), (SHA_A, "Someone/stock-overview"))

    def test_this_repository(self):
        if not (ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        v = version.read(ROOT)
        self.assertEqual(len(v["commit"] or ""), 40)
        self.assertRegex(v["date"] or "", r"^\d{4}-\d\d-\d\d$")

    def test_render_and_no_git(self):
        v = version.read(self.dir)
        self.assertEqual((v["commit"], v["source"], v["repo"]), (None, "unknown", version.DEFAULT_REPO))
        with mock.patch.dict(os.environ, {"RENDER_GIT_COMMIT": SHA_B, "RENDER_GIT_REPO_SLUG": "Someone/fork"}):
            v = version.read(self.dir)
        self.assertEqual((v["short"], v["source"], v["repo"], v["date"]), ("bbbbbbb", "render", "Someone/fork", None))


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def github_answer(sha=SHA_B, day="2027-01-10"):
    return FakeResponse(json.dumps({"sha": sha, "commit": {"committer": {"date": day + "T09:00:00Z"}}}).encode())


class UpdateCheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        for p in (mock.patch.object(datafiles, "DATA_DIR", self.dir), mock.patch.object(prefs, "DATA_DIR", self.dir),
                  mock.patch.object(prefs, "_cache", [None, None]), mock.patch.object(version, "_last", [None]),
                  mock.patch.object(version, "_info", {"commit": SHA_A, "short": "aaaaaaa", "date": "2026-11-27",
                                                       "branch": "main", "source": "git", "repo": "Someone/repo",
                                                       "default_branch": "main"})):
            p.start()
            self.addCleanup(p.stop)

    def test_off_by_default_and_never_asks(self):
        self.assertFalse(prefs.value("update_check"))
        with mock.patch("urllib.request.urlopen") as net:
            self.assertFalse(version.check_if_due())
            st = version.status()
        net.assert_not_called()
        self.assertIsNone(st["newer"])
        self.assertFalse(st["check_on"])

    def test_switch_is_validated(self):
        prefs.update({"update_check": True})
        self.assertIs(prefs.value("update_check"), True)
        prefs.update({"update_check": "off"})
        self.assertIs(prefs.value("update_check"), False)
        with self.assertRaises(ValueError):
            prefs.update({"update_check": "maybe"})

    def test_newer_version_from_a_faked_github(self):
        prefs.update({"update_check": True})
        with mock.patch("urllib.request.urlopen", return_value=github_answer()) as net:
            out = version.check_now()
        req = net.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.github.com/repos/Someone/repo/commits/main")
        self.assertNotIn("Authorization", req.headers)              # nothing about you is sent
        self.assertEqual(net.call_args[1]["timeout"], version.TIMEOUT)
        self.assertTrue(out["ok"])
        st = version.status()
        self.assertEqual((st["newer"], st["latest_date"]), (True, "2027-01-10"))
        self.assertEqual(appstate.get("update")["latest"], SHA_B)     # kept for the next start

    def test_same_commit_is_the_newest(self):
        prefs.update({"update_check": True})
        with mock.patch("urllib.request.urlopen", return_value=github_answer(sha=SHA_A, day="2026-11-27")):
            version.check_now()
        self.assertIs(version.status()["newer"], False)

    def test_older_or_same_day_is_not_called_newer(self):
        prefs.update({"update_check": True})
        with mock.patch("urllib.request.urlopen", return_value=github_answer(day="2026-11-01")):
            version.check_now()
        self.assertIs(version.status()["newer"], False)         # this copy has later commits of its own
        with mock.patch("urllib.request.urlopen", return_value=github_answer(day="2026-11-27")):
            version.check_now()
        self.assertIsNone(version.status()["newer"])            # same day, another commit: cannot tell

    def test_failing_check(self):
        prefs.update({"update_check": True})
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            out = version.check_now()
        self.assertFalse(out["ok"])
        st = version.status()
        self.assertIsNone(st["newer"])
        self.assertIn("could not be reached", st["error"])
        err = urllib.error.HTTPError("u", 403, "rate limited", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertIn("too many requests", version.check_now()["error"])

    def test_at_most_once_a_day(self):
        prefs.update({"update_check": True})
        with mock.patch.object(version, "check_now") as check:
            self.assertTrue(version.check_if_due())
            for _ in range(50):
                if check.called:
                    break
                time.sleep(0.02)
        check.assert_called_once()
        version._last[0] = {"checked_at": time.time() - 3600, "ok": True}
        self.assertFalse(version.check_if_due())
        version._last[0] = {"checked_at": time.time() - 25 * 3600, "ok": True}
        with mock.patch.object(version, "check_now"):
            self.assertTrue(version.check_if_due())

    def test_how_to_update_where_it_runs(self):
        with mock.patch.dict(os.environ, {"RENDER": "true"}):
            self.assertIn("Sync fork", version.how_to_update())
        with mock.patch.dict(os.environ, {"RENDER": ""}), mock.patch.object(version.os.path, "exists", return_value=False):
            self.assertIn("./run.sh update", version.how_to_update())


GIT = shutil.which("git")


def git(cwd, *args):
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(cwd), "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com",
                           "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false", *args],
                          cwd=cwd, env=env, capture_output=True, text=True, check=True)


@unittest.skipIf(GIT is None or os.name == "nt", "needs git and a POSIX shell")
class RunShUpdateTest(unittest.TestCase):
    """./run.sh update on throwaway repositories: an "upstream" and a copy of it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.up = self.tmp / "upstream"
        self.up.mkdir()
        shutil.copy(ROOT / "run.sh", self.up)
        shutil.copy(ROOT / "requirements.txt", self.up)
        (self.up / "README.md").write_text("first\n")
        (self.up / ".gitignore").write_text(".venv/\ndata/mine.json\n")
        (self.up / "data").mkdir()
        (self.up / "data" / "example.sample.json").write_text("{}\n")
        git(self.up, "init", "-q")
        git(self.up, "add", ".")
        git(self.up, "commit", "-q", "-m", "First version")
        self.copy = self.tmp / "copy"
        git(self.tmp, "clone", "-q", str(self.up), str(self.copy))
        venv = self.copy / ".venv"                       # no pip install: requirements already "installed"
        (venv / "bin").mkdir(parents=True)
        os.symlink(sys.executable, venv / "bin" / "python")
        with open(self.copy / "requirements.txt") as fh:
            want = subprocess.run(["cksum"], stdin=fh, capture_output=True, text=True).stdout.strip()
        (venv / ".requirements").write_text(want + "\n")
        (self.copy / "data" / "mine.json").write_text('{"private": true}\n')   # the user's data

    def update(self):
        env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.tmp), "GIT_TERMINAL_PROMPT": "0"}
        return subprocess.run(["sh", "run.sh", "update"], cwd=self.copy, env=env, capture_output=True,
                              text=True, timeout=60)

    def test_up_to_date(self):
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Already up to date", r.stdout)
        self.assertIn("start it again", r.stdout)

    def test_newer_version_is_pulled_and_data_is_kept(self):
        (self.up / "README.md").write_text("second\n")
        git(self.up, "commit", "-q", "-am", "Better prices")
        r = self.update()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Updated from version", r.stdout)
        self.assertIn("Better prices", r.stdout)
        self.assertEqual((self.copy / "README.md").read_text(), "second\n")
        self.assertEqual((self.copy / "data" / "mine.json").read_text(), '{"private": true}\n')

    def test_changed_app_files_stop_the_update(self):
        (self.up / "README.md").write_text("second\n")
        git(self.up, "commit", "-q", "-am", "Second")
        (self.copy / "README.md").write_text("my edit\n")
        r = self.update()
        self.assertEqual(r.returncode, 1)
        self.assertIn("were changed on this computer", r.stdout)
        self.assertIn("git stash", r.stdout)
        self.assertEqual((self.copy / "README.md").read_text(), "my edit\n")

    def test_no_connection(self):
        git(self.copy, "remote", "set-url", "origin", str(self.tmp / "gone"))
        r = self.update()
        self.assertEqual(r.returncode, 1)
        self.assertIn("Could not update; nothing was changed", r.stdout)
        self.assertIn("no internet connection", r.stdout)

    def test_not_a_git_folder(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        shutil.copy(ROOT / "run.sh", plain)
        env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(self.tmp)}
        r = subprocess.run(["sh", "run.sh", "update"], cwd=plain, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not downloaded with git", r.stdout)


if __name__ == "__main__":
    unittest.main()
