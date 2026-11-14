"""Wise -- live, read-only balances per currency with a personal API token.

Make the token on wise.com: Settings -> Integrations and tools -> API tokens ->
Add new token -> Read only. Sent as `Authorization: Bearer <token>`.

    GET /{VERSION}/profiles                              -> [{id, type}]
    GET /{VERSION}/profiles/{id}/balances?types=STANDARD,SAVINGS
        -> [{currency, type, name, amount, totalWorth, visible}]

Wise uses quarterly, date-based API versions in the path (2026Q4 at the time of
writing); the older /v2/profiles and /v4/.../balances paths "are still
supported", so they are the fallback if a dated path is ever retired. The
balance list accepts personal tokens; the PSD2 limits on personal tokens (no
balance *statements* for UK/EU profiles) do not apply to it.

Docs: https://docs.wise.com/api-reference/balance/balancelist and
https://docs.wise.com/guides/developer/auth-and-security/personal-api-token
"""

from __future__ import annotations

import os

from .live import ConnectorError, Snapshot, get_json, num

NAME = "Wise"
BASE_URL = os.getenv("WISE_BASE_URL", "https://api.wise.com")
VERSION = os.getenv("WISE_API_VERSION", "2026Q4")
_HINTS = {403: "Wise limits some data for personal tokens. If this keeps happening, "
               "enter the balance by hand instead."}


def _get(token: str, path: str):
    return get_json(NAME, BASE_URL + path, hints=_HINTS,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})


def _get_first(token: str, paths: list[str]):
    """First path that exists (dated version first, legacy path on 404)."""
    for i, path in enumerate(paths):
        try:
            return _get(token, path)
        except ConnectorError as e:
            if e.status != 404 or i == len(paths) - 1:
                raise


def fetch(creds: dict, conn: dict | None = None) -> Snapshot:
    token = (creds.get("token") or "").strip()
    if not token:
        raise ConnectorError("Paste your Wise API token.")
    profiles = _get_first(token, [f"/{VERSION}/profiles", "/v2/profiles"]) or []
    if not profiles:
        raise ConnectorError("Wise returned no profiles for this token.")
    several = len(profiles) > 1
    cash = []
    for p in profiles:
        pid = p.get("id")
        if pid is None:
            continue
        query = "?types=STANDARD,SAVINGS"
        balances = _get_first(token, [f"/{VERSION}/profiles/{pid}/balances{query}",
                                      f"/v4/profiles/{pid}/balances{query}"]) or []
        prefix = (str(p.get("type") or "").title() + " ") if several else ""
        for b in balances:
            if b.get("visible") is False:
                continue
            worth = b.get("totalWorth") or b.get("amount") or {}
            ccy = worth.get("currency") or b.get("currency") or ""
            if not ccy:
                continue
            savings = b.get("type") == "SAVINGS"
            name = f"{b.get('name') or 'Jar'} ({ccy})" if savings else ccy
            cash.append({"name": prefix + name, "currency": ccy,
                         "balance": round(num(worth.get("value")), 2),
                         "type": "savings" if savings else "cash"})
    n = len(cash)
    return Snapshot(summary=f"{n} balance{'s' if n != 1 else ''}", cash=cash)
