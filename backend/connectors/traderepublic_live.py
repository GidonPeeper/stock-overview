"""Trade Republic -- live, read-only sync through the unofficial web API.

Trade Republic has no official API. This module talks to the private API that
its own web app (app.traderepublic.com) uses, the way the open-source client
pytr does (github.com/pytr-org/pytr, MIT; endpoints, headers and message
shapes below are taken from its source and test fixtures, Nov 2026). It is
unofficial: it can stop working whenever Trade Republic changes its app, and
using it may be against Trade Republic's terms. The user decides.

READ-ONLY BY CONSTRUCTION
  * HTTP: only the web sign-in (start, confirm, resend the code), the session
    refresh and reading the securities account number. Nothing else.
  * Websocket: `Feed.get()` refuses every subscription type that is not in
    READ_SUBSCRIPTIONS (portfolio, cash, instrument name and price, the
    timeline). This app has no code for orders, savings plans, payouts,
    transfers, price alarms or settings, here or anywhere else.

Sign-in ("v2" web login, what app.traderepublic.com does today)
  POST /api/v2/auth/web/login {phoneNumber, pin}   -> {processId, countdownInSeconds}
  GET  /api/v2/auth/web/login/processes/{id}       -> {status: PENDING | CONFIRMED |
                                                       COMPLETED, requiredAction?, expiresAt?}
  POST /api/v2/auth/web/login/processes/{id}/authenticator-verification {code}
       (only accounts with an authenticator app; they may still approve in the app)
  The user approves the sign-in in the Trade Republic app; the process then
  sets the session cookies. Every login call carries X-TR-Device-Info,
  X-TR-App-Version, X-Tr-Platform and Accept-Language, or Trade Republic
  answers 400 MISSING_REQUIRED_HEADER. This web login does not log the phone
  out (the old app/device login, which does, is not implemented). It is not
  behind the AWS WAF bot challenge: pytr's contributors measured that in Aug
  2026, and one unauthenticated request from here reached the app in Nov 2026.

  Fallback "v1" web login (4-digit code from the app or by SMS):
  POST /api/v1/auth/web/login {phoneNumber, pin}   -> {processId, countdownInSeconds}
  POST /api/v1/auth/web/login/{processId}/{code}   -> session cookies
  POST /api/v1/auth/web/login/{processId}/resend   (the code by SMS)
  It sits behind an AWS WAF challenge that only a real browser passes, so it
  is used only with an `aws-waf-token`: TR_WAF_TOKEN, or Playwright if the user
  installed it (pip install playwright && playwright install chromium). Without
  one it is not tried. TR_LOGIN=v1|v2|auto picks the route (auto: v2, then v1
  if v2 is refused and a token can be had).

  The PIN is sent once, in the first request, and is never stored or kept.

Staying signed in
  The session cookie lasts about five minutes; GET /api/v1/auth/web/session
  with the refresh cookie renews it, at the start of every sync. Only Trade
  Republic's cookies and the securities account number are stored, inside the
  connection's credentials (data/connections.json: git-ignored, encrypted in
  the vault, never sent to the browser). When the refresh is refused the
  connection asks the user to sign in again (ConnectorError status SIGN_IN)
  and the dashboard keeps showing the last data it read.

Websocket (wss://api.traderepublic.com)
  connect 31 {locale, platformId: webtrading, clientId: app.traderepublic.com, ...}  -> "connected"
  sub N {json}  ->  "N A {json}" answer | "N D <delta>" change | "N C" closed | "N E {json}" error
  unsub N
  compactPortfolioByType {secAccNo}  -> categories[].positions[]: isin, netSize, averageBuyIn
  cash                               -> [{currencyId, amount}]
  instrument {id: ISIN}              -> shortName, exchangeIds, typeId
  ticker {id: ISIN.EXCHANGE}         -> last.price (only for holdings Yahoo cannot price)
  timelineTransactions / timelineActivityLog {after: cursor} -> {items[], cursors.after}
  timelineDetailV2 {id}              -> the detail page of one timeline item

Data
  Positions (ISIN, quantity, average buy price in EUR) and cash come from the
  portfolio every few minutes. The history is read from the timeline, newest
  first, at most every few hours (or when the user presses Refresh): only
  items not seen before, and the detail page only for items that move shares
  (buys, sells, savings plans, round-ups, Saveback, splits and other
  corporate actions, transfers). Dividends, interest and taxes are read from
  the timeline list itself. What was read is kept with the connection
  (`history`), so a sync asks only for what is new. Until the whole history is
  read, no trades or income are reported (half a history would give wrong
  gains); the uploaded file, if any, is used meanwhile (connectors/traderepublic.py).

  Mapping (all EUR, as Trade Republic books cash in EUR):
    buy / savings plan / round-up   Trade BUY, amount = cash paid incl. the fee
    sell                            Trade SELL, amount = cash received + tax withheld
                                    (net of the fee, like the CSV import); the tax
                                    is a fee row (negative)
    Saveback / stock perk           Trade BUY at its value + the same amount as a bonus
    split, bonus shares             BUY of the new shares at no cost
    spin-off                        BUY of the new company's shares at no cost
    reverse split, merger, swap     the cost moves from the old shares to the new ones
    worthless                       SELL at 0
    shares transferred in / out     in: at Trade Republic's average buy price (if still
                                    held, else 0); out: at cost (no gain)
    dividend, interest              net amounts as booked (after withholding tax)
    Vorabpauschale, tax correction  fee rows (negative / positive)
  Instrument names on detail pages are in German (locale "de", as in pytr's
  fixtures); numbers come both as "1.234,56" and "49.000000" and are read
  accordingly, checked against price x shares where both are shown.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import platform
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import requests

from .base import Holding
from .live import ConnectorError, Snapshot, http_message
from ..positions import Trade

NAME = "Trade Republic"
API = os.getenv("TR_API_URL", "https://api.traderepublic.com")
WS_URL = os.getenv("TR_WS_URL", "wss://api.traderepublic.com")
LOCALE = os.getenv("TR_LOCALE") or "de"

# What the web frontend sends. Trade Republic can reject a stale value at any
# time; each one can be overridden from the environment (pytr does the same).
DEFAULT_APP_VERSION = "2.2631.13"
DEFAULT_PLATFORM = "web-pro"
DEFAULT_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/146.0.0.0 Safari/537.36")
WS_PROTOCOL = 31

TIMEOUT = (5, 20)              # (connect, read) seconds for HTTP calls
POLL = 2.0                     # seconds between checks of a sign-in waiting for the app
REQUEST_TIMEOUT = 15.0         # seconds for one websocket answer
HISTORY_EVERY = 6 * 3600       # incremental history sync at most this often
HISTORY_BUDGET = 75.0          # seconds one sync may spend on the history
MAX_DETAILS = 1000             # detail pages per sync (the rest follow next time)
DETAIL_CONCURRENCY = 8         # detail pages asked for at once
OVERLAP = timedelta(days=7)    # re-read this much before the last sync (late bookings)
INSTRUMENT_RETRY = 24 * 3600   # retry an instrument Trade Republic did not describe
PARSER_VERSION = 1             # bump to re-read the whole history after a mapping change
SIGN_IN = "sign_in"            # ConnectorError.status: only a new sign-in helps
SIGN_IN_RETRY = 12 * 3600

# The only websocket subscriptions this app ever sends (all read-only).
READ_SUBSCRIPTIONS = frozenset({
    "compactPortfolioByType", "cash", "instrument", "ticker", "accountPairs",
    "timelineTransactions", "timelineActivityLog", "timelineDetailV2",
})

ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
_ISIN_ANY = re.compile(r"(?<![A-Z0-9])([A-Z]{2}[A-Z0-9]{9}[0-9])(?![A-Z0-9])")

SIGN_IN_MESSAGE = ("Trade Republic has signed this app out. Press “Sign in again” to keep it "
                   "updating; until then the dashboard shows the last data it read.")
_VERSION_MESSAGE = ("Trade Republic did not accept this app's sign-in ({code}). Trade Republic has "
                    "probably changed its web app. Try again later or update Stock Overview "
                    "(TR_APP_VERSION can override the version, see the README).")
_LOGIN_ERRORS = {
    "TOO_MANY_REQUESTS": "Too many sign-in attempts. Trade Republic asks you to wait before trying "
                         "again; this can take up to an hour.",
    "PROCESS_GONE": "The sign-in expired. Start again.",
    "ALREADY_PROCESSED": "This sign-in was already used or was declined in the app. Start again.",
    "NOT_FOUND": "Trade Republic no longer knows this sign-in. Start again.",
    "VALIDATION_CODE_INVALID": "That code is not right. Check it and try again.",
    "VALIDATION_CODE_ALREADY_USED": "That code was already used. Wait for a new one.",
}
_BAD_LOGIN = ("Trade Republic did not accept that phone number and PIN. Check both and try again "
              "(after several wrong PINs Trade Republic locks the sign-in for a while).")
_WAF_MESSAGE = ("Trade Republic's bot protection blocked the sign-in. Try again later.")
_V1_NEEDS_BROWSER = ("The sign-in with a 4-digit code needs a browser token that this app cannot "
                     "make by itself. Install Playwright (pip install playwright && playwright "
                     "install chromium) or set TR_WAF_TOKEN, see the README.")
# Answers that mean "this route is refused" (not "wrong PIN"): try the v1 route.
_V2_REFUSED = {404, 405, 410, "BLOCKED", "MOBILE_APP_VERSION_NOT_SUPPORTED", "CLIENT_VERSION_OUTDATED"}


def _env(name: str, default: str) -> str:
    return (os.getenv(name) or "").strip() or default


def user_agent() -> str:
    return _env("TR_USER_AGENT", DEFAULT_USER_AGENT)


# ------------------------------------------------------------------ helpers
def num(value, default: float | None = 0.0) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_code(code) -> str:
    code = str(code or "")
    return code if re.fullmatch(r"[A-Z0-9_\-]{1,64}", code) else ""


def _error_code(resp) -> str:
    try:
        errs = resp.json().get("errors") or []
        return _safe_code(errs[0].get("errorCode"))
    except Exception:
        return ""


def _json(resp) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise ConnectorError(f"{NAME} sent an answer this app could not read.") from None
    return data if isinstance(data, dict) else {}


def _call(http, method: str, path: str, **kw):
    """One HTTP call to the API. Network problems become a plain ConnectorError
    without the URL (it can hold the login process id)."""
    try:
        return getattr(http, method)(API + path, timeout=TIMEOUT, **kw)
    except requests.Timeout:
        raise ConnectorError(f"{NAME} did not answer in time. Try again in a minute.") from None
    except requests.RequestException:
        raise ConnectorError(f"Could not reach {NAME}. Check the internet connection.") from None


def new_http():
    """A fresh HTTP session (tests replace this with a fake)."""
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent()})
    return s


def _tz_name() -> str:
    try:
        return os.path.realpath("/etc/localtime").split("zoneinfo/")[1]
    except (OSError, IndexError):
        return "Etc/UTC"


def _device_id() -> str:
    """The same id on every sign-in from this computer, so Trade Republic sees
    the same "browser" each time: a SHA-512 hash of machine identifiers (as
    pytr does; the web app hashes a canvas fingerprint). Nothing is stored,
    and the hash reveals nothing about the machine."""
    seed = "|".join(["stock-overview", str(uuid.getnode()), platform.node(), platform.machine(),
                     platform.system()])
    return hashlib.sha512(seed.encode()).hexdigest()


def login_headers() -> dict:
    """The header set the web frontend sends on every sign-in call."""
    ua = user_agent()
    chrome = re.search(r"Chrome/([\d.]+)", ua)
    offset = datetime.now().astimezone().utcoffset()
    device = {
        "stableDeviceId": _device_id(),
        "browser": "Chrome",
        "browserVersion": chrome.group(1) if chrome else "",
        "os": platform.system(),
        "osVersion": platform.release(),
        "timezone": _tz_name(),
        "timezoneOffset": -int(offset.total_seconds() // 60) if offset else 0,
        "screen": "1920x1080x24",
        "preferredLanguages": [LOCALE],
        "numberOfCores": os.cpu_count() or 1,
    }
    return {
        "X-TR-Device-Info": base64.b64encode(json.dumps(device).encode()).decode(),
        "X-TR-App-Version": _env("TR_APP_VERSION", DEFAULT_APP_VERSION),
        "X-Tr-Platform": _env("TR_PLATFORM", DEFAULT_PLATFORM),
        "Accept-Language": LOCALE,
    }


def normalise_phone(raw: str) -> str:
    s = re.sub(r"[\s\-().\/]", "", raw or "")
    if s.startswith("00"):
        s = "+" + s[2:]
    if not re.fullmatch(r"\+[1-9]\d{6,14}", s):
        raise ConnectorError("Enter your phone number with the country code, for example "
                             "+49 170 1234567.")
    return s


def phone_hint(phone: str) -> str:
    return "…" + phone[-2:] if len(phone) > 4 else ""


def account_hash(sec_acc_no: str) -> str:
    return hashlib.sha256(("tr:" + str(sec_acc_no)).encode()).hexdigest()[:16]


# -------------------------------------------------------------- cookies
def cookies_out(jar) -> list[dict]:
    """Trade Republic's cookies from a cookie jar, as plain data to store."""
    out = []
    for c in jar:
        if (c.domain or "").lstrip(".").endswith("traderepublic.com") and c.value:
            out.append({"name": c.name, "value": c.value, "domain": c.domain,
                        "path": c.path or "/", "expires": c.expires})
    return out


