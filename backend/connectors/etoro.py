"""eToro -- live, read-only positions and cash with a "Read" API key pair.

Make the key on the eToro website: Settings -> Trading -> API Key Management
-> Create New Key, environment "Real", permission "Read". Every request sends
both keys plus a fresh UUID:

    x-api-key: <public API key>   x-user-key: <user key>   x-request-id: <uuid4>

    GET /api/v1/trading/info/portfolio -> {clientPortfolio: {positions[],
        mirrors[] (copy trading, each with positions[]), credit (USD cash)}}
        position: instrumentID, isBuy, leverage, units,
                  initialAmountInDollars, unitsBaseValueDollars (current value)
    GET /api/v1/market-data/instruments?instrumentIds=1,2 -> {instrumentDisplayDatas:
        [{instrumentID, instrumentDisplayName, symbolFull}]}

Docs: https://api-portal.etoro.com/api-reference/trading--real/get-portfolio-breakdown
      https://api-portal.etoro.com/api-reference/market-data/get-instrument-display-data
      https://api-portal.etoro.com/core/getting-started/authentication

Interest: the API reference (api-portal.etoro.com, openapi.json v1.427, Nov
2026) has no interest rate and no interest transaction type (cash
transactions are card, internalTransfer, bankTransfer, balanceAdjustment), so
the rate is typed in.

Only plain long positions (no leverage, not short) are counted as holdings:
for CFDs the units times price is exposure, not money you own. Values are in
USD as eToro reports them; prices come from eToro itself, so no ticker
mapping is needed (eToro does not give ISINs).
"""

from __future__ import annotations

import os
import uuid

from .base import Holding
from .live import ConnectorError, Snapshot, get_json, num

NAME = "eToro"
BASE_URL = os.getenv("ETORO_BASE_URL", "https://public-api.etoro.com")
INTEREST_NOTE = ("eToro's API gives no interest rate and does not mark interest payments, so the "
                 "rate cannot be read automatically. Enter it yourself (0 if this cash earns none).")
_HINTS = {401: "Check you copied both the public API key and the user key",
          403: "Use a key for the Real environment with Read permission."}

_META: dict[int, dict] = {}  # instrumentID -> display data (static, cached)


def _get(creds: dict, path: str, params: dict | None = None):
    headers = {"x-api-key": creds.get("api_key", ""), "x-user-key": creds.get("user_key", ""),
               "x-request-id": str(uuid.uuid4()), "Accept": "application/json"}
    return get_json(NAME, BASE_URL + path, headers=headers, params=params, hints=_HINTS)


def _instruments(creds: dict, ids: list[int]) -> None:
    missing = [i for i in ids if i not in _META]
    for start in range(0, len(missing), 50):
        chunk = missing[start:start + 50]
        data = _get(creds, "/api/v1/market-data/instruments",
                    {"instrumentIds": ",".join(str(i) for i in chunk)}) or {}
        for d in data.get("instrumentDisplayDatas") or []:
            if d.get("instrumentID") is not None:
                _META[int(d["instrumentID"])] = d


def fetch(creds: dict, conn: dict | None = None) -> Snapshot:
    if not (creds.get("api_key") or "").strip() or not (creds.get("user_key") or "").strip():
        raise ConnectorError("Fill in both the public API key and the user key.")
    creds = {k: (v or "").strip() for k, v in creds.items()}
    portfolio = (_get(creds, "/api/v1/trading/info/portfolio") or {}).get("clientPortfolio")
    if not isinstance(portfolio, dict):
        raise ConnectorError("eToro sent an answer this app could not read.")
    positions = list(portfolio.get("positions") or [])
    for mirror in portfolio.get("mirrors") or []:
        positions += mirror.get("positions") or []

    groups: dict[int, dict] = {}
    skipped = 0
    for p in positions:
        units = num(p.get("units"))
        if units <= 0:
            continue
        if p.get("isBuy") is False or num(p.get("leverage"), 1.0) != 1.0:
            skipped += 1
            continue
        g = groups.setdefault(int(p.get("instrumentID") or 0), {"units": 0.0, "cost": 0.0, "value": 0.0})
        g["units"] += units
        g["cost"] += num(p.get("initialAmountInDollars"), num(p.get("amount")))
        g["value"] += num(p.get("unitsBaseValueDollars"))
    try:
        _instruments(creds, [i for i in groups if i])
    except ConnectorError:
        pass  # names are a nicety; values are already known

    holdings = []
    for iid, g in groups.items():
        meta = _META.get(iid, {})
        name = meta.get("instrumentDisplayName") or meta.get("symbolFull") or f"eToro instrument {iid}"
        holdings.append(Holding(
            broker="", ticker="", name=name, quantity=round(g["units"], 8),
            buy_price=round(g["cost"] / g["units"], 6), currency="USD",
            price=round(g["value"] / g["units"], 6) if g["value"] else None, buy_currency="USD"))
    cash = [{"name": "cash USD", "currency": "USD", "balance": round(num(portfolio.get("credit")), 2)}]
    # Money set aside for copy trading but not invested: each mirror's
    # availableAmount ("reserved for mirror operations"), which `credit`
    # ("funds available for new positions") does not include.
    copy_cash = sum(num(m.get("availableAmount")) for m in portfolio.get("mirrors") or [])
    if copy_cash:
        cash.append({"name": "copy trading cash USD", "currency": "USD", "balance": round(copy_cash, 2)})
    summary = f"{len(holdings)} positions"
    if skipped:
        summary += f" ({skipped} leveraged or short left out)"
    return Snapshot(summary=summary, cash=cash, holdings=holdings)
