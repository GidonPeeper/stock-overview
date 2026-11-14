"""Enable Banking -- live, read-only bank balances through PSD2 open banking.

One Enable Banking *application* (an application ID plus an RSA private key)
serves every bank you link. Each link is one consent ("session") at one bank,
which can cover several accounts; it lasts up to the bank's maximum (180 days
for most banks) and is then renewed by going through the bank's approval again.

Flow (all calls carry `Authorization: Bearer <JWT>`):
    GET  /aspsps                         banks: name, country, psu_types,
                                         maximum_consent_validity (seconds),
                                         required_psu_headers, beta, logo
    POST /auth {access: {valid_until}, aspsp: {name, country}, state,
                redirect_url, psu_type}  -> {url, authorization_id}
         the user approves at the bank, which sends them to
         redirect_url?code=...&state=...
    POST /sessions {code}                -> {session_id, accounts: [{uid,
                                         account_id: {iban}, details, product,
                                         cash_account_type, currency,
                                         credit_limit, identification_hash}],
                                         aspsp, psu_type, access: {valid_until}}
    GET  /accounts/{uid}/balances        -> {balances: [{name, balance_amount:
                                         {amount, currency}, balance_type,
                                         reference_date, last_change_date_time}]}
    DELETE /sessions/{session_id}        end a consent (on remove / renew)

JWT: header {typ: "JWT", alg: "RS256" (the only one accepted), kid:
<application ID>}; claims iss "enablebanking.com", aud "api.enablebanking.com",
iat, exp (at most 86400 s after iat; 3600 is used, as in the quick start).
Signed here with `cryptography` (RSASSA-PKCS1-v1_5 + SHA-256, base64url).

Which balance: Enable Banking calls XPCD the "Instant Balance", so it comes
first, then the booked ones (ITBD intraday, CLBD accounting, ...). "Available"
types (ITAV, CLAV, FWAV, OPAV) can include an overdraft, so they come last,
and the account's credit limit is taken off them when it is known.

Limits: many banks allow only 4 reads a day while the user is not online (the
FAQ says wait 6 hours after hitting it), so balances are kept for 6 hours and
saved with the connection. A refresh the user asks for sends the PSU headers
(Psu-Ip-Address, Psu-User-Agent, ...) that tell the bank the user is online:
all of a bank's `required_psu_headers` or none.

Restricted production (free, personal use): a new production application is
"Inactive" until you click "Activate by linking accounts" in the Control Panel
and link your own accounts; it can then read only those accounts, and the API
authorisation (POST /auth) is still needed for each of them.

Sources (checked 2026-11-14):
  API reference  https://enablebanking.com/docs/api/reference/
  Quick start    https://enablebanking.com/docs/api/quick-start/
  Control Panel  https://enablebanking.com/docs/api/control-panel/
  Restricted use https://enablebanking.com/docs/api/linked-accounts/
  FAQ (180-day consent, 4 reads a day)  https://enablebanking.com/docs/faq
  Terms (9 Jan 2026: free; production limited to linked accounts, for the
         personal use of private individuals)  https://enablebanking.com/terms
  Markets (30 EEA countries, no UK)  https://enablebanking.com/docs/markets/
  Redirect URLs must be https: Actual Budget's setup guide
         https://actualbudget.org/docs/advanced/bank-sync/enable-banking
         (Enable Banking's own docs do not say).
Not documented, handled defensively: `error` / `error_description` on the
redirect when the user cancels (as in OAuth 2).

Errors are ConnectorErrors in plain words; the key, the JWT and the session ID
never appear in a message or a log line.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

from .live import TIMEOUT, ConnectorError, Snapshot, consent, num

NAME = "Enable Banking"
PROVIDER = "enablebanking"
BASE_URL = os.getenv("ENABLEBANKING_BASE_URL", "https://api.enablebanking.com")
CALLBACK_PATH = "/enablebanking/callback"

JWT_LIFETIME = 3600              # seconds (the API accepts up to 86400)
BANKS_TTL = 24 * 3600            # the bank list is fetched once a day
BALANCES_TTL = 6 * 3600          # 4 reads a day is the usual bank limit
RETRY = 30 * 60                  # after an ordinary failure
LIMIT_WAIT = 6 * 3600            # after a bank's rate limit / an ended consent
DEFAULT_CONSENT_DAYS = 90        # when a bank does not say its maximum
MAX_KEY = 20_000                 # bytes; a 4096-bit PEM is about 3.3 KB

PREFER = ("XPCD", "ITBD", "CLBD", "PRCD", "OPBD", "VALU", "INFO", "OTHR",
          "ITAV", "CLAV", "FWAV", "OPAV")
AVAILABLE = {"ITAV", "CLAV", "FWAV", "OPAV"}
_ENDED = {"EXPIRED_SESSION", "REVOKED_SESSION", "CLOSED_SESSION", "SESSION_DOES_NOT_EXIST"}
_TYPE_NAMES = {"SVGS": "Savings", "CARD": "Card", "LOAN": "Loan", "CACC": "Current account"}
_PSU_FROM = (("user-agent", "Psu-User-Agent"), ("accept", "Psu-Accept"),
             ("accept-language", "Psu-Accept-Language"),
             ("accept-encoding", "Psu-Accept-Encoding"))

_lock = threading.Lock()
_jwts: dict[str, tuple[str, float]] = {}        # app fingerprint -> (jwt, exp)
_banks: dict[str, tuple[float, list[dict]]] = {}  # app id -> (fetched_at, banks)


# --------------------------------------------------------------- the app
@dataclass(frozen=True)
class App:
    app_id: str
    private_key: str      # PEM text
    source: str = "app"   # "app" (saved in Settings) or "env" (server settings)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(f"{self.app_id}\n{self.private_key}".encode()).hexdigest()


def check_app_id(value: str) -> str:
    try:
        return str(uuid.UUID(str(value or "").strip()))
    except ValueError:
        raise ConnectorError("The application ID looks like aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee "
                             "(the .pem file is named after it).") from None


def clean_pem(text: str) -> str:
    text = (text or "").strip()
    if "\\n" in text and "\n" not in text:        # one-line env value
        text = text.replace("\\n", "\n")
    return text


def load_key(pem: str):
    """The RSA private key in `pem` (raises a plain ConnectorError)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    text = clean_pem(pem)
    if not text:
        raise ConnectorError("Choose the .pem file Enable Banking downloaded, or paste its text.")
    if len(text) > MAX_KEY:
        raise ConnectorError("That file is too large to be a private key.")
    if "PUBLIC KEY" in text or "CERTIFICATE" in text:
        raise ConnectorError("This is a public key or certificate. The app needs the private key: "
                             "the .pem file your browser downloaded when you registered the app.")
    if "PRIVATE KEY" not in text:
        raise ConnectorError("This is not a private key. Choose the .pem file Enable Banking "
                             "downloaded when you registered the application.")
    try:
        key = serialization.load_pem_private_key(text.encode(), password=None)
    except TypeError:
        raise ConnectorError("This key is protected with a password. Use the .pem file as "
                             "Enable Banking downloaded it.") from None
    except Exception:
        raise ConnectorError("Could not read this private key. Copy the whole .pem file, "
                             "including the BEGIN and END lines.") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ConnectorError("Enable Banking needs an RSA private key.")
    return key


