"""Enable Banking bank links: JWT, app setup, the trip to the bank (start,
callback, single-use state), balances, consent expiry and error paths.

No network: requests.request in backend/connectors/enablebanking.py is
replaced by canned answers. Keys are generated here; nothing personal.
Run: .venv/bin/python -m unittest discover -s tests -t .
"""

import ast
import base64
import json
import os
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from fastapi import HTTPException
from starlette.requests import Request

from backend import banklink_api, cache, cash, catalog, connections, connections_api, fx
from backend.connectors import enablebanking as eb
from backend.connectors import registry
from backend.connectors.live import ConnectorError, consent

APP_ID = "0b6d1f4e-1111-4222-8333-944455556666"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption()).decode()
DAY = 86400


def iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(t))


def answer(status=200, body=None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(body if body is not None else {}).encode()
    return r


ASPSPS = {"aspsps": [
    {"name": "Rabobank", "country": "NL", "psu_types": ["business", "personal"],
     "maximum_consent_validity": 180 * DAY, "required_psu_headers": ["Psu-Ip-Address", "Psu-User-Agent"],
     "logo": "https://example.com/rabo.png"},
    {"name": "ING", "country": "NL", "psu_types": ["personal"], "maximum_consent_validity": 90 * DAY},
    {"name": "ING Business", "country": "NL", "psu_types": ["business"]},
    {"name": "ABN AMRO", "country": "NL", "psu_types": ["personal"]},
    {"name": "bunq", "country": "NL"},
    {"name": "Revolut", "country": "LT"},
    {"name": "Revolut", "country": "NL"},
    {"name": "Trade Republic", "country": "DE"},
    {"name": "Nordea", "country": "FI", "psu_types": ["business"]},
]}


def session(sid="sess-1", until=None):
    return {"session_id": sid, "aspsp": {"name": "Rabobank", "country": "NL"}, "psu_type": "personal",
            "access": {"valid_until": until or iso(time.time() + 180 * DAY)},
            "accounts": [
                {"uid": f"{sid}-uid-1", "account_id": {"iban": "NL00TEST0000001234"}, "name": "A. Holder",
                 "details": "Betaalrekening", "cash_account_type": "CACC", "currency": "EUR",
                 "identification_hash": "hash-1"},
                {"uid": f"{sid}-uid-2", "account_id": {"iban": "NL00TEST0000005678"},
                 "product": "Spaarrekening", "cash_account_type": "SVGS", "currency": "EUR",
                 "identification_hash": "hash-2"}]}


def bal(kind, amount, ccy="EUR", stamp=""):
    return {"name": kind, "balance_type": kind, "balance_amount": {"amount": amount, "currency": ccy},
            "last_change_date_time": stamp}


BALANCES = {"uid-1": {"balances": [bal("ITAV", "1500.00"), bal("XPCD", "1234.56")]},
            "uid-2": {"balances": [bal("CLBD", "5000.00")]}}


class FakeAPI:
    """Fake requests.request for Enable Banking: (METHOD, path regex) -> answer."""

    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def __call__(self, method, url, json=None, params=None, headers=None, timeout=None):
        path = url[len(eb.BASE_URL):]
        self.calls.append({"method": method, "path": path, "json": json, "headers": headers or {}})
        for (m, pattern), value in self.routes.items():
            if m == method and re.fullmatch(pattern, path):
                value = value(path, json) if callable(value) else value
                return value if isinstance(value, requests.Response) else answer(200, value)
        return answer(404, {"message": "Not found", "error": "SESSION_DOES_NOT_EXIST" if "session" in path else ""})

    def paths(self, method=None):
        return [c["path"] for c in self.calls if method is None or c["method"] == method]


def balances_route(path, _):
    uid = path.split("/")[2]
    for key, value in BALANCES.items():
        if uid.endswith(key):
            return value
    return answer(404, {"error": "ACCOUNT_DOES_NOT_EXIST"})


def make_request(headers=None, client=("8.8.8.8", 50000)):
    hdrs = {"host": "127.0.0.1:8105", **(headers or {})}
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                    "headers": [(k.lower().encode(), v.encode()) for k, v in hdrs.items()],
                    "client": client, "scheme": "http", "server": ("127.0.0.1", 8105), "root_path": ""})


