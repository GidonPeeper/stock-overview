"""Which version of the app this is, and (only when switched on) whether a
newer one is available.

The version is the git commit this copy runs: read straight from the .git
folder (no git program needed), or from RENDER_GIT_COMMIT on Render. A copy
without .git (a downloaded zip, a Docker image) says "unknown".

The check for a newer version is off until it is switched on in Settings
(prefs "update_check"). Then, at most once a day, it asks GitHub for the
newest commit on the project's main branch:

    GET https://api.github.com/repos/<owner>/<repo>/commits/<branch>

That request carries nothing about you or your data (no token, no account,
no numbers): only the usual things any web request has, such as your IP
address. The answer is kept in data/app_state.json (appstate.py) and shown on
the check-up.
"""

from __future__ import annotations

import json
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

from . import appstate

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = "GidonPeeper/stock-overview"
DEFAULT_BRANCH = "main"
CHECK_EVERY = 24 * 3600        # at most one request a day
TIMEOUT = 10

_GITHUB = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_info: dict | None = None
_lock = threading.Lock()
_running = [False]


# ------------------------------------------------------------ reading .git
def _git_dir(root: Path) -> Path | None:
    g = root / ".git"
    if g.is_dir():
        return g
    try:
        text = g.read_text().strip()          # a worktree: "gitdir: <path>"
    except OSError:
        return None
    if text.startswith("gitdir:"):
        p = Path(text[7:].strip())
        p = p if p.is_absolute() else (root / p)
        return p if p.is_dir() else None
    return None


def _common_dir(git_dir: Path) -> Path:
    try:
        p = Path((git_dir / "commondir").read_text().strip())
        return (p if p.is_absolute() else git_dir / p).resolve()
    except OSError:
        return git_dir


def _ref(git_dir: Path, common: Path, ref: str) -> str | None:
    for base in (git_dir, common):
        try:
            sha = (base / ref).read_text().strip()
            if _SHA.match(sha):
                return sha
        except OSError:
            continue
    try:
        for line in (common / "packed-refs").read_text().splitlines():
            parts = line.strip().split(" ")
            if len(parts) == 2 and parts[1] == ref and _SHA.match(parts[0]):
                return parts[0]
    except OSError:
        pass
    return None


def _commit_text(common: Path, sha: str) -> str | None:
    """The raw commit object (loose, or a whole commit in a pack file)."""
    loose = common / "objects" / sha[:2] / sha[2:]
    try:
        raw = zlib.decompress(loose.read_bytes())
        return raw.split(b"\0", 1)[1].decode("utf-8", "replace")
    except (OSError, zlib.error, IndexError):
        pass
    want = bytes.fromhex(sha)
    for idx in sorted((common / "objects" / "pack").glob("*.idx")):
        try:
            offset = _pack_offset(idx.read_bytes(), want)
            if offset is None:
                continue
            with open(idx.with_suffix(".pack"), "rb") as fh:
                fh.seek(offset)
                head = fh.read(64 * 1024)
            byte = head[0]
            kind = (byte >> 4) & 7
            i = 1
            while byte & 0x80:                 # the size continues in the next bytes
                byte = head[i]
                i += 1
            if kind != 1:                      # stored as a delta: leave it to git
                return None
            return zlib.decompressobj().decompress(head[i:]).decode("utf-8", "replace")
        except (OSError, zlib.error, IndexError, struct.error, ValueError):
            continue
    return None


def _pack_offset(idx: bytes, want: bytes) -> int | None:
    """Offset of object `want` in a version-2 pack index, or None."""
    if idx[:4] != b"\xfftOc" or struct.unpack(">I", idx[4:8])[0] != 2:
        return None
    fan = struct.unpack(">256I", idx[8:8 + 1024])
    n = fan[255]
    lo = fan[want[0] - 1] if want[0] else 0
    hi = fan[want[0]]
    names = 8 + 1024
    while lo < hi:
        mid = (lo + hi) // 2
        got = idx[names + mid * 20:names + mid * 20 + 20]
        if got == want:
            offsets = names + n * 20 + n * 4
            off = struct.unpack(">I", idx[offsets + mid * 4:offsets + mid * 4 + 4])[0]
            if off & 0x80000000:               # a large pack: 8-byte offsets follow
                big = offsets + n * 4 + (off & 0x7FFFFFFF) * 8
                off = struct.unpack(">Q", idx[big:big + 8])[0]
            return off
        if got < want:
            lo = mid + 1
        else:
            hi = mid
    return None


def _commit_time(common: Path, sha: str) -> int | None:
    text = _commit_text(common, sha)
    m = re.search(r"^committer .* (\d+) [+-]\d{4}$", text or "", re.M)
    if m:
        return int(m.group(1))
    try:                                       # a delta in a pack: ask git, if it is installed
        import subprocess
        out = subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%ct", sha],
                             capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except Exception:
        return None


def _repo_from_config(common: Path) -> str | None:
    try:
        text = (common / "config").read_text()
    except OSError:
        return None
    section = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == '[remote "origin"]' and line.replace(" ", "").startswith("url="):
            m = _GITHUB.search(line.split("=", 1)[1].strip())
            if m:
                return f"{m.group(1)}/{m.group(2)}"
    return None


def _default_branch(common: Path) -> str | None:
    try:
        text = (common / "refs" / "remotes" / "origin" / "HEAD").read_text().strip()
    except OSError:
        return None
    return text.rsplit("/", 1)[-1] if text.startswith("ref: refs/remotes/origin/") else None


