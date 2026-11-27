"""Accounts added in Settings ("Add an account") and the data they bring in.

Store
    data/connections.json      one record per connection (credentials included)
    data/manual_holdings.json  positions read from a generic CSV, per connection
Both are git-ignored and bundled (encrypted) into data/vault.enc like the other
private files; every edit is pushed with github_sync.push_vault_async().

A connection record:
    {id, institution, institution_name, label, method, connector, wrapper,
     credentials: {...}, created, last_sync, status, error, summary}
`method` is the catalog method type (api / upload / aggregator), `connector`
the code that reads it (registry.CONNECTORS id, or "generic_csv"). `wrapper`
is "ISA" for a UK ISA (tax-free) or "". Secrets never leave the server:
`public()` masks every credential the connector does not mark non-secret.
A bank link (aggregator) also keeps `bank` (bank name, country, account
names; nothing secret), `consent_expires` (ISO date-time) and `snapshot` (the
last balances, so a restart does not spend one of the bank's few daily reads).

A Trade Republic sign-in (traderepublic_api.py) keeps only Trade Republic's
session cookies and the securities account number in `credentials` (never
the PIN), `tr` (an account fingerprint and the phone's last two digits, for
the UI), `history` (the timeline items already read, so a sync asks only for
what is new) and `snapshot` (the last positions and cash). When its session
can no longer be renewed, status is "signin": the user signs in again, and
the dashboard keeps the last snapshot meanwhile.

connections.json also holds `apps`: one set of credentials per bank-link
service (e.g. the Enable Banking application ID and private key), shared by
all its connections and never sent to the browser. Bank links in progress
(the single-use `state` sent to the bank) live in bank_link_states.json for
30 minutes; that file is git-ignored and not part of the vault.

Runtime
    Live data is fetched in a small thread pool with timeouts and cached per
    connection for the connector's TTL. Dashboard calls wait a few seconds for
    a first result, then use the last good one while a refresh runs in the
    background. A failing institution only marks its own connection as
    "error"; it never raises into the dashboard. After a failure it is tried
    again after the connector's retry delay (or the error's `retry_after`).

Integration
    holdings() / trade_sources()  -> sources.py
    cash_entries()                -> cash.py ("auto" entries)
    get_dividends() / get_interest() / get_fees() / income_by_account()
                                  -> income.py, uktax.py
    t212_accounts()               -> connectors/trading212.py (same code path
                                     as the T212_* env keys)
    snapshot()                    -> connectors/traderepublic.py (it counts a
                                     Trade Republic sign-in and an uploaded
                                     export of the same account once)
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
from pathlib import Path

from .connectors import registry
from .connectors.base import Holding
from .connectors.live import ConnectorError, Snapshot, consent
from .datafiles import DATA_DIR, resolve
from .fx import to_eur

log = logging.getLogger("stock_overview.connections")

FILE = "connections.json"
HOLDINGS_FILE = "manual_holdings.json"
STATES_FILE = "bank_link_states.json"

# (After a failed fetch, an institution is tried again after its connector's
# `retry`, registry.Spec, 120 s by default, or the error's `retry_after`.)

# A bank link must come back from the bank within this many seconds.
STATE_TTL = 30 * 60

# ConnectorError.status of a connection whose sign-in has ended (Trade
# Republic): shown as "Sign in again", not as an error.
SIGN_IN = "sign_in"

# Never sent to the browser (besides the credentials, which are masked).
_PRIVATE = ("credentials", "snapshot", "history")

_lock = threading.RLock()
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="connection")
_cache: dict[str, tuple[Snapshot, float]] = {}      # id -> (snapshot, fetched_at)
_failed: dict[str, tuple[float, float]] = {}        # id -> (failed_at, seconds to wait)
_inflight: dict[str, object] = {}                    # id -> Future
_read_cache: dict[str, tuple[float, object]] = {}    # file name -> (mtime, parsed)
damaged: dict[str, str] = {}                        # file name -> why it could not be read


# ------------------------------------------------------------------- files
def _file(name: str) -> Path:
    path, real = resolve(name)
    return path if real else DATA_DIR / name


def _copy(data):
    """A deep copy, except each connection's `history` (Trade Republic: can be
    thousands of items, read on every page load). It is shared: code replaces
    it as a whole (save_fields) and never changes it in place."""
    if not isinstance(data, dict) or not isinstance(data.get("connections"), list):
        return copy.deepcopy(data)
    out = {k: copy.deepcopy(v) for k, v in data.items() if k != "connections"}
    out["connections"] = [
        {k: (v if k == "history" else copy.deepcopy(v)) for k, v in c.items()}
        if isinstance(c, dict) else copy.deepcopy(c) for c in data["connections"]]
    return out


def _read(name: str, default):
    path = _file(name)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return copy.deepcopy(default)
    with _lock:
        hit = _read_cache.get(name)
        if hit and hit[0] == mtime:
            return _copy(hit[1])
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, type(default)):
                raise ValueError(f"expected {type(default).__name__}, found {type(data).__name__}")
        except (ValueError, OSError) as e:
            # Damaged (cut off, edited by hand): run without it, say so, and
            # keep the file aside before anything is saved over it (_write).
            if name not in damaged:
                log.warning("%s could not be read (%s); running without it", name, e)
            damaged[name] = f"{type(e).__name__}: {e}"[:200]
            return copy.deepcopy(default)
        damaged.pop(name, None)
        _read_cache[name] = (mtime, data)
        return _copy(data)


def _write(name: str, data) -> None:
    path = _file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if name in damaged and path.exists():
            # never save over a damaged file: keep it for the user to recover
            keep = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
            os.replace(path, keep)
            log.warning("%s was damaged; kept as %s before saving a new one", name, keep.name)
            damaged.pop(name, None)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        os.replace(tmp, path)
        _read_cache.pop(name, None)


def load_all() -> list[dict]:
    data = _read(FILE, {"connections": []})
    conns = data.get("connections")
    out = []
    for c in conns if isinstance(conns, list) else []:
        if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"]:
            c.setdefault("label", c.get("institution_name") or c.get("institution") or c["id"])
            out.append(c)
    return out


def _save_all(conns: list[dict], apps: dict | None = None) -> None:
    doc = {"version": 1, "connections": conns}
    apps = _read(FILE, {}).get("apps") if apps is None else apps
    if apps:
        doc["apps"] = apps
    doc["updated"] = time.strftime("%Y-%m-%d %H:%M")
    _write(FILE, doc)


# ------------------------------------------------- bank-link app credentials
def app_credentials(provider: str) -> dict:
    """Credentials of a bank-link service's app (shared by all its
    connections), as saved in Settings; {} when none. Server-side only."""
    apps = _read(FILE, {}).get("apps") or {}
    return dict(apps.get(provider) or {})


def save_app_credentials(provider: str, creds: dict) -> None:
    with _lock:
        doc = _read(FILE, {})
        apps = dict(doc.get("apps") or {})
        apps[provider] = {**creds, "saved": _now()}
        _save_all(load_all(), apps)


def forget_app_credentials(provider: str) -> bool:
    with _lock:
        apps = dict(_read(FILE, {}).get("apps") or {})
        if apps.pop(provider, None) is None:
            return False
        _save_all(load_all(), apps)
        return True


# ------------------------------------------------------ bank links in progress
def _state_key(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def new_link_state(info: dict, now: float | None = None) -> str:
    """A random single-use `state` for one trip to the bank. Only its hash is
    stored, with `info` (which bank, which connection to renew) and a
    STATE_TTL expiry; expired entries are dropped on every write."""
    now = time.time() if now is None else now
    state = secrets.token_urlsafe(24)
    with _lock:
        states = {k: v for k, v in _read(STATES_FILE, {}).items()
                  if isinstance(v, dict) and v.get("expires", 0) > now}
        states[_state_key(state)] = {"info": info, "expires": now + STATE_TTL}
        _write(STATES_FILE, states)
    return state


def take_link_state(state: str, now: float | None = None) -> dict | None:
    """The info saved with `state`, removing it (single use). None when the
    state is unknown, already used or expired."""
    if not state or len(state) > 200:
        return None
    now = time.time() if now is None else now
    key = _state_key(state)
    with _lock:
        states = _read(STATES_FILE, {})
        entry = states.pop(key, None)
        if entry is None:
            return None
        _write(STATES_FILE, {k: v for k, v in states.items()
                             if isinstance(v, dict) and v.get("expires", 0) > now})
    if not isinstance(entry, dict) or entry.get("expires", 0) <= now:
        return None
    return entry.get("info") or {}


def get(conn_id: str) -> dict | None:
    return next((c for c in load_all() if c["id"] == conn_id), None)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new(institution: dict, method: str, connector: str, label: str, wrapper: str,
        credentials: dict) -> dict:
    return {
        "id": "c_" + secrets.token_hex(5),
        "institution": institution["id"],
        "institution_name": institution.get("name") or institution["id"],
        "label": label.strip() or institution.get("name") or institution["id"],
        "method": method,
        "connector": connector,
        "wrapper": "ISA" if (wrapper or "").upper() == "ISA" else "",
        "credentials": credentials,
        "created": _now(),
        "last_sync": None,
        "status": "pending",
        "error": "",
        "summary": "",
    }


def add(conn: dict) -> dict:
    with _lock:
        conns = load_all()
        conns.append(conn)
        _save_all(conns)
    return conn


def remove(conn_id: str) -> dict | None:
    with _lock:
        conns = load_all()
        gone = next((c for c in conns if c["id"] == conn_id), None)
        if gone is None:
            return None
        _save_all([c for c in conns if c["id"] != conn_id])
        holdings = _read(HOLDINGS_FILE, {})
        if conn_id in holdings:
            holdings.pop(conn_id)
            _write(HOLDINGS_FILE, holdings)
        _cache.pop(conn_id, None)
        _failed.pop(conn_id, None)
    return gone


def _set_state(conn_id: str, **fields) -> None:
    """Persist status / last_sync / error / summary (or any other stored
    field) of one connection. Does nothing for an unknown id."""
    with _lock:
        conns = load_all()
        for c in conns:
            if c["id"] == conn_id:
                if all(c.get(k) == v for k, v in fields.items()):
                    return
                c.update(fields)
                _save_all(conns)
                return


def save_fields(conn_id: str, **fields) -> None:
    """Store fields a connector renewed during a sync (Trade Republic's
    session cookies, the history read so far), keeping the cached data."""
    _set_state(conn_id, **fields)


def update(conn_id: str, **fields) -> dict | None:
    """Change stored fields of one connection (e.g. a renewed bank link's
    credentials) and forget its cached data, so the next read fetches anew."""
    _set_state(conn_id, **fields)
    with _lock:
        _cache.pop(conn_id, None)
        _failed.pop(conn_id, None)
    return get(conn_id)


def unique_label(label: str, taken: set[str]) -> str:
    base, n, out = label.strip(), 2, label.strip()
    lower = {t.lower() for t in taken}
    while out.lower() in lower:
        out = f"{base} ({n})"
        n += 1
    return out


# ------------------------------------------------------------------ masking
def _shown_fields(conn: dict) -> set[str]:
    """Names of credential fields that may be shown (marked non-secret)."""
    spec = registry.connector(conn.get("connector"))
    return {f["name"] for f in (spec.fields if spec else ()) if f.get("secret") is False}


def mask(value) -> str:
    if isinstance(value, (list, tuple, dict)):
        return "••••" if value else ""
    s = str(value or "")
    return "••••" + s[-4:] if len(s) >= 16 else ("••••" if s else "")


def public(conn: dict) -> dict:
    """The connection as the browser may see it: secrets masked, plus the
    number of positions for a CSV import and how long a bank link's consent
    still runs. Every credential is treated as secret unless the connector
    explicitly marks the field as not secret."""
    shown = _shown_fields(conn)
    out = {k: v for k, v in conn.items() if k not in _PRIVATE}
    out["credentials"] = {k: (v if k in shown else mask(v))
                          for k, v in (conn.get("credentials") or {}).items()}
    spec = registry.connector(conn.get("connector"))
    out["updates"] = spec.updates if spec else "When you upload a new file"
    if conn.get("consent_expires"):
        out["consent"] = consent(conn["consent_expires"])
    if conn.get("connector") == registry.GENERIC_CSV:
        entry = _read(HOLDINGS_FILE, {}).get(conn["id"]) or {}
        out["positions"] = len(entry.get("holdings") or [])
        out["file_name"] = entry.get("file_name", "")
    return out


# ------------------------------------------------------------- credentials
def canonical_credentials(connector_id: str, given: dict, catalog_fields: list[dict]) -> dict:
    """Credentials keyed by the connector's own field names. The catalog may
    call a field differently ("flex_token" vs "token"); the catalog view maps
    them already, this also accepts either name. Values are trimmed."""
    spec = registry.connector(connector_id)
    if spec is None:
        return {}
    given = {str(k): str(v).strip() for k, v in (given or {}).items() if v is not None}
    out = {}
    for f in spec.fields:
        names = [f["name"], *f.get("aliases", ())]
        value = next((given[n] for n in names if given.get(n)), "")
        out[f["name"]] = value
    return out


def missing_fields(connector_id: str, creds: dict) -> list[str]:
    spec = registry.connector(connector_id)
    return [f.get("label") or f["name"] for f in (spec.fields if spec else ())
            if not creds.get(f["name"])]


# ----------------------------------------------------------------- fetching
def _retry_wait(spec, err: ConnectorError | None) -> float:
    if err is not None and err.retry_after:
        return float(err.retry_after)
    return min(spec.retry, spec.ttl)


def _persisted(conn: dict, spec=None) -> tuple[Snapshot, float] | None:
    """The last data saved with the connection (connectors with `persist`), as
    a cache entry: balances and positions, or what the connector's `restore`
    rebuilds from them."""
    s = conn.get("snapshot")
    if not isinstance(s, dict) or not s.get("at") or not isinstance(s.get("cash"), list):
        return None
    if spec is not None and spec.restore is not None:
        try:
            snap = spec.restore(conn)
        except Exception:
            snap = None
        return (snap, float(s["at"])) if snap is not None else None
    holdings = []
    for h in s.get("holdings") or []:
        try:
            holdings.append(Holding(**h))
        except TypeError:
            continue
    return Snapshot(summary=s.get("summary") or "", cash=s["cash"], holdings=holdings), float(s["at"])


def _state_after(spec, snap: Snapshot, at: float) -> dict:
    fields = {"status": "ok", "error": "", "last_sync": _now(), "summary": snap.summary}
    if spec is not None and spec.persist:
        fields["snapshot"] = {"cash": snap.cash, "summary": snap.summary, "at": at}
        if snap.holdings:
            fields["snapshot"]["holdings"] = [dataclasses.asdict(h) for h in snap.holdings]
    return fields


def _status_of(err: ConnectorError) -> str:
    if err.status == SIGN_IN:
        return "signin"
    return "pending" if err.pending else "error"


def _run(conn: dict, validate: bool = False) -> Snapshot:
    """Fetch one connection. `conn` may carry `_psu` (headers that tell a bank
    the user is online: only for fetches the user asked for)."""
    spec = registry.connector(conn.get("connector"))
    if spec is None or spec.fetch is None:
        raise ConnectorError("This connection type is not available.")
    try:
        snap = spec.fetch(conn.get("credentials") or {}, conn)
    except ConnectorError as e:
        if not validate:
            with _lock:
                _failed[conn["id"]] = (time.time(), _retry_wait(spec, e))
            _set_state(conn["id"], status=_status_of(e), error=str(e))
        raise
    except Exception as e:  # unexpected: never let details (URLs, keys) escape
        err = ConnectorError(f"Something went wrong reading {spec.name} ({type(e).__name__}).")
        if not validate:
            with _lock:
                _failed[conn["id"]] = (time.time(), _retry_wait(spec, None))
            _set_state(conn["id"], status="error", error=str(err))
        raise err from None
    if not validate:
        now = time.time()
        with _lock:
            if get(conn["id"]) is None:
                return snap          # removed while fetching
            _cache[conn["id"]] = (snap, now)
            _failed.pop(conn["id"], None)
        _set_state(conn["id"], **_state_after(spec, snap, now))
    return snap


def _start(conn: dict):
    with _lock:
        fut = _inflight.get(conn["id"])
        if fut is not None and not fut.done():
            return fut
        fut = _pool.submit(_run, conn)
        _inflight[conn["id"]] = fut
        return fut


def _snapshots(conns: list[dict]) -> dict[str, Snapshot]:
    """Latest snapshot per live connection, never blocking for long: fresh
    cache -> used; stale -> used while a refresh runs; none -> wait up to the
    connector's first_wait (all connections in parallel)."""
    out: dict[str, Snapshot] = {}
    waiting: list[tuple[str, object, float]] = []
    now = time.time()
    for c in conns:
        spec = registry.connector(c.get("connector"))
        if spec is None or spec.fetch is None:
            continue
        hit = _cache.get(c["id"])
        if hit is None and spec.persist:
            hit = _persisted(c, spec)      # after a restart: the saved balances
            if hit is not None:
                with _lock:
                    _cache.setdefault(c["id"], hit)
        if hit and now - hit[1] < spec.ttl:
            out[c["id"]] = hit[0]
            continue
        failed = _failed.get(c["id"])
        if failed and now - failed[0] < failed[1]:
            if hit:
                out[c["id"]] = hit[0]
            continue
        fut = _start(c)
        if hit:
            out[c["id"]] = hit[0]
        else:
            waiting.append((c["id"], fut, spec.first_wait))
    deadline = now + max((w for _, _, w in waiting), default=0)
    for cid, fut, _ in waiting:
        try:
            out[cid] = fut.result(timeout=max(0.0, deadline - time.time()))
        except (FutureTimeout, ConnectorError):
            continue
        except Exception:
            continue
    return out


