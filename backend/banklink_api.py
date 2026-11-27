"""HTTP endpoints for bank links through Enable Banking (included by main.py).

  GET    /api/enablebanking          is the app set up? + the redirect URL to register
  POST   /api/enablebanking/app      save the application ID + private key (checked live)
  DELETE /api/enablebanking/app      forget the ones saved in the app (env still applies)
  GET    /api/enablebanking/banks    banks in one country (+ the one matching an institution)
  POST   /api/enablebanking/start    start a link, or renew one: returns the bank's page
  GET    /enablebanking/callback     the bank sends the user back here (PUBLIC, see below)

The callback needs no login. On an iPhone the bank often sends the user back
in Safari instead of the home-screen app, and Safari has its own cookies, so
the dashboard session is not there. The callback is protected instead by the
random single-use `state` that /start created (only its hash is stored, for
30 minutes): without a valid state it does nothing.

The redirect URL is built from the request (X-Forwarded-Proto/-Host on
Render), or fixed with ENABLEBANKING_REDIRECT_URL; it must be registered in
the Enable Banking Control Panel exactly as shown in Settings.
"""

from __future__ import annotations

import html
import os
import re
import threading

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from starlette.responses import HTMLResponse

from . import cache, catalog, connections, connections_api, github_sync
from .connectors import enablebanking as eb
from .connectors.live import ConnectorError

router = APIRouter()

_HOST = re.compile(r"^(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?$")
FIRST_WAIT = 12.0     # seconds the callback page waits for the first balances


def redirect_url(request: Request) -> str:
    fixed = os.getenv("ENABLEBANKING_REDIRECT_URL", "").strip()
    if fixed:
        return fixed
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http")
    proto = proto.split(",")[0].strip().lower()
    proto = proto if proto in ("http", "https") else "http"
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "")
    host = host.split(",")[0].strip()
    if not _HOST.match(host):
        host = request.url.netloc
    return f"{proto}://{host}{eb.CALLBACK_PATH}"


def _status(request: Request) -> dict:
    app, problem = eb.current()
    url = redirect_url(request)
    return {"configured": app is not None, "source": app.source if app else "",
            "app_id": connections.mask(app.app_id) if app else "",
            "problem": "" if app else problem,
            "redirect_url": url, "https": url.startswith("https://")}


def _app() -> eb.App:
    try:
        return eb.require_app()
    except ConnectorError as e:
        raise HTTPException(400, str(e))


def _psu(request: Request) -> dict:
    return eb.psu_from_request(request.headers, request.client.host if request.client else "")


# ------------------------------------------------------------------ the app
@router.get("/api/enablebanking")
def status(request: Request) -> dict:
    return _status(request)


@router.post("/api/enablebanking/app")
def save_app(request: Request, app_id: str = Form(""), private_key: str = Form(""),
             key_file: UploadFile | None = File(None)) -> dict:
    pem = private_key
    if not pem.strip() and key_file is not None:
        pem = key_file.file.read(eb.MAX_KEY + 1).decode("utf-8", "replace")
    try:
        app = eb.App(eb.check_app_id(app_id), eb.clean_pem(pem), "app")
        eb.load_key(app.private_key)
        found = eb.banks(app, force=True)        # a real call: is the pair accepted?
    except ConnectorError as e:
        raise HTTPException(400, str(e))
    connections.save_app_credentials(eb.PROVIDER, {"app_id": app.app_id,
                                                   "private_key": app.private_key})
    github_sync.push_vault_async()
    return {**_status(request), "banks": len(found)}


@router.delete("/api/enablebanking/app")
def forget_app(request: Request) -> dict:
    connections.forget_app_credentials(eb.PROVIDER)
    github_sync.push_vault_async()
    return _status(request)


@router.get("/api/enablebanking/banks")
def bank_list(country: str = "", institution: str = "") -> dict:
    app = _app()
    try:
        every = eb.banks(app)
    except ConnectorError as e:
        raise HTTPException(400, str(e))
    inst = catalog.find(institution) if institution else None
    country = re.sub(r"[^A-Z]", "", country.upper())[:2] or eb.choose_country(inst, every)
    rows = sorted((b for b in every if b["country"] == country), key=lambda b: b["name"].lower())
    hit = eb.suggest(inst, rows)
    return {"countries": eb.countries(every), "country": country,
            "suggested": hit["name"] if hit else "",
            "banks": [{"name": b["name"], "max_days": b["max_days"], "beta": b["beta"]}
                      for b in rows]}


