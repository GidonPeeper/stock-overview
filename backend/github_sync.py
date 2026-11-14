"""Cloud persistence for in-app edits on ephemeral-disk hosts.

Free hosting wipes the local disk on every restart, so uploads, balances and
keys added in-app would silently disappear. When a GITHUB_TOKEN env var is
present (fine-grained PAT, Contents read/write on this repo only), every edit
re-encrypts the vault and stores it on a dedicated `data-sync` branch; on boot
the stored vault is pulled back before unlocking. So each upload is needed once.

  * The branch holds one commit with only data/vault.enc, replaced on each save
    (force-updated), so the public repo does not grow with every edit. Data
    commits stay away from `main`, so the host's deploy-on-push never triggers.
  * Saves run one at a time in a background thread; edits made while a save is
    running are picked up by one more save right after it.
  * If the stored vault cannot be read at boot (network or token problem),
    saving pauses: pushing data restored from the older vault in `main` would
    overwrite newer cloud data. Reading is retried every minute; once it works,
    files not edited since boot are replaced with the cloud copy and saving resumes.

Without a token nothing changes: edits live until the next restart.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

BRANCH = "data-sync"
VAULT_PATH = "data/vault.enc"
_API = "https://api.github.com"
PULL_RETRY_SECONDS = 60
PUSH_RETRY_SECONDS = (10, 30, 60)

last_result: dict = {"enabled": False}

_lock = threading.Lock()
_dirty = False            # an edit is waiting to be saved
_running = False          # the save worker is running
_pulled: bool | None = None   # None: not tried / no token; False: cloud copy unreadable
_restored_at = 0.0        # files older than this came from a vault, not from an edit


def _spawn(fn) -> None:   # replaced in tests to run synchronously
    threading.Thread(target=fn, daemon=True).start()


def _sleep(seconds: float) -> None:   # replaced in tests
    time.sleep(seconds)


def _token() -> str | None:
    return os.getenv("GITHUB_TOKEN")


def _repo() -> str:
    return os.getenv("GITHUB_REPO", "GidonPeeper/stock-overview")


def _req(path: str, method: str = "GET", body: dict | None = None):
    req = urllib.request.Request(
        _API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {_token()}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "stock-overview"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _describe(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 401:
            return "GitHub rejected the token (expired or wrong)"
        if e.code in (403, 404):
            return f"HTTP {e.code}: the token needs Contents read & write on {_repo()}"
        return f"HTTP {e.code}"
    if isinstance(e, SystemExit):
        return str(e)
    return type(e).__name__


# ---------------------------------------------------------------- reading

def _fetch_cloud_vault() -> bytes | None:
    """The stored vault, or None if nothing is stored yet. Raises on other errors."""
    try:
        meta = _req(f"/repos/{_repo()}/contents/{VAULT_PATH}?ref={BRANCH}")
    except urllib.error.HTTPError as e:
        if e.code == 404:    # no branch or no file yet (a bad token gives 401)
            return None
        raise
    if meta.get("content"):
        return base64.b64decode(meta["content"])
    # the contents API leaves files over 1 MB out; the blob API serves up to 100 MB
    blob = _req(f"/repos/{_repo()}/git/blobs/{meta['sha']}")
    return base64.b64decode(blob["content"])


def pull_vault(attempts: int = 3) -> bool:
    """Fetch the stored vault into data/vault.enc. True if one was fetched."""
    global _pulled, last_result
    if not _token():
        return False
    from . import vault
    err: BaseException | None = None
    for attempt in range(attempts):
        try:
            content = _fetch_cloud_vault()
        except Exception as e:
            err = e
            if attempt + 1 < attempts:
                _sleep(2 * (attempt + 1))
            continue
        with _lock:
            _pulled = True
        last_result = {"enabled": True, "ok": True}
        if content is None:
            return False
        vault.DATA_DIR.mkdir(exist_ok=True)
        vault.VAULT.write_bytes(content)
        return True
    with _lock:
        _pulled = False
    last_result = {"enabled": True, "ok": False, "paused": True,
                   "error": f"could not read the cloud copy ({_describe(err)}); "
                            "saving is paused until it can"}
    return False


def restore() -> dict:
    """On boot: pull the stored vault, then decrypt it into data/."""
    global _restored_at
    from . import vault
    pulled = pull_vault()
    state = vault.unlock()
    state["cloud"] = pulled
    _restored_at = time.time()
    if _pulled is False:
        _spawn(_retry_pull)
    return state


def _retry_pull() -> None:
    """Keep trying to read the cloud copy; then take it in and resume saving."""
    from . import cache, vault
    while _pulled is False:
        _sleep(PULL_RETRY_SECONDS)
        if pull_vault(attempts=1):
            # files edited since boot are newer than the cloud copy: keep those
            vault.unlock(replace_older_than=_restored_at)
            try:
                cache.invalidate()
            except Exception:
                pass
    with _lock:
        waiting = _dirty
    if waiting:
        push_vault_async()


# ---------------------------------------------------------------- saving

def _set_ref(sha: str) -> None:
    try:
        _req(f"/repos/{_repo()}/git/refs/heads/{BRANCH}", "PATCH", {"sha": sha, "force": True})
    except urllib.error.HTTPError as e:
        if e.code not in (404, 422):     # 422/404: the branch does not exist yet
            raise
        _req(f"/repos/{_repo()}/git/refs", "POST", {"ref": f"refs/heads/{BRANCH}", "sha": sha})


def _push_once() -> dict:
    from . import vault
    if not vault._key_material():
        return {"enabled": True, "ok": False,
                "error": "DATA_KEY or DASHBOARD_SECRET is not set on the host"}
    try:
        names = vault.encrypt()
        repo = _repo()
        blob = _req(f"/repos/{repo}/git/blobs", "POST",
                    {"content": base64.b64encode(vault.VAULT.read_bytes()).decode(),
                     "encoding": "base64"})
        tree = _req(f"/repos/{repo}/git/trees", "POST",
                    {"tree": [{"path": VAULT_PATH, "mode": "100644", "type": "blob",
                               "sha": blob["sha"]}]})
        commit = _req(f"/repos/{repo}/git/commits", "POST",
                      {"message": "chore: sync encrypted data vault (in-app edit)",
                       "tree": tree["sha"], "parents": []})
        _set_ref(commit["sha"])
        return {"enabled": True, "ok": True, "files": names, "at": time.strftime("%H:%M:%S")}
    except (Exception, SystemExit) as e:   # vault.encrypt exits when there is nothing to save
        return {"enabled": True, "ok": False, "error": _describe(e)}


def _push_loop() -> None:
    global _dirty, _running, last_result
    failures = 0
    while True:
        with _lock:
            if not _dirty or _pulled is False:
                _running = False
                return
            _dirty = False
        result = _push_once()
        last_result = result
        if result.get("ok"):
            failures = 0
            continue
        with _lock:
            _dirty = True               # keep the edit queued
        if failures >= len(PUSH_RETRY_SECONDS):
            with _lock:
                _running = False        # the next edit tries again
            return
        _sleep(PUSH_RETRY_SECONDS[failures])
        failures += 1


def push_vault_async() -> None:
    """After an edit: save the current data files to the cloud in the background."""
    global last_result, _dirty, _running
    if not _token():
        last_result = {"enabled": False,
                       "hint": "set GITHUB_TOKEN to persist edits across restarts"}
        return
    with _lock:
        _dirty = True
        if _running or _pulled is False:
            return    # the running save picks this edit up, or saving is paused
        _running = True
    _spawn(_push_loop)


def pending() -> bool:
    with _lock:
        return _dirty or _running
