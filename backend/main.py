"""Where is my money API.

Combines holdings across brokers and serves:
  * /api/portfolio — open holdings, unrealized P/L, realized P/L, all-time total
  * /api/closed    — closed (previous) positions with realized profit
  * /api/profit    — period views (today / week / month / year)
  * /api/history   — daily snapshots for the trendline

Run:  uvicorn backend.main:app --reload
Open: http://127.0.0.1:8000
"""

from __future__ import annotations

import errno
import functools
import ipaddress
import logging
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response

try:
    from dotenv import load_dotenv

    # Load .env from the project root explicitly, regardless of launch cwd.
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

from collections import defaultdict

from . import github_sync
# fetch the cloud vault (no-op without GITHUB_TOKEN), then hydrate the private
# data files before anything reads them
_VAULT_STATE = github_sync.restore()

from .connectors import trading212
from .fx import BASE_CURRENCY, display_rates, from_eur, to_eur
from .prices import fetch_quotes, quotes_status
from . import (analytics, cache, cash, clock, countries, datafiles, finances, income, insights, keynumbers, lookup, market,
               periods, prefs, realized, reports, sectors, sources, store, trend, uktax, watch)


def _demo() -> bool:
    """Demo mode = nothing of the user's own yet (no broker key, statement,
    connected account or typed balance): only then are the sample holdings and
    a synthetic trendline shown (datafiles.demo)."""
    return datafiles.demo()

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"

# Set once the warm-up has computed the main dashboard.
_WARM_FIRST = threading.Event()


def _warm_tasks() -> list:
    """What the background warmer computes at start-up and keeps fresh (see
    backend/cache.py), most important first: what the first screen asks for,
    then the Performance chart (one big price download), then the reports the
    other tabs show. Insights' market screens are left to their first visit,
    so a start-up does not fire a burst of requests at Yahoo."""
    return [portfolio, profit, sparklines, indices, closed, cash_get, finances_get,
            trend.series, health, analytics_endpoint, harvest_endpoint, savings,
            uk_tax, annual_report, income_projection, projection_endpoint]


_STARTED = time.time()


def _keep_awake() -> None:
    """Render's free plan puts a service to sleep after 15 minutes without
    requests, and waking it takes about a minute. On Render the app pings its
    own public address every 10 minutes so it stays awake (one service uses
    about 744 of the 750 free hours a month). KEEPALIVE_SELF=0 turns it off."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url or os.getenv("KEEPALIVE_SELF", "1") == "0":
        return

    def loop() -> None:
        import requests
        while True:
            time.sleep(600)
            try:
                requests.get(url.rstrip("/") + "/healthz", timeout=30)
            except Exception:
                pass

    threading.Thread(target=loop, name="keep-awake", daemon=True).start()


@asynccontextmanager
async def _lifespan(_app):
    if os.getenv("WARMUP", "1") != "0":
        cache.start_warmer(_warm_tasks, first=_WARM_FIRST)
    else:
        _WARM_FIRST.set()
    _keep_awake()
    if mcp_server is None:        # the optional `mcp` package is not installed
        yield
        return
    async with mcp_server.http_lifespan():   # the /mcp endpoint (MCP.md)
        yield


app = FastAPI(title="Where is my money", lifespan=_lifespan)

# Login gate with a session cookie + a login page (works nicely as a phone web
# app: iOS saves the password to Keychain and unlocks it with Face ID). Disabled
# when DASHBOARD_PASSWORD is unset, so local use stays frictionless.
import secrets

_DASH_USER = os.getenv("DASHBOARD_USER", "admin")
_DASH_PASS = os.getenv("DASHBOARD_PASSWORD")
_DASH_SECRET = os.getenv("DASHBOARD_SECRET") or secrets.token_urlsafe(32)

# Reachable without logging in: the login page, the PWA assets, the
# data-free health check used by keep-alive pings, and the page a bank sends
# you back to after approving a bank link (on iPhone it often opens in Safari,
# which has no login cookie; it is protected by a single-use `state` instead,
# see banklink_api.py).
_PUBLIC = {
    "/login", "/manifest.webmanifest", "/favicon.ico",
    "/icon-180.png", "/icon-192.png", "/icon-512.png",
    "/healthz", "/sw.js",
    "/enablebanking/callback",
}


# Without a password the app is open to anyone who can reach it. That is fine
# on your own computer, never on a host: there every page except the health
# check says to set DASHBOARD_PASSWORD first. ALLOW_NO_PASSWORD=1 is for a
# copy protected some other way (e.g. Cloudflare Access, see HOSTING.md).
_LOCAL_CLIENTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_SETUP_PAGE = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Set a password</title>
<body style="font:16px/1.5 system-ui,sans-serif;max-width:560px;margin:48px auto;padding:0 16px">
<h1 style="font-size:22px">Set a password first</h1>
<p>This dashboard can be reached from other devices but has no password yet, so it
shows nothing. Add these three settings:</p>
<ul><li><code>DASHBOARD_USER</code>: a username</li>
<li><code>DASHBOARD_PASSWORD</code>: a long password</li>
<li><code>DASHBOARD_SECRET</code>: a long random text (it also encrypts your saved data)</li></ul>
<p><b>On your own computer:</b> put them in the file <code>.env</code> in the app's folder
(copy <code>.env.sample</code> to <code>.env</code> first), then stop the app and start it again.<br>
<b>On a host such as Render:</b> add them on the service's <b>Environment</b> page and wait for the restart.</p>
<p>Then reload this page. Step by step: SETUP.md in the app's folder.</p>
</body>"""


def _needs_password(request: Request) -> bool:
    if _DASH_PASS or os.getenv("ALLOW_NO_PASSWORD") == "1":
        return False
    if os.getenv("RENDER"):            # Render sets RENDER=true on every service
        return True
    host = request.client.host if request.client else ""
    proxied = request.headers.get("x-forwarded-for") or request.headers.get("cf-connecting-ip")
    return bool(proxied) or host not in _LOCAL_CLIENTS


# Two checks against other websites open in the same browser, which can send
# requests to an app on this computer:
#  * a change (POST, PUT, DELETE) sent by a page of another site is refused;
#  * without a password, the app answers only when opened by a local address
#    (localhost, an IP address, a home-network name). A site on the internet
#    can point its own name at 127.0.0.1 and read the answers otherwise ("DNS
#    rebinding"). Other names go in ALLOWED_HOSTS (comma-separated).
_READS = {"GET", "HEAD", "OPTIONS"}
_HOME_NAMES = (".local", ".lan", ".home", ".internal", ".localhost", ".home.arpa")