def cookies_in(jar, cookies: list[dict] | None) -> None:
    for c in cookies or []:
        if isinstance(c, dict) and c.get("name") and c.get("value"):
            jar.set(c["name"], c["value"], domain=c.get("domain") or ".traderepublic.com",
                    path=c.get("path") or "/", expires=c.get("expires"))


def cookie_header(cookies: list[dict] | None) -> str:
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies or []
                     if isinstance(c, dict) and c.get("name") and c.get("value"))


# ================================================================ sign-in
@dataclass
class LoginProcess:
    """One sign-in in progress, kept in memory only. It never holds the PIN:
    that is sent once, in start_login(), and not needed again."""
    http: object                # HTTP session with the process's cookies
    mode: str                   # "v2" (approve in the app) or "v1" (4-digit code)
    process_id: str
    step: str                   # "app" | "authenticator" | "code"
    expires: float
    countdown: int
    hint: str = ""              # last two digits of the phone number, for the UI
    started: float = field(default_factory=time.time)


def _raise_login(resp) -> None:
    status = resp.status_code
    if status < 400:
        return
    code = _error_code(resp)
    if status == 405 and "awselb" in (resp.headers.get("server") or "").lower():
        raise ConnectorError(_WAF_MESSAGE, status="BLOCKED")
    if code in _LOGIN_ERRORS:
        raise ConnectorError(_LOGIN_ERRORS[code], status=code)
    if code in ("MOBILE_APP_VERSION_NOT_SUPPORTED", "CLIENT_VERSION_OUTDATED",
                "MISSING_REQUIRED_HEADER") or status == 426:
        raise ConnectorError(_VERSION_MESSAGE.format(code=code or f"HTTP {status}"),
                             status=code or status)
    if status == 429:
        raise ConnectorError(_LOGIN_ERRORS["TOO_MANY_REQUESTS"], status=429)
    if status in (401, 403) or code in ("INVALID_CREDENTIALS", "UNAUTHORIZED", "WRONG_PIN",
                                        "AUTHENTICATION_ERROR", "INVALID_PIN"):
        raise ConnectorError(_BAD_LOGIN, status=status)
    if status >= 500:
        raise ConnectorError(f"{NAME} is having problems right now (HTTP {status}). Try again later.",
                             status=status)
    if status in (404, 405, 410):
        raise ConnectorError(f"{NAME} no longer offers this sign-in (HTTP {status}).", status=status)
    raise ConnectorError(f"{NAME} did not accept the sign-in ({code or 'HTTP ' + str(status)}).",
                         status=code or status)


def _process_id(data: dict) -> str:
    pid = str(data.get("processId") or "")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,100}", pid):
        raise ConnectorError(f"{NAME} did not start the sign-in. Try again in a minute.")
    return pid


def _deadline(expires_at, fallback: float) -> float:
    try:
        if isinstance(expires_at, (int, float)):
            return expires_at / 1000 if expires_at > 1e11 else float(expires_at)
        return datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return time.time() + fallback


def login_mode() -> str:
    mode = _env("TR_LOGIN", "auto").lower()
    return mode if mode in ("v1", "v2", "auto") else "auto"


def waf_available() -> bool:
    return bool(_env("TR_WAF_TOKEN", "")) or importlib.util.find_spec("playwright") is not None