def from_env() -> tuple[App | None, str]:
    """(app, problem) from ENABLEBANKING_APP_ID with ENABLEBANKING_PRIVATE_KEY
    or ENABLEBANKING_KEY_FILE (e.g. a Render Secret File)."""
    app_id = os.getenv("ENABLEBANKING_APP_ID", "").strip()
    pem = os.getenv("ENABLEBANKING_PRIVATE_KEY", "")
    path = os.getenv("ENABLEBANKING_KEY_FILE", "").strip()
    if not app_id and not pem and not path:
        return None, ""
    if not pem and path:
        candidates = [Path(path)]
        if not Path(path).is_absolute():
            root = Path(__file__).resolve().parents[2]
            candidates += [root / path, Path("/etc/secrets") / path]
        found = next((p for p in candidates if p.is_file()), None)
        if found is None:
            return None, "ENABLEBANKING_KEY_FILE is set, but the server cannot find that file."
        try:
            pem = found.read_text()[:MAX_KEY + 1]
        except OSError:
            return None, "ENABLEBANKING_KEY_FILE is set, but the server cannot read that file."
    if not app_id or not pem:
        return None, ("Set both ENABLEBANKING_APP_ID and ENABLEBANKING_PRIVATE_KEY "
                      "(or ENABLEBANKING_KEY_FILE) on the server.")
    try:
        return App(check_app_id(app_id), clean_pem(pem), "env"), ""
    except ConnectorError as e:
        return None, f"ENABLEBANKING_APP_ID on the server: {e}"