# ---------------------------------------------------------------- linking
@router.post("/api/enablebanking/start")
def start(request: Request, institution: str = Form(""), method: str = Form(""),
          aspsp: str = Form(""), country: str = Form(""), label: str = Form(""),
          renew: str = Form("")) -> dict:
    app = _app()
    if renew:
        conn = connections.get(renew)
        if conn is None or conn.get("connector") != eb.PROVIDER:
            raise HTTPException(404, "That account is no longer connected (it may have been removed in another window). Reload the page.")
        meta = conn.get("bank") or {}
        aspsp, country = meta.get("aspsp", ""), meta.get("country", "")
        info = {"renew": conn["id"], "institution": conn["institution"],
                "institution_name": conn.get("institution_name", ""), "label": conn["label"]}
    else:
        inst = catalog.find(institution)
        if inst is None:
            raise HTTPException(404, "That bank or broker is not in the list. Reload the page and pick it again.")
        m = catalog.method(inst, method)
        if m is None or m.get("connector") != eb.PROVIDER:
            raise HTTPException(400, "This option can't be linked in the app. Choose another way to add the account.")
        info = {"renew": "", "institution": inst["id"], "institution_name": inst.get("name", ""),
                "label": label.strip()[:60]}
    try:
        bank = eb.find_bank(eb.banks(app), aspsp, country)
    except ConnectorError as e:
        raise HTTPException(400, str(e))
    if bank is None:
        raise HTTPException(400, f"Enable Banking no longer lists {aspsp} ({country}). Remove this "
                                 "link and add the bank again." if renew else
                            "Pick your bank from the list.")
    url = redirect_url(request)
    until = eb.consent_until(bank)
    state = connections.new_link_state({**info, "aspsp": bank["name"], "country": bank["country"],
                                        "required_psu_headers": bank["required_psu_headers"],
                                        "valid_until": until})
    try:
        out = eb.start_auth(app, bank, url, state, until)
    except ConnectorError as e:
        connections.take_link_state(state)          # never used
        msg = str(e)
        if e.status == "REDIRECT_URI_NOT_ALLOWED":
            msg = (f"{msg} In the Enable Banking Control Panel, add {url} to the application's "
                   "redirect URLs (exactly), then try again.")
        if not url.startswith("https://"):
            msg += " Enable Banking only sends you back to an https address: open the dashboard " \
                   "at its https address to link a bank."
        raise HTTPException(400, msg)
    return {"url": out["url"], "redirect_url": url}


def _same_link(bank: dict) -> dict | None:
    """An existing link to the same bank accounts (linking twice renews)."""
    hashes = {a["hash"] for a in bank["accounts"] if a.get("hash")}
    for c in connections.load_all():
        meta = c.get("bank") or {}
        if (c.get("connector") == eb.PROVIDER and meta.get("aspsp") == bank["aspsp"]
                and meta.get("country") == bank["country"]
                and hashes & {a.get("hash") for a in meta.get("accounts") or [] if a.get("hash")}):
            return c
    return None


def _save(info: dict, s: dict) -> tuple[dict, str]:
    """Store a new or renewed link. Returns (connection, old session id)."""
    keep = ("uid", "name", "currency", "iban", "type", "credit_limit")
    creds = {"session_id": s["session_id"],
             "accounts": [{k: a.get(k) for k in keep} for a in s["accounts"]]}
    bank = {"aspsp": info["aspsp"], "country": info["country"], "psu_type": s.get("psu_type") or "",
            "required_psu_headers": info.get("required_psu_headers") or [],
            "accounts": [{k: a.get(k) for k in ("name", "currency", "iban", "hash")}
                         for a in s["accounts"]]}
    fields = {"bank": bank, "consent_expires": s.get("valid_until") or info.get("valid_until") or "",
              "status": "pending", "error": ""}
    target = (connections.get(info["renew"]) if info.get("renew") else None) or _same_link(bank)
    if target is not None:
        old = (target.get("credentials") or {}).get("session_id") or ""
        conn = connections.update(target["id"], credentials=creds, **fields) or target
        return conn, old
    inst = catalog.find(info["institution"]) or {"id": info["institution"],
                                                 "name": info.get("institution_name") or info["aspsp"]}
    label = connections.unique_label(info.get("label") or inst.get("name") or info["aspsp"],
                                     connections_api._taken_labels())
    conn = connections.new(inst, "aggregator", eb.PROVIDER, label, "", creds)   # cash: no ISA
    conn.update(fields)
    connections.add(conn)
    return conn, ""