def start_login(phone: str, pin: str) -> LoginProcess:
    """Send phone number + PIN to Trade Republic; it then asks the user to
    approve (or sends a code). Returns the process to confirm."""
    phone = normalise_phone(phone)
    if not pin or len(pin) > 128:
        raise ConnectorError("Fill in your Trade Republic PIN.")
    mode = login_mode()
    if mode == "v1":
        return _start_v1(phone, pin)
    try:
        return _start_v2(phone, pin)
    except ConnectorError as e:
        if mode == "auto" and e.status in _V2_REFUSED and waf_available():
            return _start_v1(phone, pin)
        raise


def _start_v2(phone: str, pin: str) -> LoginProcess:
    http = new_http()
    headers = login_headers()
    r = _call(http, "post", "/api/v2/auth/web/login",
              json={"phoneNumber": phone, "pin": pin}, headers=headers)
    _raise_login(r)
    data = _json(r)
    pid = _process_id(data)
    countdown = int(num(data.get("countdownInSeconds"), 120) or 120)
    proc = {}
    p = _call(http, "get", f"/api/v2/auth/web/login/processes/{pid}", headers=headers)
    if p.status_code < 400:
        proc = _json(p)
    step = "authenticator" if proc.get("requiredAction") == "AUTHENTICATOR_VERIFICATION" else "app"
    return LoginProcess(http=http, mode="v2", process_id=pid, step=step,
                        expires=_deadline(proc.get("expiresAt"), max(countdown, 120)),
                        countdown=countdown, hint=phone_hint(phone))


def _start_v1(phone: str, pin: str) -> LoginProcess:
    token = waf_token()
    if not token:
        raise ConnectorError(_V1_NEEDS_BROWSER, status="NO_WAF_TOKEN")
    http = new_http()
    http.cookies.set("aws-waf-token", token, domain=".traderepublic.com", path="/")
    r = _call(http, "post", "/api/v1/auth/web/login",
              json={"phoneNumber": phone, "pin": pin}, headers=login_headers())
    _raise_login(r)
    data = _json(r)
    countdown = int(num(data.get("countdownInSeconds"), 30) or 30)
    return LoginProcess(http=http, mode="v1", process_id=_process_id(data), step="code",
                        expires=time.time() + 5 * 60, countdown=countdown, hint=phone_hint(phone))


def waf_token() -> str | None:
    """An aws-waf-token for the v1 sign-in: TR_WAF_TOKEN, or one a headless
    browser gets by opening the login page (only if Playwright is installed).
    Never faked: without either there is no token."""
    token = _env("TR_WAF_TOKEN", "")
    if token:
        return token
    try:
        from playwright.sync_api import sync_playwright  # optional, not a dependency
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(user_agent=user_agent())
                page = ctx.new_page()
                page.goto("https://app.traderepublic.com/login", wait_until="domcontentloaded",
                          timeout=30000)
                deadline = time.time() + 30
                while time.time() < deadline:
                    hit = next((c["value"] for c in ctx.cookies() if c["name"] == "aws-waf-token"),
                               None)
                    if hit:
                        return hit
                    page.wait_for_timeout(500)
            finally:
                browser.close()
    except Exception:
        return None
    return None


sleep = time.sleep   # tests replace this


def confirm_login(proc: LoginProcess, code: str | None = None, wait: float = 15.0) -> bool:
    """Finish a sign-in. v1: send the 4-digit code. v2: send the authenticator
    code if one is needed, then check (for up to `wait` seconds) whether the
    user approved in the app. True once signed in, False while still waiting."""
    if time.time() > proc.expires:
        raise ConnectorError("The sign-in expired. Start again.", status="EXPIRED")
    headers = login_headers()
    if proc.mode == "v1":
        digits = re.sub(r"\D", "", code or "")
        if len(digits) != 4:
            raise ConnectorError("Enter the 4-digit code from the Trade Republic app or SMS.")
        r = _call(proc.http, "post", f"/api/v1/auth/web/login/{proc.process_id}/{digits}",
                  headers=headers)
        _raise_login(r)
        return True
    if proc.step == "authenticator":
        digits = re.sub(r"\D", "", code or "")
        if len(digits) != 6:
            raise ConnectorError("Enter the 6-digit code from your authenticator app.")
        r = _call(proc.http, "post",
                  f"/api/v2/auth/web/login/processes/{proc.process_id}/authenticator-verification",
                  json={"code": digits}, headers=headers)
        _raise_login(r)
        proc.step = "app"     # some accounts still approve in the app after the code
    until = min(time.time() + max(wait, 0.0), proc.expires)
    while True:
        r = _call(proc.http, "get", f"/api/v2/auth/web/login/processes/{proc.process_id}",
                  headers=headers)
        _raise_login(r)
        status = str(_json(r).get("status") or "").upper()
        if status in ("CONFIRMED", "COMPLETED"):
            return True
        if status not in ("PENDING", ""):
            word = "declined in the Trade Republic app" if status in (
                "REJECTED", "DECLINED", "DENIED", "CANCELED", "CANCELLED") else \
                "not finished in time" if status == "EXPIRED" else f"stopped ({status.lower()})"
            raise ConnectorError(f"The sign-in was {word}. Start again.", status="ENDED")
        if time.time() + POLL > until:
            return False
        sleep(POLL)


def resend_code(proc: LoginProcess) -> None:
    """v1 only: ask Trade Republic to send the code by SMS."""
    if proc.mode != "v1":
        raise ConnectorError("Confirm the sign-in in the Trade Republic app instead.")
    r = _call(proc.http, "post", f"/api/v1/auth/web/login/{proc.process_id}/resend",
              headers=login_headers())
    _raise_login(r)


# ================================================================ session
def signed_out() -> ConnectorError:
    return ConnectorError(SIGN_IN_MESSAGE, status=SIGN_IN, retry_after=SIGN_IN_RETRY)


def refresh_session(http) -> None:
    """Renew the five-minute session cookie with the refresh cookie."""
    r = _call(http, "get", "/api/v1/auth/web/session")
    if r.status_code in (401, 403):
        raise signed_out()
    if r.status_code >= 400:
        raise ConnectorError(http_message(NAME, r.status_code), status=r.status_code)


def account_number(http) -> str:
    r = _call(http, "get", "/api/v2/auth/account")
    if r.status_code in (401, 403):
        raise signed_out()
    if r.status_code >= 400:
        raise ConnectorError(http_message(NAME, r.status_code), status=r.status_code)
    sec = str(_json(r).get("securitiesAccountNumber") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9\-]{1,40}", sec):
        raise ConnectorError(f"{NAME} did not say which securities account to read.")
    return sec


# ============================================================== websocket
class SubscriptionError(Exception):
    def __init__(self, payload):
        super().__init__("subscription error")
        self.payload = payload if isinstance(payload, dict) else {}

    @property
    def codes(self) -> set[str]:
        errs = self.payload.get("errors") or []
        return {_safe_code(e.get("errorCode")) for e in errs if isinstance(e, dict)}

    @property
    def auth(self) -> bool:
        return bool(self.codes & {"AUTHENTICATION_ERROR", "UNAUTHORIZED", "SESSION_EXPIRED",
                                  "UNAUTHENTICATED"})


def apply_delta(previous: str, delta: str) -> str:
    """Rebuild an answer from a "D" message: tab-separated "+text" (insert,
    URL-encoded), "=n" (keep n characters) and "-n" (skip n characters)."""
    out, i = [], 0
    for part in delta.split("\t"):
        if not part:
            continue
        sign, body = part[0], part[1:]
        if sign == "+":
            out.append(urllib.parse.unquote_plus(body))
        elif sign == "=":
            n = int(body)
            out.append(previous[i:i + n])
            i += n
        elif sign == "-":
            i += int(body)
    return "".join(out)


def connect_message() -> dict:
    return {"locale": LOCALE, "platformId": "webtrading", "platformVersion": "chrome - 94.0.4606",
            "clientId": "app.traderepublic.com", "clientVersion": "5582"}


async def _open_websocket(cookies: list[dict]):
    import websockets   # installed with uvicorn[standard]

    return await websockets.connect(
        WS_URL, additional_headers={"Cookie": cookie_header(cookies)},
        user_agent_header=user_agent(), open_timeout=10, close_timeout=2,
        max_size=32 * 2 ** 20, ping_interval=20, ping_timeout=20)


ws_connect = _open_websocket   # tests replace this with a fake


