"""Starling Bank -- live, read-only balances with a personal access token,
and the interest each account earns.

Starling lets customers make a personal access token for their own account in
the developer portal (developer.starlingbank.com -> Personal Access). Give it
the `account:read` and `balance:read` scopes, and `transaction:read` to see the
interest: all three only read, none can move money.

    GET /api/v2/accounts                      -> accounts[]: accountUid, accountType,
        defaultCategory, currency, createdAt, name
    GET /api/v2/accounts/{accountUid}/balance -> totalEffectiveBalance /
        totalClearedBalance / effectiveBalance / clearedBalance: {currency, minorUnits}

Endpoints as in Starling's API reference (developer.starlingbank.com/docs ->
the OpenAPI spec it loads, /api/openapi.json, read Nov 2026) and its SDK
(github.com/starlingbank/starling-developer-sdk, src/entities/account.js;
production host https://api.starlingbank.com). Amounts are in minor units
(pence/cents). "total" balances include money set aside in Spaces ("totalled
across the main account and all spaces, savings goals etc."), so they are
preferred.

Interest. No Starling endpoint states an interest rate (the only "rate" in
the spec is a feed item's exchangeRate). Interest paid shows as a feed item:

    GET /api/v2/feed/account/{accountUid}/settled-transactions-between
        ?minTransactionTimestamp=2026-07-25T00:00:00.000Z&maxTransactionTimestamp=...
        scope transaction:read; at most one year; -> {feedItems: [FeedItem]}
    FeedItem: feedItemUid, categoryUid, amount {currency, minorUnits},
        direction IN | OUT, status (SETTLED, ...), transactionTime,
        settlementTime, source, spendingCategory, counterPartyType,
        counterPartyUid, ...
    interest paid in: direction IN and source INTEREST_PAYMENT or
        INTEREST_V2_PAYMENT (spendingCategory INTEREST_GAINED)

The rate is then implied from those payments and the balance history rebuilt
from today's cleared balance and the settled feed (backend/interest.py). A move
between the account and its own Spaces (counterPartyType CATEGORY, the other
side one of this account's categories) does not change the total balance, so
it is left out; a move to the token's other account counts. Starling's Easy
Saver terms (May 2026): "We pay interest into your Easy Saver on the first day
of every month" and "we won't take off any tax you might owe", so the figure
is before tax. The personal current account pays none, which shows as 0%.

A token without transaction:read gets HTTP 403 on the feed: the balances keep
working and the account says which permission to add. Personal tokens may
make 5 requests a second and 1000 a day; the interest read is 1 + 2 per
account every 6 hours.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from .live import ConnectorError, Snapshot, get_json, num

NAME = "Starling"
BASE_URL = os.getenv("STARLING_BASE_URL", "https://api.starlingbank.com")
_HINTS = {403: "Make a new token with account:read and balance:read ticked "
               "(and transaction:read, to see the interest)."}

INTEREST_SOURCES = {"INTEREST_PAYMENT", "INTEREST_V2_PAYMENT"}
_COUNTED = {"SETTLED", "REFUNDED"}
PERMISSION = {
    "status": "permission",
    "text": "To read the interest automatically, make a new Starling token with transaction:read "
            "ticked as well.",
    "steps": [
        "Sign in at developer.starlingbank.com and open Personal Access.",
        "Click Create Token and tick account:read, balance:read and transaction:read. All three "
        "only let this app read; none can move money.",
        "Copy the new token.",
        "Here in Settings, under Connected accounts, remove Starling (✕). Then add it again under "
        "Add an account → Starling with the new token. Rates and kinds you set for its accounts "
        "are kept.",
        "Back in the Starling developer portal, delete the old token.",
    ],
}


def _get(token: str, path: str, hints: dict | None = None):
    return get_json(NAME, BASE_URL + path, hints=_HINTS if hints is None else hints,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})


def _major(amount: dict | None, fallback_ccy: str) -> tuple[float, str] | None:
    if not isinstance(amount, dict) or amount.get("minorUnits") is None:
        return None
    return num(amount["minorUnits"]) / 100.0, amount.get("currency") or fallback_ccy


def _balance(bal: dict, ccy: str, keys: tuple[str, ...]) -> tuple[float, str] | None:
    for key in keys:
        amount = _major(bal.get(key), ccy)
        if amount is not None:
            return amount
    return None


def _token(creds: dict) -> str:
    token = (creds.get("token") or "").strip()
    if not token:
        raise ConnectorError("Paste your Starling personal access token.")
    return token


def _accounts(token: str) -> list[dict]:
    accounts = (_get(token, "/api/v2/accounts") or {}).get("accounts") or []
    if not accounts:
        raise ConnectorError("Starling returned no accounts for this token.")
    return [a for a in accounts if isinstance(a, dict) and a.get("accountUid")]


def fetch(creds: dict, conn: dict | None = None) -> Snapshot:
    token = _token(creds)
    cash = []
    for a in _accounts(token):
        uid = a["accountUid"]
        bal = _get(token, f"/api/v2/accounts/{uid}/balance") or {}
        amount = _balance(bal, a.get("currency") or "GBP",
                          ("totalEffectiveBalance", "effectiveBalance", "totalClearedBalance",
                           "clearedBalance"))
        if amount is None:
            continue
        name = a.get("name") or (a.get("accountType") or "Account").title()
        cash.append({"name": name, "currency": amount[1], "balance": round(amount[0], 2),
                     "ref": uid})
    n = len(cash)
    return Snapshot(summary=f"{n} account{'s' if n != 1 else ''}", cash=cash)


# ---------------------------------------------------------------- interest
def _stamp(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def is_interest(item: dict) -> bool:
    """Interest paid into the account (not overdraft or loan interest)."""
    return item.get("direction") == "IN" and (
        item.get("source") in INTEREST_SOURCES or item.get("spendingCategory") == "INTEREST_GAINED")


def interest(creds: dict, conn: dict | None = None, now: datetime | None = None) -> dict:
    """registry.Spec.interest: {"accounts": {accountUid: result}}, or the
    missing permission for all of them (backend/interest.py)."""
    from .. import interest as rates

    token = _token(creds)
    now = now or datetime.now(timezone.utc)
    today = now.date()
    since = today - timedelta(days=rates.LOOKBACK_DAYS)
    accounts = _accounts(token)
    feeds: dict[str, list[dict]] = {}
    for a in accounts:
        uid = a["accountUid"]
        path = (f"/api/v2/feed/account/{uid}/settled-transactions-between"
                f"?minTransactionTimestamp={since.isoformat()}T00:00:00.000Z"
                f"&maxTransactionTimestamp={_stamp(now)}")
        try:
            data = _get(token, path, hints={}) or {}
        except ConnectorError as e:
            if e.status == 403:
                return dict(PERMISSION)        # the token has no transaction:read
            raise
        feeds[uid] = [i for i in data.get("feedItems") or [] if isinstance(i, dict)]
    # categories (main + Spaces) of each account, to tell its own Space moves apart
    own = {a["accountUid"]: {a.get("defaultCategory")} | {i.get("categoryUid") for i in feeds[a["accountUid"]]}
           for a in accounts}
    out = {}
    for a in accounts:
        uid, ccy = a["accountUid"], a.get("currency") or "GBP"
        others = set().union(*(c for u, c in own.items() if u != uid)) - {None}
        bal = _get(token, f"/api/v2/accounts/{uid}/balance") or {}
        amount = _balance(bal, ccy, ("totalClearedBalance", "clearedBalance",
                                     "totalEffectiveBalance", "effectiveBalance"))
        if amount is None:
            continue
        credits, flows = [], []
        for item in feeds[uid]:
            if item.get("status") not in _COUNTED:
                continue
            value = _major(item.get("amount"), ccy)
            when = str(item.get("settlementTime") or item.get("transactionTime") or "")[:10]
            if value is None or len(when) != 10:
                continue
            signed = value[0] if item.get("direction") == "IN" else -value[0]
            if item.get("counterPartyType") == "CATEGORY" and item.get("counterPartyUid") not in others:
                continue                        # to or from one of its own Spaces
            (credits if is_interest(item) else flows).append((when, signed))
        out[uid] = rates.implied(credits, flows, amount[0], today, currency=amount[1],
                                 basis="gross", since=since)
    return {"accounts": out}