def current() -> tuple[App | None, str]:
    """The application to use: the one saved in Settings, else the server's
    environment. Returns (app, problem with the environment settings)."""
    from .. import connections

    saved = connections.app_credentials(PROVIDER)
    env_app, problem = from_env()
    if saved.get("app_id") and saved.get("private_key"):
        return App(saved["app_id"], saved["private_key"], "app"), problem
    return env_app, problem


def require_app() -> App:
    app, problem = current()
    if app is None:
        raise ConnectorError(problem or "Enable Banking is not set up yet. Add the application ID "
                             "and private key in Settings → Add an account → Bank link.")
    return app


# ------------------------------------------------------------------ JWT
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def make_jwt(app: App, now: float | None = None) -> str:
    """A signed RS256 token for the API (see the module docstring)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    iat = int(time.time() if now is None else now)
    header = {"typ": "JWT", "alg": "RS256", "kid": app.app_id}
    claims = {"iss": "enablebanking.com", "aud": "api.enablebanking.com",
              "iat": iat, "exp": iat + JWT_LIFETIME}
    signing_input = (_b64(json.dumps(header, separators=(",", ":")).encode()) + "."
                     + _b64(json.dumps(claims, separators=(",", ":")).encode()))
    signature = load_key(app.private_key).sign(signing_input.encode(), padding.PKCS1v15(),
                                               hashes.SHA256())
    return signing_input + "." + _b64(signature)


def _jwt(app: App) -> str:
    now = time.time()
    with _lock:
        hit = _jwts.get(app.fingerprint)
        if hit and hit[1] - now > 300:
            return hit[0]
    token = make_jwt(app, now)
    with _lock:
        _jwts[app.fingerprint] = (token, int(now) + JWT_LIFETIME)
    return token


# ------------------------------------------------------------------ HTTP
def _safe(text) -> str:
    """An API message fit to show: no tokens, short."""
    s = re.sub(r"eyJ[\w-]+\.[\w-]+\.[\w-]*", "…", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:160]


def _error(resp: requests.Response, bank: str) -> ConnectorError:
    status = resp.status_code
    try:
        body = resp.json()
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}
    code = str(body.get("error") or "").upper()
    detail = _safe(body.get("message"))
    who = bank or "The bank"
    if code in _ENDED:
        return ConnectorError(f"The link to {who} has ended. Press Renew to link it again.",
                              status="expired", retry_after=LIMIT_WAIT)
    if code == "ASPSP_RATE_LIMIT_EXCEEDED" or status == 429:
        return ConnectorError(f"{who} allows only a few updates a day. The balance will update "
                              "again in a few hours (or press Refresh then).",
                              status=429, retry_after=LIMIT_WAIT)
    if code == "REDIRECT_URI_NOT_ALLOWED":
        return ConnectorError("Enable Banking does not accept this app's return address yet.",
                              status=code)
    if code in ("EXPIRED_AUTHORIZATION_CODE", "WRONG_AUTHORIZATION_CODE", "ALREADY_AUTHORIZED"):
        return ConnectorError("This approval from the bank has expired or was already used. "
                              "Go back to the app and link the bank again.", status=code)
    if code == "NO_ACCOUNTS_ADDED":
        return ConnectorError("Your Enable Banking application has no linked accounts yet. In the "
                              "Control Panel, click 'Activate by linking accounts' and link this "
                              "bank first.", status=code)
    if code in ("ASPSP_ACCOUNT_NOT_ACCESSIBLE", "ACCOUNT_DOES_NOT_EXIST"):
        return ConnectorError(f"Enable Banking cannot read this {who} account. A restricted "
                              "application reads only accounts you linked in its Control Panel.",
                              status=code)
    if code == "WRONG_ASPSP_PROVIDED":
        return ConnectorError("Enable Banking does not know this bank. Pick it again from the list.",
                              status=code)
    if code == "ASPSP_PSU_ACTION_REQUIRED":
        return ConnectorError(f"{who} wants you to confirm something. Press Renew to link it again.",
                              status=code)
    if code == "ASPSP_TIMEOUT" or status == 408:
        return ConnectorError(f"{who} did not answer in time. Try again later.", status=status)
    if code == "ASPSP_ERROR" or status >= 500:
        return ConnectorError(f"{who} or Enable Banking is having problems right now "
                              f"(HTTP {status}). Try again later.", status=status)
    said = f": {detail}" if detail else ""
    if status == 401 or code in ("UNAUTHORIZED_ACCESS", "AUTHORIZATION_NOT_PROVIDED"):
        return ConnectorError(f"Enable Banking did not accept the application ID and key (HTTP 401{said}). "
                              "Check the ID is the one the .pem file is named after.", status=401)
    if status == 403 or code in ("ACCESS_DENIED", "UNAUTHORIZED_IP"):
        # an unknown application ID is a 403 "Application does not exist"
        return ConnectorError(f"Enable Banking refused access (HTTP 403{said}). Check the application "
                              "ID (the .pem file is named after it), and that the application is "
                              "activated in the Control Panel by linking your accounts.", status=403)
    return ConnectorError(f"Enable Banking answered with an error (HTTP {status})"
                          + (f": {detail}" if detail else "."), status=code or status)


def _call(app: App, method: str, path: str, *, body: dict | None = None,
          params: dict | None = None, headers: dict | None = None, bank: str = ""):
    h = {"Authorization": f"Bearer {_jwt(app)}", "Accept": "application/json"}
    h.update(headers or {})
    try:
        resp = requests.request(method, BASE_URL + path, json=body, params=params, headers=h,
                                timeout=TIMEOUT)
    except requests.Timeout:
        raise ConnectorError(f"{NAME} did not answer in time. Try again later.") from None
    except requests.RequestException:
        raise ConnectorError(f"Could not reach {NAME}. Check the internet connection.") from None
    if resp.status_code >= 400:
        raise _error(resp, bank) from None
    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        raise ConnectorError(f"{NAME} sent an answer this app could not read.") from None


# ---------------------------------------------------------------- banks
def _bank(a: dict) -> dict:
    seconds = int(num(a.get("maximum_consent_validity"), 0))
    logo = a.get("logo") if str(a.get("logo") or "").startswith("https://") else ""
    return {"name": str(a["name"]), "country": str(a["country"]).upper(),
            "max_seconds": seconds, "max_days": seconds // 86400 if seconds else None,
            "psu_types": [str(t) for t in (a.get("psu_types") or [])],
            "required_psu_headers": [str(h) for h in (a.get("required_psu_headers") or [])],
            "beta": bool(a.get("beta")), "logo": logo}


def banks(app: App, force: bool = False) -> list[dict]:
    """Every bank Enable Banking offers (cached for a day; on a failure the
    last list is reused when there is one)."""
    with _lock:
        hit = _banks.get(app.app_id)
    if hit and not force and time.time() - hit[0] < BANKS_TTL:
        return hit[1]
    try:
        data = _call(app, "GET", "/aspsps")
    except ConnectorError:
        if hit and not force:
            return hit[1]
        raise
    out = [_bank(a) for a in (data.get("aspsps") or []) if isinstance(a, dict)
           and a.get("name") and a.get("country")]
    with _lock:
        _banks[app.app_id] = (time.time(), out)
    return out


def countries(bank_list: list[dict]) -> list[str]:
    return sorted({b["country"] for b in bank_list})


def find_bank(bank_list: list[dict], name: str, country: str) -> dict | None:
    return next((b for b in bank_list if b["name"] == name and b["country"] == country.upper()), None)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def plain_name(name: str) -> str:
    """'ING (Netherlands)' -> 'ING'."""
    return re.sub(r"\s*\(.*?\)\s*", " ", name or "").strip()


def suggest(institution: dict | None, bank_list: list[dict]) -> dict | None:
    """The bank in `bank_list` that is this catalog institution, if any:
    same name first, then the shortest name starting with it (either way)."""
    if not institution:
        return None
    want = _norm(plain_name(institution.get("name") or institution.get("id") or ""))
    if len(want) < 2:
        return None
    exact = [b for b in bank_list if _norm(b["name"]) == want]
    if exact:
        return exact[0]
    close = [b for b in bank_list if len(_norm(b["name"])) >= 3
             and (_norm(b["name"]).startswith(want) or want.startswith(_norm(b["name"])))]
    return min(close, key=lambda b: len(b["name"])) if close else None


def choose_country(institution: dict | None, bank_list: list[dict]) -> str:
    """The country to open the bank list on: one of the institution's
    countries where Enable Banking lists it, else any country that lists it
    (Trade Republic may be listed under DE only), else one of the
    institution's countries, else NL."""
    available = countries(bank_list)
    wanted = [{"UK": "GB"}.get(c.upper(), c.upper()) for c in (institution or {}).get("countries") or []]
    for c in wanted + available:
        if c in available and suggest(institution, [b for b in bank_list if b["country"] == c]):
            return c
    for c in wanted:
        if c in available:
            return c
    return "NL" if "NL" in available or not available else available[0]


