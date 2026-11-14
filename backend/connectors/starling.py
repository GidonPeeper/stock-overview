"""Starling Bank -- live, read-only balances with a personal access token.

Starling lets customers make a personal access token for their own account in
the developer portal (developer.starlingbank.com -> Personal Access). Give it
only the `account:read` and `balance:read` scopes.

    GET /api/v2/accounts                      -> accounts[]: accountUid, name, currency
    GET /api/v2/accounts/{accountUid}/balance -> totalEffectiveBalance /
        effectiveBalance / clearedBalance: {currency, minorUnits}

Endpoints as used by Starling's own SDK (github.com/starlingbank/
starling-developer-sdk, src/entities/account.js; production host
https://api.starlingbank.com). Amounts are in minor units (pence/cents).
"total" balances include money set aside in Spaces, so they are preferred.
"""

from __future__ import annotations

import os

from .live import ConnectorError, Snapshot, get_json, num

NAME = "Starling"
BASE_URL = os.getenv("STARLING_BASE_URL", "https://api.starlingbank.com")
_HINTS = {403: "Make a new token with account:read and balance:read ticked."}


def _get(token: str, path: str):
    return get_json(NAME, BASE_URL + path, hints=_HINTS,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})


def _major(amount: dict | None, fallback_ccy: str) -> tuple[float, str] | None:
    if not isinstance(amount, dict) or amount.get("minorUnits") is None:
        return None
    return num(amount["minorUnits"]) / 100.0, amount.get("currency") or fallback_ccy


def fetch(creds: dict, conn: dict | None = None) -> Snapshot:
    token = (creds.get("token") or "").strip()
    if not token:
        raise ConnectorError("Paste your Starling personal access token.")
    accounts = (_get(token, "/api/v2/accounts") or {}).get("accounts") or []
    if not accounts:
        raise ConnectorError("Starling returned no accounts for this token.")
    cash = []
    for a in accounts:
        uid = a.get("accountUid")
        if not uid:
            continue
        bal = _get(token, f"/api/v2/accounts/{uid}/balance") or {}
        ccy = a.get("currency") or "GBP"
        amount = None
        for key in ("totalEffectiveBalance", "effectiveBalance", "totalClearedBalance", "clearedBalance"):
            amount = _major(bal.get(key), ccy)
            if amount is not None:
                break
        if amount is None:
            continue
        name = a.get("name") or (a.get("accountType") or "Account").title()
        cash.append({"name": name, "currency": amount[1], "balance": round(amount[0], 2)})
    n = len(cash)
    return Snapshot(summary=f"{n} account{'s' if n != 1 else ''}", cash=cash)