RENDER = {"host": "internal:10000", "x-forwarded-proto": "https", "x-forwarded-host": "my-app.onrender.com",
          "x-forwarded-for": "8.8.8.8, 10.0.0.1", "user-agent": "Mozilla/5.0 (iPhone)"}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for target, value in ((connections, {"_file": lambda name: Path(self.dir) / name}),
                              (fx, {"_live_rates": lambda *a, **k: {"EUR": 1.0, "GBP": 1.2, "USD": 0.9}}),
                              (connections_api.github_sync, {"push_vault_async": lambda: None}),
                              (cash, {"manual_accounts": lambda: []})):
            p = mock.patch.multiple(target, **value)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items()
                                           if not k.startswith("ENABLEBANKING_")}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        for d in (connections._cache, connections._failed, connections._inflight, connections._read_cache,
                  eb._banks, eb._jwts):
            d.clear()

    def api(self, routes=None):
        base = {("GET", "/aspsps"): ASPSPS, ("GET", r"/accounts/[^/]+/balances"): balances_route,
                ("DELETE", r"/sessions/[^/]+"): {"message": "OK"}}
        fake = FakeAPI({**base, **(routes or {})})
        p = mock.patch("backend.connectors.enablebanking.requests.request", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    def save_app(self):
        connections.save_app_credentials(eb.PROVIDER, {"app_id": APP_ID, "private_key": PEM})

    def rabo_method(self):
        inst = catalog.find("rabobank")
        return next(m["key"] for m in inst["methods"] if m.get("connector") == "enablebanking")

    def start(self, fake, request=None, **form):
        fields = {"institution": "rabobank", "method": self.rabo_method(), "aspsp": "Rabobank",
                  "country": "NL", "label": "", "renew": "", **form}
        out = banklink_api.start(request or make_request(RENDER), **fields)
        body = [c for c in fake.calls if c["path"] == "/auth"][-1]["json"]
        return out, body["state"]

    def link(self, fake, sid="sess-1", **form):
        fake.routes[("POST", "/sessions")] = session(sid)
        _, state = self.start(fake, **form)
        page = banklink_api.callback(make_request(RENDER), state=state, code="code-" + sid)
        return page, state


# -------------------------------------------------------------------- JWT
class JwtTest(unittest.TestCase):
    def test_signature_and_claims(self):
        token = eb.make_jwt(eb.App(APP_ID, PEM), now=1_800_000_000)
        head, claims, sig = token.split(".")
        dec = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        self.assertEqual(json.loads(dec(head)), {"typ": "JWT", "alg": "RS256", "kid": APP_ID})
        c = json.loads(dec(claims))
        self.assertEqual((c["iss"], c["aud"], c["iat"]), ("enablebanking.com", "api.enablebanking.com", 1_800_000_000))
        self.assertEqual(c["exp"] - c["iat"], 3600)
        self.assertLessEqual(c["exp"] - c["iat"], 86400)
        KEY.public_key().verify(dec(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
        with self.assertRaises(InvalidSignature):
            KEY.public_key().verify(dec(sig), f"{head}.{claims}x".encode(), padding.PKCS1v15(), hashes.SHA256())

    def test_key_problems_are_plain(self):
        with self.assertRaisesRegex(ConnectorError, "not a private key"):
            eb.load_key("hello")
        public = KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                               serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        with self.assertRaisesRegex(ConnectorError, "public key"):
            eb.load_key(public)
        with self.assertRaisesRegex(ConnectorError, "Could not read"):
            eb.load_key("-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----")
        locked = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.BestAvailableEncryption(b"pw")).decode()
        with self.assertRaisesRegex(ConnectorError, "password"):
            eb.load_key(locked)
        ec_key = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        with self.assertRaisesRegex(ConnectorError, "RSA"):
            eb.load_key(ec_key)
        self.assertIsNotNone(eb.load_key(PEM.replace("\n", "\\n")))   # one-line env value

    def test_app_id(self):
        self.assertEqual(eb.check_app_id(" " + APP_ID.upper() + " "), APP_ID)
        with self.assertRaisesRegex(ConnectorError, "aaaaaaaa-bbbb"):
            eb.check_app_id("my-app")


# ------------------------------------------------------------- app setup
class AppSetupTest(Base):
    def test_env_file_and_saved_app(self):
        self.assertEqual(eb.current(), (None, ""))
        key_file = Path(self.dir) / "eb.pem"
        key_file.write_text(PEM)
        with mock.patch.dict(os.environ, {"ENABLEBANKING_APP_ID": APP_ID, "ENABLEBANKING_KEY_FILE": str(key_file)}):
            app, problem = eb.current()
            self.assertEqual((app.app_id, app.source, problem), (APP_ID, "env", ""))
            self.save_app()                                      # saved in Settings wins
            self.assertEqual(eb.current()[0].source, "app")
        with mock.patch.dict(os.environ, {"ENABLEBANKING_APP_ID": APP_ID}):
            connections.forget_app_credentials(eb.PROVIDER)
            app, problem = eb.current()
            self.assertIsNone(app)
            self.assertIn("ENABLEBANKING_PRIVATE_KEY", problem)
        with mock.patch.dict(os.environ, {"ENABLEBANKING_APP_ID": APP_ID, "ENABLEBANKING_PRIVATE_KEY": PEM}):
            self.assertEqual(eb.current()[0].private_key, PEM.strip())

    def test_rejected_key_saves_nothing(self):
        fake = self.api({("GET", "/aspsps"): answer(401, {"message": "Unauthorized", "error": "UNAUTHORIZED_ACCESS"})})
        with self.assertRaises(HTTPException) as cm:
            banklink_api.save_app(make_request(), app_id=APP_ID, private_key=PEM, key_file=None)
        self.assertIn("did not accept the application ID and key", cm.exception.detail)
        self.assertEqual(connections.app_credentials(eb.PROVIDER), {})
        self.assertTrue(fake.calls[0]["headers"]["Authorization"].startswith("Bearer ey"))
        # what the live API answers for an application ID it does not know
        fake.routes[("GET", "/aspsps")] = answer(403, {"code": 403, "message": "Application does not exist"})
        with self.assertRaises(HTTPException) as cm:
            banklink_api.save_app(make_request(), app_id=APP_ID, private_key=PEM, key_file=None)
        self.assertIn("Application does not exist", cm.exception.detail)
        self.assertIn("Check the application ID", cm.exception.detail)
        self.assertEqual(connections.app_credentials(eb.PROVIDER), {})

    def test_fake_key_is_a_clear_error_without_calling(self):
        fake = self.api()
        with self.assertRaises(HTTPException) as cm:
            banklink_api.save_app(make_request(), app_id=APP_ID, private_key="not a key", key_file=None)
        self.assertIn("not a private key", cm.exception.detail)
        self.assertEqual(fake.calls, [])

    def test_saved_key_never_reaches_the_browser(self):
        self.api()
        out = banklink_api.save_app(make_request(RENDER), app_id=APP_ID, private_key=PEM, key_file=None)
        self.assertTrue(out["configured"])
        self.assertEqual(out["banks"], len(ASPSPS["aspsps"]))
        self.assertEqual(out["redirect_url"], "https://my-app.onrender.com/enablebanking/callback")
        self.assertTrue(out["https"])
        self.assertEqual(connections.app_credentials(eb.PROVIDER)["app_id"], APP_ID)
        shown = json.dumps([out, banklink_api.status(make_request()), connections_api.list_connections()])
        self.assertNotIn("PRIVATE KEY", shown)
        self.assertNotIn(APP_ID, shown)
        # other connections keep the app credentials when they are saved
        connections.add(connections.new({"id": "starling", "name": "Starling"}, "api", "starling", "S", "",
                                        {"token": "t" * 20}))
        self.assertEqual(connections.app_credentials(eb.PROVIDER)["private_key"], PEM.strip())

    def test_local_redirect_url_is_flagged(self):
        out = banklink_api.status(make_request())
        self.assertEqual(out["redirect_url"], "http://127.0.0.1:8105/enablebanking/callback")
        self.assertFalse(out["https"])
        self.assertFalse(out["configured"])


# ------------------------------------------------------------- bank list
class BankListTest(Base):
    def test_suggests_the_institution_and_caches_the_list(self):
        self.save_app()
        fake = self.api()
        rabo = banklink_api.bank_list(country="", institution="rabobank")
        self.assertEqual((rabo["country"], rabo["suggested"]), ("NL", "Rabobank"))
        self.assertIn("FI", rabo["countries"])
        self.assertEqual(banklink_api.bank_list(institution="ing_nl")["suggested"], "ING")
        self.assertEqual(banklink_api.bank_list(institution="revolut")["country"], "NL")
        self.assertEqual(banklink_api.bank_list(institution="abnamro")["suggested"], "ABN AMRO")
        tr = banklink_api.bank_list(institution="traderepublic")       # listed under DE only
        self.assertEqual((tr["country"], tr["suggested"]), ("DE", "Trade Republic"))
        self.assertEqual(banklink_api.bank_list(country="FI")["banks"][0]["name"], "Nordea")
        self.assertEqual(fake.paths(), ["/aspsps"])                       # fetched once

    def test_not_set_up(self):
        with self.assertRaises(HTTPException) as cm:
            banklink_api.bank_list(institution="rabobank")
        self.assertIn("not set up", cm.exception.detail)


# ------------------------------------------------------------ link flow
class LinkFlowTest(Base):
    def setUp(self):
        super().setUp()
        self.save_app()
        self.fake = self.api({("POST", "/auth"): {"url": "https://bank.example/approve", "authorization_id": "a1"}})

    def test_start_asks_the_bank_with_an_https_redirect(self):
        before = time.time()
        out, state = self.start(self.fake)
        self.assertEqual(out["url"], "https://bank.example/approve")
        body = self.fake.calls[-1]["json"]
        self.assertEqual(body["aspsp"], {"name": "Rabobank", "country": "NL"})
        self.assertEqual(body["redirect_url"], "https://my-app.onrender.com/enablebanking/callback")
        self.assertEqual(body["psu_type"], "personal")
        until = time.mktime(time.strptime(body["access"]["valid_until"][:19], "%Y-%m-%dT%H:%M:%S")) - time.timezone
        self.assertLessEqual(until, before + 180 * DAY)
        self.assertGreater(until, before + 179 * DAY)
        self.assertGreaterEqual(len(state), 30)
        stored = (Path(self.dir) / connections.STATES_FILE).read_text()
        self.assertNotIn(state, stored)                                    # only a hash is kept

    def test_callback_saves_the_link_and_balances_flow_into_cash(self):
        page, _ = self.link(self.fake)
        self.assertEqual(page.status_code, 200)
        text = page.body.decode()
        self.assertIn("Rabobank is linked — you can go back to the app.", text)
        self.assertIn('href="/settings"', text)
        self.assertNotIn("sess-1", text)
        self.assertEqual(self.fake.calls[[c["path"] for c in self.fake.calls].index("/sessions")]["json"],
                         {"code": "code-sess-1"})
        [conn] = connections.load_all()
        self.assertEqual((conn["connector"], conn["institution"], conn["label"]), ("enablebanking", "rabobank", "Rabobank"))
        self.assertEqual(conn["bank"]["aspsp"], "Rabobank")
        self.assertEqual(conn["status"], "ok")
        pub = connections.public(conn)
        self.assertNotIn("sess-1", json.dumps(pub))
        self.assertNotIn("uid-1", json.dumps(pub))
        self.assertEqual(pub["consent"]["days_left"], 179)
        self.assertFalse(pub["consent"]["warn"])
        # the user was online (back from the bank): PSU headers were sent
        first = [c for c in self.fake.calls if c["path"].endswith("/balances")][0]
        self.assertEqual(first["headers"]["Psu-Ip-Address"], "8.8.8.8")
        self.assertEqual(first["headers"]["Psu-User-Agent"], "Mozilla/5.0 (iPhone)")
        cash_rows = {r["name"]: r for r in connections.cash_entries()}
        self.assertEqual(cash_rows["Rabobank Betaalrekening ••1234"]["balance"], 1234.56)   # XPCD over ITAV
        self.assertEqual(cash_rows["Rabobank Spaarrekening ••5678"]["type"], "savings")
        self.assertTrue(all(r["auto"] and r["institution"] == "Rabobank" for r in cash_rows.values()))

    def test_state_is_single_use(self):
        page, state = self.link(self.fake)
        self.assertEqual(page.status_code, 200)
        again = banklink_api.callback(make_request(RENDER), state=state, code="code-sess-1")
        self.assertEqual(again.status_code, 400)
        self.assertIn("expired", again.body.decode())
        self.assertEqual(self.fake.paths("POST").count("/sessions"), 1)

    def test_unknown_or_old_state_is_rejected(self):
        bad = banklink_api.callback(make_request(), state="made-up", code="x")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(banklink_api.callback(make_request(), state="", code="x").status_code, 400)
        self.assertNotIn("/sessions", self.fake.paths())
        state = connections.new_link_state({"aspsp": "Rabobank"}, now=1000.0)
        self.assertIsNone(connections.take_link_state(state, now=1000.0 + 30 * 60 + 1))
        self.assertIsNone(connections.take_link_state(state, now=1001.0))      # gone after the first try
        fresh = connections.new_link_state({"aspsp": "Rabobank"})
        self.assertEqual(connections.take_link_state(fresh)["aspsp"], "Rabobank")

    def test_cancelled_at_the_bank(self):
        _, state = self.start(self.fake)
        page = banklink_api.callback(make_request(), state=state, error="access_denied",
                                     error_description="User cancelled")
        self.assertEqual(page.status_code, 400)
        self.assertIn("Rabobank was not linked", page.body.decode())
        self.assertIn("User cancelled", page.body.decode())
        self.assertEqual(connections.load_all(), [])
        self.assertIsNone(connections.take_link_state(state))

    def test_renew_replaces_the_session(self):
        self.link(self.fake, "sess-1")
        [conn] = connections.load_all()
        self.fake.routes[("POST", "/sessions")] = session("sess-2")
        _, state = self.start(self.fake, renew=conn["id"], institution="", method="", aspsp="", country="")
        page = banklink_api.callback(make_request(RENDER), state=state, code="code-2")
        self.assertIn("Rabobank is renewed", page.body.decode())
        [after] = connections.load_all()
        self.assertEqual(after["id"], conn["id"])
        self.assertEqual(after["credentials"]["session_id"], "sess-2")
        deadline = time.time() + 3
        while "/sessions/sess-1" not in self.fake.paths("DELETE") and time.time() < deadline:
            time.sleep(0.02)
        self.assertIn("/sessions/sess-1", self.fake.paths("DELETE"))     # old consent ended

    def test_linking_the_same_accounts_again_renews(self):
        self.link(self.fake, "sess-1")
        self.link(self.fake, "sess-3")
        [conn] = connections.load_all()
        self.assertEqual(conn["credentials"]["session_id"], "sess-3")

    def test_redirect_not_registered(self):
        self.fake.routes[("POST", "/auth")] = answer(422, {"message": "Redirect URI not allowed",
                                                           "error": "REDIRECT_URI_NOT_ALLOWED"})
        with self.assertRaises(HTTPException) as cm:
            self.start(self.fake)
        self.assertIn("https://my-app.onrender.com/enablebanking/callback", cm.exception.detail)
        self.assertEqual(json.loads((Path(self.dir) / connections.STATES_FILE).read_text()), {})

    def test_key_form_refuses_a_bank_link(self):
        with self.assertRaises(HTTPException) as cm:
            connections_api.create(institution="rabobank", method=self.rabo_method(), label="", wrapper="",
                                   credentials="{}", mapping="", currency="EUR", file=None)
        self.assertIn("Link button", cm.exception.detail)

    def test_remove_ends_the_consent(self):
        self.link(self.fake)
        [conn] = connections.load_all()
        connections_api.delete(conn["id"])
        deadline = time.time() + 3
        while "/sessions/sess-1" not in self.fake.paths("DELETE") and time.time() < deadline:
            time.sleep(0.02)
        self.assertIn("/sessions/sess-1", self.fake.paths("DELETE"))

    def test_callback_is_public(self):
        src = (Path(__file__).resolve().parents[1] / "backend" / "main.py").read_text()
        node = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", "") == "_PUBLIC" for t in n.targets))
        self.assertIn(eb.CALLBACK_PATH, ast.literal_eval(node.value))
        self.assertTrue(any(getattr(r, "path", "") == eb.CALLBACK_PATH for r in banklink_api.router.routes))


# --------------------------------------------------------------- balances
class BalanceTest(Base):
    def test_balance_choice(self):
        self.assertEqual(eb.pick_balance([bal("ITAV", "10"), bal("CLBD", "8"), bal("ITBD", "9")]), (9.0, "EUR"))
        self.assertEqual(eb.pick_balance([bal("CLBD", "7", stamp="2026-11-01"), bal("CLBD", "8", stamp="2026-11-13")]),
                         (8.0, "EUR"))
        # an available balance includes the overdraft: take the credit limit off
        self.assertEqual(eb.pick_balance([bal("ITAV", "1500")], {"amount": "1000", "currency": "EUR"}), (500.0, "EUR"))
        self.assertEqual(eb.pick_balance([bal("WEIRD", "3", "USD")]), (3.0, "USD"))
        self.assertIsNone(eb.pick_balance([{"balance_type": "XPCD"}]))
        self.assertIsNone(eb.pick_balance([]))

    def test_single_account_is_named_after_the_label(self):
        self.save_app()
        self.api()
        conn = connections.add(connections.new({"id": "bunq", "name": "bunq"}, "aggregator", "enablebanking", "bunq", "",
                                                {"session_id": "s", "accounts": [{"uid": "x-uid-1", "name": "Main",
                                                                                  "type": "CACC", "iban": "••1234"}]}))
        conn["bank"] = {"aspsp": "bunq", "country": "NL"}
        self.assertEqual(eb.fetch(conn["credentials"], conn).cash, [
            {"name": "Main", "currency": "EUR", "balance": 1234.56, "type": "cash"}])
        self.assertEqual([r["name"] for r in connections.cash_entries()], ["bunq"])


# -------------------------------------------------------- consent + errors
class ConsentAndErrorTest(Base):
    def linked(self, expires_in=100 * DAY):
        self.save_app()
        conn = connections.new({"id": "rabobank", "name": "Rabobank"}, "aggregator", "enablebanking", "Rabobank", "",
                               {"session_id": "s-9", "accounts": [{"uid": "s-9-uid-1", "name": "Betaal", "type": "CACC"}]})
        conn.update(bank={"aspsp": "Rabobank", "country": "NL", "required_psu_headers": ["Psu-Ip-Address"]},
                    consent_expires=iso(time.time() + expires_in))
        return connections.add(conn)

    def test_warning_from_14_days(self):
        now = 1_800_000_000
        self.assertEqual(consent(iso(now + 10 * DAY + 60), now=now),
                         {"expires": time.strftime("%Y-%m-%d", time.gmtime(now + 10 * DAY + 60)),
                          "days_left": 10, "warn": True, "expired": False})
        self.assertTrue(consent(iso(now + 14 * DAY + 60), now=now)["warn"])
        self.assertFalse(consent(iso(now + 15 * DAY + 60), now=now)["warn"])
        self.assertTrue(consent(iso(now - 1), now=now)["expired"])
        self.assertIsNone(consent("soon"))
        conn = self.linked(expires_in=5 * DAY + 60)
        self.assertTrue(connections.public(conn)["consent"]["warn"])

    def test_ended_consent_is_a_clear_error_and_never_breaks_the_dashboard(self):
        fake = self.api()
        conn = self.linked(expires_in=-DAY)
        self.assertEqual(connections.cash_entries(), [])
        stored = connections.get(conn["id"])
        self.assertEqual(stored["status"], "error")
        self.assertIn("ended on", stored["error"])
        self.assertIn("Renew", stored["error"])
        self.assertEqual(fake.calls, [])                                  # no call spent
        self.assertGreaterEqual(connections._failed[conn["id"]][1], 6 * 3600)
        self.assertTrue(connections.public(stored)["consent"]["expired"])

    def test_session_ended_at_the_bank(self):
        self.api({("GET", r"/accounts/[^/]+/balances"): answer(401, {"message": "Session is expired",
                                                                     "error": "EXPIRED_SESSION"})})
        conn = self.linked()
        self.assertEqual(connections.cash_entries(), [])
        self.assertIn("has ended. Press Renew", connections.get(conn["id"])["error"])

    def test_rate_limit_keeps_the_last_balances(self):
        fake = self.api()
        conn = self.linked()
        self.assertEqual(connections.cash_entries()[0]["balance"], 1234.56)
        # restart: the saved balances are used, no new call
        connections._cache.clear()
        n = len(fake.calls)
        self.assertEqual(connections.cash_entries()[0]["balance"], 1234.56)
        self.assertEqual(len(fake.calls), n)
        # 7 hours later the bank says "too many": the old balance stays, retry in 6 h
        snap = connections.get(conn["id"])["snapshot"]
        connections.update(conn["id"], snapshot={**snap, "at": time.time() - 7 * 3600})
        fake.routes[("GET", r"/accounts/[^/]+/balances")] = answer(429, {"error": "ASPSP_RATE_LIMIT_EXCEEDED"})
        self.assertEqual(connections.cash_entries()[0]["balance"], 1234.56)
        deadline = time.time() + 3
        while connections.get(conn["id"])["status"] != "error" and time.time() < deadline:
            time.sleep(0.02)
        self.assertIn("only a few updates a day", connections.get(conn["id"])["error"])
        self.assertEqual(connections._failed[conn["id"]][1], eb.LIMIT_WAIT)

    def test_refresh_tells_the_bank_the_user_is_online(self):
        fake = self.api()
        conn = self.linked()
        connections_api.refresh(conn["id"], make_request({"x-forwarded-for": "1.1.1.1", "user-agent": "UA"}))
        self.assertEqual(fake.calls[-1]["headers"]["Psu-Ip-Address"], "1.1.1.1")
        connections_api.refresh(conn["id"], make_request(client=("127.0.0.1", 1)))    # local: not a public IP
        self.assertNotIn("Psu-Ip-Address", fake.calls[-1]["headers"])
        # a bank that needs a header we do not have gets none
        self.assertEqual(eb.psu_headers({"Psu-Ip-Address": "1.2.3.4"}, ["Psu-Ip-Address", "Psu-Geo-Location"]), {})

    def test_error_messages_hide_tokens(self):
        self.save_app()
        self.api({("GET", "/aspsps"): answer(400, {"message": "bad token eyJhbGciOi.eyJpc3Mi.c2ln here",
                                                   "error": "WRONG_REQUEST_PARAMETERS"})})
        with self.assertRaises(ConnectorError) as cm:
            eb.banks(eb.require_app(), force=True)
        self.assertNotIn("eyJ", str(cm.exception))
        self.assertIn("HTTP 400", str(cm.exception))

    def test_network_errors(self):
        self.save_app()
        with mock.patch("backend.connectors.enablebanking.requests.request", side_effect=requests.Timeout()):
            with self.assertRaisesRegex(ConnectorError, "did not answer in time"):
                eb.banks(eb.require_app(), force=True)
        with mock.patch("backend.connectors.enablebanking.requests.request",
                        side_effect=requests.ConnectionError("https://api.enablebanking.com/aspsps")):
            with self.assertRaises(ConnectorError) as cm:
                eb.banks(eb.require_app(), force=True)
        self.assertNotIn("http", str(cm.exception))


# ------------------------------------------------------------- dashboard
class IntegrationTest(Base):
    def test_manual_balance_for_the_same_bank_is_flagged(self):
        conn = connections.add(connections.new({"id": "ing_nl", "name": "ING (Netherlands)"}, "aggregator",
                                               "enablebanking", "ING", "", {"session_id": "s", "accounts": []}))
        manual = [{"name": "ING betaal", "institution": ""}, {"name": "Savings", "institution": "ING"},
                  {"name": "Starling", "institution": "Starling Bank"}, {"name": "Rabo", "institution": "Rabobank"}]
        with mock.patch.object(cash, "manual_accounts", return_value=manual), \
                mock.patch.object(connections, "_snapshots", return_value={}):
            [pub] = [c for c in connections_api.list_connections()["connections"] if c["id"] == conn["id"]]
        self.assertEqual(pub["manual_duplicates"], ["ING betaal", "Savings"])

    def test_catalog_defaults(self):
        for inst_id, want in (("rabobank", "enablebanking"), ("revolut", "enablebanking"),
                              ("ing_nl", "enablebanking"), ("abnamro", "enablebanking"), ("bunq", "enablebanking")):
            inst = catalog.find(inst_id)
            m = catalog.method(inst, inst["default"])
            self.assertEqual((m["support"], m.get("connector")), ("live", want), inst_id)
            self.assertTrue(m.get("recommended"), inst_id)
            self.assertEqual(m["covers"], ["cash"])
        wise = catalog.find("wise")
        self.assertEqual(catalog.method(wise, wise["default"])["connector"], "wise")
        tr = catalog.find("traderepublic")
        # the in-app sign-in (unofficial, read-only) is recommended; the upload stays
        default = catalog.method(tr, tr["default"])
        self.assertEqual((default["support"], default["connector"]), ("live", "traderepublic"))
        self.assertTrue(any(m["support"] == "import" for m in tr["methods"]))
        self.assertTrue(any(m.get("connector") == "enablebanking" for m in tr["methods"]))
        rabo = catalog.find("rabobank")
        self.assertEqual([m["support"] for m in rabo["methods"] if m.get("provider") == "gocardless"], ["info"])
        self.assertEqual(registry.connector("enablebanking").ttl, 6 * 3600)


if __name__ == "__main__":
    unittest.main()