# ---------------------------------------------------------- authorisation
def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def psu_type(bank: dict) -> str:
    types = bank.get("psu_types") or []
    return "personal" if not types or "personal" in types else types[0]


def consent_until(bank: dict, now: float | None = None) -> str:
    """How long to ask access for: the bank's maximum (minus 10 minutes, so
    it is never above it), or DEFAULT_CONSENT_DAYS when the bank does not say."""
    now = time.time() if now is None else now
    seconds = bank.get("max_seconds") or DEFAULT_CONSENT_DAYS * 86400
    return _iso(now + max(3600, seconds - 600))


def start_auth(app: App, bank: dict, redirect_url: str, state: str,
               valid_until: str | None = None) -> dict:
    """Ask Enable Banking for the bank's approval page -> {url, valid_until}."""
    valid_until = valid_until or consent_until(bank)
    body = {"access": {"valid_until": valid_until},
            "aspsp": {"name": bank["name"], "country": bank["country"]},
            "state": state, "redirect_url": redirect_url, "psu_type": psu_type(bank)}
    data = _call(app, "POST", "/auth", body=body, bank=bank["name"])
    url = str(data.get("url") or "")
    if not url.startswith("https://"):
        raise ConnectorError(f"{NAME} did not return the bank's approval page. Try again.")
    return {"url": url, "valid_until": valid_until}


