"""Trade Republic live sign-in: login, session, websocket data, history
mapping, and counting the account once next to an uploaded export.

No network: a fake Trade Republic answers the HTTP sign-in and the websocket
with messages shaped like the real ones (shapes from pytr's source and test
fixtures, github.com/pytr-org/pytr). All data is made up.
Run: .venv/bin/python -m unittest discover -s tests -t .
"""

import asyncio
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
from fastapi import HTTPException

from backend import cache, connections, connections_api, fx, income, sources, traderepublic_api
from backend.connectors import registry, traderepublic, traderepublic_live as trl
from backend.connectors.live import ConnectorError
from backend.positions import compute_positions

PHONE = "+49 170 0000001"
PIN = "9Z3Z"            # made-up; letters so it cannot hide inside other numbers
SEC = "0000000001"
AAPL, SP500, BTC = "US0378331005", "IE00B5BMR087", "XF000BTC0017"


def resp(status=200, body=None, headers=None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body if body is not None else {}).encode()
    r.headers.update(headers or {})
    return r


def err(code, status=400):
    return resp(status, {"errors": [{"errorCode": code, "errorMessage": None, "meta": None}]})


# ------------------------------------------------------------ timeline data
def item(iid, ts, title, subtitle, value, event_type, isin="", status="EXECUTED", detail=True):
    return {"id": iid, "timestamp": ts, "title": title, "subtitle": subtitle,
            "icon": f"logos/{isin}/v2" if isin else "logos/timeline_plus_circle/v2",
            "amount": None if value is None else {"currency": "EUR", "value": value, "fractionDigits": 2},
            "status": status, "eventType": event_type,
            "action": {"type": "timelineDetail", "payload": iid} if detail else None}


def header(isin, title="Du hast 1,00 € investiert"):
    return {"title": title, "type": "header", "data": {"icon": f"logos/{isin}/v2", "status": "executed"},
            "action": {"type": "instrumentDetail", "payload": isin}}


def row(title, text, prefix="", nested=None):
    det = {"text": text, "type": "text", "displayValue": {"text": text, "prefix": prefix}}
    if nested:
        det["action"] = {"type": "infoPage", "payload": {"sections": [
            {"title": "Transaktion", "type": "title"},
            {"type": "table", "data": [row(t, x) for t, x in nested]}]}}
    return {"title": title, "detail": det, "style": "plain"}


def overview(*rows):
    return {"title": "Übersicht", "type": "table", "data": list(rows)}


def trade_detail(isin, shares, price, subtotal, fee, total, tax=None):
    rows = [row("Portfolio", "Brokerage"), row("Asset", "Apple"),
            row("Transaktion", f"{shares} ×  {price}", f"{shares} × ",
                [("Aktien", shares), ("Aktienkurs", price), ("Summe", subtotal)])]
    if tax is not None:
        rows.append(row("Steuer", tax))
    rows += [row("Gebühr", fee), row("Summe", total)]
    return {"sections": [header(isin), overview(*rows),
                         {"title": "Dokumente", "type": "documents", "data": []}]}


TX_PAGE_1 = [
    item("t-saveback", "2025-06-10T13:54:23.344+0000", "Core S&P 500 USD (Acc)", "Saveback", -15.0,
         "benefits_saveback_execution", SP500),
    item("t-pending", "2025-05-02T01:05:56.903+0000", "Apple", "Sparplan ausstehend", -50.0,
         "TRADING_SAVINGSPLAN_EXECUTION_PENDING", AAPL),
    item("t-vorab", "2025-05-01T14:54:28.155+0000", "Core S&P 500 USD (Acc)", "Vorabpauschale", -0.19,
         "ssp_corporate_action_invoice_cash", SP500),
    item("t-card", "2025-04-03T10:00:00.000+0000", "Bakery", None, -4.5, "CARD_SUCCESSFUL_TRANSACTION"),
    item("t-deposit", "2025-04-02T17:07:26.374+0000", "Jane Doe", None, 200.0, "BANK_TRANSACTION_INCOMING"),
]
TX_PAGE_2 = [
    item("t-interest", "2025-04-01T06:46:37.846+0000", "Zinsen", "2 % p.a.", 4.87, "INTEREST_PAYOUT", detail=False),
    item("t-div", "2025-03-15T14:19:56.295+0000", "Apple", "Bardividende", 0.75,
         "ssp_corporate_action_invoice_cash", AAPL),
    item("t-sell", "2025-03-05T18:38:36.065+0000", "Apple", "Verkaufsorder", 179.20, "trading_trade_executed", AAPL),
    item("t-plan", "2025-02-03T08:26:13.657+0000", "Apple", "Sparplan ausgeführt", -50.0,
         "TRADING_SAVINGSPLAN_EXECUTED", AAPL),
    item("t-buy", "2025-01-10T19:29:43.928+0000", "Apple", "Kauforder", -301.0, "trading_trade_executed", AAPL),
    item("t-cancelled", "2025-01-09T10:00:00.000+0000", "Apple", "Kauforder", -99.0, "trading_trade_executed",
         AAPL, status="CANCELED"),
]
ACTIVITY = [
    item("a-split", "2025-07-01T08:06:40.347+0000", "Apple", "Aktiensplit", None,
         "ssp_corporate_action_invoice_shares", AAPL),
    item("a-docs", "2025-06-30T08:00:00.000+0000", "Kosteninformation", None, None, "EX_POST_COST_REPORT"),
]
DETAILS = {
    "t-buy": trade_detail(AAPL, "2", "150,00 €", "300,00 €", "1,00 €", "301,00 €"),
    "t-plan": trade_detail(AAPL, "0,333333", "150,00 €", "50,00 €", "Kostenlos", "50,00 €"),
    "t-sell": trade_detail(AAPL, "1", "180,50 €", "180,50 €", "1,00 €", "179,20 €", tax="0,30 €"),
    "t-saveback": {"sections": [header(SP500, "Du hast 15,00 € erhalten"), overview(
        row("Saveback", "Abgeschlossen"), row("Asset", "Core S&P 500 USD (Acc)"),
        row("Transaktion", "", "0.025000 x"), row("Gebühr", "Kostenlos"), row("Gesamt", "15,00 €"))]},
    "a-split": {"sections": [header(AAPL, "Du hast Aktien aus einer Kapitalmaßnahme erhalten"), overview(
        row("Status", "Ausgeführt"), row("Orderart", "Aktiensplit"), row("Wertpapier", "Apple"),
        row("Aktien hinzugefügt", "1.333333"))]},
}
NAMES = {AAPL: "Apple", SP500: "Core S&P 500 USD (Acc)", BTC: "Bitcoin"}