@router.get(eb.CALLBACK_PATH, include_in_schema=False)
def callback(request: Request, state: str = "", code: str = "", error: str = "",
             error_description: str = ""):
    info = connections.take_link_state(state)
    if info is None:
        return _page("This link has expired", "It was already used, or it is more than 30 minutes "
                     "old. Go back to the app and link the bank again.", ok=False, status=400)
    bank = info.get("aspsp") or "The bank"
    if error or not code:
        why = eb._safe(error_description)
        return _page(f"{bank} was not linked", "The bank did not give access"
                     + (f" ({why})." if why else ".") + " Go back to the app and try again.",
                     ok=False, status=400)
    try:
        app = eb.require_app()
        session = eb.finish_auth(app, code, bank)
        conn, old = _save(info, session)
    except ConnectorError as e:
        return _page(f"{bank} was not linked", str(e), ok=False, status=400)
    if old and old != session["session_id"]:
        threading.Thread(target=eb.end_session, args=(app, old), daemon=True).start()
    connections_api._changed(conn)        # vault + hooks (a GET does not do this by itself)
    cache.invalidate()
    conn = connections.refresh(conn["id"], wait=FIRST_WAIT, psu=_psu(request)) or conn
    lines = [f"{a.get('name') or 'Account'}{' ' + a['iban'] if a.get('iban') else ''}"
             f"{' (' + a['currency'] + ')' if a.get('currency') else ''}"
             for a in (conn.get("bank") or {}).get("accounts") or []]
    info_c = eb.consent(conn.get("consent_expires"))
    if info_c and not info_c["expired"]:
        lines.append(f"Bank access until {eb.nice_date(info_c['expires'])}. The app reminds you to "
                     "renew it 14 days before.")
    if conn.get("status") == "error" and conn.get("error"):
        lines.append(f"Balances: {conn['error']}")
    word = "renewed" if old else "linked"
    return _page(f"{bank} is {word}", f"{bank} is {word} — you can go back to the app.",
                 ok=True, details=lines)


_STYLE = """:root{--bg:#0b0e11;--panel:#151a21;--text:#e8edf2;--muted:#8b98a9;--green:#26a862;
--red:#f0616d;--accent:#4c8dff;--border:#232b36}
@media (prefers-color-scheme: light){:root{--bg:#f4f6f9;--panel:#fff;--text:#17202b;
--muted:#5b6673;--green:#118a53;--red:#d43d4f;--accent:#2f6fed;--border:#e4e9ef}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:460px;margin:12vh auto 40px;padding:24px 20px;background:var(--panel);
border:1px solid var(--border);border-radius:16px}
.mark{width:44px;height:44px;border-radius:50%;display:flex;align-items:center;
justify-content:center;font-size:22px;font-weight:700;color:#fff}
.mark.ok{background:var(--green)}.mark.bad{background:var(--red)}
h1{font-size:20px;margin:14px 0 6px}p{margin:6px 0}ul{color:var(--muted);font-size:14px;padding-left:20px}
a.btn{display:inline-block;margin-top:14px;background:var(--accent);color:#fff;text-decoration:none;
padding:10px 16px;border-radius:10px;font-weight:600}.fine{color:var(--muted);font-size:13px;margin-top:14px}"""


def _page(title: str, text: str, *, ok: bool, status: int = 200,
          details: list[str] | tuple = ()) -> HTMLResponse:
    e = html.escape
    items = "".join(f"<li>{e(d)}</li>" for d in details)
    body = (f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8" />'
            f'<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />'
            f'<meta name="robots" content="noindex" /><title>{e(title)} — Stock Overview</title>'
            f'<style>{_STYLE}</style></head><body><main>'
            f'<div class="mark {"ok" if ok else "bad"}">{"✓" if ok else "!"}</div>'
            f'<h1>{e(title)}</h1><p>{e(text)}</p>{f"<ul>{items}</ul>" if items else ""}'
            f'<a class="btn" href="/settings">Back to the app</a>'
            f'<p class="fine">On iPhone: if this page opened in Safari, you can close it and go back '
            f'to the app on your home screen.</p></main></body></html>')
    return HTMLResponse(body, status_code=status,
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