def snapshot(conn: dict) -> Snapshot | None:
    """The latest snapshot of one live connection (cached, stale while it
    refreshes, or the saved one), or None. Never raises."""
    try:
        return _snapshots([conn]).get(conn["id"])
    except Exception:
        return None


def fetch_soon(conn_id: str) -> None:
    """Start a full sync in the background now (e.g. the history after a
    sign-in), keeping the cached data until it is done."""
    conn = get(conn_id)
    if conn is None:
        return
    with _lock:
        _failed.pop(conn_id, None)
    _start({**conn, "_manual": True})


def validate(conn: dict) -> Snapshot:
    """Real read-only test call for a new connection (raises ConnectorError)."""
    spec = registry.connector(conn.get("connector"))
    if spec is None:
        raise ConnectorError("This connection type is not available.")
    fut = _pool.submit(_run, conn, True)
    try:
        return fut.result(timeout=spec.validate_wait)
    except FutureTimeout:
        raise ConnectorError(f"{spec.name} is taking too long to answer. It will be "
                             "checked again in the background.", pending=True) from None


def remember(conn: dict, snap: Snapshot) -> None:
    """Keep the validation result as the first snapshot (no second fetch)."""
    now = time.time()
    with _lock:
        _cache[conn["id"]] = (snap, now)
    _set_state(conn["id"], **_state_after(registry.connector(conn.get("connector")), snap, now))