def _hostname(netloc: str) -> str:
    try:
        return (urlsplit("//" + netloc).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _local_name(host: str) -> bool:
    if not host or host == "localhost" or host.endswith(_HOME_NAMES) or "." not in host:
        return bool(host)
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _refused(request: Request) -> str | None:
    """Why this request is not served, or None."""
    host = request.headers.get("host") or ""
    origin = request.headers.get("origin")
    if request.method not in _READS and origin is not None:
        ours = {host.lower(), (request.headers.get("x-forwarded-host") or host).lower(),
                os.getenv("RENDER_EXTERNAL_HOSTNAME", host).lower()}   # the host's public name
        try:
            theirs = urlsplit(origin).netloc.lower()
        except ValueError:
            theirs = ""
        if not theirs or theirs not in ours:
            return ("This request came from another website and was refused. If you used this app, "
                    "reload its page and try again.")
    if not _DASH_PASS:
        name = _hostname(host)
        allowed = {h.strip().lower() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()}
        if name and not _local_name(name) and name not in allowed:
            return (f"This dashboard has no password and was opened as {name}, an address it "
                    f"does not know. Set DASHBOARD_PASSWORD, or add ALLOWED_HOSTS={name} to .env.")
    return None


@app.middleware("http")
async def _auth(request: Request, call_next):
    path = request.url.path
    if mcp_server is not None and path == mcp_server.PATH:  # MCP clients have no login cookie: /mcp checks MCP_TOKEN itself
        return await call_next(request)
    setup_first = not _DASH_PASS and _needs_password(request)   # gets the "set a password" page
    if path != "/healthz" and not setup_first:
        why = _refused(request)
        if why:
            return Response(why, status_code=403)
    if not _DASH_PASS:  # auth disabled on your own computer
        if _needs_password(request) and path not in ("/healthz", "/sw.js"):
            if path.startswith("/api"):
                return Response("Set DASHBOARD_PASSWORD first", status_code=503)
            return Response(_SETUP_PAGE, status_code=503, media_type="text/html")
        return await call_next(request)
    # /vendor/: the chart library and the font, the same files for everyone
    if path in _PUBLIC or path.startswith("/vendor/") or request.session.get("auth"):
        return await call_next(request)
    if path.startswith("/api"):
        return Response("Authentication required", status_code=401)
    return RedirectResponse("/login")


# Added after _auth so SessionMiddleware wraps it (request.session is available).
app.add_middleware(
    SessionMiddleware, secret_key=_DASH_SECRET,
    max_age=7 * 24 * 3600, same_site="lax",
)


@app.middleware("http")
async def _cache_hooks(request: Request, call_next):
    """Cache bookkeeping for every request (backend/cache.py):
    * any successful write under /api/ (upload, cash, finances, watchlist, and
      whatever endpoint is added later) marks data-derived caches out of date;
    * API reads count as activity, which keeps the background warmer going;
    * the header lets the service worker tell this app's page apart from a
      host's "waking up" page."""
    response = await call_next(request)
    cache.after_request(request.method, request.url.path, response.status_code)
    response.headers["X-Stock-Overview"] = "1"
    return response


# Outermost: compress JSON and HTML (index.html ~83 KB -> ~20 KB).
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


# disk full, read-only, missing folder... (not every name exists on Windows)
_FILE_ERRNOS = {getattr(errno, n) for n in ("ENOSPC", "EDQUOT", "EACCES", "EPERM", "EROFS", "ENOENT",
                                            "EISDIR", "ENOTDIR", "EIO") if hasattr(errno, n)}


def _problem_text(exc: BaseException) -> str:
    """What the browser shows when an /api/ call fails unexpectedly: plain
    words, no stack trace (that goes to the server log)."""
    from .connectors.live import ConnectorError
    if isinstance(exc, (datafiles.DataFileError, ConnectorError)):
        return str(exc)
    if isinstance(exc, OSError) and exc.errno in _FILE_ERRNOS:
        return (f"A file on the server could not be read or saved ({exc.strerror or type(exc).__name__}). "
                "Check that the data folder exists, can be written to, and that the disk is not full. "
                "Nothing was changed.")
    return ("A data source did not answer or sent something unexpected (most often Yahoo Finance, "
            "for prices: check the internet connection). The rest of the dashboard is not affected. "
            "Try again in a minute; if it keeps happening, the app's log says more: the Terminal "
            f"window where it runs, or your host's Logs page ({type(exc).__name__}).")


@app.exception_handler(Exception)
async def _unexpected_error(request: Request, exc: Exception):
    """Any error an endpoint did not handle. /api/ calls get a JSON answer
    ({"detail": ...}, like HTTPException) the card can show."""
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": _problem_text(exc)}, status_code=500)
    return Response("Something went wrong. Reload the page in a minute.", status_code=500,
                    media_type="text/plain")


@app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
async def healthz():
    """Cheap liveness check for keep-alive pings: no auth, no data, no work.
    `version` is the deployed commit (Render sets RENDER_GIT_COMMIT; the repo is public)."""
    return {"ok": True, "version": (os.getenv("RENDER_GIT_COMMIT") or "")[:7],
            "up_minutes": round((time.time() - _STARTED) / 60)}


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(FRONTEND_DIR / "login.html")


# Wrong passwords: at most 5 per visitor and 50 in total per 15 minutes; then
# the login waits (a session you already have keeps working). The visitor is
# told apart by the first forwarded address, which a caller can fake, so the
# total cap is what really stops guessing.
_FAIL_WINDOW, _FAIL_PER_CLIENT, _FAIL_TOTAL = 15 * 60, 5, 50
_fails: dict[str, list[float]] = {}
_fail_lock = threading.Lock()


def _client_key(request: Request) -> str:
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "?")


def _login_blocked(key: str, now: float) -> bool:
    with _fail_lock:
        for k in list(_fails):
            _fails[k] = [t for t in _fails[k] if now - t < _FAIL_WINDOW]
            if not _fails[k]:
                del _fails[k]
        return (len(_fails.get(key, ())) >= _FAIL_PER_CLIENT
                or sum(len(v) for v in _fails.values()) >= _FAIL_TOTAL)