def _tail(account: dict) -> str:
    ident = account.get("account_id") or {}
    iban = ident.get("iban") if isinstance(ident, dict) else ""
    if not iban and isinstance(ident, dict):
        other = ident.get("other") or {}
        iban = other.get("identification") if isinstance(other, dict) else ""
    digits = re.sub(r"\s", "", str(iban or ""))
    return "••" + digits[-4:] if len(digits) >= 4 else ""


def _account_name(a: dict) -> str:
    # `name` is the account holder's name, so the description or product is used.
    for key in ("details", "product"):
        v = str(a.get(key) or "").strip()
        if v:
            return v[:60]
    return _TYPE_NAMES.get(str(a.get("cash_account_type") or "").upper(), "Account")


def finish_auth(app: App, code: str, bank: str = "") -> dict:
    """Swap the code from the bank's redirect for a session:
    {session_id, accounts: [{uid, name, currency, iban, type, hash,
    credit_limit}], valid_until, aspsp, country, psu_type}."""
    if not code or len(code) > 2000:
        raise ConnectorError("The bank did not send an approval code. Link the bank again.")
    data = _call(app, "POST", "/sessions", body={"code": code}, bank=bank)
    session_id = str(data.get("session_id") or "")
    if not session_id:
        raise ConnectorError(f"{NAME} did not open a session. Link the bank again.")
    accounts = []
    for a in data.get("accounts") or []:
        a = {"uid": a} if isinstance(a, str) else a
        if not isinstance(a, dict) or not a.get("uid"):
            continue
        limit = a.get("credit_limit") if isinstance(a.get("credit_limit"), dict) else None
        accounts.append({"uid": str(a["uid"]), "name": _account_name(a),
                         "currency": str(a.get("currency") or "").upper(), "iban": _tail(a),
                         "type": str(a.get("cash_account_type") or "").upper(),
                         "hash": str(a.get("identification_hash") or ""),
                         "credit_limit": limit})
    if not accounts:
        raise ConnectorError(f"{bank or 'The bank'} shared no accounts. Link it again and select "
                             "at least one account.")
    aspsp = data.get("aspsp") if isinstance(data.get("aspsp"), dict) else {}
    return {"session_id": session_id, "accounts": accounts,
            "valid_until": str((data.get("access") or {}).get("valid_until") or ""),
            "aspsp": str(aspsp.get("name") or bank), "country": str(aspsp.get("country") or ""),
            "psu_type": str(data.get("psu_type") or "")}


def end_session(app: App, session_id: str) -> None:
    """Best effort: tell Enable Banking an old consent is no longer used."""
    if not session_id:
        return
    try:
        _call(app, "DELETE", f"/sessions/{session_id}")
    except ConnectorError:
        pass