def refresh(conn_id: str, wait: float = 30.0, psu: dict | None = None) -> dict | None:
    """Fetch again now. `psu`: headers saying the user is online (they asked
    for this refresh), so a bank does not count it against its daily limit.
    The connector sees `_manual` (Trade Republic then also reads new history)."""
    conn = get(conn_id)
    if conn is None:
        return None
    if conn.get("connector") == registry.GENERIC_CSV:
        return conn
    if conn.get("connector") == "trading212":
        _clear_t212(conn)
    with _lock:
        _cache.pop(conn_id, None)
        _failed.pop(conn_id, None)
    try:
        _start({**conn, "_manual": True, **({"_psu": psu} if psu else {})}).result(timeout=wait)
    except Exception:
        pass
    return get(conn_id)


def _clear_t212(conn: dict) -> None:
    from .connectors import trading212
    for fn in (trading212.account_currency, trading212.account_summary,
               trading212.account_holdings, trading212.account_trades,
               trading212.account_dividends, trading212.account_transactions):
        try:
            fn.cache_clear()
        except AttributeError:
            pass


def feeds_here(spec, feed: str) -> bool:
    """Does this module feed `feed` for the connector? Not when a connector
    module handles it (Trading 212 keys: everything; Trade Republic sign-ins:
    holdings and trades, see registry.Spec `managed_feeds`)."""
    if spec is None or feed not in spec.feeds:
        return False
    if not spec.extra.get("managed_by"):
        return True
    return feed not in spec.extra.get("managed_feeds", spec.feeds)