def read(root: Path = ROOT) -> dict:
    """{commit, short, date (YYYY-MM-DD) , branch, repo, default_branch, source}.
    Fields that cannot be read are None (repo falls back to the project's own)."""
    out = {"commit": None, "short": None, "date": None, "branch": None, "source": "unknown",
           "repo": None, "default_branch": None}
    gd = _git_dir(root)
    if gd is not None:
        common = _common_dir(gd)
        try:
            head = (gd / "HEAD").read_text().strip()
        except OSError:
            head = ""
        if head.startswith("ref: "):
            ref = head[5:].strip()
            out["commit"] = _ref(gd, common, ref)
            out["branch"] = ref.split("refs/heads/", 1)[-1]
        elif _SHA.match(head):
            out["commit"] = head
        if out["commit"]:
            out["source"] = "git"
            ts = _commit_time(common, out["commit"])
            if ts:
                out["date"] = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        out["repo"] = _repo_from_config(common)
        out["default_branch"] = _default_branch(common)
    render = (os.getenv("RENDER_GIT_COMMIT") or "").strip().lower()
    if render and _SHA.match(render) and render != out["commit"]:
        out.update(commit=render, date=None, source="render")
        out["branch"] = os.getenv("RENDER_GIT_BRANCH") or out["branch"]
    out["repo"] = (os.getenv("RENDER_GIT_REPO_SLUG") or "").strip() or out["repo"] or DEFAULT_REPO
    out["default_branch"] = out["default_branch"] or DEFAULT_BRANCH
    out["short"] = out["commit"][:7] if out["commit"] else None
    return out


def info() -> dict:
    """This copy's version (read once: the running code does not change)."""
    global _info
    if _info is None:
        try:
            _info = read()
        except Exception:
            _info = {"commit": None, "short": None, "date": None, "branch": None, "source": "unknown",
                     "repo": DEFAULT_REPO, "default_branch": DEFAULT_BRANCH}
    return dict(_info)


def where() -> str:
    """'host' (Render), 'docker' or 'computer': decides how to update."""
    if os.getenv("RENDER"):
        return "host"
    if os.path.exists("/.dockerenv"):
        return "docker"
    return "computer"


def docs_url(name: str = "TROUBLESHOOTING.md", anchor: str = "") -> str:
    """A document of the project on GitHub (the app has no viewer of its own)."""
    v = info()
    return (f"https://github.com/{v['repo']}/blob/{v['default_branch']}/{name}"
            + (f"#{anchor}" if anchor else ""))


def how_to_update() -> str:
    w = where()
    if w == "host":
        return ("On GitHub, open your copy of the project and press “Sync fork”. Render installs the "
                "new version by itself within a few minutes.")
    if w == "docker":
        return ("In the project folder run git pull, then docker compose up -d --build.")
    return ("Stop the app (Ctrl+C in its window), run ./run.sh update (on Windows: run.bat update), "
            "then start it again with ./run.sh.")


# ------------------------------------------------------- the update check
def enabled() -> bool:
    from . import prefs
    try:
        return bool(prefs.value("update_check"))
    except Exception:
        return False


def _fetch_latest(repo: str, branch: str) -> dict:
    """The newest commit on `branch` of the public repo: {sha, date}."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/commits/{branch}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "stock-overview"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.loads(r.read())
    sha = str(data.get("sha") or "")
    when = (((data.get("commit") or {}).get("committer") or {}).get("date") or "")
    if not _SHA.match(sha):
        raise ValueError("GitHub sent an unexpected answer")
    return {"sha": sha, "date": when[:10] or None}


def _describe(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 403:
            return "GitHub asked to wait (too many requests from this network); it is tried again tomorrow"
        if e.code == 404:
            return "the project was not found on GitHub"
        return f"GitHub answered with error {e.code}"
    if isinstance(e, (urllib.error.URLError, TimeoutError, OSError)):
        return "GitHub could not be reached (no internet connection?)"
    return str(e) or type(e).__name__


def check_now() -> dict:
    """Ask GitHub once and keep the answer (never raises)."""
    v = info()
    result = {"checked_at": time.time(), "repo": v["repo"], "branch": v["default_branch"]}
    try:
        latest = _fetch_latest(v["repo"], v["default_branch"])
        result.update(ok=True, latest=latest["sha"], latest_date=latest["date"])
    except Exception as e:
        result.update(ok=False, error=_describe(e))
    _last[0] = result
    appstate.put("update", result)
    return result


_last: list = [None]


def last() -> dict | None:
    if _last[0] is None:
        saved = appstate.get("update")
        _last[0] = saved if isinstance(saved, dict) else None
    return _last[0]


def check_if_due() -> bool:
    """Start a check in the background when switched on and the last one is
    a day old. True when one was started."""
    if not enabled():
        return False
    prev = last()
    if prev and time.time() - float(prev.get("checked_at") or 0) < CHECK_EVERY:
        return False
    with _lock:
        if _running[0]:
            return False
        _running[0] = True

    def run():
        try:
            check_now()
        finally:
            _running[0] = False
    threading.Thread(target=run, name="update-check", daemon=True).start()
    return True


def status() -> dict:
    """This version and what the last check found. `newer` is True when the
    project has a newer commit, False when this is the newest, None when it
    cannot be told (check off, failed, or this copy's date unknown)."""
    v = info()
    out = {**v, "check_on": enabled(), "where": where(), "how_to_update": how_to_update(),
           "checked_at": None, "newer": None, "latest_date": None, "error": None}
    prev = last()
    if out["check_on"] and prev:
        out["checked_at"] = prev.get("checked_at")
        if prev.get("ok"):
            out["latest_date"] = prev.get("latest_date")
            if v["commit"] and prev.get("latest") == v["commit"]:
                out["newer"] = False
            elif v["date"] and prev.get("latest_date") and prev["latest_date"] != v["date"]:
                out["newer"] = prev["latest_date"] > v["date"]   # same day, other commit: cannot tell
        else:
            out["error"] = prev.get("error")
    return out
