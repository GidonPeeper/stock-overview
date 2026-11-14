""""Add an account": store, masking, validation, live connectors (canned
responses), generic CSV import, and the hand-off to sources / cash.

No network: requests.get is replaced by canned responses.
Run: .venv/bin/python -m unittest discover -s tests -t .
"""

import hashlib
import hmac
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import requests
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from backend import catalog, connections, connections_api, fx, sources
from backend.connectors import bitvavo, etoro, ibkr_flex, registry, starling, trading212, wise
from backend.connectors.live import ConnectorError, Snapshot
from backend.importers import generic_csv

RATES = {"EUR": 1.0, "GBP": 1.2, "USD": 0.9}


def response(status=200, body=None, raw: bytes | None = None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
    return r


class Routes:
    """Fake requests.get: the first route whose key is in the URL answers."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, headers=None, params=None, auth=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "params": params or {}})
        for key, value in self.routes.items():
            if key in url:
                value = value(url, params) if callable(value) else value
                if isinstance(value, list) and value and isinstance(value[0], requests.Response):
                    return value.pop(0)
                return value if isinstance(value, requests.Response) else response(200, value)
        return response(404, {"error": "not found"})


class TempStore(unittest.TestCase):
    """Every test gets its own empty data directory."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        p = mock.patch.object(connections, "_file", lambda name: __import__("pathlib").Path(self.dir) / name)
        p.start()
        self.addCleanup(p.stop)
        r = mock.patch.object(fx, "_live_rates", return_value=RATES)
        r.start()
        self.addCleanup(r.stop)
        s = mock.patch.object(connections_api.github_sync, "push_vault_async", lambda: None)
        s.start()
        self.addCleanup(s.stop)
        for d in (connections._cache, connections._failed, connections._inflight, connections._read_cache):
            d.clear()

    def http(self, routes):
        fake = Routes(routes)
        p = mock.patch("backend.connectors.live.requests.get", fake)
        p.start()
        self.addCleanup(p.stop)
        return fake


# ------------------------------------------------------------------ store
class StoreTest(TempStore):
    def test_masking_never_returns_secrets(self):
        token = "flex-token-0123456789abcdef"
        inst = {"id": "ibkr", "name": "Interactive Brokers"}
        conn = connections.add(connections.new(inst, "api", "ibkr_flex", "", "ISA",
                                               {"token": token, "query_id": "123456"}))
        pub = connections.public(connections.get(conn["id"]))
        self.assertNotIn(token, json.dumps(pub))
        self.assertEqual(pub["credentials"]["token"], "••••cdef")
        self.assertEqual(pub["credentials"]["query_id"], "123456")   # marked not secret
        self.assertEqual(pub["wrapper"], "ISA")
        self.assertEqual(pub["label"], "Interactive Brokers")

    def test_unknown_fields_are_masked_by_default(self):
        conn = connections.new({"id": "x", "name": "X"}, "api", "nope", "X", "", {"pin": "1234"})
        self.assertEqual(connections.public(conn)["credentials"], {"pin": "••••"})

    def test_add_remove_and_labels(self):
        inst = {"id": "starling", "name": "Starling Bank"}
        a = connections.add(connections.new(inst, "api", "starling", "Starling", "", {"token": "t" * 20}))
        self.assertEqual([c["id"] for c in connections.load_all()], [a["id"]])
        self.assertEqual(connections.unique_label("starling", {"Starling"}), "starling (2)")
        self.assertIsNotNone(connections.remove(a["id"]))
        self.assertEqual(connections.load_all(), [])
        self.assertIsNone(connections.remove(a["id"]))

    def test_canonical_credentials_accept_catalog_names(self):
        creds = connections.canonical_credentials("ibkr_flex", {"flex_token": " abc ", "query_id": "42"}, [])
        self.assertEqual(creds, {"token": "abc", "query_id": "42"})
        self.assertEqual(connections.missing_fields("ibkr_flex", {"token": "abc", "query_id": ""}),
                         ["Flex Query ID"])


# -------------------------------------------------------------- endpoints
class CreateTest(TempStore):
    def test_failed_validation_is_a_clear_error_and_saves_nothing(self):
        self.http({"/api/v2/accounts": response(401, {"error": "invalid_token"})})
        with self.assertRaises(HTTPException) as cm:
            connections_api.create(institution="starling", method="0", label="", wrapper="",
                                   credentials=json.dumps({"access_token": "fake-token-123"}),
                                   mapping="", currency="GBP", file=None)
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("did not accept the key", cm.exception.detail)
        self.assertNotIn("fake-token-123", cm.exception.detail)
        self.assertEqual(connections.load_all(), [])

    def test_missing_field(self):
        with self.assertRaises(HTTPException) as cm:
            connections_api.create(institution="ibkr", method="0", label="", wrapper="",
                                   credentials=json.dumps({"flex_token": "abc"}), mapping="",
                                   currency="GBP", file=None)
        self.assertIn("Flex Query ID", cm.exception.detail)

    def test_successful_connection_feeds_cash(self):
        self.http(STARLING_ROUTES)
        out = connections_api.create(institution="starling", method="0", label="", wrapper="ISA",
                                     credentials=json.dumps({"access_token": "good-token-0123456789"}),
                                     mapping="", currency="GBP", file=None)
        conn = out["connection"]
        self.assertEqual(conn["status"], "ok")
        self.assertEqual(conn["wrapper"], "")           # banks have no ISA box
        self.assertEqual(conn["credentials"]["token"], "••••6789")
        cash = connections.cash_entries()
        self.assertEqual([(c["name"], c["balance"], c["currency"], c["auto"]) for c in cash],
                         [("Starling Bank Personal", 1234.56, "GBP", True),
                          ("Starling Bank Euro", 50.0, "EUR", True)])
        listed = connections_api.list_connections()["connections"]
        self.assertNotIn("good-token-0123456789", json.dumps(listed))

    def test_generic_csv_connection_appears_in_holdings(self):
        up = UploadFile(file=io.BytesIO(SCHWAB_CSV.encode()), filename="positions.csv")
        out = connections_api.create(institution="hl", method="csv", label="HL", wrapper="ISA",
                                     credentials="", mapping="", currency="GBP", file=up)
        self.assertEqual(out["report"]["used"], 2)
        conn = out["connection"]
        self.assertEqual((conn["positions"], conn["wrapper"]), (2, "ISA"))
        with mock.patch.object(trading212, "get_holdings", return_value=[]), \
                mock.patch.object(sources.degiro, "get_holdings", return_value=[]), \
                mock.patch.object(sources.traderepublic, "get_holdings", return_value=[]):
            hs = sources.holdings()
        aapl = next(h for h in hs if h.ticker == "AAPL")
        self.assertEqual((aapl.broker, aapl.quantity, aapl.buy_price, aapl.wrapper), ("HL", 10, 150.0, "ISA"))
        self.assertIsNone(aapl.price)                   # live price for known symbols
        self.assertEqual(aapl.buy_currency, "USD")


# ------------------------------------------------------------ connectors
STARLING_ROUTES = {
    "/api/v2/accounts/a1/balance": {"clearedBalance": {"currency": "GBP", "minorUnits": 100000},
                                    "effectiveBalance": {"currency": "GBP", "minorUnits": 120000},
                                    "totalEffectiveBalance": {"currency": "GBP", "minorUnits": 123456}},
    "/api/v2/accounts/a2/balance": {"effectiveBalance": {"currency": "EUR", "minorUnits": 5000}},
    "/api/v2/accounts": {"accounts": [
        {"accountUid": "a1", "accountType": "PRIMARY", "currency": "GBP", "name": "Personal"},
        {"accountUid": "a2", "accountType": "ADDITIONAL", "currency": "EUR", "name": "Euro"}]},
}


class StarlingTest(TempStore):
    def test_balances(self):
        fake = self.http(STARLING_ROUTES)
        snap = starling.fetch({"token": "tok"})
        self.assertEqual(snap.cash, [{"name": "Personal", "currency": "GBP", "balance": 1234.56},
                                     {"name": "Euro", "currency": "EUR", "balance": 50.0}])
        self.assertEqual(fake.calls[0]["headers"]["Authorization"], "Bearer tok")

    def test_403_hint(self):
        self.http({"/api/v2/accounts": response(403)})
        with self.assertRaises(ConnectorError) as cm:
            starling.fetch({"token": "tok"})
        self.assertIn("balance:read", str(cm.exception))


WISE_BALANCES = [
    {"id": 1, "currency": "GBP", "type": "STANDARD", "name": None, "visible": True,
     "amount": {"value": 100.5, "currency": "GBP"}, "totalWorth": {"value": 100.5, "currency": "GBP"}},
    {"id": 2, "currency": "EUR", "type": "SAVINGS", "name": "Holiday", "visible": True,
     "amount": {"value": 20, "currency": "EUR"}, "totalWorth": {"value": 20, "currency": "EUR"}},
    {"id": 3, "currency": "USD", "type": "STANDARD", "visible": False,
     "amount": {"value": 9, "currency": "USD"}},
]


class WiseTest(TempStore):
    def test_balances_per_currency(self):
        self.http({"/2026Q4/profiles/7/balances": WISE_BALANCES,
                   "/2026Q4/profiles": [{"id": 7, "type": "PERSONAL"}]})
        snap = wise.fetch({"token": "t"})
        self.assertEqual(snap.cash, [
            {"name": "GBP", "currency": "GBP", "balance": 100.5, "type": "cash"},
            {"name": "Holiday (EUR)", "currency": "EUR", "balance": 20.0, "type": "savings"}])

    def test_falls_back_to_legacy_paths(self):
        fake = self.http({"/v4/profiles/7/balances": WISE_BALANCES[:1],
                          "/v2/profiles": [{"id": 7, "type": "personal"}]})
        snap = wise.fetch({"token": "t"})
        self.assertEqual(len(snap.cash), 1)
        self.assertTrue(any("/2026Q4/profiles" in c["url"] for c in fake.calls))


FLEX_OK = b"""<FlexStatementResponse timestamp="14 November, 2026 10:00 AM EST">
<Status>Success</Status><ReferenceCode>9876543210</ReferenceCode>
<Url>https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>
</FlexStatementResponse>"""
FLEX_WAIT = b"""<FlexStatementResponse><Status>Warn</Status><ErrorCode>1019</ErrorCode>
<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage></FlexStatementResponse>"""
FLEX_BAD_TOKEN = b"""<FlexStatementResponse><Status>Fail</Status><ErrorCode>1015</ErrorCode>
<ErrorMessage>Token is invalid.</ErrorMessage></FlexStatementResponse>"""
FLEX_REPORT = b"""<FlexQueryResponse queryName="Dashboard" type="AF"><FlexStatements count="1">
<FlexStatement accountId="U1234567" fromDate="20251114" toDate="20261113" period="Last365CalendarDays">
<AccountInformation accountId="U1234567" currency="GBP" />
<OpenPositions>
 <OpenPosition accountId="U1234567" currency="USD" assetCategory="STK" symbol="AAPL" description="APPLE INC"
   isin="US0378331005" listingExchange="NASDAQ" position="10" markPrice="230" positionValue="2300"
   costBasisPrice="150" costBasisMoney="1500" levelOfDetail="SUMMARY" />
 <OpenPosition accountId="U1234567" currency="USD" assetCategory="STK" symbol="AAPL" position="10"
   positionValue="2300" costBasisMoney="1500" levelOfDetail="LOT" />
 <OpenPosition accountId="U1234567" currency="GBP" assetCategory="STK" symbol="VWRL" description="VANGUARD FTSE ALL-WORLD"
   isin="IE00B3RBWM25" listingExchange="LSEETF" position="20" positionValue="2400" costBasisMoney="2000" levelOfDetail="SUMMARY" />
 <OpenPosition accountId="U1234567" currency="EUR" assetCategory="STK" symbol="XYZ" description="SOMEWHERE ELSE"
   isin="XX0000000001" listingExchange="ODDX" position="4" positionValue="40" costBasisMoney="44" levelOfDetail="SUMMARY" />
 <OpenPosition accountId="U1234567" currency="USD" assetCategory="OPT" symbol="AAPL 261218C00250000"
   position="1" positionValue="500" costBasisMoney="300" levelOfDetail="SUMMARY" />
</OpenPositions>
<Trades>
 <Trade accountId="U1234567" currency="USD" assetCategory="STK" symbol="AAPL" description="APPLE INC" isin="US0378331005"
   listingExchange="NASDAQ" dateTime="20260105;093000" tradeDate="20260105" quantity="10" tradePrice="150"
   proceeds="-1500" ibCommission="-1" netCash="-1501" buySell="BUY" levelOfDetail="EXECUTION" />
 <Trade accountId="U1234567" currency="USD" assetCategory="STK" symbol="MSFT" description="MICROSOFT" isin="US5949181045"
   listingExchange="NASDAQ" dateTime="2026-03-02;10:15:00" quantity="-5" tradePrice="400" proceeds="2000"
   ibCommission="-1" netCash="1999" buySell="SELL" levelOfDetail="EXECUTION" />
 <Trade accountId="U1234567" currency="USD" assetCategory="STK" symbol="MSFT" quantity="5" netCash="-2000"
   buySell="BUY (Ca.)" levelOfDetail="EXECUTION" />
</Trades>
<CashTransactions>
 <CashTransaction currency="USD" type="Dividends" symbol="AAPL" isin="US0378331005" amount="2.50" dateTime="20260515" levelOfDetail="DETAIL" />
 <CashTransaction currency="USD" type="Withholding Tax" symbol="AAPL" isin="US0378331005" amount="-0.38" dateTime="20260515" levelOfDetail="DETAIL" />
 <CashTransaction currency="GBP" type="Broker Interest Received" amount="4.10" dateTime="20260603;000000" levelOfDetail="DETAIL" />
 <CashTransaction currency="GBP" type="Deposits/Withdrawals" amount="5000" dateTime="20260101" levelOfDetail="DETAIL" />
</CashTransactions>
<CashReport>
 <CashReportCurrency accountId="U1234567" currency="BASE_SUMMARY" endingCash="812.4" levelOfDetail="BaseCurrency" />
 <CashReportCurrency accountId="U1234567" currency="GBP" endingCash="700" levelOfDetail="Currency" />
 <CashReportCurrency accountId="U1234567" currency="USD" endingCash="140.5" levelOfDetail="Currency" />
</CashReport>
</FlexStatement></FlexStatements></FlexQueryResponse>"""


class IbkrTest(TempStore):
    def test_two_step_download_waits_for_report(self):
        fake = self.http({"SendRequest": [response(raw=FLEX_OK)],
                          "GetStatement": [response(raw=FLEX_WAIT), response(raw=FLEX_REPORT)]})
        sleeps = []
        root = ibkr_flex.download("TOKEN123", "42", sleep=sleeps.append)
        self.assertEqual(root.tag, "FlexQueryResponse")
        self.assertEqual(sleeps, [2])
        self.assertEqual(fake.calls[0]["params"], {"t": "TOKEN123", "q": "42", "v": "3"})
        self.assertEqual(fake.calls[1]["params"]["q"], "9876543210")
        self.assertIn("User-Agent", fake.calls[0]["headers"])

    def test_error_codes_are_plain_and_hide_the_token(self):
        self.http({"SendRequest": [response(raw=FLEX_BAD_TOKEN)]})
        with self.assertRaises(ConnectorError) as cm:
            ibkr_flex.fetch({"token": "SECRETTOKEN", "query_id": "42"})
        self.assertIn("token is not valid", str(cm.exception))
        self.assertNotIn("SECRETTOKEN", str(cm.exception))
        self.assertFalse(cm.exception.pending)

    def test_still_generating_is_pending(self):
        self.http({"SendRequest": [response(raw=FLEX_OK)],
                   "GetStatement": lambda url, params: response(raw=FLEX_WAIT)})
        with self.assertRaises(ConnectorError) as cm:
            ibkr_flex.download("T", "42", max_wait=4, sleep=lambda s: None)
        self.assertTrue(cm.exception.pending)

    def test_parse_report(self):
        import xml.etree.ElementTree as ET

        snap = ibkr_flex.parse(ET.fromstring(FLEX_REPORT), "IBKR")
        hs = {h.name: h for h in snap.holdings}
        self.assertEqual(sorted(hs), ["APPLE INC", "SOMEWHERE ELSE", "VANGUARD FTSE ALL-WORLD"])  # no lot dupes, no options
        aapl = hs["APPLE INC"]
        self.assertEqual((aapl.ticker, aapl.quantity, aapl.buy_price, aapl.currency, aapl.price),
                         ("AAPL", 10, 150.0, "USD", None))
        self.assertEqual(hs["VANGUARD FTSE ALL-WORLD"].ticker, "VWRL.L")
        odd = hs["SOMEWHERE ELSE"]                      # unknown exchange: report price, no ticker
        self.assertEqual((odd.ticker, odd.price, odd.buy_price), ("", 10.0, 11.0))
        self.assertEqual([(t.side, t.quantity, t.amount, t.when) for t in snap.trades],
                         [("BUY", 10, 1501.0, "2026-01-05 09:30:00"), ("SELL", 5, 1999.0, "2026-03-02 10:15:00")])
        self.assertEqual([(d["amount"], d["date"]) for d in snap.dividends], [(2.5, "2026-05-15"), (-0.38, "2026-05-15")])
        self.assertEqual([(i["amount"], i["currency"]) for i in snap.interest], [(4.1, "GBP")])
        self.assertEqual(snap.cash, [{"name": "cash GBP", "currency": "GBP", "balance": 700.0},
                                     {"name": "cash USD", "currency": "USD", "balance": 140.5}])
        self.assertIn("3 positions", snap.summary)

    def test_query_id_must_be_a_number(self):
        with self.assertRaises(ConnectorError):
            ibkr_flex.fetch({"token": "t", "query_id": "abc"})

    def test_ticker_mapping(self):
        self.assertEqual(ibkr_flex.yf_ticker("BRK B", "NYSE", "USD"), "BRK-B")
        self.assertEqual(ibkr_flex.yf_ticker("RR.", "LSE", "GBP"), "RR.L")
        self.assertEqual(ibkr_flex.yf_ticker("ASML", "AEB", "EUR", "NL0010273215"), "ASML.AS")
        self.assertEqual(ibkr_flex.yf_ticker("X", "UNKNOWN", "EUR"), "")


class EtoroTest(TempStore):
    def test_positions_and_cash(self):
        fake = self.http({
            "/trading/info/portfolio": {"clientPortfolio": {"credit": 321.5, "positions": [
                {"positionID": 1, "instrumentID": 1001, "isBuy": True, "leverage": 1, "units": 2,
                 "initialAmountInDollars": 300, "unitsBaseValueDollars": 360},
                {"positionID": 2, "instrumentID": 1001, "isBuy": True, "leverage": 1, "units": 1,
                 "initialAmountInDollars": 160, "unitsBaseValueDollars": 180},
                {"positionID": 3, "instrumentID": 2002, "isBuy": True, "leverage": 5, "units": 3,
                 "initialAmountInDollars": 100, "unitsBaseValueDollars": 900}],
                "mirrors": [{"positions": [{"positionID": 4, "instrumentID": 3003, "isBuy": True, "leverage": 1,
                                            "units": 0.5, "initialAmountInDollars": 50, "unitsBaseValueDollars": 40}]}]}},
            "/market-data/instruments": {"instrumentDisplayDatas": [
                {"instrumentID": 1001, "instrumentDisplayName": "Apple", "symbolFull": "AAPL"}]},
        })
        etoro._META.clear()
        snap = etoro.fetch({"api_key": "pub", "user_key": "usr"})
        hs = {h.name: h for h in snap.holdings}
        self.assertEqual(sorted(hs), ["Apple", "eToro instrument 3003"])
        self.assertEqual((hs["Apple"].quantity, hs["Apple"].buy_price, hs["Apple"].price, hs["Apple"].currency),
                         (3, round(460 / 3, 6), 180.0, "USD"))
        self.assertEqual(snap.cash, [{"name": "cash USD", "currency": "USD", "balance": 321.5}])
        self.assertIn("1 leveraged or short left out", snap.summary)
        h = fake.calls[0]["headers"]
        self.assertEqual((h["x-api-key"], h["x-user-key"]), ("pub", "usr"))
        self.assertEqual(len(h["x-request-id"]), 36)


class BitvavoTest(TempStore):
    def test_balances_valued_in_eur(self):
        fake = self.http({
            "/v2/balance": [{"symbol": "EUR", "available": "250.10", "inOrder": "0"},
                            {"symbol": "BTC", "available": "0.5", "inOrder": "0.1"},
                            {"symbol": "DUST", "available": "0", "inOrder": "0"}],
            "/v2/ticker/price": [{"market": "BTC-EUR", "price": "60000"}, {"market": "ETH-EUR", "price": "3000"}],
        })
        snap = bitvavo.fetch({"api_key": "key", "api_secret": "secret"})
        self.assertEqual(snap.cash, [{"name": "cash EUR", "currency": "EUR", "balance": 250.1}])
        btc = snap.holdings[0]
        self.assertEqual((btc.ticker, round(btc.quantity, 6), btc.price, btc.buy_price), ("BTC-EUR", 0.6, 60000.0, 60000.0))
        h = fake.calls[0]["headers"]
        want = hmac.new(b"secret", (h["Bitvavo-Access-Timestamp"] + "GET/v2/balance").encode(),
                        hashlib.sha256).hexdigest()
        self.assertEqual(h["Bitvavo-Access-Signature"], want)


# --------------------------------------------------------------- generic CSV
SCHWAB_CSV = """"Positions for account Individual ...123 as of 11/14/2026"
""
"Symbol","Description","Quantity","Price","Market Value","Cost Basis","Currency"
"AAPL","APPLE INC","10","$230.00","$2,300.00","$1,500.00","USD"
"VWRL.L","VANGUARD FTSE ALL-WORLD","5","£120.00","£600.00","£500.00","GBP"
"Cash & Cash Investments","--","--","--","$1,000.00","--","USD"
"Account Total","","","","$3,900.00","",""
"""
DEGIRO_PORTFOLIO = """Product;Symbool/ISIN;Aantal;Slotkoers;Lokale waarde;;Waarde in EUR
ASML HOLDING;NL0010273215;3;685,20;EUR 2.055,60;;2.055,60
CASH & CASH FUND & FTX CASH (EUR);;;;EUR 12,30;;12,30
"""
HL_CSV = """Code,Stock,Units held,Price (pence),Value (£),Cost (£),Gain/loss (£)
VWRL,Vanguard FTSE All-World,"1,000",9850,"98,500.00","80,000.00","18,500.00"
"""


class GenericCsvTest(unittest.TestCase):
    def test_detects_us_layout(self):
        header, rows, _ = generic_csv.read_table(SCHWAB_CSV.encode())
        self.assertEqual(header[0], "Symbol")          # preamble lines skipped
        m = generic_csv.detect(header)
        self.assertEqual((m["symbol"], m["name"], m["quantity"], m["cost"], m["cost_kind"], m["value"],
                          m["price"], m["currency"]),
                         ("Symbol", "Description", "Quantity", "Cost Basis", "total", "Market Value",
                          "Price", "Currency"))

    def test_parse_total_cost_and_skip_cash_rows(self):
        m = generic_csv.detect(generic_csv.read_table(SCHWAB_CSV.encode())[0])
        holdings, report = generic_csv.parse(SCHWAB_CSV.encode(), m, "USD")
        self.assertEqual([(h["ticker"], h["quantity"], h["buy_price"], h["cost_currency"]) for h in holdings],
                         [("AAPL", 10, 150.0, "USD"), ("VWRL.L", 5, 100.0, "GBP")])
        self.assertEqual(report["skipped"], 2)

    def test_dutch_semicolon_file_with_isin(self):
        header, _, delim = generic_csv.read_table(DEGIRO_PORTFOLIO.encode())
        self.assertEqual(delim, ";")
        m = generic_csv.detect(header)
        self.assertEqual((m["isin"], m["quantity"], m["name"], m["price"]),
                         ("Symbool/ISIN", "Aantal", "Product", "Slotkoers"))
        holdings, _ = generic_csv.parse(DEGIRO_PORTFOLIO.encode(), m, "EUR")
        self.assertEqual(len(holdings), 1)
        asml = holdings[0]
        self.assertEqual((asml["isin"], asml["ticker"], asml["price"]), ("NL0010273215", "ASML.AS", 685.2))
        self.assertEqual(asml["buy_price"], 685.2)      # no cost column: cost = today's value

    def test_uk_layout_with_book_cost(self):
        m = generic_csv.detect(generic_csv.read_table(HL_CSV.encode())[0])
        self.assertEqual((m["symbol"], m["name"], m["quantity"], m["cost"], m["cost_kind"]),
                         ("Code", "Stock", "Units held", "Cost (£)", "total"))
        holdings, _ = generic_csv.parse(HL_CSV.encode(), m, "GBP")
        h = holdings[0]
        # a bare non-US symbol is not priced live (could be another listing): file value is used
        self.assertEqual((h["quantity"], h["buy_price"], h["ticker"], h["price"], h["price_currency"]),
                         (1000, 80.0, "", 98.5, "GBP"))

    def test_currency_from_value_prefix_and_pence(self):
        text = "Product;ISIN;Aantal;Lokale waarde\nAPPLE INC;US0378331005;2;USD 460,00\n"
        m = generic_csv.detect(generic_csv.read_table(text.encode())[0])
        h = generic_csv.parse(text.encode(), m, "EUR")[0][0]
        self.assertEqual((h["ticker"], h["cost_currency"], h["price"], h["price_currency"]),
                         ("AAPL", "USD", 230.0, "USD"))
        gbx = "Symbol,Quantity,Price,Avg cost,Currency\nRMV.L,100,600,550,GBX\n"
        m = generic_csv.detect(generic_csv.read_table(gbx.encode())[0])
        h = generic_csv.parse(gbx.encode(), m, "GBP")[0][0]
        self.assertEqual((h["price"], h["buy_price"], h["cost_currency"]), (6.0, 5.5, "GBP"))

    def test_numbers(self):
        n = generic_csv.number
        self.assertEqual(n("£1,234.56"), 1234.56)
        self.assertEqual(n("1.234,56"), 1234.56)
        self.assertEqual(n("12,5", decimal_comma=True), 12.5)
        self.assertEqual(n("(12.50)"), -12.5)
        self.assertEqual(n("1,000"), 1000)
        self.assertIsNone(n("--"))

    def test_rejects_excel(self):
        with self.assertRaises(ValueError):
            generic_csv.read_table(b"PK\x03\x04 not really a zip")

    def test_preview_reports_mapping_errors(self):
        p = generic_csv.preview(SCHWAB_CSV.encode(), {"quantity": None})
        self.assertIn("quantity", p["error"])
        self.assertEqual(p["holdings"], [])


# ----------------------------------------------------------- integration
class IntegrationTest(TempStore):
    def _ibkr(self, wrapper="ISA"):
        import xml.etree.ElementTree as ET

        inst = {"id": "ibkr", "name": "Interactive Brokers"}
        conn = connections.add(connections.new(inst, "api", "ibkr_flex", "IBKR ISA", wrapper,
                                               {"token": "t" * 20, "query_id": "1"}))
        connections.remember(conn, ibkr_flex.parse(ET.fromstring(FLEX_REPORT), conn["label"]))
        return conn

    def test_sources_holdings_and_trades_respect_isa(self):
        self._ibkr()
        hs = connections.holdings()
        self.assertTrue(hs and all(h.broker == "IBKR ISA" and h.wrapper == "ISA" for h in hs))
        ts = connections.trade_sources()
        self.assertEqual(list(ts), ["IBKR ISA"])
        is_isa, get = ts["IBKR ISA"]
        self.assertTrue(is_isa)
        self.assertEqual(len(get()), 2)
        with mock.patch.object(trading212, "accounts", return_value=[]), \
                mock.patch.object(trading212, "_statement", return_value=None):
            self.assertIn("IBKR ISA", sources.trade_sources())

    def test_cash_and_income(self):
        self._ibkr(wrapper="")
        cash = {c["name"]: c for c in connections.cash_entries()}
        self.assertEqual(set(cash), {"IBKR ISA cash GBP", "IBKR ISA cash USD"})
        self.assertTrue(all(c["auto"] for c in cash.values()))
        from backend import cash as cash_mod

        with mock.patch.object(cash_mod, "_broker_cash", return_value=[]):
            loaded = cash_mod.load()
        self.assertIn("IBKR ISA cash GBP", [a["name"] for a in loaded["accounts"]])
        inc = connections.income_by_account()["IBKR ISA"]
        self.assertEqual(inc, {"dividends": round((2.5 - 0.38) * 0.9, 2), "interest": round(4.1 * 1.2, 2)})
        self.assertFalse(connections.get_dividends()[0]["isa"])

    def test_failing_institution_never_raises_into_dashboard(self):
        inst = {"id": "starling", "name": "Starling"}
        conn = connections.add(connections.new(inst, "api", "starling", "Starling", "", {"token": "x"}))
        self.http({"/api/v2/accounts": response(500)})
        self.assertEqual(connections.cash_entries(), [])
        stored = connections.get(conn["id"])
        self.assertEqual(stored["status"], "error")
        self.assertIn("having problems", stored["error"])

    def test_in_app_trading212_key_feeds_the_t212_connector(self):
        inst = {"id": "trading212", "name": "Trading 212"}
        connections.add(connections.new(inst, "api", "trading212", "T212 Stocks", "ISA",
                                        {"api_key": "k-inapp", "api_secret": "s-inapp"}))
        with mock.patch.dict(os.environ, {"T212_INVEST_API_KEY": "k2", "T212_INVEST_API_SECRET": "s2"}, clear=True):
            accts = {a.label: a for a in trading212.accounts()}
        self.assertEqual(set(accts), {"T212 Invest", "T212 Stocks"})
        self.assertTrue(accts["T212 Stocks"].is_isa)   # from the tick box, not the label
        self.assertFalse(accts["T212 Invest"].is_isa)
        # its cash is reported by trading212.get_cash(), so not duplicated here
        self.assertEqual(connections.cash_entries(), [])


# ---------------------------------------------------------------- catalog
class CatalogTest(unittest.TestCase):
    def test_every_institution_has_a_usable_default(self):
        view = catalog.view()
        self.assertGreater(len(view["institutions"]), 9)
        for inst in view["institutions"]:
            m = catalog.method(inst, inst["default"])
            self.assertIsNotNone(m, inst["id"])
            self.assertIn(m["support"], catalog.ACTIONABLE, inst["id"])
            self.assertTrue(any(x["type"] == "manual" for x in inst["methods"]), inst["id"])

    def test_seed_institutions_link_to_code(self):
        want = {"trading212": "trading212", "ibkr": "ibkr_flex", "starling": "starling",
                "wise": "wise", "etoro": "etoro", "bitvavo": "bitvavo"}
        for inst_id, cid in want.items():
            inst = catalog.find(inst_id)
            live = [m for m in inst["methods"] if m["support"] == "live"]
            self.assertEqual([m["connector"] for m in live], [cid], inst_id)
            names = [f["name"] for f in live[0]["fields"]]
            self.assertEqual(names, [f["name"] for f in registry.connector(cid).fields])
        kinds = {i: [m.get("upload_kind") for m in catalog.find(i)["methods"] if m["support"] == "import"]
                 for i in ("degiro", "traderepublic", "trading212")}
        self.assertEqual(kinds, {"degiro": ["degiro"], "traderepublic": ["trade_republic"],
                                 "trading212": ["t212_statement"]})

    def test_link_rules(self):
        t212 = {"id": "Trading-212", "kind": "broker"}
        self.assertEqual(registry.link(t212, {"type": "upload", "file_format": "csv",
                                              "report_name": "History export (CSV)",
                                              "covers": ["transactions"]})["support"], "info")
        self.assertEqual(registry.link(t212, {"type": "upload", "file_format": "pdf",
                                              "report_name": "Activity statement"})["upload_kind"], "t212_statement")
        self.assertEqual(registry.link({"id": "x"}, {"type": "upload", "file_format": "csv",
                                                     "report_name": "Portfolio positions"})["support"], "csv")
        self.assertEqual(registry.link({"id": "x"}, {"type": "aggregator", "provider": "enablebanking"})["support"],
                         "info")
        self.assertEqual(registry.link({"id": "x"}, {"type": "unofficial", "library": "pytr"})["support"], "info")

    def test_unverified_and_aggregators_pass_through(self):
        view = catalog.view()
        self.assertTrue(any(m.get("verified") is False for i in view["institutions"] for m in i["methods"]))
        rabo = catalog.find("rabobank")
        agg = [m for m in rabo["methods"] if m["type"] == "aggregator"]
        self.assertTrue(agg and agg[0]["aggregator"]["name"])


if __name__ == "__main__":
    unittest.main()