@app.post("/login", include_in_schema=False)
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    key, now = _client_key(request), time.time()
    if _login_blocked(key, now):
        return RedirectResponse("/login?e=2", status_code=303)
    ok = (_DASH_PASS and secrets.compare_digest(username, _DASH_USER)
          and secrets.compare_digest(password, _DASH_PASS))
    if ok:
        with _fail_lock:
            _fails.pop(key, None)
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    with _fail_lock:
        _fails.setdefault(key, []).append(now)
    return RedirectResponse("/login?e=1", status_code=303)


@app.get("/logout", include_in_schema=False)
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

# Saves that read a file, change it and write it back (balances, budget,
# watchlist) run one at a time, so two saves at once never lose one of them.
# Reads are never held up. (Uploads run on the event loop, one at a time.)
_save_lock = threading.Lock()


def _one_save_at_a_time(fn):
    @functools.wraps(fn)
    def run(*args, **kwargs):
        with _save_lock:
            return fn(*args, **kwargs)
    return run


def _move_eur(value_eur: float, price: float, h, q: dict, ref: float | None,
              ccy: str | None = None) -> float | None:
    """EUR change in a holding's value since the market price `ref`.

    When the price comes from the market data itself, or the broker quotes the
    same currency, it is (price - ref) x quantity, in `ccy` (the currency the
    row is valued in, default the holding's). While the market has not
    traded today its price is still the previous close (prices.py); a broker
    price that differs from it then (after-hours, another venue, a stale
    price) is no session's move, so the market's own price counts and a
    closed market shows no change. When the broker quotes another listing in
    another currency (e.g. a euro line of a US share), the market's % move is
    applied to the holding's value instead, so the two currencies are never
    mixed."""
    if not ref:
        return None
    mkt, qccy = q.get("price"), q.get("currency") or ""
    ccy = ccy or h.currency
    if h.price is None or not qccy or qccy == ccy:
        now = price
        if h.price is not None and mkt and mkt == q.get("prev_close"):
            now = mkt
        return to_eur((now - ref) * h.quantity, ccy)
    if not mkt:
        return None
    pct = mkt / ref - 1
    return value_eur * pct / (1 + pct)


@cache.cached(ttl=10, copy=True)
def _open_holdings_rows() -> tuple[list[dict], float, float, dict]:
    """Returns (rows, total_value_eur, total_cost_eur, value_by_broker).
    Shared by most endpoints, so it is memoised for 10 s (a page load computes
    it once) and recomputed at once after any upload or edit. Callers get
    their own copy and may modify it.

    Money in a row is rounded to the cent for display; the unrounded amounts
    are kept in row["_exact"], and every total is summed from those and
    rounded once, so a total never differs from the exact sum."""
    holdings = sources.holdings()

    # Live quotes (price + previous close) for every holding — the previous
    # close drives the per-holding "today" change.
    quotes = fetch_quotes([h.ticker for h in holdings])

    rows: list[dict] = []
    total_value = total_cost = 0.0
    by_broker: dict[str, float] = {}

    for h in holdings:
        q = quotes.get(h.ticker, {})
        if h.price is not None:          # the broker's own price
            price, ccy = h.price, h.currency
        elif q.get("price"):             # the market's, in the market's currency
            price, ccy = q["price"], q.get("currency") or h.currency
        else:
            # No price anywhere: valued at cost (priced_live False flags it).
            # The cost is in its own currency, so convert it to the row's.
            ccy = h.currency
            price = from_eur(to_eur(h.buy_price, h.cost_currency()), ccy)
        value_eur = to_eur(price * h.quantity, ccy)
        if h.broker_profit_eur is not None:
            # Trust the broker's exact P/L; derive cost from it.
            profit_eur = h.broker_profit_eur
            cost_eur = value_eur - profit_eur
        else:
            cost_eur = to_eur(h.buy_price * h.quantity, h.cost_currency())
            profit_eur = value_eur - cost_eur

        # Today's change: value now minus value at the last close before today
        # (calendar day: zero until the market trades today), in EUR.
        daily_eur = _move_eur(value_eur, price, h, q, q.get("prev_close"), ccy)

        total_value += value_eur
        total_cost += cost_eur
        by_broker[h.broker] = by_broker.get(h.broker, 0.0) + value_eur

        rows.append(
            {
                "broker": h.broker,
                "ticker": h.ticker,
                "isin": h.isin,
                "name": h.name,
                "quantity": h.quantity,
                "currency": ccy,         # of `price`
                "buy_price": round(h.buy_price, 2),
                "price": round(price, 2),
                "value_eur": round(value_eur, 2),
                "cost_eur": round(cost_eur, 2),
                "profit_eur": round(profit_eur, 2),
                "profit_pct": round((profit_eur / cost_eur * 100) if cost_eur else 0, 2),
                "daily_eur": round(daily_eur, 2) if daily_eur is not None else None,
                "priced_live": h.ticker in quotes or (h.price is not None and h.price > 0),
                "wrapper": h.wrapper,
                # market quote behind the "today" and period changes (periods.py)
                "_mkt": {"price": q.get("price"), "prev": q.get("prev_close"),
                         "currency": q.get("currency") or "", "own_price": h.price is not None},
                "_exact": {"value_eur": value_eur, "cost_eur": cost_eur,
                           "profit_eur": profit_eur, "daily_eur": daily_eur},
            }
        )

    rows.sort(key=lambda r: r["value_eur"], reverse=True)
    return rows, total_value, total_cost, by_broker