class Feed:
    """Read-only websocket session: subscribe, take the first answer, unsubscribe."""

    def __init__(self, ws):
        self.ws = ws
        self._next = 1
        self._waiting: dict[str, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None

    async def open(self) -> None:
        try:
            await self.ws.send(f"connect {WS_PROTOCOL} {json.dumps(connect_message())}")
            first = await asyncio.wait_for(self.ws.recv(), REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            raise ConnectorError(f"{NAME} did not open the data connection. Try again later.") from None
        except Exception:
            raise ConnectorError(f"{NAME} closed the data connection. Try again later.") from None
        if first != "connected":
            raise ConnectorError(f"{NAME} refused the data connection. Try again later.")
        self._reader = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        try:
            while True:
                self._dispatch(await self.ws.recv())
        except asyncio.CancelledError:
            raise
        except Exception:
            for fut in self._waiting.values():
                if not fut.done():
                    fut.set_exception(ConnectorError(
                        f"The connection to {NAME} was closed. It will try again shortly."))

    def _dispatch(self, msg) -> None:
        if not isinstance(msg, str):
            return
        sid, _, rest = msg.partition(" ")
        fut = self._waiting.get(sid)
        if fut is None or fut.done():
            return
        code, payload = rest[:1], rest[1:].strip()
        try:
            if code == "A":
                fut.set_result(json.loads(payload) if payload else {})
            elif code == "D":     # a change before the first answer: nothing to apply it to
                fut.set_result(json.loads(apply_delta("", payload) or "{}"))
            elif code == "E":
                fut.set_exception(SubscriptionError(json.loads(payload) if payload else {}))
            elif code == "C":
                fut.set_exception(SubscriptionError({}))
        except ValueError:
            fut.set_exception(ConnectorError(f"{NAME} sent an answer this app could not read."))

    async def get(self, payload: dict, timeout: float = REQUEST_TIMEOUT):
        kind = payload.get("type")
        if kind not in READ_SUBSCRIPTIONS:
            raise ValueError(f"refusing a subscription that is not read-only: {kind!r}")
        sid = str(self._next)
        self._next += 1
        fut = asyncio.get_running_loop().create_future()
        self._waiting[sid] = fut
        try:
            try:
                await self.ws.send(f"sub {sid} {json.dumps(payload)}")
            except Exception:
                raise ConnectorError(f"The connection to {NAME} was closed. It will try again "
                                     "shortly.") from None
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise ConnectorError(f"{NAME} took too long to answer. It will try again later.") from None
        finally:
            self._waiting.pop(sid, None)
            try:
                await self.ws.send(f"unsub {sid}")
            except Exception:
                pass

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except BaseException:
                pass
        try:
            await self.ws.close()
        except Exception:
            pass


# ============================================================ parsing
def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _etype(item: dict) -> str:
    return str(item.get("eventType") or "").upper().replace("-", "_")


def _value(item: dict) -> float | None:
    amount = item.get("amount")
    return num(amount.get("value"), None) if isinstance(amount, dict) else None


def _ts(item: dict) -> str:
    """'2025-10-10T19:29:43.928+0000' -> '2025-10-10 19:29:43' (UTC)."""
    raw = str(item.get("timestamp") or "")
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})?", raw)
    if not m:
        return ""
    t = datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}")
    zone = m.group(3) or "Z"
    if zone != "Z":
        sign = 1 if zone[0] == "+" else -1
        hh, mm = int(zone[1:3]), int(zone[-2:])
        t -= sign * timedelta(hours=hh, minutes=mm)
    return t.strftime("%Y-%m-%d %H:%M:%S")


_TRADE = {"ORDER_EXECUTED", "TRADING_TRADE_EXECUTED", "SAVINGS_PLAN_EXECUTED",
          "SAVINGS_PLAN_INVOICE_CREATED", "TRADING_SAVINGSPLAN_EXECUTED",
          "BENEFITS_SPARE_CHANGE_EXECUTION", "SPARE_CHANGE_AGGREGATE", "TRADE_INVOICE",
          "TRADE_CORRECTED", "IPO_TRADE_EXECUTED", "IPO_SUBSCRIPTION_ALLOCATION",
          "PRIVATE_MARKETS_TRADE_EXECUTED", "PRIVATE_MARKET_FUND_TRADE_EXECUTED"}
_SAVEBACK = {"BENEFITS_SAVEBACK_EXECUTION", "SAVEBACK_AGGREGATE"}
_PERK = {"ACQUISITION_TRADE_PERK"}
_CASH_ACTION = {"CREDIT", "SSP_CORPORATE_ACTION_INVOICE_CASH", "SSP_CORPORATE_ACTION_CASH"}
_INTEREST = {"INTEREST_PAYOUT", "INTEREST_PAYOUT_CREATED"}
_TAX_REFUND = {"SSP_TAX_CORRECTION", "SSP_TAX_CORRECTION_INVOICE", "TAX_CORRECTION", "TAX_REFUND"}
_SHARES_ACTION = {"SSP_CORPORATE_ACTION_INVOICE_SHARES"}
_TRANSFER_IN = {"SSP_SECURITIES_TRANSFER_INCOMING", "SECURITIES_TRANSFER_INCOMING"}
_TRANSFER_OUT = {"SSP_SECURITIES_TRANSFER_OUTGOING", "SECURITIES_TRANSFER_OUTGOING"}
_NOT_DONE = ("_PENDING", "_FAILED", "_CANCELED", "_CANCELLED", "_REJECTED", "_EXPIRED",
             "_CREATED", "_RECEIVED")

# Items without a (known) eventType: older and migrated items, by title/subtitle.
_SUB_TRADE = {"kauforder", "verkaufsorder", "limit-buy-order", "limit-sell-order",
              "limit verkauf-order neu abgerechnet", "sparplan ausgeführt", "stop-sell-order",
              "round up", "buy order", "sell order", "limit buy order", "limit sell order",
              "savings plan executed", "stop sell order"}
_SUB_DIVIDEND = {"bardividende", "dividende", "bardividende korrigiert", "aktienprämiendividende",
                 "dividende wahlweise", "cash dividend", "dividend"}
_SUB_SHARES = {"aktiensplit", "bonusaktien", "spin-off", "aktiendividende",
               "zwischenvertrieb von wertpapieren", "reverse split", "tausch", "zusammenschluss",
               "wertlos", "stock split", "bonus shares", "stock dividend", "merger", "worthless"}
_SUB_TAX = {"vorabpauschale", "advance tax"}
_SUB_TRANSFER_IN = {"aktien erhalten", "shares received"}
_SUB_TRANSFER_OUT = {"aktien übertragen", "aktien gesendet", "shares transferred", "shares sent"}
_SUB_TRANSFER = {"wertpapiertransfer", "securities transfer"}
_SUB_SPINOFF = {"spin-off", "zwischenvertrieb von wertpapieren"}
_SUB_WORTHLESS = {"wertlos", "worthless"}
_TITLE_INTEREST = {"zinsen", "interest"}
_TITLE_TAX_REFUND = {"steuerkorrektur", "tax correction"}
_TITLE_PERK = {"aktien-bonus", "stock bonus"}

# Kinds read from the timeline list alone; the others need the detail page.
LIST_KINDS = {"dividend", "interest", "tax", "tax_refund"}
DETAIL_KINDS = {"trade", "saveback", "perk", "shares", "transfer", "transfer_in", "transfer_out"}


def classify(item: dict) -> str:
    """What one timeline item is, from the list entry alone. "" for items that
    are not part of the investment return (deposits, card payments, documents,
    orders that did not execute); "gone" for cancelled/deleted ones; "pending"
    for ones not booked yet."""
    status = str(item.get("status") or "").upper()
    if status in ("CANCELED", "CANCELLED") or item.get("deleted"):
        return "gone"
    if status == "PENDING":
        return "pending"
    et, title, sub = _etype(item), _norm(item.get("title")), _norm(item.get("subtitle"))
    value = _value(item)
    if "storniert" in sub or "cancelled" in sub or "canceled" in sub:
        return ""
    if et in _TRADE:
        return "trade"
    if et in _SAVEBACK:
        return "saveback"
    if et in _PERK:
        return "perk"
    if et in _INTEREST:
        return "interest"
    if et in _TAX_REFUND:
        return "tax_refund"
    if et in _SHARES_ACTION:
        return "shares"
    if et in _TRANSFER_IN:
        return "transfer_in"
    if et in _TRANSFER_OUT:
        return "transfer_out"
    if et in _CASH_ACTION:
        if sub in _SUB_TAX or (value is not None and value < 0):
            return "tax"
        if sub == "aufruf von zwischenpapieren":
            return ""
        return "dividend" if value else ""
    if et and et.endswith(_NOT_DONE):
        return ""
    if title in _TITLE_INTEREST:
        return "interest" if value else ""
    if title in _TITLE_TAX_REFUND:
        return "tax_refund" if value else ""
    if title in _TITLE_PERK:
        return "perk"
    if sub == "saveback":
        return "saveback"
    if sub in _SUB_TRADE:
        return "trade"
    if sub in _SUB_DIVIDEND:
        return "dividend" if value else ""
    if sub in _SUB_TAX:
        return "tax" if value else ""
    if sub in _SUB_SHARES:
        return "shares"
    if sub in _SUB_TRANSFER_IN:
        return "transfer_in"
    if sub in _SUB_TRANSFER_OUT:
        return "transfer_out"
    if sub in _SUB_TRANSFER:
        return "transfer"
    return ""


