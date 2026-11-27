"""Which code handles which catalog entry -- the ONE place that links
`backend/broker_catalog.json` (research data) to code.

The catalog describes institutions and the ways to track them. It can be
replaced wholesale by a bigger researched catalog: nothing in the UI is
per-broker. A method is linked to code in this order:

  1. an explicit key on the method in the catalog: `"connector": "starling"`
     (api / aggregator methods) or `"importer": "degiro"` (upload methods);
  2. otherwise the first matching rule in `LINKS` below, by institution id
     (compared loosely: case and punctuation ignored, so "trade_republic" ==
     "TradeRepublic") and the method's type, auth style or report name;
  3. otherwise an upload of a CSV/TXT positions export falls back to the
     generic CSV positions import, and everything else is shown as
     information only (steps + official link), or as a manual balance.

So the catalog file stays pure data: renaming or adding institutions there
needs no code change, and only a new importer or connector needs a line here.

To add a live connector: write `backend/connectors/<name>.py` with
`fetch(creds, conn) -> live.Snapshot`, add a `Spec` to `CONNECTORS`, and link
it in `LINKS` (or set "connector" on the catalog method). An aggregator
plugs in the same way under its provider id: every catalog method
`{"type": "aggregator", "provider": "enablebanking"}` is live because
`CONNECTORS["enablebanking"]` exists, while other providers (GoCardless,
TrueLayer, SnapTrade, ...) stay information only ("Coming soon") until they
get a connector. A bank link has no per-connection fields: its Spec says
`extra={"flow": "bank_link"}`, and the account is added by the redirect flow
in backend/banklink_api.py instead of the key form. Trade Republic signs in
with phone number + PIN + approval in its app: `extra={"flow": "tr_login"}`,
added by backend/traderepublic_api.py.

`managed_by` + `managed_feeds` hand some feeds to a connector module that
also reads another source for the same account (Trading 212 keys and the
statement; Trade Republic's sign-in and the uploaded export), so each account
is counted once; connections.py feeds only the rest. `restore` rebuilds the
last snapshot saved with a `persist` connection after a restart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from . import bitvavo, enablebanking, etoro, ibkr_flex, starling, traderepublic_live, wise
from .live import ConnectorError, Snapshot


@dataclass(frozen=True)
class Spec:
    id: str
    name: str                       # used in messages: "Starling did not accept..."
    feeds: tuple[str, ...]          # what flows into the app: cash / holdings / trades / dividends
    ttl: int                        # seconds a result is reused before fetching again
    updates: str                    # plain-language refresh cadence for the UI
    fields: tuple[dict, ...] = ()   # fallback form fields if the catalog has none
    fetch: Callable[[dict, dict], Snapshot] | None = None
    first_wait: float = 8.0         # seconds the dashboard waits for a first result
    validate_wait: float = 45.0     # seconds "Connect" waits for the test call
    retry: float = 120.0            # seconds before retrying after a failure
    persist: bool = False           # keep the last balances in connections.json
    restore: Callable[[dict], Snapshot | None] | None = None   # rebuild a saved snapshot
    extra: dict = field(default_factory=dict)


def _t212_fetch(creds: dict, conn: dict) -> Snapshot:
    """Trading 212 accounts added in-app feed backend/connectors/trading212.py
    directly (it lists them next to the T212_* env keys), so holdings, trades,
    dividends and cash are not duplicated here; this only checks the key."""
    import requests

    from . import trading212
    from ..connections import t212_account

    acct = t212_account(conn)
    if not acct.key or not acct.secret:
        raise ConnectorError("Fill in both the API key and the API secret.")
    try:
        try:
            summary = trading212._get(acct, "/equity/account/summary")
            ccy = summary.get("currency") or "EUR"
        except requests.HTTPError as e:
            if getattr(e.response, "status_code", None) not in (404, 405):
                raise
            ccy = trading212._get(acct, "/equity/account/info").get("currencyCode") or "EUR"
        positions = len(trading212.account_holdings(acct))
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", 0) or 0
        hint = {401: "Check you copied both the key and the secret (the secret is shown only once)",
                403: "Make a new key with the read permissions ticked (account data, portfolio, history)."}
        from .live import http_message
        raise ConnectorError(http_message("Trading 212", status, hint), status=status) from None
    except requests.Timeout:
        raise ConnectorError("Trading 212 did not answer in time. Try again later.") from None
    except requests.RequestException:
        raise ConnectorError("Could not reach Trading 212. Check the internet connection.") from None
    return Snapshot(summary=f"{ccy} account · {positions} positions")


CONNECTORS: dict[str, Spec] = {
    "trading212": Spec(
        id="trading212", name="Trading 212", feeds=("holdings", "trades", "cash", "dividends"),
        ttl=60, updates="Every minute",
        fields=({"name": "api_key", "label": "API key", "secret": True, "aliases": ("key",)},
                {"name": "api_secret", "label": "API secret", "secret": True,
                 "aliases": ("secret",)}),
        fetch=_t212_fetch, extra={"managed_by": "trading212"}),
    "starling": Spec(
        id="starling", name="Starling", feeds=("cash",), ttl=300, updates="Every few minutes",
        fields=({"name": "token", "label": "Personal access token", "secret": True,
                 "aliases": ("access_token", "personal_access_token", "api_token")},),
        fetch=starling.fetch),
    "wise": Spec(
        id="wise", name="Wise", feeds=("cash",), ttl=300, updates="Every few minutes",
        fields=({"name": "token", "label": "API token", "secret": True,
                 "aliases": ("api_token", "personal_token", "access_token")},),
        fetch=wise.fetch),
    "ibkr_flex": Spec(
        id="ibkr_flex", name="Interactive Brokers", feeds=("holdings", "trades", "cash", "dividends"),
        ttl=6 * 3600, updates="Once a day (IBKR prepares the report overnight)",
        fields=({"name": "token", "label": "Flex Web Service token", "secret": True,
                 "aliases": ("flex_token", "access_token")},
                {"name": "query_id", "label": "Flex Query ID", "secret": False,
                 "aliases": ("flex_query_id", "queryid")}),
        fetch=ibkr_flex.fetch, first_wait=10.0, validate_wait=40.0),
    "etoro": Spec(
        id="etoro", name="eToro", feeds=("holdings", "cash"), ttl=300, updates="Every few minutes",
        fields=({"name": "api_key", "label": "Public API key", "secret": True,
                 "aliases": ("public_api_key", "x_api_key")},
                {"name": "user_key", "label": "User key", "secret": True,
                 "aliases": ("x_user_key",)}),
        fetch=etoro.fetch),
    "bitvavo": Spec(
        id="bitvavo", name="Bitvavo", feeds=("holdings", "cash"), ttl=300, updates="Every few minutes",
        fields=({"name": "api_key", "label": "API key", "secret": True, "aliases": ("key",)},
                {"name": "api_secret", "label": "API secret", "secret": True,
                 "aliases": ("secret",)}),
        fetch=bitvavo.fetch),
    # One Enable Banking application serves every linked bank; each connection
    # is one consent at one bank. Banks allow about 4 background reads a day,
    # so balances are kept for 6 hours (and saved, to survive a restart).
    "enablebanking": Spec(
        id="enablebanking", name="Enable Banking", feeds=("cash",),
        ttl=enablebanking.BALANCES_TTL, retry=enablebanking.RETRY, persist=True,
        updates="Up to 4 times a day (a bank limit), and when you press Refresh",
        fetch=enablebanking.fetch, first_wait=10.0,
        extra={"flow": "bank_link", "psu": True}),
    # Unofficial: Trade Republic's own web sign-in (phone, PIN, approve in the
    # app). Positions and cash every few minutes, the history a few times a
    # day. Holdings and trades go through connectors/traderepublic.py, which
    # also reads the uploaded export and counts the account once.
    "traderepublic": Spec(
        id="traderepublic", name="Trade Republic", feeds=("holdings", "trades", "cash", "dividends"),
        ttl=300, retry=300.0, persist=True, restore=traderepublic_live.restore,
        updates="Every few minutes (history a few times a day)",
        fetch=traderepublic_live.fetch, first_wait=8.0, validate_wait=60.0,
        extra={"flow": "tr_login", "managed_by": "traderepublic",
               "managed_feeds": ("holdings", "trades")}),
}

# Upload importers handled by the existing /api/upload/{kind} endpoint, with
# the file types each one reads.
UPLOAD_KINDS = {"degiro": {"pdf", "csv"}, "trade_republic": {"csv", "pdf", "json"},
                "t212_statement": {"pdf", "csv", "json"}}
GENERIC_CSV = "generic_csv"
_TEXT_FORMATS = {"csv", "txt", "tsv"}

# Institution id (normalised, see norm()) -> rules. A rule matches a catalog
# method when every key in it matches: "type" (method type), "auth" (api auth
# style), "report" (regex on the upload's report_name, case-insensitive) and
# "format" (set of file formats). The first matching rule gives "connector"
# (live) or "importer" (upload kind). Several ids can share one rule list.
_T212 = [{"type": "api", "auth": "api_key_secret", "connector": "trading212"},
         {"type": "upload", "report": r"activity statement|history export",
          "importer": "t212_statement"}]
_IBKR = [{"type": "api", "auth": "flex_token_query", "connector": "ibkr_flex"}]
_DEGIRO = [{"type": "upload", "report": r"account (statement|overview)|rekeningoverzicht",
            "importer": "degiro"}]
_TR = [{"type": "api", "auth": "phone_pin", "connector": "traderepublic"},
       {"type": "upload", "report": r"transa[ck]tions?[ -]?export|account statement|rekeningoverzicht|kontoauszug",
        "importer": "trade_republic"}]
_STARLING = [{"type": "api", "auth": "token", "connector": "starling"}]
_WISE = [{"type": "api", "auth": "token", "connector": "wise"}]
_ETORO = [{"type": "api", "auth": "api_key_secret", "connector": "etoro"}]
_BITVAVO = [{"type": "api", "auth": "api_key_secret", "connector": "bitvavo"}]
LINKS: dict[str, list[dict]] = {
    "trading212": _T212, "t212": _T212,
    "ibkr": _IBKR, "interactivebrokers": _IBKR,
    "degiro": _DEGIRO,
    "traderepublic": _TR,
    "starling": _STARLING, "starlingbank": _STARLING,
    "wise": _WISE,
    "etoro": _ETORO,
    "bitvavo": _BITVAVO,
}


def norm(institution_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (institution_id or "").lower())


def connector(connector_id: str | None) -> Spec | None:
    return CONNECTORS.get(connector_id or "")


def _matches(rule: dict, method: dict) -> bool:
    if rule.get("type") and rule["type"] != method.get("type"):
        return False
    if rule.get("auth") and rule["auth"] != method.get("auth"):
        return False
    if rule.get("report") and not re.search(rule["report"], method.get("report_name") or "", re.I):
        return False
    if rule.get("format") and (method.get("file_format") or "").lower() not in rule["format"]:
        return False
    return True


def _rule(institution: dict, method: dict) -> dict:
    rules = LINKS.get(norm(institution.get("id", "")), [])
    return next((r for r in rules if _matches(r, method)), {})


def is_positions_file(method: dict) -> bool:
    """A CSV-like export that is one table of holdings. A statement that also
    lists transactions (IBKR/LYNX Activity Statement, Revolut stocks statement)
    has several sections with their own headers: the positions import cannot
    read it, so it is shown for information only."""
    fmt = (method.get("file_format") or "").lower()
    covers = set(method.get("covers") or [])
    report = method.get("report_name") or ""
    if fmt not in _TEXT_FORMATS:
        return False
    if re.search(r"position|holding|portfolio|portefeuille|valuation", report, re.I):
        return True
    return bool(covers & {"positions", "holdings"}) and "transactions" not in covers


def link(institution: dict, method: dict) -> dict:
    """How the app handles one catalog method:
    {support, connector?, upload_kind?}. support is one of
      live    -- a live connector exists (form with fields + Connect)
      import  -- an existing importer reads the file (/api/upload/{kind})
      csv     -- generic CSV positions import
      manual  -- enter a balance by hand (cash form)
      info    -- shown for information only (unofficial routes, aggregators
                 not built yet, files that can't be read automatically)"""
    mtype = method.get("type") or ""
    rule = _rule(institution, method)
    if mtype in ("api", "aggregator"):
        cid = method.get("connector") or rule.get("connector") \
            or (method.get("provider") if mtype == "aggregator" else None)
        if cid in CONNECTORS:
            return {"support": "live", "connector": cid}
        return {"support": "info"}
    if mtype == "upload":
        importer = method.get("importer") or rule.get("importer")
        if importer in UPLOAD_KINDS:
            return {"support": "import", "upload_kind": importer,
                    "accept": sorted(UPLOAD_KINDS[importer])}
        if importer == GENERIC_CSV or is_positions_file(method):
            return {"support": "csv", "connector": GENERIC_CSV}
        return {"support": "info"}
    if mtype == "manual":
        return {"support": "manual"}
    return {"support": "info"}
