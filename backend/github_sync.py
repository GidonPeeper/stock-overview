"""Cloud persistence for in-app edits on ephemeral-disk hosts.

Free hosting wipes the local disk on every restart, so balances and uploads
edited in-app would silently revert to demo data. When a GITHUB_TOKEN env var
is present (fine-grained PAT, Contents read/write on this repo only), every
mutation re-encrypts the vault and commits it to a dedicated `data-sync`
branch via the GitHub API; on boot the freshest vault is pulled back before
unlocking. The branch keeps data commits away from `main`, so the host's
deploy-on-push never triggers.

Everything is best-effort: no token or an API error never breaks the app —
it just means edits live only until the next restart.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import urllib.request

BRANCH = "data-sync"
_API = "https://api.github.com"

last_result: dict = {"enabled": False}


def _repo() -> str:
    return os.getenv("GITHUB_REPO", "GidonPeeper/stock-overview")


def _req(path: str, method: str = "GET", body: dict | None = None):
    token = os.getenv("GITHUB_TOKEN")
    req = urllib.request.Request(
        _API + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "stock-overview"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _ensure_branch() -> None:
    try:
        _req(f"/repos/{_repo()}/git/ref/heads/{BRANCH}")
        return
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    main = _req(f"/repos/{_repo()}/git/ref/heads/main")
    _req(f"/repos/{_repo()}/git/refs", "POST",
         {"ref": f"refs/heads/{BRANCH}", "sha": main["object"]["sha"]})


def pull_vault() -> bool:
    """On boot: fetch the freshest vault from the data-sync branch (if any)."""
    if not os.getenv("GITHUB_TOKEN"):
        return False
    from . import vault
    try:
        meta = _req(f"/repos/{_repo()}/contents/data/vault.enc?ref={BRANCH}")
        content = base64.b64decode(meta["content"])
        vault.DATA_DIR.mkdir(exist_ok=True)
        vault.VAULT.write_bytes(content)
        return True
    except Exception:
        return False  # branch/file doesn't exist yet, or network hiccup


def push_vault_async() -> None:
    """After a mutation: re-encrypt current data files and commit the vault.
    Runs in a background thread so requests stay fast."""
    global last_result
    if not os.getenv("GITHUB_TOKEN"):
        last_result = {"enabled": False,
                       "hint": "set GITHUB_TOKEN to persist edits across restarts"}
        return

    def task():
        global last_result
        from . import vault
        try:
            names = vault.encrypt()
            _ensure_branch()
            path = f"/repos/{_repo()}/contents/data/vault.enc"
            sha = None
            try:
                sha = _req(path + f"?ref={BRANCH}").get("sha")
            except Exception:
                pass
            body = {"message": "chore: sync encrypted data vault (in-app edit)",
                    "content": base64.b64encode(vault.VAULT.read_bytes()).decode(),
                    "branch": BRANCH}
            if sha:
                body["sha"] = sha
            _req(path, "PUT", body)
            import time
            last_result = {"enabled": True, "ok": True, "files": names,
                           "at": time.strftime("%H:%M:%S")}
        except Exception as e:
            last_result = {"enabled": True, "ok": False, "error": type(e).__name__}

    threading.Thread(target=task, daemon=True).start()