def _feeding(feed: str) -> list[dict]:
    return [c for c in load_all() if feeds_here(registry.connector(c.get("connector")), feed)]


# -------------------------------------------------------------- integration
def holdings() -> list[Holding]:
    out: list[Holding] = []
    conns = load_all()
    csv_store = _read(HOLDINGS_FILE, {})
    for c in conns:
        if c.get("connector") == registry.GENERIC_CSV:
            out.extend(_csv_holdings(c, csv_store.get(c["id"]) or {}))
    live = _feeding("holdings")
    snaps = _snapshots(live)
    for c in live:
        snap = snaps.get(c["id"])
        if snap:
            out.extend(replace(h, broker=c["label"], wrapper=c.get("wrapper", ""))
                       for h in snap.holdings)
    return out


def _csv_holdings(conn: dict, entry: dict) -> list[Holding]:
    out = []
    for h in entry.get("holdings") or []:
        try:
            qty = float(h["quantity"])
        except (KeyError, TypeError, ValueError):
            continue
        ticker = h.get("ticker") or ""
        price = h.get("price")
        out.append(Holding(
            broker=conn["label"], ticker=ticker, name=h.get("name") or ticker,
            quantity=qty, buy_price=float(h.get("buy_price") or 0.0),
            # with a ticker the live price is used; else the file's own price
            currency=h.get("price_currency") or h.get("cost_currency") or "EUR",
            isin=h.get("isin") or "",
            price=None if ticker else (float(price) if price is not None else None),
            buy_currency=h.get("cost_currency") or None,
            wrapper=conn.get("wrapper", ""),
        ))
    return out