def _merge_holdings(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Merge the same company held at multiple brokers into one holding (by ISIN)
    and aggregate value by sector for the allocation chart. Sums the rows'
    unrounded amounts and rounds once, so a merged holding, its sector and
    the portfolio total agree with each other."""
    sector_map = sectors.get_sectors([r["ticker"] for r in rows if r["ticker"]])

    merged: dict[str, dict] = {}
    for r in rows:
        key = r["isin"] or r["ticker"] or r["name"]
        m = merged.get(key)
        if m is None:
            m = merged[key] = {
                "name": r["name"], "ticker": r["ticker"], "isin": r["isin"],
                "sector": sector_map.get(r["ticker"], "Other"),
                "brokers": [], "quantity": 0.0, "price": r["price"],
                "currency": r["currency"], "value_eur": 0.0, "_cost": 0.0,
                "profit_eur": 0.0, "daily_eur": 0.0, "_has_daily": False,
                "priced_live": False,
            }
        e = r.get("_exact") or r
        m["quantity"] += r["quantity"]
        m["value_eur"] += e["value_eur"]
        m["_cost"] += e["cost_eur"]
        m["profit_eur"] += e["profit_eur"]
        if r.get("daily_eur") is not None:
            m["daily_eur"] += e["daily_eur"]
            m["_has_daily"] = True
        m["priced_live"] = m["priced_live"] or r["priced_live"]
        if r["broker"] not in m["brokers"]:
            m["brokers"].append(r["broker"])

    out = []
    exact_value: dict[int, float] = {}
    for m in merged.values():
        cost = m.pop("_cost")
        has_daily = m.pop("_has_daily")
        value, profit, daily = m["value_eur"], m["profit_eur"], m["daily_eur"]
        m["quantity"] = round(m["quantity"], 4)
        m["value_eur"] = round(value, 2)
        m["profit_eur"] = round(profit, 2)
        m["profit_pct"] = round((profit / cost * 100) if cost else 0, 2)
        # today's change: value now vs (value − today's move)
        prev_val = value - daily
        m["daily_eur"] = round(daily, 2) if has_daily else None
        m["daily_pct"] = round((daily / prev_val * 100), 2) \
            if has_daily and prev_val else None
        exact_value[id(m)] = value
        out.append(m)
    out.sort(key=lambda x: x["value_eur"], reverse=True)

    by_sector: dict[str, float] = defaultdict(float)
    for m in out:
        by_sector[m["sector"]] += exact_value[id(m)]
    by_sector_list = [
        {"sector": s, "value": round(v, 2)}
        for s, v in sorted(by_sector.items(), key=lambda kv: -kv[1])
    ]
    return out, by_sector_list


def _fx_live(rows: list[dict]) -> bool:
    """Were live exchange rates used for every non-EUR holding? (fx.py keeps
    the last live rate per currency; without one it uses a fixed fallback.)"""
    from . import fx
    need = {fx.unit(r["currency"])[1] for r in rows} - {"EUR"}
    return need <= set(fx._cache)


@app.get("/api/portfolio")
def portfolio() -> dict:
    rows, total_value, total_cost, by_broker = _open_holdings_rows()
    holdings, by_sector = _merge_holdings(rows)
    unrealized = total_value - total_cost
    realized_total = realized.summary()["total_realized_eur"]
    inc = _income_summary()
    all_time = unrealized + realized_total
    daily_exact = periods.today_total(rows)   # the same sum as the Performance card's "today"
    daily = round(daily_exact, 2)
    prev_value = total_value - daily_exact

    return {
        "base_currency": BASE_CURRENCY,
        "fx": display_rates(),
        "demo_mode": _demo(),
        # {as_of, refreshing}: quotes older than a minute are shown while a
        # background refresh runs; the UI then says "updating…" and re-polls.
        # `unpriced`: holdings without a live price (valued at what was paid);
        # `fx_live`: False while exchange rates are the built-in fallback.
        "quotes": {**quotes_status([r["ticker"] for r in rows]),
                   "unpriced": sum(1 for r in rows if not r["priced_live"]),
                   "fx_live": _fx_live(rows)},
        # sources that could not be read just now, in plain words (sources.py)
        "problems": sources.problems(),
        "totals": {
            "value": round(total_value, 2),
            "cost": round(total_cost, 2),
            "unrealized": round(unrealized, 2),
            "unrealized_pct": round((unrealized / total_cost * 100) if total_cost else 0, 2),
            "realized": realized_total,
            "all_time": round(all_time, 2),
            "daily": daily,
            "daily_pct": round((daily_exact / prev_value * 100), 2) if prev_value else 0,
            "dividends": inc["dividends"],
            "interest": inc["interest"],
            "income": inc["total"],
            "fees": inc["fees"],
            # everything you made: price gains (realized + unrealized) +
            # dividends + interest, after fees charged outside trades
            # (None, shown as "—", when the income could not be read)
            "total_return": round(all_time + inc["total"] + inc["fees"], 2)
            if inc["total"] is not None else None,
        },
        "income_by_broker": inc["by_broker"],
        "by_broker": [
            {"broker": b, "value": round(v, 2)}
            for b, v in sorted(by_broker.items(), key=lambda kv: -kv[1])
        ],
        "by_sector": by_sector,
        "holdings": holdings,
    }


def _income_summary() -> dict:
    """income.summary(), or (when a damaged statement makes it fail) every
    part None with the reason in problems(): the dashboard then shows "—" and
    says why, instead of failing as a whole or counting the income as 0."""
    try:
        out = income.summary()
        sources._clear("income")
        return out
    except Exception as exc:
        sources._note("income", "Dividends and interest", f"{sources._why(exc, 'They')}. "
                      "They are left out of the total return for now.")
        return {"by_broker": {}, "dividends": None, "interest": None, "total": None, "fees": None}


@app.get("/api/closed")
def closed() -> dict:
    """Previous (fully closed) positions and the realized profit on them."""
    return realized.summary()


@app.get("/api/profit")
def profit() -> dict:
    """Profit today, this week, this month and this year, plus the all-time
    figures. Slower than /api/portfolio because it pulls price history."""
    rows, total_value, total_cost, _ = _open_holdings_rows()
    unrealized = total_value - total_cost
    realized_total = realized.summary()["total_realized_eur"]
    period = periods.compute(rows)
    # Week, month and year: the profit actually made in the period, from the
    # same history as the chart. The price move of today's holdings since the
    # period began (what compute() gives) is kept beside it as `price_move`.
    today = clock.today()
    try:
        hist = _history()
    except Exception:
        hist = []
    shown = period["periods"]
    period["price_move"] = {k: shown.get(k) for k in ("week", "month", "year")}
    shown.update(periods.from_history(hist, clock.period_starts(today), today, shown.get("today")))
    period.setdefault("starts", {k: v.isoformat() for k, v in clock.period_starts(today).items()})
    return {
        "unrealized": round(unrealized, 2),
        "realized": realized_total,
        "all_time": round(unrealized + realized_total, 2),
        **period,
    }


def _history() -> list[dict]:
    """Value and profit per day since the first trade, ending with today's
    live totals (trend.py)."""
    _, total_value, total_cost, _ = _open_holdings_rows()
    inc = income.summary()
    if _demo():  # sample data: a made-up past ending on today's totals (store.py)
        return store.demo_history(total_value, total_cost,
                                  realized.summary()["total_realized_eur"] + inc["total"] + inc["fees"])
    return trend.history(total_value, total_cost, realized.summary()["total_realized_eur"],
                         inc["total"] + inc["fees"])


@app.get("/api/history")
def history() -> list[dict]:
    return _history()


@app.get("/api/indices")
def indices() -> list[dict]:
    """Market snapshot for the ticker strip (S&P 500, Nasdaq, AEX, EUR/USD…)."""
    return market.indices()


@app.get("/api/sparklines")
def sparklines() -> dict[str, list[float]]:
    """~1 month of daily closes per held ticker, for the row mini-charts."""
    tickers = [h.ticker for h in sources.holdings() if h.ticker]
    return market.sparklines(tickers)


@app.get("/api/news/{ticker}")
def news(ticker: str) -> list[dict]:
    """Recent headlines for one holding (used in the detail view)."""
    return market.news(ticker)


@app.get("/api/ticker_history/{ticker}")
def ticker_history(ticker: str, range: str = "1mo") -> list[float]:
    """Price history for the detail-sheet chart at a chosen range."""
    return market.ticker_history(ticker, range)


@app.get("/api/quote/{ticker}")
def quote(ticker: str) -> dict:
    """Price lookup for any share, fund, index, currency or crypto (lookup.py)."""
    try:
        return lookup.quote(ticker)
    except lookup.QuoteError as e:
        raise HTTPException(e.status, str(e))


@app.get("/api/quote/{ticker}/chart")
def quote_chart(ticker: str, range: str = "1mo") -> dict:
    """The price lookup's chart for one range: 1d, 5d, 1mo, 6mo, 1y or 5y."""
    try:
        return lookup.chart(ticker, range)
    except lookup.QuoteError as e:
        raise HTTPException(e.status, str(e))


@app.get("/api/keynumbers/{ticker}")
def key_numbers(ticker: str) -> dict:
    """P/E, EBITDA, margins, debt and more for one share or fund (keynumbers.py)."""
    try:
        return keynumbers.get(ticker)
    except lookup.QuoteError as e:
        raise HTTPException(e.status, str(e))


@app.get("/api/datastatus")
def datastatus() -> dict:
    """Which data sources are live vs sample — shown as a banner in the UI."""
    out = datafiles.status()
    out["_t212_accounts"] = trading212.status()
    out["_vault"] = _VAULT_STATE
    out["_trend"] = trend.last_build
    # disk_kept: on your own computer the data/ folder survives restarts, so
    # no cloud copy is needed; a free host (Render) wipes its disk.
    out["_sync"] = {**github_sync.last_result, "pending": github_sync.pending(),
                    "token": bool(os.getenv("GITHUB_TOKEN")),
                    "disk_kept": not os.getenv("RENDER")}
    # Settings shows how to connect Claude (MCP.md): the start command with
    # this copy's own path on your computer, or the /mcp address on a host.
    own_computer = not os.getenv("RENDER") and not os.path.exists("/.dockerenv")
    out["_mcp"] = {"dir": str(datafiles.ROOT) if own_computer else None,
                   "python": sys.executable if own_computer else None,
                   "url_on": bool(os.getenv("MCP_TOKEN"))}
    return out


@app.get("/api/checkup")
def checkup_endpoint() -> dict:
    """Check-up: what needs attention and exactly what to do (checkup.py).
    Reads only what the app already knows; never waits long for a source."""
    from . import checkup, version
    version.check_if_due()          # the optional daily check for a newer version
    return checkup.run(checkup.Context(_open_holdings_rows, _VAULT_STATE))


@app.get("/api/version")
def version_endpoint() -> dict:
    """This copy's version, how to update it, and (when switched on in
    Settings) what the daily check for a newer version found (version.py)."""
    from . import version
    version.check_if_due()
    return version.status()


# Back up and restore (Settings; backend/backup.py): one zip file with your data files.
@app.get("/api/backup")
def backup_download():
    from . import backup
    try:
        blob, _ = backup.make()
    except backup.BackupError as e:
        raise HTTPException(404, str(e))
    backup.mark_downloaded()
    name = f"stock-overview-backup-{time.strftime('%Y-%m-%d')}.zip"
    return Response(blob, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.get("/api/backup/info")
def backup_info() -> dict:
    from . import backup
    return backup.info()


@app.post("/api/backup/check")
def backup_check(file: UploadFile = File(...)) -> dict:
    """What a restore of this file would do; nothing is changed."""
    from . import backup
    try:
        return backup.preview(file.file.read(backup.MAX_ZIP + 1))
    except backup.BackupError as e:
        raise HTTPException(400, str(e))


@app.post("/api/backup/restore")
@_one_save_at_a_time
def backup_restore(file: UploadFile = File(...), confirm: str = Form("")) -> dict:
    """Replace your data files with a backup's (checked first; the old files
    are kept in data/before-restore-<date>/)."""
    from . import backup
    if confirm != "yes":
        raise HTTPException(400, "A restore replaces your current data: confirm it first.")
    try:
        return backup.restore(file.file.read(backup.MAX_ZIP + 1))
    except backup.BackupError as e:
        raise HTTPException(400, str(e))


@app.get("/settings", include_in_schema=False)
def settings_page():
    return FileResponse(FRONTEND_DIR / "settings.html")


@app.get("/api/cash")
def cash_get() -> dict:
    """Bank/cash accounts + EUR total (edited in-app on the Settings page)."""
    return cash.load()


@app.post("/api/cash")
@_one_save_at_a_time
def cash_upsert(name: str = Form(...), institution: str = Form(""),
                balance: float = Form(...), currency: str = Form("EUR"),
                type: str = Form("cash"), rate: float = Form(0)) -> dict:
    if not name.strip():
        raise HTTPException(400, "Fill in the account name.")
    out = cash.upsert(name, institution, balance, currency, type, rate)
    github_sync.push_vault_async()
    return out


@app.post("/api/cash/auto")
@_one_save_at_a_time
def cash_auto(name: str = Form(...), rate: str = Form(""), type: str = Form("")) -> dict:
    """Interest rate (blank = not known) and kind of an account that updates
    itself: Trading 212 cash, Starling, a linked bank."""
    try:
        value = float(rate.replace(",", ".")) if rate.strip() else None
    except ValueError:
        raise HTTPException(400, "The interest rate must be a number, for example 3.5")
    if value is not None and not 0 <= value <= 25:
        raise HTTPException(400, "The interest rate must be between 0 and 25 percent a year")
    out = cash.set_auto(name, value, type or None)
    github_sync.push_vault_async()
    return out


@app.delete("/api/cash/{name}")
@_one_save_at_a_time
def cash_delete(name: str) -> dict:
    out = cash.delete(name)
    github_sync.push_vault_async()
    return out


@app.post("/api/upload/{kind}")
async def upload_statement(kind: str, file: UploadFile = File(...)) -> dict:
    """Upload a broker export from the browser/phone — takes effect immediately.
    Accepts the files brokers actually hand out and converts them server-side:
      degiro          account statement PDF, or the CSV export
      trade_republic  the app's "Transaction export" CSV or the account statement
                      PDF (both fill trades AND dividends/interest), or the trades JSON
      tr_income       dividends/interest JSON (only needed for hand-transcribed data)
      t212_statement  Trading 212 activity-statement PDF, its converted JSON, or
                      a History CSV export (added to the statement already
                      uploaded when it continues it)
    Light validation so a wrong file fails loudly, not silently."""
    from .importers import (degiro_pdf, traderepublic_csv, traderepublic_pdf, trading212_csv,
                            trading212_pdf)
    import json as _json

    content = await file.read()
    # A statement of many years is a few MB. Much larger files are something
    # else, and reading one can use more memory than a small host has.
    if len(content) > 12_000_000:
        raise HTTPException(413, "That file is larger than 12 MB, which is more than a broker "
                                 "statement should be. Export a shorter period, or check it is the right file.")
    if not content.strip():
        raise HTTPException(400, "That file is empty. Download it again from your broker and upload the new file.")
    is_pdf = content[:5] == b"%PDF-"
    text = "" if is_pdf else _decode_text(content)
    if not is_pdf:
        content = text.encode("utf-8")   # saved as UTF-8, so it can always be read back
    report: dict = {}
    try:
        if kind == "degiro":
            from .connectors.degiro import check_statement
            if is_pdf:
                text, report = degiro_pdf.convert(content)
                content = text.encode("utf-8")
            # any statement language the app reads; else a 400 that says why
            report = {**check_statement(text, pdf=is_pdf), **report}
            datafiles.save_upload("degiro", content)
        elif kind == "trade_republic":
            if is_pdf:
                trades, income_, report = traderepublic_pdf.convert(content)
                if not trades["trades"] and not income_["income"]:
                    raise HTTPException(400, "No trades or payouts found in that PDF. Upload the account statement from "
                                             "the Trade Republic app: Profile → Account statements.")
                datafiles.save_file("trades_trade_republic.json", _json.dumps(trades, indent=1).encode())
                datafiles.save_file("income_trade_republic.json", _json.dumps(income_, indent=1).encode())
            elif traderepublic_csv.looks_like(text):
                trades, income_, report = traderepublic_csv.convert(text)
                datafiles.save_file("trades_trade_republic.json", _json.dumps(trades, indent=1).encode())
                datafiles.save_file("income_trade_republic.json", _json.dumps(income_, indent=1).encode())
            else:
                if "trades" not in _loads_or_400(text, _NOT_TR):
                    raise HTTPException(400, _NOT_TR)
                datafiles.save_upload("trade_republic", content)
        elif kind == "tr_income":
            wrong = ('That is not a Trade Republic dividends file: it must be JSON with an "income" '
                     'list, as data/income_trade_republic.sample.json. Most people do not need this: '
                     'the transaction export (CSV) already holds dividends and interest.')
            if "income" not in _loads_or_400(text, wrong):
                raise HTTPException(400, wrong)
            datafiles.save_upload("tr_income", content)
        elif kind == "t212_statement" and (is_pdf or trading212_csv.looks_like(text)):
            if is_pdf:
                st, report = trading212_pdf.convert(content)
                if not st["trades"] and not st["positions"]:
                    raise HTTPException(400, "No trades or positions found in that PDF. Upload the Trading 212 activity "
                                             "statement of the Invest account: Menu → History → Export / Activity statement.")
            else:
                st, report = trading212_csv.convert(text)
            # joined to the statement already uploaded, never silently replacing it
            path, real = datafiles.resolve("trading212_statement.json")
            st, report = trading212_csv.combine(_json.loads(path.read_text()) if real else None, st, report)
            datafiles.save_upload("t212_statement",
                                  _json.dumps(st, indent=1, ensure_ascii=False).encode())
        elif kind == "t212_statement":
            if not {"positions", "trades"} & set(_loads_or_400(text, _NOT_T212)):
                raise HTTPException(400, _NOT_T212)
            datafiles.save_upload("t212_statement", content)
        else:
            raise HTTPException(404, "Unknown kind of statement. Reload the page and upload it again.")
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    # (the trendline rebuilds itself: trend.py notices the new trades)
    github_sync.push_vault_async()
    out = datafiles.status()
    out["_report"] = report
    return out


@app.delete("/api/upload/{kind}")
@_one_save_at_a_time
def remove_statement(kind: str) -> dict:
    """Remove an uploaded statement (DEGIRO, Trade Republic, Trading 212), so
    that account no longer counts. The broker's own records are not touched:
    upload the file again to bring it back."""
    try:
        gone = datafiles.remove_upload(kind)
    except ValueError:
        raise HTTPException(404, "Unknown kind of statement. Reload the page and try again.")
    except OSError as e:
        raise HTTPException(500, f"The file could not be removed ({e.__class__.__name__}). Check that the "
                                 "data folder can be written to, then try again.")
    if not gone:
        raise HTTPException(404, "There is no uploaded statement to remove here.")
    cache.invalidate()
    github_sync.push_vault_async()
    out = datafiles.status()
    out["_removed"] = gone
    return out


def _decode_text(content: bytes) -> str:
    """An uploaded text file as text: UTF-8 (with or without a byte-order
    mark), else Windows-1252, which spreadsheet programs often save CSVs in."""
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


# What to say when an upload is not the file the broker's slot expects. Most
# people upload a CSV or PDF, never JSON, so the message names those.
_NOT_TR = ("That doesn't look like a Trade Republic file. Upload the transaction export (CSV) or the "
           "account statement (PDF): in the Trade Republic app, Profile → Account statements.")
_NOT_T212 = ("That doesn't look like a Trading 212 statement. Upload the activity statement (PDF) or a "
             "History export (CSV): in Trading 212, Menu → History → Export.")


def _loads_or_400(text: str, wrong_file: str) -> dict:
    """The upload as JSON (the app's own format, for hand-made files), or a
    400 with `wrong_file`: it is then most likely another broker's file."""
    import json as _json
    try:
        payload = _json.loads(text)
    except ValueError:
        raise HTTPException(400, wrong_file)
    return payload if isinstance(payload, dict) else {}


# "Add an account": catalog, connected accounts, live connectors, CSV import,
# bank links through Enable Banking, and the Trade Republic sign-in.
from . import banklink_api, connections_api, traderepublic_api  # noqa: E402

app.include_router(connections_api.router)
app.include_router(banklink_api.router)
app.include_router(traderepublic_api.router)

# Claude and other MCP clients: read-only tools at /mcp, behind MCP_TOKEN (MCP.md)
# The connector needs the `mcp` package. It is optional: the dashboard itself
# never depends on Claude or any other AI service, and starts without it.
try:
    from . import mcp_server  # noqa: E402
except ImportError as _e:      # `pip install -r requirements.txt` adds it
    mcp_server = None
    logging.getLogger("stock_overview").info("Claude connector off: %s", _e)
else:
    mcp_server.mount(app)


def _connections_changed(trades_changed: bool) -> None:
    """A connection was added/removed: leave (or return to) demo mode. (The
    trendline rebuilds itself when the trade history changes, see trend.py.)"""
    datafiles.refresh()


connections_api.after_change.append(_connections_changed)


@app.get("/api/analytics")
def analytics_endpoint() -> dict:
    """Quant metrics: beta, volatility, Sharpe, max drawdown, concentration,
    currency exposure. Computed from snapshot history + current holdings."""
    rows, *_ = _open_holdings_rows()
    holdings, _ = _merge_holdings(rows)
    return analytics.summary(holdings)


@app.get("/api/brief")
def brief(ccy: str = "EUR") -> dict:
    """The Daily Brief: generated portfolio summary + renowned-source headlines."""
    port = portfolio()
    ana = analytics.summary(port["holdings"])
    return insights.daily_brief(port, ana, ccy.upper())


@app.get("/api/prefs")
def prefs_get() -> dict:
    """The choices made in Settings: country, currency, time zone, colours (prefs.py)."""
    return prefs.get()


@app.post("/api/prefs")
@_one_save_at_a_time
def prefs_save(changes: dict = Body(...)) -> dict:
    """Save one or more choices: a JSON object such as {"currency": "USD"}.
    null forgets a choice."""
    try:
        out = prefs.update(changes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    cache.invalidate()                 # country, currency and time zone change what is shown
    github_sync.push_vault_async()
    return out


@app.get("/api/dismissed")
def dismissed_get() -> dict:
    """Warnings the user chose to hide: {"keys": {key: day}} (prefs.dismissed)."""
    return {"keys": prefs.dismissed()}


@app.post("/api/dismissed")
@_one_save_at_a_time
def dismissed_save(body: dict = Body(...)) -> dict:
    """Hide a warning ({"key": ..., "dismissed": true}) or show it again
    ("dismissed": false). A check-up finding's key is its id."""
    try:
        keys = prefs.set_dismissed(body.get("key"), bool(body.get("dismissed", True)),
                                   clock.today().isoformat())
    except ValueError as e:
        raise HTTPException(400, str(e))
    github_sync.push_vault_async()
    return {"keys": keys}


@app.get("/api/fx")
def fx_rates() -> dict:
    """EUR -> GBP/USD rates for the browser's currency switch (amounts are
    computed in EUR and shown in the currency the viewer picks)."""
    return display_rates()


@app.get("/api/uk_tax")
def uk_tax() -> dict:
    """UK tax year so far: realised gains/losses and dividends outside ISAs,
    ISA allowance used, and the 30-day rule for recent sales. Only when the
    country chosen in Settings is the UK; otherwise {"applies": false} and the card hides."""
    from .harvest import residence
    if residence() != "UK":
        return {"applies": False}
    return uktax.summary()


@app.get("/api/harvest")
def harvest_endpoint(min_pct: float | None = None, min_loss: float | None = None) -> dict:
    """Tax-loss harvesting: positions outside ISAs down enough to sell for a
    tax loss, the tax saved, the 30-day rebuy date and swaps to stay invested.
    Thresholds default to HARVEST_MIN_LOSS_PCT / HARVEST_MIN_LOSS (10%, £250)."""
    from . import harvest
    rows, *_ = _open_holdings_rows()
    return harvest.suggest(rows, min_pct=min_pct, min_loss=min_loss)


@app.get("/api/savings")
def savings() -> dict:
    """Ways to save: money-saving tips ranked by size (ISA and CGT allowances,
    dividend tax, fund fees, pension relief, loans vs cash). See advisors.py."""
    from . import advisors

    rows, *_ = _open_holdings_rows()
    return advisors.collect(rows, demo=_demo())


@app.get("/api/headlines")
def headlines_endpoint(country: str | None = None) -> list[dict]:
    """Interleaved market headlines: the news feeds of `country` (default: the
    country chosen for the news in Settings, else your country; "" for none),
    then CNBC / MarketWatch / Yahoo Finance (+ NewsAPI when a NEWSAPI_KEY env
    var is provided). Each has title, url (http/https), source, published_at
    (ISO, UTC, or null), language and country ("" = international)."""
    try:
        code = None if country is None else countries.clean_code(country)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return insights.headlines(code)


@app.get("/api/countries")
def countries_list() -> dict:
    """Every country for the picker in Settings, with what the app has for
    each (tax overview, news, tax cards, suggested currency and time zone),
    and `problems`: country files left out because they break SCHEMA.md."""
    return {"countries": countries.all(), "tax_cards": list(countries.TAX_CARDS),
            "problems": countries.problems()}


@app.get("/api/country")
def country_overview(code: str | None = None) -> dict:
    """How tax on investments works in the chosen country (or `code`):
    `available` false with a `message` when no country is chosen or there is
    no file for it yet."""
    try:
        return countries.overview(code)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/earnings")
def earnings() -> list[dict]:
    """Upcoming earnings dates for your current holdings."""
    rows, *_ = _open_holdings_rows()
    return insights.earnings_calendar([r["ticker"] for r in rows])


@app.get("/api/rising_stars")
def rising_stars() -> dict:
    """Transparent small-cap screener — method string included in the response."""
    return insights.rising_stars()


def _merged_holdings() -> list[dict]:
    rows, *_ = _open_holdings_rows()
    return _merge_holdings(rows)[0]


@app.get("/api/health")
def health() -> dict:
    """Portfolio Health Score (0-100, graded A-E) with disclosed methodology."""
    return reports.health_score(_merged_holdings())


@app.get("/api/stress")
def stress() -> dict:
    """What-if scenarios: market ±, EUR/USD ± — scaled by your beta & FX exposure."""
    return reports.stress(_merged_holdings())


@app.get("/api/correlation")
def correlation() -> dict:
    """1-year daily-return correlation matrix across holdings."""
    return reports.correlation([h["ticker"] for h in _merged_holdings()])


@app.get("/api/income_projection")
def income_projection() -> dict:
    """Forward dividend income: trailing-12M dividends/share × shares, in EUR."""
    return reports.income_projection(_merged_holdings())


@app.get("/api/annual_report")
def annual_report() -> dict:
    """Realized P/L + dividend income per calendar year."""
    return reports.annual_report()


def _liquid_assets() -> float | None:
    """Investments + cash (the holdings are cached for the dashboard anyway).
    None when either cannot be read: Freedom Day is then left out rather than
    worked out from a 0."""
    try:
        return _open_holdings_rows()[1] + (cash.load().get("total_eur") or 0.0)
    except Exception:
        return None


@app.get("/api/finances")
def finances_get() -> dict:
    """Loans, income, monthly payments + cash-flow totals and Freedom Day."""
    return finances.load(_liquid_assets())


@app.post("/api/finances/{kind}")
@_one_save_at_a_time
def finances_upsert(kind: str, name: str = Form(...), balance: float = Form(0),
                    rate: float = Form(0), monthly_payment: float = Form(0),
                    monthly_eur: float = Form(0), currency: str = Form("EUR"),
                    fixed: bool = Form(True), tax: str = Form(""),
                    pension_pct: float = Form(0), pension_type: str = Form("sacrifice"),
                    employer_pct: float = Form(0),
                    started: str = Form(""), value: float = Form(0),
                    category: str = Form("other"), paid: float = Form(0),
                    bought: str = Form("")) -> dict:
    if kind not in finances.KINDS:
        raise HTTPException(404, "Unknown kind of entry. Reload the page and try again.")
    if not name.strip():
        raise HTTPException(400, "Fill in a name.")
    ccy = currency.upper()
    if kind == "loans":
        fields = {"balance": balance, "rate": rate, "monthly_payment": monthly_payment,
                  "currency": ccy}
    elif kind == "assets":
        fields = {"value": value, "currency": ccy, "category": category.strip().lower(),
                  "paid": paid, "bought": bought.strip()[:10]}
    else:
        # `monthly_eur` is the form field's historical name; the amount is in `currency`.
        fields = {"amount": monthly_eur, "currency": ccy, "fixed": fixed}
        if kind == "income":
            if tax in ("before", "after"):
                fields["tax"] = tax
            fields["pension_pct"] = max(min(pension_pct, 100.0), 0.0)
            fields["pension_type"] = pension_type
            fields["employer_pct"] = max(min(employer_pct, 100.0), 0.0)
            fields["started"] = started.strip()[:7]
    out = finances.upsert(kind, name, fields)
    github_sync.push_vault_async()
    return out


@app.delete("/api/finances/{kind}/{name}")
@_one_save_at_a_time
def finances_delete(kind: str, name: str) -> dict:
    if kind not in finances.KINDS:
        raise HTTPException(404, "Unknown kind of entry. Reload the page and try again.")
    out = finances.delete(kind, name)
    github_sync.push_vault_async()
    return out


@app.get("/api/watchlist")
def watchlist_get() -> dict:
    """Watchlist with live quotes, daily change and sparklines."""
    return watch.load()


@app.post("/api/watchlist")
@_one_save_at_a_time
def watchlist_add(ticker: str = Form(...), name: str = Form("")) -> dict:
    out = watch.add(ticker.strip(), name.strip() or ticker.strip())
    github_sync.push_vault_async()
    return out


@app.delete("/api/watchlist/{ticker}")
@_one_save_at_a_time
def watchlist_remove(ticker: str) -> dict:
    out = watch.remove(ticker)
    github_sync.push_vault_async()
    return out


@app.get("/api/search")
def search_tickers(q: str, kinds: str = "") -> list[dict]:
    """Global ticker search (Yahoo symbol search via yfinance): shares and ETFs
    for the watchlist, or with kinds=all every kind the price lookup shows."""
    if not q.strip():
        return []
    if kinds == "all":
        try:
            return lookup.search(q)
        except lookup.QuoteError as e:
            raise HTTPException(e.status, str(e))
    return watch.search(q.strip())


@app.get("/api/hindsight")
def hindsight_endpoint() -> dict:
    """What your exits are worth today vs what you sold them for."""
    return reports.hindsight()


@app.get("/api/projection")
def projection_endpoint() -> dict:
    """10-year net-worth projection under 3/6/9% scenarios with your real
    contribution rate."""
    return reports.projection(_open_holdings_rows()[1])


@app.get("/api/export/holdings.csv")
def export_holdings():
    csv_text = reports.holdings_csv(_merged_holdings())
    return Response(csv_text, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=holdings.csv"})


@app.get("/api/benchmark")
def benchmark() -> dict[str, float]:
    """Money-weighted S&P 500 comparison: what the portfolio would be worth today
    if every euro put in (or taken out) had gone into the S&P 500 instead, valued
    on each day of the chart. Directly comparable to the portfolio value curve."""
    if _demo():
        return {}
    return trend.benchmark(_history())


# Serve the dashboard explicitly at "/" (same reliable FileResponse the login
# page uses) rather than relying on the static mount's directory-index behaviour,
# which can 404 the root on some hosts.
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


# Static assets (icons, manifest, etc.). Mounted last so routes take precedence.
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
