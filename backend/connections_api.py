"""HTTP endpoints for the "Add an account" flow (included by main.py).

  GET    /api/institutions               catalog + what the app can do with each method
  GET    /api/connections                connected accounts (secrets masked), plus
                                         server-configured T212 keys and uploaded files
  POST   /api/connections                add one (live: a real read-only test call
                                         first; generic CSV: file + column mapping)
  POST   /api/connections/csv/preview    read a CSV and guess its columns
  POST   /api/connections/{id}/csv       replace a CSV connection's file
  POST   /api/connections/{id}/refresh   fetch again now
  DELETE /api/connections/{id}           remove (and forget its key)

Bank links (Enable Banking) are added through a trip to the bank instead of
a key form: see banklink_api.py. Connections that feed cash list
`manual_duplicates`: balances typed in by hand for the same institution, so
the UI can suggest removing them (they would be counted twice).

Every edit re-encrypts the vault via github_sync.push_vault_async(). main.py
registers `after_change` hooks (rebuild the trendline when trade history
changed, leave demo mode).
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Callable

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from . import cash, catalog, connections, datafiles, github_sync
from .connectors import registry
from .connectors.live import ConnectorError
from .importers import generic_csv

router = APIRouter()

# Called with trades_changed=True/False after a connection is added or removed.
after_change: list[Callable[[bool], None]] = []

MAX_CSV = 5_000_000
_FIXED_LABELS = {"DeGiro", "Trade Republic", "Trading212"}
_FILE_SOURCES = [("degiro", "degiro", "DEGIRO account statement"),
                 ("trade_republic", "traderepublic", "Trade Republic transaction export"),
                 ("t212_statement", "trading212", "Trading 212 activity statement")]


def _changed(conn: dict) -> None:
    github_sync.push_vault_async()
    spec = registry.connector(conn.get("connector"))
    trades = bool(spec and "trades" in spec.feeds)
    for hook in after_change:
        try:
            hook(trades)
        except Exception:
            pass


def _json_form(text: str, what: str) -> dict:
    if not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        raise HTTPException(400, f"Could not read the {what}.")
    if not isinstance(value, dict):
        raise HTTPException(400, f"Could not read the {what}.")
    return value


def _read_upload(file: UploadFile | None) -> tuple[bytes, str]:
    if file is None:
        raise HTTPException(400, "Choose a file first.")
    content = file.file.read(MAX_CSV + 1)
    if len(content) > MAX_CSV:
        raise HTTPException(413, "That file is too large (5 MB max).")
    if not content.strip():
        raise HTTPException(400, "The file is empty.")
    return content, os.path.basename(file.filename or "")[:120]


def _taken_labels(exclude: str | None = None) -> set[str]:
    from .connectors import trading212

    labels = {c["label"] for c in connections.load_all() if c["id"] != exclude}
    labels |= {a.label for a in trading212.accounts()}
    return labels | _FIXED_LABELS


def _names(conn: dict) -> set[str]:
    """Names an institution goes by: 'ING (Netherlands)' -> {'ING', ...}."""
    out = {re.sub(r"\s*\(.*?\)\s*", " ", conn.get("institution_name") or "").strip(),
           (conn.get("bank") or {}).get("aspsp") or ""}
    return {n for n in out if len(registry.norm(n)) >= 2}


def _mentions(text: str, name: str) -> bool:
    text = text or ""
    if registry.norm(text) == registry.norm(name):
        return True
    return re.search(r"(?<![a-z0-9])" + re.escape(name.lower()) + r"(?![a-z0-9])",
                     text.lower()) is not None


def manual_duplicates(conn: dict, manual: list[dict]) -> list[str]:
    """Hand-typed balances that look like the same institution as `conn`."""
    names = _names(conn)
    return [a.get("name") or "" for a in manual
            if any(_mentions(a.get("institution") or "", n) or _mentions(a.get("name") or "", n)
                   for n in names)]


# ------------------------------------------------------------------ catalog
@router.get("/api/institutions")
def institutions() -> dict:
    return catalog.view()


# -------------------------------------------------------------- connections
@router.get("/api/connections")
def list_connections() -> dict:
    from .connectors import trading212

    conns = connections.load_all()
    t212_conns = [c for c in conns if c.get("connector") == "trading212"]
    inapp = {connections.t212_account(c).key for c in t212_conns}
    server = []
    env_accounts = [a for a in trading212.accounts() if a.key not in inapp]
    status = {s["label"]: s for s in trading212.status()} if env_accounts or t212_conns else {}
    for c in t212_conns:   # live health of in-app Trading 212 keys (cached by trading212.py)
        s = status.get(c["label"])
        if s and s.get("ok"):
            c.update(status="ok", error="", summary=f"{s.get('currency', '')} account · {s.get('positions', 0)} positions")
        elif s:
            c.update(status="error", error=s.get("error") or "Trading 212 refused the key.")
    if env_accounts:
        for a in env_accounts:
            s = status.get(a.label, {})
            server.append({"label": a.label, "institution_name": "Trading 212",
                           "wrapper": "ISA" if a.is_isa else "", "status": "ok" if s.get("ok") else "error",
                           "error": s.get("error", ""),
                           "summary": f"{s.get('currency', '')} account · {s.get('positions', 0)} positions"
                           if s.get("ok") else ""})
    files = []
    st = datafiles.status()
    for kind, inst_id, name in _FILE_SOURCES:
        v = st.get(kind) or {}
        if v.get("real"):
            try:
                updated = os.path.getmtime(v["path"])
            except OSError:
                updated = None
            files.append({"kind": kind, "institution": inst_id, "label": name, "updated": updated})
    manual = cash.manual_accounts()
    out = []
    for c in conns:
        pub = connections.public(c)
        spec = registry.connector(c.get("connector"))
        if manual and spec and "cash" in spec.feeds and not spec.extra.get("managed_by"):
            pub["manual_duplicates"] = manual_duplicates(c, manual)
        out.append(pub)
    return {"connections": out, "server": server, "files": files}


@router.post("/api/connections/csv/preview")
def csv_preview(file: UploadFile = File(...), mapping: str = Form(""),
                currency: str = Form("GBP")) -> dict:
    content, _ = _read_upload(file)
    try:
        return generic_csv.preview(content, _json_form(mapping, "column choice") or None,
                                   currency)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _parse_csv(content: bytes, mapping: dict, currency: str) -> tuple[list[dict], dict, dict]:
    try:
        if not mapping:
            header, _, _ = generic_csv.read_table(content)
            mapping = generic_csv.detect(header)
        holdings, report = generic_csv.parse(content, mapping, currency)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not holdings:
        raise HTTPException(400, "No positions found in that file. Check the quantity column, "
                                 "or that this is a positions (not transactions) export.")
    return holdings, report, mapping


@router.post("/api/connections")
def create(institution: str = Form(...), method: str = Form(...), label: str = Form(""),
           wrapper: str = Form(""), credentials: str = Form(""), mapping: str = Form(""),
           currency: str = Form("GBP"), file: UploadFile | None = File(None)) -> dict:
    inst = catalog.find(institution)
    if inst is None:
        raise HTTPException(404, "Unknown bank or broker.")
    m = catalog.method(inst, method)
    if m is None:
        raise HTTPException(400, "Unknown way of adding this account.")
    label = connections.unique_label(label.strip()[:60] or inst["name"], _taken_labels())
    wrapper = "ISA" if wrapper.upper() == "ISA" and inst.get("wrappers") else ""

    if m["support"] == "csv":
        content, fname = _read_upload(file)
        holdings, report, used = _parse_csv(content, _json_form(mapping, "column choice"),
                                            currency.upper()[:3])
        conn = connections.new(inst, "upload", registry.GENERIC_CSV, label, wrapper, {})
        connections.add(conn)
        connections.save_csv_holdings(conn["id"], holdings, used, fname, currency.upper()[:3])
        _changed(conn)
        return {"connection": connections.public(connections.get(conn["id"]) or conn),
                "report": report}

    if m["support"] != "live":
        raise HTTPException(400, "This option can't be connected in the app.")

    cid = m["connector"]
    if registry.connector(cid).extra.get("flow") == "bank_link":
        raise HTTPException(400, "Link this bank with the Link button: it opens your bank's "
                                 "page to approve access.")
    creds = connections.canonical_credentials(cid, _json_form(credentials, "details"),
                                              m.get("fields") or [])
    if any(len(v) > 4096 for v in creds.values()):
        raise HTTPException(400, "One of the values is too long. Copy it again.")
    missing = connections.missing_fields(cid, creds)
    if missing:
        raise HTTPException(400, "Please fill in: " + ", ".join(missing) + ".")
    if cid == "trading212":
        from .connectors import trading212
        if any(a.key == creds["api_key"] for a in trading212.accounts()):
            raise HTTPException(400, "This Trading 212 key is already connected.")

    conn = connections.new(inst, m["type"], cid, label, wrapper, creds)
    try:
        snap = connections.validate(conn)
    except ConnectorError as e:
        if not e.pending:
            raise HTTPException(400, str(e))
        conn.update(status="pending", error=str(e))   # accepted; data still being prepared
        connections.add(conn)
        _changed(conn)
        return {"connection": connections.public(conn), "pending": True}
    connections.add(conn)
    connections.remember(conn, snap)
    _changed(conn)
    return {"connection": connections.public(connections.get(conn["id"]) or conn)}


@router.post("/api/connections/{conn_id}/csv")
def replace_csv(conn_id: str, file: UploadFile = File(...), mapping: str = Form(""),
                currency: str = Form("")) -> dict:
    conn = connections.get(conn_id)
    if conn is None or conn.get("connector") != registry.GENERIC_CSV:
        raise HTTPException(404, "Connection not found.")
    content, fname = _read_upload(file)
    old = connections.csv_entry(conn_id)
    ccy = (currency or old.get("currency") or "GBP").upper()[:3]
    chosen = _json_form(mapping, "column choice")
    if not chosen and old.get("mapping"):
        try:
            header = generic_csv.read_table(content)[0]
        except ValueError as e:
            raise HTTPException(400, str(e))
        cols = [v for k, v in old["mapping"].items() if k in generic_csv.FIELDS and v]
        if all(c in header for c in cols):
            chosen = old["mapping"]          # same layout as last time
    holdings, report, used = _parse_csv(content, chosen, ccy)
    connections.save_csv_holdings(conn_id, holdings, used, fname, ccy)
    _changed(conn)
    return {"connection": connections.public(connections.get(conn_id) or conn), "report": report}


@router.post("/api/connections/{conn_id}/refresh")
def refresh(conn_id: str, request: Request) -> dict:
    existing = connections.get(conn_id)
    spec = registry.connector((existing or {}).get("connector"))
    psu = None
    if spec and spec.extra.get("psu"):
        # the user pressed Refresh: tell the bank they are online, so it does
        # not count against its few background reads a day
        from .connectors.enablebanking import psu_from_request
        psu = psu_from_request(request.headers, request.client.host if request.client else "")
    conn = connections.refresh(conn_id, psu=psu)
    if conn is None:
        raise HTTPException(404, "Connection not found.")
    return {"connection": connections.public(conn)}


@router.delete("/api/connections/{conn_id}")
def delete(conn_id: str) -> dict:
    gone = connections.remove(conn_id)
    if gone is None:
        raise HTTPException(404, "Connection not found.")
    if gone.get("connector") == "trading212":
        connections._clear_t212(gone)
    if gone.get("connector") == "enablebanking":
        _end_bank_link(gone)
    _changed(gone)
    return {"removed": conn_id}


def _end_bank_link(conn: dict) -> None:
    """End the consent at Enable Banking too (best effort, in the background)."""
    from .connectors import enablebanking as eb

    session_id = (conn.get("credentials") or {}).get("session_id")
    app, _ = eb.current()
    if app is not None and session_id:
        threading.Thread(target=eb.end_session, args=(app, session_id), daemon=True).start()