def save_csv_holdings(conn_id: str, holdings: list[dict], mapping: dict, file_name: str,
                      currency: str) -> None:
    with _lock:
        store = _read(HOLDINGS_FILE, {})
        store[conn_id] = {"updated": _now(), "file_name": file_name, "currency": currency,
                          "mapping": mapping, "holdings": holdings}
        _write(HOLDINGS_FILE, store)
    n = len(holdings)
    _set_state(conn_id, status="ok", error="", last_sync=_now(),
               summary=f"{n} position{'s' if n != 1 else ''} from {file_name or 'a CSV file'}")


def csv_entry(conn_id: str) -> dict:
    return _read(HOLDINGS_FILE, {}).get(conn_id) or {}


def _trades(conn_id: str):
    conn = get(conn_id)
    if conn is None:
        return []
    snap = _snapshots([conn]).get(conn_id)
    return list(snap.trades) if snap else []


def trade_sources() -> dict:
    """{label: (is_isa, get_trades)} for live connections with trade history."""
    return {c["label"]: (c.get("wrapper") == "ISA", functools.partial(_trades, c["id"]))
            for c in _feeding("trades")}


def cash_entries() -> list[dict]:
    """Bank balances and broker cash as read-only ("auto") cash accounts.
    For backend/interest.py each row also carries `connector`, the account's
    `ref` at the provider (a Snapshot.cash row's "ref") and any "interest"
    the connector read with the balances; cash.py drops them after use."""
    conns = _feeding("cash")
    snaps = _snapshots(conns)
    out = []
    for c in conns:
        snap = snaps.get(c["id"])
        if not snap:
            continue
        rows = [r for r in snap.cash if r.get("balance")]
        for r in rows:
            name = c["label"] if len(rows) == 1 and not r.get("name", "").startswith("cash") \
                else f"{c['label']} {r.get('name') or r['currency']}"
            out.append({"name": name, "institution": c.get("institution_name") or c["institution"],
                        "balance": round(float(r["balance"]), 2), "currency": r["currency"],
                        "type": r.get("type") or "cash", "rate": 0.0, "auto": True,
                        "connection": c["id"], "connector": c.get("connector"),
                        "ref": r.get("ref"), "interest": r.get("interest")})
    return out