def numbers(text, prefer_thousands: bool = True) -> list[float]:
    """Readings of one number as Trade Republic shows it: "3.002,80 €",
    "0,685102", "49.000000", "1.0", "+ 18,77 €", "-0,78 €". Most likely first;
    a lone separator before exactly three digits ("1.000", "1,500") is
    ambiguous and gives both readings."""
    if text is None:
        return []
    s = str(text).replace("−", "-")
    s = re.sub(r"[^\d,.\-]", "", s)
    neg = s.startswith("-")
    s = s.replace("-", "")
    if not re.search(r"\d", s):
        return []
    sign = -1.0 if neg else 1.0
    try:
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                return [sign * float(s.replace(".", "").replace(",", "."))]
            return [sign * float(s.replace(",", ""))]
        for sep in (",", "."):
            if sep in s:
                if s.count(sep) > 1:
                    return [sign * float(s.replace(sep, ""))]
                whole, frac = s.split(sep)
                dec = sign * float((whole or "0") + "." + (frac or "0"))
                if len(frac) == 3 and whole:
                    thou = sign * float(whole + frac)
                    # "1.000" in a German trade is a thousand; "1,500" is one and a half
                    return [thou, dec] if (sep == "." and prefer_thousands) else [dec, thou]
                return [dec]
        return [sign * float(s)]
    except ValueError:
        return []


def money(text) -> float | None:
    vals = numbers(text)
    return vals[0] if vals else None


def _rows(details, depth: int = 0) -> list[dict]:
    """Every table row of a detail page (nested pages included), flattened:
    {title, text, prefix, depth}."""
    out: list[dict] = []
    for sec in (details or {}).get("sections") or []:
        if not isinstance(sec, dict):
            continue
        data = sec.get("data")
        if not isinstance(data, list):
            continue
        for it in data:
            if not isinstance(it, dict):
                continue
            det = it.get("detail") if isinstance(it.get("detail"), dict) else {}
            dv = det.get("displayValue") if isinstance(det.get("displayValue"), dict) else {}
            out.append({"title": _norm(it.get("title")), "text": det.get("text"),
                        "prefix": dv.get("prefix") or "", "depth": depth,
                        "section": _norm(sec.get("title"))})
            action = det.get("action") if isinstance(det.get("action"), dict) else {}
            if isinstance(action.get("payload"), dict):
                out.extend(_rows(action["payload"], depth + 1))
    return out


def _find(rows: list[dict], titles: set[str], deepest: bool = True) -> list[dict]:
    hits = [r for r in rows if r["title"] in titles]
    return sorted(hits, key=lambda r: -r["depth"]) if deepest else hits


