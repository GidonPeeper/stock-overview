"""HTTP endpoints for signing in to Trade Republic (included by main.py).

  POST   /api/traderepublic/login                 phone + PIN: Trade Republic asks the
                                                  user to approve in its app (or sends a code)
  POST   /api/traderepublic/login/{id}/confirm    the code, if one is needed; then waits
                                                  (up to ~15 s per call) for the approval
  POST   /api/traderepublic/login/{id}/resend     the 4-digit code by SMS (fallback login only)
  DELETE /api/traderepublic/login/{id}            cancel

The PIN is sent to Trade Republic once, in the first call, and is not kept:
the sign-in in progress (memory only, a few minutes) holds the HTTP session
and the process id, not the PIN. Once approved, the app reads positions and
cash and stores the connection with only Trade Republic's session cookies and
the securities account number (connections.json; never sent back to the
browser). The history is read in the background right after. Signing in
again for the same account renews that connection instead of adding another
(`renew`, or matched by the account fingerprint).

Unofficial and read-only: see backend/connectors/traderepublic_live.py.
"""

from __future__ import annotations

import secrets
import threading
import time

from fastapi import APIRouter, Form, HTTPException

from . import cache, catalog, connections, connections_api
from .connectors import traderepublic_live as trl
from .connectors.live import ConnectorError

router = APIRouter()

CONFIRM_WAIT = 15.0      # seconds one confirm call waits for the approval in the app
MAX_PENDING = 5
_LEFT_OVER = 60          # seconds a sign-in is kept after Trade Republic's own deadline

_lock = threading.Lock()
_pending: dict[str, tuple[trl.LoginProcess, dict]] = {}   # login id -> (process, what to save)


def _prune(now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _lock:
        for key in [k for k, (p, _) in _pending.items() if p.expires + _LEFT_OVER < now]:
            _pending.pop(key, None)


def _get(login_id: str) -> tuple[trl.LoginProcess, dict]:
    _prune()
    with _lock:
        hit = _pending.get(login_id)
    if hit is None:
        raise HTTPException(404, "This sign-in has expired. Start again.")
    return hit


def _step(login_id: str, proc: trl.LoginProcess, status: str = "started") -> dict:
    message = {
        "app": "Open the Trade Republic app on your phone and confirm the sign-in.",
        "authenticator": "Enter the 6-digit code from your authenticator app.",
        "code": "Enter the 4-digit code Trade Republic sent to your app or by SMS.",
    }[proc.step]
    return {"login": login_id, "status": status, "step": proc.step, "message": message,
            "expires_in": max(0, int(proc.expires - time.time())),
            "countdown": proc.countdown, "phone": proc.hint,
            "can_resend": proc.mode == "v1"}


@router.post("/api/traderepublic/login")
def start(phone: str = Form(""), pin: str = Form(""), institution: str = Form("traderepublic"),
          method: str = Form(""), label: str = Form(""), renew: str = Form("")) -> dict:
    inst = catalog.find(institution or "traderepublic") or {"id": "traderepublic",
                                                            "name": "Trade Republic"}
    if renew:
        conn = connections.get(renew)
        if conn is None or conn.get("connector") != "traderepublic":
            raise HTTPException(404, "Connection not found.")
    _prune()
    with _lock:
        if len(_pending) >= MAX_PENDING:
            raise HTTPException(429, "Too many sign-ins in progress. Wait a few minutes.")
    try:
        proc = trl.start_login(phone, pin)
    except ConnectorError as e:
        raise HTTPException(400, str(e))
    finally:
        pin = ""          # the PIN is not needed again and not kept
    login_id = secrets.token_urlsafe(18)
    info = {"institution": inst, "method": method, "label": label.strip()[:60], "renew": renew}
    with _lock:
        _pending[login_id] = (proc, info)
    return _step(login_id, proc)


@router.post("/api/traderepublic/login/{login_id}/confirm")
def confirm(login_id: str, code: str = Form("")) -> dict:
    proc, info = _get(login_id)
    try:
        done = trl.confirm_login(proc, code or None, wait=CONFIRM_WAIT)
    except ConnectorError as e:
        if e.status in ("EXPIRED", "ENDED", "PROCESS_GONE", "ALREADY_PROCESSED", "NOT_FOUND",
                        "TOO_MANY_REQUESTS", 429):
            with _lock:
                _pending.pop(login_id, None)
        raise HTTPException(400, str(e))
    if not done:
        return _step(login_id, proc, "waiting")
    with _lock:
        _pending.pop(login_id, None)

    target: dict = {}

    def history_for(account: str):
        """The stored history of the connection this sign-in renews."""
        conn = connections.get(info["renew"]) if info.get("renew") else None
        if conn is None:
            conn = next((c for c in connections.load_all() if c.get("connector") == "traderepublic"
                         and (c.get("tr") or {}).get("account") == account), None)
        if conn is not None:
            target["conn"] = conn
        return (conn or {}).get("history")

    try:
        snap, creds, history = trl.first_sync(proc, history_for)
    except ConnectorError as e:
        raise HTTPException(400, str(e))
    conn = _save(info, target.get("conn"), snap, creds, history, proc.hint)
    connections_api._changed(conn)        # vault + hooks (leave demo mode)
    cache.invalidate()
    connections.fetch_soon(conn["id"])    # the history, in the background
    return {"status": "done", "connection": connections.public(connections.get(conn["id"]) or conn)}


def _save(info: dict, existing: dict | None, snap, creds: dict, history: dict, hint: str) -> dict:
    tr = {"account": snap.account, "phone": hint}
    if existing is not None:
        conn = connections.update(existing["id"], credentials=creds, history=history, tr=tr,
                                  status="ok", error="") or existing
    else:
        inst = info["institution"]
        taken = connections_api._taken_labels(free={"Trade Republic"})
        label = connections.unique_label(info.get("label") or inst.get("name") or "Trade Republic",
                                         taken)
        conn = connections.new(inst, "api", "traderepublic", label, "", creds)
        conn.update(history=history, tr=tr)
        connections.add(conn)
    connections.remember(conn, snap)
    return connections.get(conn["id"]) or conn


@router.post("/api/traderepublic/login/{login_id}/resend")
def resend(login_id: str) -> dict:
    proc, _ = _get(login_id)
    try:
        trl.resend_code(proc)
    except ConnectorError as e:
        raise HTTPException(400, str(e))
    return _step(login_id, proc, "resent")


@router.delete("/api/traderepublic/login/{login_id}")
def cancel(login_id: str) -> dict:
    with _lock:
        _pending.pop(login_id, None)
    return {"cancelled": True}