def _income(kind: str) -> list[dict]:
    conns = _feeding("dividends")
    snaps = _snapshots(conns)
    out = []
    for c in conns:
        snap = snaps.get(c["id"])
        for x in (getattr(snap, kind, None) or [] if snap else []):
            out.append({**x, "account": c["label"], "isa": c.get("wrapper") == "ISA",
                        "amount_eur": round(to_eur(float(x["amount"]), x["currency"]), 2)})
    return out


def get_dividends() -> list[dict]:
    """[{account, isa, date, ticker, isin, amount, currency, amount_eur}]"""
    return _income("dividends")


def get_interest() -> list[dict]:
    """[{account, isa, date, amount, currency, amount_eur}]"""
    return _income("interest")


def get_fees() -> list[dict]:
    """Fees and taxes charged outside trades (Trade Republic: tax withheld on
    sales, Vorabpauschale; refunds positive): [{account, isa, date, amount,
    currency, amount_eur, kind}]."""
    return _income("fees")


def income_by_account() -> dict[str, dict]:
    zero = lambda: {"dividends": 0.0, "interest": 0.0, "fees": 0.0}  # noqa: E731
    out: dict[str, dict] = {c["label"]: zero() for c in _feeding("dividends")}
    for kind, rows in (("dividends", get_dividends()), ("interest", get_interest()),
                       ("fees", get_fees())):
        for x in rows:
            out.setdefault(x["account"], zero())[kind] += x["amount_eur"]
    # "fees" only where there are any (most connectors report none)
    return {k: {f: round(v, 2) for f, v in d.items() if f != "fees" or v} for k, d in out.items()}


def t212_account(conn: dict):
    from .connectors.trading212 import Account

    creds = conn.get("credentials") or {}
    return Account(label=conn.get("label") or "Trading 212",
                   key=(creds.get("api_key") or "").strip(),
                   secret=(creds.get("api_secret") or "").strip(),
                   isa=conn.get("wrapper") == "ISA")


def t212_accounts() -> list:
    """Trading 212 accounts added in-app, for connectors/trading212.accounts()."""
    return [t212_account(c) for c in load_all() if c.get("connector") == "trading212"]


def any_real() -> bool:
    return bool(load_all())