def _isins(obj, out: list[str]) -> list[str]:
    """ISINs mentioned in a detail page (action payloads, logo paths), in order."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("payload", "icon", "asset") and isinstance(v, str):
                for m in _ISIN_ANY.findall(v):
                    if m not in out:
                        out.append(m)
            else:
                _isins(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _isins(v, out)
    return out


def _isin(item: dict, details: dict | None = None) -> str:
    for sec in (details or {}).get("sections") or []:
        if not isinstance(sec, dict):
            continue
        action = sec.get("action") if isinstance(sec.get("action"), dict) else {}
        if action.get("type") == "instrumentDetail" and ISIN_RE.match(str(action.get("payload") or "")):
            return action["payload"]
        if sec.get("type") == "header":
            icon = (sec.get("data") or {}).get("icon") if isinstance(sec.get("data"), dict) else None
            icon = icon.get("asset") if isinstance(icon, dict) else icon
            m = _ISIN_ANY.search(str(icon or ""))
            if m:
                return m.group(1)
    for key in ("icon", "avatar"):
        icon = item.get(key)
        icon = icon.get("asset") if isinstance(icon, dict) else icon
        m = _ISIN_ANY.search(str(icon or ""))
        if m:
            return m.group(1)
    return ""


_SHARES = {"aktien", "anteile", "shares", "stück", "quantity"}
_ADDED = {"aktien hinzugefügt", "shares added", "added shares"}
_REMOVED = {"aktien entfernt", "shares removed", "removed shares"}
_PRICE = {"aktienkurs", "kurs", "share price", "price", "preis"}
_TOTAL = {"summe", "gesamt", "total"}
_TAX = {"steuer", "steuern", "tax", "taxes"}
_FEE = {"gebühr", "entgelt", "fee", "fees"}
_TRANSACTION = {"transaktion", "transaction"}
_SECURITY = {"wertpapier", "asset", "vermögenswert", "security", "instrument"}


def _shares_from(rows: list[dict], titles: set[str], prefer_thousands: bool) -> list[float]:
    for r in _find(rows, titles):
        vals = numbers(r["text"], prefer_thousands)
        if vals:
            return vals
    return []


def _shares_from_transaction(rows: list[dict]) -> list[float]:
    """"0,685102 × 160,56 €" or a prefix "0.546348 x" -> the share count."""
    for r in _find(rows, _TRANSACTION):
        for text in (r["prefix"], r["text"]):
            m = re.match(r"\s*([-\d.,]+)\s*[×x]", str(text or ""))
            if m:
                vals = numbers(m.group(1))
                if vals:
                    return vals
    return []


def _pick(cands: list[float], price: float | None, totals: list[float]) -> float | None:
    """Of several readings of a share count, the one where shares x price
    comes closest to an amount on the page."""
    cands = [c for c in cands if c and c > 0]
    if not cands:
        return None
    targets = [abs(t) for t in totals if t]
    if len(cands) == 1 or not price or not targets:
        return cands[0]
    return min(cands, key=lambda q: min(abs(q * price - t) / t for t in targets))


def _base(item: dict, kind: str) -> dict:
    amount = item.get("amount") if isinstance(item.get("amount"), dict) else {}
    return {"id": str(item.get("id")), "ts": _ts(item), "kind": kind,
            "name": str(item.get("title") or "")[:120],
            "currency": amount.get("currency") or "EUR"}


def _unread(item: dict, why: str) -> dict:
    return {"id": str(item.get("id")), "ts": _ts(item), "kind": "unread", "why": why,
            "name": str(item.get("title") or "")[:120]}


def list_record(item: dict, kind: str) -> dict | None:
    """A dividend / interest / tax item, read from the timeline list."""
    value = _value(item)
    if value is None:
        return None
    rec = _base(item, kind)
    rec["amount"] = round(value, 2)
    if kind == "dividend":
        rec["isin"] = _isin(item)
    if kind == "interest":
        rate = stated_rate(item.get("subtitle"))
        if rate is not None:
            rec["rate"] = rate
    return rec


_RATE_RE = re.compile(r"(\d{1,2}(?:[.,]\d{1,3})?)\s*%\s*p\.?\s*a\b", re.I)


def stated_rate(subtitle) -> float | None:
    """The yearly rate an interest payout states in its subtitle ("2 % p.a."
    in pytr's fixture tests/events/zinsen.json; often null), else None."""
    m = _RATE_RE.search(str(subtitle or ""))
    return float(m.group(1).replace(",", ".")) if m else None


def parse_event(item: dict, details: dict, kind: str) -> dict:
    """One share-moving timeline item + its detail page -> a stored record."""
    rows = _rows(details)
    isin = _isin(item, details)
    value = _value(item)
    sub = _norm(item.get("subtitle"))
    if not isin:
        return _unread(item, "no ISIN")
    rec = _base(item, kind)
    rec["isin"] = isin

    if kind == "trade":
        price_rows = _find(rows, _PRICE)
        price = money(price_rows[0]["text"]) if price_rows else None
        totals = [money(r["text"]) for r in _find(rows, _TOTAL)] + [value]
        cands = _shares_from(rows, _SHARES, True) or _shares_from_transaction(rows)
        shares = _pick(cands, abs(price) if price else None, [t for t in totals if t])
        if sub in _SUB_WORTHLESS and not value:
            if not shares:
                return _unread(item, "shares")
            return {**rec, "kind": "worthless", "shares": shares}
        if not value or not shares:
            return _unread(item, "shares" if value else "amount")
        fee_rows = _find(rows, _FEE)
        fee = abs(money(fee_rows[0]["text"]) or 0.0) if fee_rows else 0.0
        tax_rows = _find(rows, _TAX)
        tax = abs(money(tax_rows[0]["text"]) or 0.0) if tax_rows else 0.0
        side = "buy" if value < 0 else "sell"
        if side == "sell" and tax:
            # Tax withheld is shown as a positive number. If the page's own
            # figures say it was credited instead, flip it.
            nested = [money(r["text"]) for r in _find(rows, _TOTAL) if r["depth"] > 0]
            gross = abs(nested[0]) if nested and nested[0] else (shares * abs(price) if price else None)
            if gross:
                implied = gross - fee - value
                if abs(implied + tax) < abs(implied - tax):
                    tax = -tax
        return {**rec, "kind": side, "shares": round(shares, 8), "amount": round(abs(value), 2),
                "fee": round(fee, 2), "tax": round(tax, 2) if side == "sell" else 0.0}

    if kind in ("saveback", "perk"):
        shares = _pick(_shares_from_transaction(rows) or _shares_from(rows, _SHARES, False), None, [])
        amount = abs(value) if value else abs(next((money(r["text"]) for r in _find(rows, _TOTAL)
                                                    if money(r["text"])), 0.0) or 0.0)
        if not shares:
            return _unread(item, "shares")
        return {**rec, "shares": round(shares, 8), "amount": round(amount, 2)}

    if kind in ("transfer", "transfer_in", "transfer_out"):
        shares = _pick(_shares_from(rows, _SHARES, False), None, [])
        if kind == "transfer":
            head = " ".join(_norm(s.get("title")) for s in (details or {}).get("sections") or []
                            if isinstance(s, dict))
            kind = "transfer_in" if ("erhalten" in head or "received" in head) else \
                "transfer_out" if any(w in head for w in ("gesendet", "übertragen", "sent", "transferred")) \
                else ""
        if not shares or not kind:
            return _unread(item, "shares" if not shares else "direction")
        return {**rec, "kind": kind, "shares": round(shares, 8)}

    # corporate action with shares: split, bonus shares, spin-off, merger, ...
    added = _pick(_shares_from(rows, _ADDED, False), None, [])
    removed = _pick(_shares_from(rows, _REMOVED, False), None, [])
    names = [str(r["text"] or "") for r in _find(rows, _SECURITY, deepest=False) if r["text"]]
    others = [i for i in _isins(details, []) if i != isin]
    isin2, name2 = (others[0] if others else ""), (names[-1] if names else "")
    if sub in _SUB_WORTHLESS:
        shares = removed or _pick(_shares_from(rows, _SHARES, False), None, [])
        return {**rec, "kind": "worthless", "shares": shares} if shares else _unread(item, "shares")
    if added and removed:
        return {**rec, "kind": "move", "shares": removed, "shares2": added, "isin2": isin2,
                "name2": name2}
    if added:
        other_name = name2 and _norm(name2) != _norm(item.get("title"))
        if sub in _SUB_SPINOFF or isin2 or (sub == "aktiendividende" and other_name):
            return {**rec, "kind": "spinoff", "shares2": added, "isin2": isin2, "name2": name2}
        return {**rec, "kind": "add", "shares": added}
    return _unread(item, "corporate action")


# ============================================================ building
@dataclass
class TRSnapshot(Snapshot):
    """A Snapshot plus what only Trade Republic reports."""
    fees: list[dict] = field(default_factory=list)   # {date, amount, currency, kind}
    history_complete: bool = False
    unread: int = 0
    account: str = ""                                 # account_hash() of the securities account


_ORDER = {"move": 0, "add": 1, "spinoff": 1, "transfer_in": 2, "buy": 3, "saveback": 3, "perk": 3,
          "sell": 4, "transfer_out": 4, "worthless": 4}


def _simple(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    s = re.sub(r"\b(inc|corp|corporation|ag|se|sa|plc|nv|ltd|o n|on|registered|shares|"
               r"namens aktien|aktien|class|cl|adr)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _isin_by_name(name: str, known: dict[str, str]) -> str:
    """ISIN of a currently held instrument whose name matches `name`."""
    want = _simple(name)
    if not want:
        return ""
    for isin, have in known.items():
        h = _simple(have)
        if h and (h == want or h.startswith(want) or want.startswith(h)):
            return isin
    return ""


def build(events: dict, avg_buy: dict[str, float] | None = None,
          names: dict[str, str] | None = None, ticker=None):
    """Stored records -> (trades, dividends, interest, fees, unread). Average-cost
    pools as in positions.compute_positions, so shares that move at cost
    (mergers, transfers out) carry their cost along."""
    avg_buy, names = avg_buy or {}, names or {}
    ticker = ticker or (lambda isin: "")
    recs = sorted((r for r in (events or {}).values() if isinstance(r, dict) and r.get("ts")),
                  key=lambda r: (r["ts"], _ORDER.get(r.get("kind"), 5), r.get("id", "")))
    pools: dict[str, list[float]] = {}
    trades: list[Trade] = []
    dividends: list[dict] = []
    interest: list[dict] = []
    fees: list[dict] = []
    unread = 0

    def cost_of(isin: str, q: float) -> float:
        qty, cost = pools.get(isin, [0.0, 0.0])
        return cost / qty * min(q, qty) if qty > 1e-9 else 0.0

    def trade(r: dict, side: str, isin: str, name: str, q: float, amount: float) -> None:
        qty, cost = pools.get(isin, [0.0, 0.0])
        if side == "BUY":
            pools[isin] = [qty + q, cost + amount]
        else:
            pools[isin] = [qty - q, cost - (cost / qty * min(q, qty) if qty > 1e-9 else 0.0)]
        trades.append(Trade(when=r["ts"][:16], key=isin, name=name or isin, side=side,
                            quantity=round(q, 8), amount=round(amount, 2), currency="EUR",
                            ticker=ticker(isin)))

    for r in recs:
        k, isin, name, day = r.get("kind"), r.get("isin", ""), r.get("name", ""), r["ts"][:10]
        q = float(r.get("shares") or 0)
        if k == "buy":
            trade(r, "BUY", isin, name, q, float(r["amount"]))
        elif k == "sell":
            tax = float(r.get("tax") or 0)
            trade(r, "SELL", isin, name, q, float(r["amount"]) + tax)
            if tax:
                fees.append({"date": day, "amount": round(-tax, 2), "currency": "EUR", "kind": "tax"})
        elif k in ("saveback", "perk"):
            trade(r, "BUY", isin, name, q, float(r.get("amount") or 0))
            if r.get("amount"):
                interest.append({"date": day, "amount": round(float(r["amount"]), 2),
                                 "currency": "EUR", "kind": "bonus"})
        elif k == "add":
            trade(r, "BUY", isin, name, q, 0.0)
        elif k == "spinoff":
            isin2 = r.get("isin2") or _isin_by_name(r.get("name2", ""), names)
            if isin2:
                trade(r, "BUY", isin2, r.get("name2") or names.get(isin2, ""),
                      float(r.get("shares2") or 0), 0.0)
            else:
                unread += 1
        elif k == "move":
            isin2 = r.get("isin2") or _isin_by_name(r.get("name2", ""), names) or isin
            moved = cost_of(isin, q)
            trade(r, "SELL", isin, name, q, moved)
            trade(r, "BUY", isin2, r.get("name2") or name, float(r.get("shares2") or 0), moved)
        elif k == "worthless":
            trade(r, "SELL", isin, name, q, 0.0)
        elif k == "transfer_in":
            trade(r, "BUY", isin, name, q, q * float(avg_buy.get(isin) or 0.0))
        elif k == "transfer_out":
            trade(r, "SELL", isin, name, q, cost_of(isin, q))
        elif k == "dividend":
            dividends.append({"date": day, "ticker": ticker(r.get("isin", "")),
                              "isin": r.get("isin", ""), "amount": float(r["amount"]),
                              "currency": r.get("currency") or "EUR"})
        elif k == "interest":
            interest.append({"date": day, "amount": float(r["amount"]),
                             "currency": r.get("currency") or "EUR",
                             **({"rate": r["rate"]} if r.get("rate") is not None else {})})
        elif k in ("tax", "tax_refund"):
            fees.append({"date": day, "amount": float(r["amount"]),
                         "currency": r.get("currency") or "EUR", "kind": k})
        elif k == "unread":
            unread += 1
    return trades, dividends, interest, fees, unread


def empty_history() -> dict:
    return {"v": PARSER_VERSION, "events": {}, "marks": {}, "complete": False, "synced": 0,
            "instruments": {}}


def history_of(conn: dict | None) -> dict:
    """The connection's stored history (a copy), started again after a
    mapping change (PARSER_VERSION)."""
    hist = (conn or {}).get("history")
    if not isinstance(hist, dict) or hist.get("v") != PARSER_VERSION:
        fresh = empty_history()
        if isinstance(hist, dict) and isinstance(hist.get("instruments"), dict):
            fresh["instruments"] = dict(hist["instruments"])
        return fresh
    out = dict(hist)
    out["events"] = dict(hist.get("events") or {})
    out["marks"] = dict(hist.get("marks") or {})
    out["instruments"] = dict(hist.get("instruments") or {})
    return out


def _ticker(isin: str) -> str:
    from ..instruments import ticker_for
    try:
        return ticker_for(isin, "EUR")
    except Exception:
        return ""


def assemble(positions: list[dict], cash: list[dict], history: dict, prices: dict | None = None,
             account: str = "") -> TRSnapshot:
    """Positions + cash + the stored history -> one snapshot. Trades and
    income only once the whole history has been read."""
    from ..instruments import price_currency

    prices = prices or {}
    inst = history.get("instruments") or {}
    events = history.get("events") or {}
    seen_names: dict[str, str] = {}
    for r in sorted(events.values(), key=lambda r: r.get("ts", "")):
        if r.get("isin") and r.get("name") and r.get("kind") in ("buy", "sell", "saveback"):
            seen_names[r["isin"]] = r["name"]
    holdings = []
    for p in positions:
        isin = p["isin"]
        t = _ticker(isin)
        name = p.get("name") or (inst.get(isin) or {}).get("name") or seen_names.get(isin) or isin
        holdings.append(Holding(
            broker=NAME, ticker=t, name=name, quantity=round(p["quantity"], 6),
            buy_price=round(p["avg"], 4), currency=price_currency(t) if t else "EUR",
            isin=isin, price=None if t else prices.get(isin), buy_currency="EUR"))
    return with_history(TRSnapshot(cash=cash, holdings=holdings, account=account), history)


# Interest on cash: Trade Republic states no rate anywhere in what this module
# reads (no subscription in pytr's api.py returns one), except that an interest
# payout's subtitle can say it ("2 % p.a." in pytr's 2025 fixture; null in its
# 2024 and 2026 ones). Payouts are monthly and booked after tax, and the
# timeline read here keeps only investment items, so the cash balance history
# needed to work a rate out is not available: without a stated rate it is
# typed in.
INTEREST_NOTE = ("Trade Republic states its interest rate only with some interest payments, and "
                 "none read so far had it, so the rate cannot be read automatically. Enter it "
                 "yourself.")


def cash_rate(interest: list[dict], today: datetime | None = None) -> dict:
    """The rate of the cash, from the latest interest payout (interest.py form)."""
    paid = sorted((i for i in interest if i.get("kind") != "bonus" and i.get("date")),
                  key=lambda i: i["date"])
    if not paid:
        return {"status": "none", "text": "Trade Republic paid no interest on this cash in the "
                                          "history read. If it earns none, enter 0%."}
    last = paid[-1]
    day = datetime.fromisoformat(last["date"][:10])
    if last.get("rate") is None or ((today or datetime.now()) - day).days > 45:
        return {"status": "unsupported", "text": INTEREST_NOTE}
    return {"status": "ok", "source": "provider", "pct": float(last["rate"]), "basis": "gross",
            "from": last["date"][:10], "to": last["date"][:10],
            "note": f"stated by Trade Republic with the interest paid on {day.day} {day:%b}, "
                    "before tax"}


def with_history(snap: TRSnapshot, history: dict) -> TRSnapshot:
    complete = bool(history.get("complete"))
    names = {h.isin: h.name for h in snap.holdings}
    names.update({i: (v or {}).get("name") or names.get(i, "")
                  for i, v in (history.get("instruments") or {}).items()})
    unread = 0
    if complete:
        try:   # tickers of every instrument in the history, a few lookups at a time
            from ..instruments import prefetch
            prefetch({r["isin"]: "EUR" for r in (history.get("events") or {}).values()
                      if isinstance(r, dict) and r.get("isin")})
        except Exception:
            pass
        trades, divs, interest, fees, unread = build(
            history.get("events") or {}, {h.isin: h.buy_price for h in snap.holdings},
            {i: n for i, n in names.items() if n}, _ticker)
        rate = cash_rate(interest)
        snap = replace(snap, trades=trades, dividends=divs, interest=interest, fees=fees,
                       cash=[{**c, "interest": rate} for c in snap.cash])
    n = len(snap.holdings)
    parts = [f"{n} position{'s' if n != 1 else ''}"]
    if not complete:
        parts.append("reading your history…")
    elif unread:
        parts.append(f"{unread} history item{'s' if unread != 1 else ''} not read")
    return replace(snap, history_complete=complete, unread=unread, summary=" · ".join(parts))


def restore(conn: dict) -> TRSnapshot | None:
    """The last snapshot saved with the connection, after a restart (also when
    the session has expired: the dashboard keeps the last data)."""
    s = conn.get("snapshot")
    if not isinstance(s, dict) or not s.get("at"):
        return None
    holdings = []
    for h in s.get("holdings") or []:
        try:
            holdings.append(Holding(**h))
        except TypeError:
            continue
    snap = TRSnapshot(cash=list(s.get("cash") or []), holdings=holdings,
                      account=(conn.get("tr") or {}).get("account", ""))
    return with_history(snap, history_of(conn))


# ============================================================ syncing
def _positions(portfolio) -> list[dict]:
    if isinstance(portfolio, dict) and isinstance(portfolio.get("categories"), list):
        rows = [p for c in portfolio["categories"] if isinstance(c, dict)
                for p in (c.get("positions") or [])]
    elif isinstance(portfolio, dict):
        rows = portfolio.get("positions") or []
    else:
        rows = portfolio if isinstance(portfolio, list) else []
    out = []
    for p in rows:
        if not isinstance(p, dict):
            continue
        isin = str(p.get("isin") or p.get("instrumentId") or "")
        qty, avg = num(p.get("netSize")), num(p.get("averageBuyIn"))
        if not ISIN_RE.match(isin) or not qty or qty <= 0:
            continue
        out.append({"isin": isin, "quantity": qty, "avg": avg or 0.0,
                    "name": str(p.get("name") or "")[:120]})
    return out


def _cash(rows) -> list[dict]:
    if isinstance(rows, dict):
        rows = rows.get("cash") or rows.get("accounts") or []
    rows = [r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]
    out = []
    for r in rows:
        ccy = str(r.get("currencyId") or r.get("currency") or "EUR")[:3].upper()
        out.append({"name": "Cash" if len(rows) == 1 else f"Cash {ccy}", "currency": ccy,
                    "balance": round(num(r.get("amount")) or 0.0, 2)})
    return out


async def _instruments(feed: Feed, isins: list[str], cache: dict) -> None:
    """Name, first exchange and type of instruments not described yet."""
    now = time.time()
    todo = [i for i in isins if not (cache.get(i) or {}).get("name")
            and now - (cache.get(i) or {}).get("tried", 0) > INSTRUMENT_RETRY]
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)

    async def one(isin: str) -> None:
        async with sem:
            try:
                d = await feed.get({"type": "instrument", "id": isin})
            except (SubscriptionError, ConnectorError):
                cache[isin] = {**(cache.get(isin) or {}), "tried": now}
                return
        ex = d.get("exchangeIds") or [e.get("slug") for e in d.get("exchanges") or []
                                      if isinstance(e, dict)]
        cache[isin] = {"name": str(d.get("shortName") or d.get("name") or "")[:120],
                       "exchange": str((ex or [""])[0] or ""), "type": str(d.get("typeId") or "")}

    await asyncio.gather(*(one(i) for i in todo))


async def _prices(feed: Feed, isins: list[str], cache: dict) -> dict[str, float]:
    """Trade Republic's own last price, for holdings Yahoo cannot price."""
    out: dict[str, float] = {}

    async def one(isin: str) -> None:
        info = cache.get(isin) or {}
        if not info.get("exchange"):
            return
        try:
            d = await feed.get({"type": "ticker", "id": f"{isin}.{info['exchange']}"})
        except (SubscriptionError, ConnectorError):
            return
        price = num(((d.get("last") or {}) if isinstance(d, dict) else {}).get("price"), None)
        if price:
            out[isin] = price / 100 if info.get("type") == "bond" else price

    await asyncio.gather(*(one(i) for i in isins))
    return out


def _take(item: dict, kind: str, events: dict, need: list) -> bool:
    """Handle one timeline list item. Returns True when the stored history changed."""
    iid = str(item.get("id") or "")
    if not iid:
        return False
    if kind == "gone":
        return events.pop(iid, None) is not None
    if kind in LIST_KINDS:
        rec = list_record(item, kind)
        if rec and events.get(iid) != rec:
            events[iid] = rec
            return True
        return False
    if kind in DETAIL_KINDS and iid not in events:
        need.append((item, kind))
    return False


async def _details(feed: Feed, need: list, events: dict, deadline: float) -> bool:
    """Fetch and read the detail pages. False if time ran out."""
    sem = asyncio.Semaphore(DETAIL_CONCURRENCY)
    done = {"all": True}

    async def one(item: dict, kind: str) -> None:
        async with sem:
            if time.monotonic() > deadline:
                done["all"] = False
                return
            try:
                d = await feed.get({"type": "timelineDetailV2", "id": item["id"]})
            except SubscriptionError as e:
                if e.auth:
                    raise signed_out() from None
                events[str(item["id"])] = _unread(item, "no detail page")
                return
            except ConnectorError:
                done["all"] = False
                return
        events[str(item["id"])] = parse_event(item, d if isinstance(d, dict) else {}, kind)

    await asyncio.gather(*(one(it, k) for it, k in need))
    return done["all"]


async def sync_history(feed: Feed, hist: dict, deadline: float) -> bool:
    """Read the timeline, newest first, until the part already read (with a
    week's overlap). Returns True when the stored history changed."""
    events, marks = hist.setdefault("events", {}), hist.setdefault("marks", {})
    changed, finished, need, new_marks = False, True, [], {}
    for source, typ in (("transactions", "timelineTransactions"), ("activity", "timelineActivityLog")):
        stop_before = ""
        if hist.get("complete") and marks.get(source):
            stop_before = (datetime.fromisoformat(marks[source]) - OVERLAP).strftime("%Y-%m-%d %H:%M:%S")
        cursor, newest, done, seen = None, "", False, set()
        while time.monotonic() < deadline:
            try:
                page = await feed.get({"type": typ, "after": cursor})
            except SubscriptionError as e:
                if e.auth:
                    raise signed_out() from None
                break                       # try again next time
            except ConnectorError:
                break
            page = page if isinstance(page, dict) else {}
            items = [i for i in page.get("items") or [] if isinstance(i, dict)]
            older = False
            for it in items:
                ts = _ts(it)
                if not ts:
                    continue
                newest = max(newest, ts)
                if stop_before and ts < stop_before:
                    older = True
                    break
                changed |= _take(it, classify(it), events, need)
            cursor = (page.get("cursors") or {}).get("after") if isinstance(page.get("cursors"), dict) else None
            if older or not cursor or not items or cursor in seen:
                done = True
                break
            seen.add(cursor)
        if done:
            new_marks[source] = newest or marks.get(source, "")
        finished &= done
    todo = need[:MAX_DETAILS]
    if todo:
        changed = True
    finished &= await _details(feed, todo, events, deadline) and len(need) <= MAX_DETAILS
    if finished:
        marks.update({k: v for k, v in new_marks.items() if v})
        hist["complete"] = True
        hist["synced"] = time.time()
    return changed


async def _read(creds: dict, history: dict, want_history: bool, budget: float) -> dict:
    """One websocket session: portfolio, cash, names/prices, and the history."""
    try:
        ws = await ws_connect(creds.get("cookies") or [])
    except ConnectorError:
        raise
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (401, 403):
            raise signed_out() from None
        raise ConnectorError(f"Could not open the data connection to {NAME}. Try again later.") from None
    feed = Feed(ws)
    try:
        await feed.open()
        try:
            portfolio = await feed.get({"type": "compactPortfolioByType", "secAccNo": creds["sec_acc_no"]})
            cash = await feed.get({"type": "cash"})
        except SubscriptionError as e:
            if e.auth:
                raise signed_out() from None
            raise ConnectorError(f"{NAME} did not send the portfolio. Try again later.") from None
        positions = _positions(portfolio)
        inst = history.setdefault("instruments", {})
        isins = [p["isin"] for p in positions]
        await _instruments(feed, isins, inst)
        from ..instruments import prefetch
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, prefetch, {i: "EUR" for i in isins})
        except Exception:
            pass
        unpriced = [i for i in isins if not _ticker(i)]
        prices = await _prices(feed, unpriced, inst) if unpriced else {}
        changed = False
        if want_history:
            try:
                changed = await sync_history(feed, history, time.monotonic() + budget)
            except ConnectorError as e:
                if e.status == SIGN_IN:
                    raise
                changed = True      # what was read so far is kept; the rest follows next time
        return {"positions": positions, "cash": _cash(cash), "prices": prices, "changed": changed}
    finally:
        await feed.close()


def _run_async(coro):
    return asyncio.run(coro)


def open_session(creds: dict):
    """An HTTP session with the stored cookies, renewed (raises SIGN_IN)."""
    if not creds.get("cookies"):
        raise signed_out()
    http = new_http()
    cookies_in(http.cookies, creds.get("cookies"))
    refresh_session(http)
    return http


def collect(http, creds: dict, history: dict, want_history: bool,
            budget: float = HISTORY_BUDGET) -> tuple[TRSnapshot, dict, bool]:
    """Read everything for one connection. Returns (snapshot, credentials with
    the renewed cookies, history changed?)."""
    creds = dict(creds)
    if not creds.get("sec_acc_no"):
        creds["sec_acc_no"] = account_number(http)
    creds["cookies"] = cookies_out(http.cookies)
    data = _run_async(_read(creds, history, want_history, budget))
    snap = assemble(data["positions"], data["cash"], history, data["prices"],
                    account_hash(creds["sec_acc_no"]))
    return snap, creds, data["changed"]


def first_sync(proc: LoginProcess, history_for=None) -> tuple[TRSnapshot, dict, dict]:
    """Right after a sign-in: positions and cash (the history follows in the
    background). `history_for(account)` gives the stored history of an
    existing connection to the same account (renewing it). Returns
    (snapshot, credentials, history)."""
    http = proc.http
    try:
        refresh_session(http)
    except ConnectorError as e:
        if e.status == SIGN_IN:
            raise ConnectorError(f"{NAME} did not finish the sign-in. Start again.") from None
        raise
    sec = account_number(http)
    acct = account_hash(sec)
    history = history_of({"history": history_for(acct)} if history_for else None)
    creds = {"cookies": cookies_out(http.cookies), "sec_acc_no": sec, "login": proc.mode}
    snap, creds, _ = collect(http, creds, history, want_history=False)
    return snap, creds, history


def _save(conn_id: str, started_with: list, creds: dict, history: dict | None) -> None:
    """Keep the renewed cookies (and the history) with the connection. Skips
    the cookies if a new sign-in replaced them while this sync ran."""
    try:
        from .. import connections
        stored = connections.get(conn_id)
        if stored is None:
            return
        fields = {}
        if ((stored.get("credentials") or {}).get("cookies") or []) == started_with:
            fields["credentials"] = creds
        if history is not None:
            fields["history"] = history
        if fields:
            connections.save_fields(conn_id, **fields)
    except Exception:
        pass


def fetch(creds: dict, conn: dict | None = None) -> TRSnapshot:
    """registry.Spec.fetch: refresh the session, read positions and cash, and
    the history when it is due (every few hours, on Refresh, or until read)."""
    conn = conn or {}
    history = history_of(conn)
    due = (bool(conn.get("_manual")) or not history.get("complete")
           or time.time() - float(history.get("synced") or 0) > HISTORY_EVERY)
    started_with = list(creds.get("cookies") or [])
    http = open_session(creds)
    new_creds = {**creds, "cookies": cookies_out(http.cookies)}
    changed = False
    try:
        snap, new_creds, changed = collect(http, new_creds, history, want_history=due)
    finally:
        if conn.get("id"):
            _save(conn["id"], started_with, new_creds, history)
    if changed:
        try:
            from .. import cache
            cache.invalidate()
        except Exception:
            pass
    return snap