# --------------------------------------------------------------- balances
def pick_balance(rows: list, credit_limit: dict | None = None) -> tuple[float, str] | None:
    """(amount, currency) of the balance to show (see PREFER), or None."""
    valid = [r for r in rows or [] if isinstance(r, dict)
             and isinstance(r.get("balance_amount"), dict)
             and r["balance_amount"].get("amount") not in (None, "")]
    if not valid:
        return None

    def rank(r: dict) -> int:
        t = str(r.get("balance_type") or "").upper()
        return PREFER.index(t) if t in PREFER else len(PREFER)

    best = min(rank(r) for r in valid)
    same = [r for r in valid if rank(r) == best]
    row = max(same, key=lambda r: str(r.get("last_change_date_time") or r.get("reference_date") or ""))
    amount = num(row["balance_amount"].get("amount"))
    ccy = str(row["balance_amount"].get("currency") or "").upper()
    if str(row.get("balance_type") or "").upper() in AVAILABLE and credit_limit:
        if str(credit_limit.get("currency") or ccy).upper() == ccy:
            amount -= num(credit_limit.get("amount"))
    return amount, ccy


def psu_headers(given: dict | None, required: list[str]) -> dict:
    """The PSU headers to send: all we have, unless the bank requires one we
    do not have (then none, as the API asks)."""
    if not given:
        return {}
    have = {k.lower() for k in given}
    if any(h.lower() not in have for h in required or []):
        return {}
    return dict(given)


def psu_from_request(headers, client_ip: str = "") -> dict:
    """PSU headers for a request the user made (Settings refresh, return from
    the bank). Empty for a private address (local use): banks want the
    user's public IP."""
    import ipaddress

    ip = (headers.get("x-forwarded-for") or "").split(",")[0].strip() or client_ip
    try:
        if not ipaddress.ip_address(ip).is_global:
            return {}
    except ValueError:
        return {}
    out = {"Psu-Ip-Address": ip}
    for src, dst in _PSU_FROM:
        v = headers.get(src)
        if v:
            out[dst] = str(v)[:300]
    return out


def balances(app: App, uid: str, bank: str = "", psu: dict | None = None) -> list:
    data = _call(app, "GET", f"/accounts/{uid}/balances", headers=psu, bank=bank)
    return data.get("balances") or []


def nice_date(iso_date: str) -> str:
    d = datetime.fromisoformat(iso_date)
    return f"{d.day} {d.strftime('%b %Y')}"


def ended_message(bank: str, info: dict) -> str:
    return f"The link to {bank} ended on {nice_date(info['expires'])}. Press Renew to link it again."


def fetch(creds: dict, conn: dict | None = None) -> Snapshot:
    conn = conn or {}
    meta = conn.get("bank") or {}
    bank = meta.get("aspsp") or conn.get("institution_name") or "the bank"
    accounts = [a for a in creds.get("accounts") or [] if isinstance(a, dict) and a.get("uid")]
    if not creds.get("session_id") or not accounts:
        raise ConnectorError(f"{bank} is not linked yet. Press Renew to link it.")
    info = consent(conn.get("consent_expires"))
    if info and info["expired"]:
        raise ConnectorError(ended_message(bank, info), status="expired", retry_after=LIMIT_WAIT)
    app = require_app()
    headers = psu_headers(conn.get("_psu"), meta.get("required_psu_headers") or [])
    several = len(accounts) > 1
    cash, problems = [], []
    for a in accounts:
        try:
            rows = balances(app, a["uid"], bank, headers)
        except ConnectorError as e:
            if e.retry_after or e.status in (401, 403):
                raise                        # the whole link is affected
            problems.append(e)
            continue
        picked = pick_balance(rows, a.get("credit_limit"))
        if picked is None:
            continue
        amount, ccy = picked
        name = a.get("name") or "Account"
        if several and a.get("iban"):
            name = f"{name} {a['iban']}"
        cash.append({"name": name, "currency": ccy or a.get("currency") or "EUR",
                     "balance": round(amount, 2),
                     "type": "savings" if a.get("type") == "SVGS" else "cash"})
    if not cash and problems:
        raise problems[0]
    if not cash:
        raise ConnectorError(f"{bank} sent no balances this app can read.")
    n = len(cash)
    return Snapshot(summary=f"{n} account{'s' if n != 1 else ''}", cash=cash)