# ------------------------------------------------------------ fake service
class FakeTR:
    """Trade Republic as far as this app talks to it."""

    def __init__(self):
        self.calls = []                 # HTTP: (method, path, json, headers, cookies)
        self.ws_sent = []               # websocket messages from the app
        self.ws_cookies = []
        self.process = {"status": "PENDING"}
        self.refresh_ok = True
        self.session_n = 0
        self.v2_error = None            # e.g. err("MOBILE_APP_VERSION_NOT_SUPPORTED", 422)
        self.tx_pages = [list(TX_PAGE_1), list(TX_PAGE_2)]
        self.activity = list(ACTIVITY)
        self.details = dict(DETAILS)
        self.positions = [{"isin": AAPL, "netSize": "2.666666", "averageBuyIn": "131.6250"},
                          {"isin": SP500, "netSize": "0.025", "averageBuyIn": "600.00"}]
        self.crypto = [{"isin": BTC, "netSize": "0.01", "averageBuyIn": "40000"}]
        self.ws_fail = None

    # HTTP --------------------------------------------------------------
    def session(self):
        return FakeHTTP(self)

    def handle(self, http, method, path, kw):
        headers = kw.get("headers") or {}
        self.calls.append((method, path, kw.get("json"), dict(headers),
                           {c.name: c.value for c in http.cookies}))
        login_call = "/auth/web/login" in path
        if login_call and not all(h in headers for h in ("X-TR-Device-Info", "X-TR-App-Version",
                                                           "X-Tr-Platform", "Accept-Language")):
            return err("MISSING_REQUIRED_HEADER")
        if method == "POST" and path == "/api/v2/auth/web/login":
            if self.v2_error is not None:
                return self.v2_error
            body = kw.get("json") or {}
            if body.get("phoneNumber") != "+491700000001" or body.get("pin") != PIN:
                return resp(401, {})
            return resp(200, {"processId": "proc-1", "countdownInSeconds": 120})
        if method == "GET" and path == "/api/v2/auth/web/login/processes/proc-1":
            if self.process["status"] in ("CONFIRMED", "COMPLETED"):
                self._new_session(http)
            return resp(200, {**self.process, "expiresAt": "2999-01-01T00:00:00Z"})
        if method == "POST" and path.endswith("/authenticator-verification"):
            if (kw.get("json") or {}).get("code") != "123456":
                return err("VALIDATION_CODE_INVALID")
            return resp(200, {})
        if method == "POST" and path == "/api/v1/auth/web/login":
            if "aws-waf-token" not in {c.name for c in http.cookies}:
                return resp(405, None, {"server": "awselb/2.0"})
            return resp(200, {"processId": "proc-v1", "countdownInSeconds": 30})
        if method == "POST" and path == "/api/v1/auth/web/login/proc-v1/4711":
            self._new_session(http)
            return resp(200, {})
        if method == "POST" and path.startswith("/api/v1/auth/web/login/proc-v1/"):
            return err("VALIDATION_CODE_INVALID")
        if method == "GET" and path == "/api/v1/auth/web/session":
            jar = {c.name: c.value for c in http.cookies}
            if not self.refresh_ok or jar.get("tr_refresh") != "refresh-1":
                return resp(401, {})
            self._new_session(http)
            return resp(200, {})
        if method == "GET" and path == "/api/v2/auth/account":
            if not any(c.name == "tr_session" for c in http.cookies):
                return resp(401, {})
            return resp(200, {"securitiesAccountNumber": SEC})
        return resp(404, {})

    def _new_session(self, http):
        self.session_n += 1
        http.cookies.set("tr_session", f"session-{self.session_n}", domain="api.traderepublic.com", path="/")
        http.cookies.set("tr_refresh", "refresh-1", domain="api.traderepublic.com", path="/")

    # websocket ---------------------------------------------------------
    async def connect(self, cookies):
        if self.ws_fail:
            raise self.ws_fail
        self.ws_cookies.append(trl.cookie_header(cookies))
        return FakeWS(self)

    def answer(self, payload):
        kind = payload.get("type")
        if kind == "compactPortfolioByType":
            if payload.get("secAccNo") != SEC:
                return "E", {"errors": [{"errorCode": "BAD_REQUEST"}]}
            return "A", {"categories": [{"categoryType": "stocksAndETFs", "positions": self.positions},
                                        {"categoryType": "cryptos", "positions": self.crypto}]}
        if kind == "cash":
            return "A", [{"accountNumber": "x", "currencyId": "EUR", "amount": 1234.56}]
        if kind == "instrument":
            isin = payload["id"]
            return "A", {"shortName": NAMES.get(isin, isin), "typeId": "crypto" if isin == BTC else "stock",
                         "exchangeIds": ["BHS" if isin == BTC else "LSX"]}
        if kind == "ticker":
            return ("A", {"last": {"price": "61000.5"}}) if payload["id"] == f"{BTC}.BHS" else \
                ("E", {"errors": [{"errorCode": "NOT_FOUND"}]})
        if kind == "timelineTransactions":
            n = {None: 0, "c2": 1}.get(payload.get("after"), 9)
            pages = self.tx_pages
            if n >= len(pages):
                return "A", {"items": [], "cursors": {}}
            return "A", {"items": pages[n], "cursors": {"after": "c2"} if n + 1 < len(pages) else {}}
        if kind == "timelineActivityLog":
            return "A", {"items": self.activity, "cursors": {}}
        if kind == "timelineDetailV2":
            d = self.details.get(payload["id"])
            return ("A", {"id": payload["id"], **d}) if d else ("E", {"errors": [{"errorCode": "NOT_FOUND"}]})
        return "E", {"errors": [{"errorCode": "UNKNOWN_TYPE"}]}

    def subs(self, kind):
        return [json.loads(m.split(" ", 2)[2]) for m in self.ws_sent
                if m.startswith("sub ") and json.loads(m.split(" ", 2)[2]).get("type") == kind]


