"""Bitvavo -- live, read-only crypto balances with a "View" API key.

Make the key on the Bitvavo website: Settings -> API -> Add new API key, tick
only "View" access. Private calls are signed:

    Bitvavo-Access-Key        the API key
    Bitvavo-Access-Timestamp  Unix time in milliseconds
    Bitvavo-Access-Signature  hex HMAC-SHA256(secret, timestamp + method + path + body),
                              path including /v2, body "" for GET
    Bitvavo-Access-Window     optional, ms (default 10000)

    GET /v2/balance       -> [{symbol, available, inOrder}]   (private, View)
    GET /v2/ticker/price  -> [{market: "BTC-EUR", price}]     (public)

Docs: https://docs.bitvavo.com/docs/rest-api/introduction/ and the OpenAPI spec
https://docs.bitvavo.com/api-specs/exchange-rest-api.yaml

Interest: the spec (exchange-rest-api.yaml v2.10) has no interest rate and no
interest on euros (only staking rewards and margin-loan interest charged), so
the rate is typed in.

EUR is shown as cash; every coin as a holding valued at Bitvavo's last EUR
price. The balance call has no purchase cost (trade history needs the Trade
permission, which a read-only key must not have), so cost is set to today's
value: the dashboard shows the value, not a made-up profit.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from .base import Holding
from .live import ConnectorError, Snapshot, get_json, num

NAME = "Bitvavo"
BASE_URL = os.getenv("BITVAVO_BASE_URL", "https://api.bitvavo.com")
INTEREST_NOTE = ("Bitvavo's API reports no interest on euros, so the rate cannot be read "
                 "automatically. Enter it yourself (0 if this cash earns none).")
_HINTS = {403: "Make a key with View access only, and check its IP whitelist includes this server."}


def _signed_get(key: str, secret: str, path: str):
    ts = str(int(time.time() * 1000))
    sig = hmac.new(secret.encode(), f"{ts}GET{path}".encode(), hashlib.sha256).hexdigest()
    headers = {"Bitvavo-Access-Key": key, "Bitvavo-Access-Timestamp": ts,
               "Bitvavo-Access-Signature": sig, "Bitvavo-Access-Window": "10000",
               "Accept": "application/json"}
    return get_json(NAME, BASE_URL + path, headers=headers, hints=_HINTS)


def fetch(creds: dict, conn: dict | None = None) -> Snapshot:
    key = (creds.get("api_key") or "").strip()
    secret = (creds.get("api_secret") or "").strip()
    if not key or not secret:
        raise ConnectorError("Fill in both the API key and the API secret.")
    balances = _signed_get(key, secret, "/v2/balance")
    if not isinstance(balances, list):
        raise ConnectorError("Bitvavo sent an answer this app could not read.")
    prices = {p.get("market"): num(p.get("price"))
              for p in (get_json(NAME, BASE_URL + "/v2/ticker/price") or []) if isinstance(p, dict)}
    cash, holdings = [], []
    for b in balances:
        sym = (b.get("symbol") or "").upper()
        qty = num(b.get("available")) + num(b.get("inOrder"))
        if not sym or qty <= 0:
            continue
        if sym == "EUR":
            cash.append({"name": "cash EUR", "currency": "EUR", "balance": round(qty, 2)})
            continue
        price = prices.get(f"{sym}-EUR")
        holdings.append(Holding(
            broker="", ticker=f"{sym}-EUR", name=sym, quantity=qty,
            buy_price=price or 0.0, currency="EUR", price=price, buy_currency="EUR"))
    n = len(holdings)
    return Snapshot(summary=f"{n} coin{'s' if n != 1 else ''}", cash=cash, holdings=holdings)