class FakeHTTP:
    def __init__(self, server):
        self.server = server
        self.cookies = requests.cookies.RequestsCookieJar()
        self.headers = {}

    def get(self, url, **kw):
        return self.server.handle(self, "GET", url.replace(trl.API, ""), kw)

    def post(self, url, **kw):
        return self.server.handle(self, "POST", url.replace(trl.API, ""), kw)


class FakeWS:
    def __init__(self, server):
        self.server = server
        self.queue = asyncio.Queue()

    async def send(self, text):
        self.server.ws_sent.append(text)
        if text.startswith("connect "):
            await self.queue.put("connected")
        elif text.startswith("sub "):
            _, sid, payload = text.split(" ", 2)
            code, body = self.server.answer(json.loads(payload))
            await self.queue.put(f"{sid} {code} {json.dumps(body)}")

    async def recv(self):
        return await self.queue.get()

    async def close(self):
        pass


class TRCase(unittest.TestCase):
    """Own data dir, fake Trade Republic, no background threads left running."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.tr = FakeTR()
        env = {k: v for k, v in os.environ.items() if not k.startswith("TR_")}
        for p in (mock.patch.object(connections, "_file", lambda name: self.dir / name),
                  mock.patch.object(fx, "_live_rates", return_value={"EUR": 1.0, "GBP": 1.2, "USD": 0.9}),
                  mock.patch.object(connections_api.github_sync, "push_vault_async", lambda: None),
                  mock.patch.object(trl, "new_http", self.tr.session),
                  mock.patch.object(trl, "ws_connect", self.tr.connect),
                  mock.patch.object(trl, "sleep", lambda s: None),
                  mock.patch.object(traderepublic_api, "CONFIRM_WAIT", 0.0),
                  mock.patch.object(connections, "fetch_soon", lambda conn_id: None),
                  mock.patch.object(traderepublic, "_data_file", lambda: self.dir / "trades_trade_republic.json"),
                  mock.patch.object(traderepublic, "_income_file", lambda: self.dir / "income_trade_republic.json"),
                  mock.patch.dict(os.environ, env, clear=True)):
            p.start()
            self.addCleanup(p.stop)
        for d in (connections._cache, connections._failed, connections._inflight, connections._read_cache):
            d.clear()
        traderepublic_api._pending.clear()
        self.addCleanup(cache.clear)

    def sign_in(self, label=""):
        """The whole sign-in through the endpoints: phone + PIN, approve in the app."""
        start = traderepublic_api.start(phone=PHONE, pin=PIN, institution="traderepublic", method="0",
                                        label=label, renew="")
        self.tr.process["status"] = "CONFIRMED"
        out = traderepublic_api.confirm(start["login"], code="")
        self.assertEqual(out["status"], "done")
        return out["connection"]

    def sync(self, conn_id, manual=True):
        """A background sync as connections.py runs it, waited for."""
        conn = connections.get(conn_id)
        return connections._run({**conn, "_manual": manual})

    def stored(self, conn_id):
        return connections.get(conn_id)


# ------------------------------------------------------------------ login
class LoginTest(TRCase):
    def test_two_step_sign_in_approved_in_the_app(self):
        start = traderepublic_api.start(phone=PHONE, pin=PIN, institution="traderepublic", method="0",
                                        label="", renew="")
        self.assertEqual((start["step"], start["status"], start["phone"]), ("app", "started", "…01"))
        self.assertIn("confirm the sign-in", start["message"])
        waiting = traderepublic_api.confirm(start["login"], code="")       # not approved yet
        self.assertEqual(waiting["status"], "waiting")
        self.tr.process["status"] = "CONFIRMED"
        done = traderepublic_api.confirm(start["login"], code="")
        self.assertEqual(done["status"], "done")
        conn = done["connection"]
        self.assertEqual((conn["label"], conn["connector"], conn["status"]), ("Trade Republic", "traderepublic", "ok"))
        self.assertIn("3 positions", conn["summary"])
        self.assertIn("reading your history", conn["summary"])
        # the login headers went with every sign-in call; the session is renewed before reading
        paths = [c[1] for c in self.tr.calls]
        self.assertEqual(paths[:2], ["/api/v2/auth/web/login", "/api/v2/auth/web/login/processes/proc-1"])
        self.assertIn("/api/v1/auth/web/session", paths)
        with self.assertRaises(HTTPException) as cm:                       # single use
            traderepublic_api.confirm(start["login"], code="")
        self.assertEqual(cm.exception.status_code, 404)

    def test_pin_is_sent_once_and_never_stored_or_returned(self):
        start = traderepublic_api.start(phone=PHONE, pin=PIN, institution="traderepublic", method="0",
                                        label="", renew="")
        proc, info = traderepublic_api._pending[start["login"]]
        self.assertNotIn(PIN, repr(vars(proc)) + repr(info))              # not even in memory
        self.tr.process["status"] = "CONFIRMED"
        done = traderepublic_api.confirm(start["login"], code="")
        sent_pin = [c for c in self.tr.calls if c[2] and PIN in json.dumps(c[2])]
        self.assertEqual(len(sent_pin), 1)                                 # only the first request
        on_disk = "".join(p.read_text() for p in self.dir.iterdir() if p.is_file())
        self.assertNotIn(PIN, on_disk)
        self.assertNotIn(PIN, json.dumps(start) + json.dumps(done))
        # cookies are stored, but never returned to the browser
        stored = self.stored(done["connection"]["id"])
        cookie_values = [c["value"] for c in stored["credentials"]["cookies"]]
        self.assertIn("refresh-1", cookie_values)
        self.assertEqual(stored["credentials"]["sec_acc_no"], SEC)
        public = json.dumps(connections.public(stored))
        for secret in cookie_values + [SEC]:
            self.assertNotIn(secret, public)
        self.assertNotIn("history", connections.public(stored))
        self.assertNotIn("+491700000001", on_disk)                         # nor the phone number

    def test_authenticator_code_then_approval(self):
        self.tr.process = {"status": "PENDING", "requiredAction": "AUTHENTICATOR_VERIFICATION"}
        start = traderepublic_api.start(phone=PHONE, pin=PIN, institution="traderepublic", method="0",
                                        label="", renew="")
        self.assertEqual(start["step"], "authenticator")
        with self.assertRaises(HTTPException) as cm:
            traderepublic_api.confirm(start["login"], code="111111")
        self.assertIn("not right", cm.exception.detail)
        out = traderepublic_api.confirm(start["login"], code="123456")    # accepted; approve in the app
        self.assertEqual((out["status"], out["step"]), ("waiting", "app"))
        self.tr.process["status"] = "CONFIRMED"
        self.assertEqual(traderepublic_api.confirm(start["login"], code="")["status"], "done")

    def test_plain_errors(self):
        with self.assertRaises(HTTPException) as cm:
            traderepublic_api.start(phone=PHONE, pin="0000", institution="traderepublic", method="0",
                                    label="", renew="")
        self.assertIn("did not accept that phone number and PIN", cm.exception.detail)
        with self.assertRaises(HTTPException) as cm:
            traderepublic_api.start(phone="0170 123", pin=PIN, institution="traderepublic", method="0",
                                    label="", renew="")
        self.assertIn("country code", cm.exception.detail)
        self.tr.v2_error = err("TOO_MANY_REQUESTS", 429)
        with self.assertRaises(HTTPException) as cm:
            traderepublic_api.start(phone=PHONE, pin=PIN, institution="traderepublic", method="0",
                                    label="", renew="")
        self.assertIn("Too many sign-in attempts", cm.exception.detail)
        self.tr.v2_error = err("MOBILE_APP_VERSION_NOT_SUPPORTED", 422)
        with self.assertRaises(HTTPException) as cm:                       # no WAF token: no fallback
            traderepublic_api.start(phone=PHONE, pin=PIN, institution="traderepublic", method="0",
                                    label="", renew="")
        self.assertIn("MOBILE_APP_VERSION_NOT_SUPPORTED", cm.exception.detail)
        self.assertIn("TR_APP_VERSION", cm.exception.detail)
        # failed attempts leave nothing behind (no file, so demo mode stays on)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_code_login_fallback_needs_a_real_waf_token(self):
        with mock.patch.dict(os.environ, {"TR_LOGIN": "v1"}), \
                mock.patch.object(trl.importlib.util, "find_spec", return_value=None):
            with self.assertRaises(ConnectorError) as cm:                  # never faked
                trl.start_login(PHONE, PIN)
            self.assertIn("browser token", str(cm.exception))
        self.tr.v2_error = err("MOBILE_APP_VERSION_NOT_SUPPORTED", 422)
        with mock.patch.dict(os.environ, {"TR_WAF_TOKEN": "waf-token-from-a-browser"}):
            proc = trl.start_login(PHONE, PIN)                             # auto: v2 refused -> v1
            self.assertEqual((proc.mode, proc.step), ("v1", "code"))
            with self.assertRaises(ConnectorError):
                trl.confirm_login(proc, "12")
            self.assertTrue(trl.confirm_login(proc, "4711"))
            snap, creds, _ = trl.first_sync(proc)
        self.assertEqual(creds["login"], "v1")
        self.assertEqual(len(snap.holdings), 3)


# ---------------------------------------------------------------- session
class SessionTest(TRCase):
    def test_session_is_renewed_and_stored_each_sync(self):
        conn = self.sign_in()
        before = [c["value"] for c in self.stored(conn["id"])["credentials"]["cookies"]
                  if c["name"] == "tr_session"]
        self.sync(conn["id"])
        after = [c["value"] for c in self.stored(conn["id"])["credentials"]["cookies"]
                 if c["name"] == "tr_session"]
        self.assertNotEqual(before, after)
        self.assertIn(f"tr_session={after[0]}", self.tr.ws_cookies[-1])  # the websocket used the new one

    def test_expired_session_asks_to_sign_in_again_and_keeps_the_last_data(self):
        conn = self.sign_in()
        self.sync(conn["id"])                                              # history read
        self.tr.refresh_ok = False
        with self.assertRaises(ConnectorError):
            self.sync(conn["id"])
        stored = self.stored(conn["id"])
        self.assertEqual(stored["status"], "signin")
        self.assertIn("Sign in again", stored["error"])
        self.assertNotIn("401", stored["error"])
        self.assertEqual(connections.public(stored)["status"], "signin")
        # the dashboard keeps working from the last snapshot, also after a restart
        for clear in (False, True):
            if clear:
                connections._cache.clear()
            holdings = traderepublic.get_holdings()
            self.assertEqual({h.isin for h in holdings}, {AAPL, SP500, BTC})
            self.assertIn("Trade Republic", traderepublic.trade_sources())
            self.assertEqual(len(traderepublic.trade_sources()["Trade Republic"][1]()), 5)
            self.assertEqual([c["balance"] for c in connections.cash_entries()], [1234.56])
        # not retried every few minutes: only a new sign-in helps
        self.assertGreaterEqual(connections._failed[conn["id"]][1], 6 * 3600)

    def test_sign_in_again_renews_the_same_connection(self):
        conn = self.sign_in()
        self.sync(conn["id"])
        self.tr.refresh_ok = False
        with self.assertRaises(ConnectorError):
            self.sync(conn["id"])
        self.tr.refresh_ok = True
        self.tr.process["status"] = "PENDING"
        start = traderepublic_api.start(phone=PHONE, pin=PIN, institution="traderepublic", method="0",
                                        label="", renew=conn["id"])
        self.tr.process["status"] = "CONFIRMED"
        out = traderepublic_api.confirm(start["login"], code="")
        self.assertEqual(out["connection"]["id"], conn["id"])
        self.assertEqual(len(connections.load_all()), 1)
        self.assertEqual(self.stored(conn["id"])["status"], "ok")
        self.assertTrue(self.stored(conn["id"])["history"]["complete"])  # the history was kept


# ------------------------------------------------------------------- data
class DataTest(TRCase):
    def test_positions_and_cash(self):
        conn = self.sign_in()
        hs = {h.isin: h for h in traderepublic.get_holdings()}
        self.assertEqual(set(hs), {AAPL, SP500, BTC})
        a = hs[AAPL]
        self.assertEqual((a.broker, a.ticker, a.quantity, a.buy_price, a.buy_currency, a.price),
                         ("Trade Republic", "AAPL", 2.666666, 131.625, "EUR", None))
        self.assertEqual(hs[SP500].ticker, "SXR8.DE")
        b = hs[BTC]                                   # no Yahoo ticker: Trade Republic's own price
        self.assertEqual((b.ticker, b.name, b.price, b.currency), ("", "Bitcoin", 61000.5, "EUR"))
        cash = connections.cash_entries()
        self.assertEqual([(c["name"], c["balance"], c["currency"], c["auto"]) for c in cash],
                         [("Trade Republic", 1234.56, "EUR", True)])
        self.assertEqual(connections.holdings(), [])  # holdings go through connectors/traderepublic.py
        self.assertEqual(connections.trade_sources(), {})
        self.assertEqual(self.stored(conn["id"])["tr"]["phone"], "…01")

    def test_history_maps_to_trades_dividends_interest_and_fees(self):
        conn = self.sign_in()
        self.assertEqual(list(traderepublic.trade_sources()), ["Trade Republic"])
        self.assertEqual(traderepublic.trade_sources()["Trade Republic"][1](), [])  # nothing until read
        self.sync(conn["id"])
        snap = connections._cache[conn["id"]][0]
        self.assertTrue(snap.history_complete)
        self.assertEqual(snap.unread, 0)
        got = [(t.when[:10], t.key, t.side, t.quantity, t.amount) for t in snap.trades]
        self.assertEqual(got, [
            ("2025-01-10", AAPL, "BUY", 2.0, 301.0),            # incl. the €1 fee
            ("2025-02-03", AAPL, "BUY", 0.333333, 50.0),        # savings plan
            ("2025-03-05", AAPL, "SELL", 1.0, 179.5),           # received + tax withheld
            ("2025-06-10", SP500, "BUY", 0.025, 15.0),          # Saveback
            ("2025-07-01", AAPL, "BUY", 1.333333, 0.0),         # split: new shares at no cost
        ])
        self.assertTrue(all(t.currency == "EUR" for t in snap.trades))
        self.assertEqual(snap.trades[0].ticker, "AAPL")
        pos = compute_positions(snap.trades)[AAPL]
        self.assertAlmostEqual(pos.quantity, 2.666666, places=6)
        self.assertAlmostEqual(pos.realized, 179.5 - 351 / 2.333333, places=2)
        self.assertEqual([(d["date"], d["isin"], d["amount"]) for d in snap.dividends],
                         [("2025-03-15", AAPL, 0.75)])
        self.assertEqual(sorted((i["date"], i["amount"]) for i in snap.interest),
                         [("2025-04-01", 4.87), ("2025-06-10", 15.0)])   # interest + Saveback bonus
        self.assertEqual(sorted((f["date"], f["amount"], f["kind"]) for f in snap.fees),
                         [("2025-03-05", -0.3, "tax"), ("2025-05-01", -0.19, "tax")])
        # through the app: realized P/L source, income per account, the chart's income events
        label_trades = traderepublic.trade_sources()["Trade Republic"][1]()
        self.assertEqual(len(label_trades), 5)
        inc = connections.income_by_account()["Trade Republic"]
        self.assertEqual(inc, {"dividends": 0.75, "interest": 19.87, "fees": -0.49})
        cache.clear()
        with mock.patch.object(income, "_degiro_rows", return_value=[]), \
                mock.patch.object(income, "_trading212", return_value={}), \
                mock.patch.object(income.trading212, "get_dividends", return_value=[]), \
                mock.patch.object(income.trading212, "get_interest", return_value=[]), \
                mock.patch.object(income.trading212, "get_fees", return_value=[]):
            s = income.summary()
            events = income.events()
        self.assertEqual(s["by_broker"]["Trade Republic"], {"dividends": 0.75, "interest": 19.87, "fees": -0.49})
        self.assertIn(("2025-05-01", -0.19), events)
        self.assertEqual(self.stored(conn["id"])["summary"], "3 positions")

    def test_history_is_read_incrementally(self):
        conn = self.sign_in()
        self.sync(conn["id"])
        first_details = len(self.tr.subs("timelineDetailV2"))
        self.assertEqual(first_details, 5)            # only items that move shares
        self.assertEqual(len(self.tr.subs("timelineTransactions")), 2)   # two pages
        # not due yet: a normal sync reads positions only
        self.tr.ws_sent.clear()
        self.sync(conn["id"], manual=False)
        self.assertEqual(self.tr.subs("timelineTransactions"), [])
        # a new trade; Refresh reads the first page only and asks for one detail page
        new = item("t-new", "2026-01-05T09:00:00.000+0000", "Apple", "Kauforder", -151.0,
                   "trading_trade_executed", AAPL)
        self.tr.tx_pages = [[new] + TX_PAGE_1, TX_PAGE_2]
        self.tr.details["t-new"] = trade_detail(AAPL, "1", "150,00 €", "150,00 €", "1,00 €", "151,00 €")
        self.tr.ws_sent.clear()
        self.sync(conn["id"])
        self.assertEqual([s["id"] for s in self.tr.subs("timelineDetailV2")], ["t-new"])
        self.assertEqual(len(self.tr.subs("timelineTransactions")), 1)
        snap = connections._cache[conn["id"]][0]
        self.assertEqual(len(snap.trades), 6)

    def test_cancelled_and_deleted_items_drop_out(self):
        conn = self.sign_in()
        self.sync(conn["id"])
        gone = dict(TX_PAGE_2[2], status="CANCELED")    # the sell was cancelled after all
        self.tr.tx_pages = [TX_PAGE_1, [TX_PAGE_2[0], TX_PAGE_2[1], gone] + TX_PAGE_2[3:]]
        stored = self.stored(conn["id"])
        hist = dict(stored["history"], complete=False)  # e.g. a full re-read
        connections.save_fields(conn["id"], history=hist)
        self.sync(conn["id"])
        sides = [t.side for t in connections._cache[conn["id"]][0].trades]
        self.assertNotIn("SELL", sides)

    def test_read_only_by_construction(self):
        async def try_order():
            feed = trl.Feed(FakeWS(self.tr))
            await feed.open()
            try:
                await feed.get({"type": "simpleCreateOrder", "parameters": {}})
            finally:
                await feed.close()
        with self.assertRaises(ValueError):
            asyncio.run(try_order())
        self.assertFalse([m for m in self.tr.ws_sent if "simpleCreateOrder" in m])
        source = Path(trl.__file__).read_text()
        for word in ("simpleCreateOrder", "cancelOrder", "createSavingsPlan", "changeSavingsPlan",
                     "/payout", "createPriceAlarm", "addToWatchlist", "priceForOrder"):
            self.assertNotIn(word, source)
        self.assertTrue(trl.READ_SUBSCRIPTIONS <= {
            "compactPortfolioByType", "cash", "instrument", "ticker", "accountPairs",
            "timelineTransactions", "timelineActivityLog", "timelineDetailV2"})


# ------------------------------------------------------- counted once
class UploadTest(TRCase):
    def write_upload(self, trades, income_rows=()):
        (self.dir / "trades_trade_republic.json").write_text(json.dumps({"trades": trades}))
        (self.dir / "income_trade_republic.json").write_text(json.dumps({"income": list(income_rows)}))

    SAME = [{"date": "2025-01-10 20:29", "isin": AAPL, "name": "Apple", "ticker": "AAPL", "side": "BUY",
             "quantity": 2, "amount_eur": 301.0},
            {"date": "2025-02-03 09:26", "isin": AAPL, "name": "Apple", "ticker": "AAPL", "side": "BUY",
             "quantity": 0.333333, "amount_eur": 50.0},
            {"date": "2025-03-05 19:38", "isin": AAPL, "name": "Apple", "ticker": "AAPL", "side": "SELL",
             "quantity": 1, "amount_eur": 179.5}]
    INCOME = [{"date": "2025-03-15", "type": "dividend", "isin": AAPL, "amount_eur": 0.75},
              {"date": "2025-04-01", "type": "interest", "amount_eur": 4.87}]

    def test_upload_only_counts_as_before(self):
        self.write_upload(self.SAME, self.INCOME)
        p = traderepublic.plan()
        self.assertEqual(p["file"], "all")
        self.assertEqual(list(traderepublic.trade_sources()), ["Trade Republic"])
        self.assertEqual([h.broker for h in traderepublic.get_holdings()], ["Trade Republic"])
        self.assertEqual(traderepublic.income_rows()[1], self.INCOME)

    def test_hand_written_lowercase_sides(self):
        # A hand-made JSON with "buy"/"sell": 2.333333 bought, 1 sold leaves
        # 1.333333 held. Before: "buy" was read as a sale, so nothing was held.
        self.write_upload([{**t, "side": t["side"].lower()} for t in self.SAME])
        (h,) = traderepublic.get_holdings()
        self.assertAlmostEqual(h.quantity, 1.333333)

    def test_same_account_live_wins_once_history_is_read(self):
        self.write_upload(self.SAME, self.INCOME)
        conn = self.sign_in()
        # signed in, history not read yet: live positions, the upload's trades and income
        p = traderepublic.plan()
        self.assertEqual(p["file"], "history")
        self.assertEqual({h.isin for h in traderepublic.get_holdings()}, {AAPL, SP500, BTC})
        self.assertEqual(len(traderepublic.get_holdings()), 3)            # not the upload's AAPL too
        ts = traderepublic.trade_sources()
        self.assertEqual(list(ts), ["Trade Republic"])
        self.assertEqual(len(ts["Trade Republic"][1]()), 3)                # the upload's
        self.assertEqual(len(traderepublic.income_rows()[1]), 2)
        # history read: the live account replaces the upload
        self.sync(conn["id"])
        self.assertEqual(traderepublic.plan()["file"], "none")
        ts = traderepublic.trade_sources()
        self.assertEqual(list(ts), ["Trade Republic"])
        self.assertEqual(len(ts["Trade Republic"][1]()), 5)                # the live history, once
        self.assertEqual(traderepublic.income_rows()[1], [])
        self.assertIn("Not counted", traderepublic.file_note())
        cache.clear()
        with mock.patch.object(income, "_degiro_rows", return_value=[]), \
                mock.patch.object(income, "_trading212", return_value={}), \
                mock.patch.object(income.trading212, "get_dividends", return_value=[]), \
                mock.patch.object(income.trading212, "get_interest", return_value=[]), \
                mock.patch.object(income.trading212, "get_fees", return_value=[]):
            by = income.summary()["by_broker"]
            years = income.by_year()
        self.assertEqual(by["Trade Republic"]["dividends"], 0.75)          # not 1.50
        self.assertEqual(round(years["2025"], 2), round(0.75 + 4.87 + 15.0, 2))
        # the dashboard's own trade sources: Trade Republic once
        with mock.patch.object(sources.trading212, "trade_sources", return_value={}):
            keys = [k for k in sources._uncached_trade_sources() if k.startswith("Trade Republic")]
        self.assertEqual(keys, ["Trade Republic"])
        self.assertEqual(connections_api.list_connections()["connections"][0]["label"], "Trade Republic")

    def test_a_different_account_counts_both(self):
        other = [{"date": f"2024-0{m}-01", "isin": "US5949181045", "name": "Microsoft", "ticker": "MSFT",
                  "side": "BUY", "quantity": 1, "amount_eur": 300.0} for m in (1, 2, 3, 4)]
        self.write_upload(other)
        conn = self.sign_in()
        self.sync(conn["id"])
        p = traderepublic.plan()
        self.assertEqual((p["file"], p["file_label"]), ("all", "Trade Republic (upload)"))
        self.assertEqual(sorted(traderepublic.trade_sources()), ["Trade Republic", "Trade Republic (upload)"])
        brokers = sorted({h.broker for h in traderepublic.get_holdings()})
        self.assertEqual(brokers, ["Trade Republic", "Trade Republic (upload)"])

    def test_same_account_check(self):
        live = [trl.Trade(when="2025-01-10 19:29", key=AAPL, name="Apple", side="BUY", quantity=2, amount=1)]
        self.assertTrue(traderepublic.same_account(self.SAME[:1], live))   # a day apart is the same
        self.assertFalse(traderepublic.same_account(
            [{"date": "2020-01-01", "isin": "US5949181045"}], live))


# ----------------------------------------------------- never breaks the dashboard
class FailureTest(TRCase):
    def test_failing_trade_republic_never_raises_into_the_dashboard(self):
        conn = self.sign_in()
        connections._cache.clear()                     # and nothing saved to fall back on:
        connections.save_fields(conn["id"], snapshot=None)
        self.tr.ws_fail = OSError("connection refused")
        self.assertEqual(traderepublic.get_holdings(), [])
        self.assertEqual(connections.cash_entries(), [])
        stored = self.stored(conn["id"])
        self.assertEqual(stored["status"], "error")
        self.assertIn("Could not open the data connection", stored["error"])
        self.assertNotIn("refused", stored["error"])   # no raw exception text
        # garbage from Trade Republic: still a plain error
        self.tr.ws_fail = None
        self.tr.positions = "nonsense"
        connections._failed.clear()
        self.assertIsInstance(traderepublic.get_holdings(), list)
        self.assertIsInstance(traderepublic.trade_sources(), dict)
        self.assertIsInstance(income.summary(), dict)

    def test_slow_history_keeps_positions_fresh(self):
        conn = self.sign_in()
        real_answer = self.tr.answer

        def no_details(payload):
            if payload.get("type") == "timelineTransactions":
                return "E", {"errors": [{"errorCode": "SERVICE_UNAVAILABLE"}]}
            return real_answer(payload)

        self.tr.answer = no_details
        snap = self.sync(conn["id"])                   # history failed; positions still read
        self.assertEqual(len(snap.holdings), 3)
        self.assertFalse(snap.history_complete)
        self.assertEqual(self.stored(conn["id"])["status"], "ok")


# ---------------------------------------------------------------- parsing
class ParsingTest(unittest.TestCase):
    def test_numbers_in_both_formats(self):
        self.assertEqual(trl.numbers("3.002,80\xa0€"), [3002.8])
        self.assertEqual(trl.numbers("0,685102"), [0.685102])
        self.assertEqual(trl.numbers("49.000000"), [49.0])
        self.assertEqual(trl.numbers("1.0"), [1.0])
        self.assertEqual(trl.numbers("-0,78\xa0€"), [-0.78])
        self.assertEqual(trl.numbers("+ 18,77\xa0€"), [18.77])
        self.assertEqual(trl.numbers("1.000"), [1000.0, 1.0])           # ambiguous: both readings
        self.assertEqual(trl.numbers("Kostenlos"), [])

    def test_shares_checked_against_price(self):
        d = trade_detail(AAPL, "1.000", "5,00 €", "5.000,00 €", "1,00 €", "5.001,00 €")
        rec = trl.parse_event(item("x", "2025-01-01T10:00:00.000+0000", "A", "Kauforder", -5001.0,
                                   "TRADING_TRADE_EXECUTED", AAPL), d, "trade")
        self.assertEqual(rec["shares"], 1000.0)

    def test_sell_with_tax_like_pytr_fixture(self):
        # shape of pytr tests/events/verkaufsorder.json (amounts made up)
        d = trade_detail(AAPL, "3,454506", "58,50 €", "202,09 €", "1,00 €", "200,93 €", tax="0,16 €")
        rec = trl.parse_event(item("s", "2025-05-12T18:38:36.065+0000", "A", "Verkaufsorder", 200.93,
                                   "trading_trade_executed", AAPL), d, "trade")
        self.assertEqual((rec["kind"], rec["shares"], rec["amount"], rec["tax"], rec["fee"]),
                         ("sell", 3.454506, 200.93, 0.16, 1.0))

    def test_corporate_actions(self):
        rs = {"sections": [header("US0000000001"), overview(
            row("Orderart", "Reverse Split"), row("Wertpapier", "Globex"),
            row("Aktien entfernt", "110.403067"), row("Wertpapier", "GLOBEX INC. O.N."),
            row("Aktien hinzugefügt", "7.360204"))]}
        rec = trl.parse_event(item("r", "2025-02-11T08:13:48.877+0000", "Globex", "Reverse Split", None,
                                   "ssp_corporate_action_invoice_shares", "US0000000001"), rs, "shares")
        self.assertEqual((rec["kind"], rec["shares"], rec["shares2"], rec["name2"]),
                         ("move", 110.403067, 7.360204, "GLOBEX INC. O.N."))
        buy = {"id": "b", "ts": "2025-01-01 10:00:00", "kind": "buy", "isin": "US0000000001", "name": "Globex",
               "shares": 110.403067, "amount": 500.0}
        trades, *_ = trl.build({"b": buy, "r": rec}, names={"US0000000002": "Globex"})
        self.assertEqual([(t.side, t.key, t.amount) for t in trades],
                         [("BUY", "US0000000001", 500.0), ("SELL", "US0000000001", 500.0),
                          ("BUY", "US0000000002", 500.0)])     # the cost moves; no gain or loss
        self.assertAlmostEqual(compute_positions(trades)["US0000000001"].realized, 0.0, places=6)
        spin = {"sections": [header("DE0000000001"), overview(
            row("Orderart", "Spin-off"), row("Wertpapier", "NewCo"), row("Aktien hinzugefügt", "0.309986"))]}
        rec = trl.parse_event(item("p", "2025-10-20T11:25:29.214+0000", "OldCo", "Spin-off", None,
                                   "ssp_corporate_action_invoice_shares", "DE0000000001"), spin, "shares")
        self.assertEqual((rec["kind"], rec["name2"]), ("spinoff", "NewCo"))
        trades, _, _, _, unread = trl.build({"p": rec})                  # new ISIN unknown: not guessed
        self.assertEqual((trades, unread), ([], 1))
        trades, *_ = trl.build({"p": rec}, names={"DE0000000002": "NewCo"})
        self.assertEqual([(t.key, t.quantity, t.amount) for t in trades], [("DE0000000002", 0.309986, 0.0)])

    def test_delta_messages(self):
        self.assertEqual(trl.apply_delta('{"a":1,"b":"x y"}', '=5\t-1\t+2\t=11'), '{"a":2,"b":"x y"}')
        self.assertEqual(trl.apply_delta("", "+%7B%22p%22%3A%221%2C00+%E2%82%AC%22%7D"), '{"p":"1,00 €"}')

    def test_classify(self):
        self.assertEqual([trl.classify(i) for i in TX_PAGE_1 + TX_PAGE_2 + ACTIVITY],
                         ["saveback", "", "tax", "", "", "interest", "dividend", "trade", "trade",
                          "trade", "gone", "shares", ""])

    def test_registry_links_the_sign_in(self):
        inst = {"id": "traderepublic", "kind": "both"}
        self.assertEqual(registry.link(inst, {"type": "api", "auth": "phone_pin"}),
                         {"support": "live", "connector": "traderepublic"})
        spec = registry.connector("traderepublic")
        self.assertEqual(spec.extra["flow"], "tr_login")
        self.assertTrue(connections.feeds_here(spec, "cash"))
        self.assertFalse(connections.feeds_here(spec, "trades"))
        with self.assertRaises(HTTPException):         # not through the key form
            connections_api.create(institution="traderepublic", method="0", label="", wrapper="",
                                   credentials="{}", mapping="", currency="EUR", file=None)


if __name__ == "__main__":
    unittest.main()
