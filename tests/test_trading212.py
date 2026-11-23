"""Trading 212 connector against canned API responses (no network).

Run: .venv/bin/python -m unittest discover -s tests -t .
"""

import os
import unittest
from unittest import mock

import requests

from backend import cache, fx
from backend.connectors import trading212 as t212
from backend.positions import compute_positions, realized_events

RATES = {"EUR": 1.0, "GBP": 1.2, "USD": 0.9}

POSITIONS = [
    {"instrument": {"ticker": "VWRPl_EQ", "name": "Vanguard FTSE All-World", "isin": "IE00BK5BQT80", "currency": "GBP"},
     "quantity": 10, "averagePricePaid": 110.0, "currentPrice": 120.0,
     "walletImpact": {"currency": "GBP", "totalCost": 1100, "currentValue": 1200, "unrealizedProfitLoss": 100, "fxImpact": None}},
    {"instrument": {"ticker": "RMVl_EQ", "name": "Rightmove", "isin": "GB00BGDT3G23", "currency": "GBX"},
     "quantity": 400, "averagePricePaid": 600.0, "currentPrice": 625.0,
     "walletImpact": {"currency": "GBP", "totalCost": 2400, "currentValue": 2500, "unrealizedProfitLoss": 100}},
    {"instrument": {"ticker": "GOOGL_US_EQ", "name": "Alphabet", "isin": "US02079K3059", "currency": "USD"},
     "quantity": 5, "averagePricePaid": 300, "currentPrice": 346,
     "walletImpact": {"currency": "GBP", "totalCost": 1150, "currentValue": 1290, "unrealizedProfitLoss": 140, "fxImpact": -5}},
]

ORDERS_P1 = {"items": [
    {"order": {"status": "FILLED", "side": "BUY", "ticker": "GOOGL_US_EQ", "createdAt": "2026-05-01T10:00:00Z",
               "instrument": {"ticker": "GOOGL_US_EQ", "isin": "US02079K3059", "name": "Alphabet", "currency": "USD"}},
     "fill": {"filledAt": "2026-05-01T10:00:01Z", "quantity": 4, "price": 300,
              "walletImpact": {"currency": "GBP", "netValue": 998.5, "fxRate": 0.8,
                               "taxes": [{"name": "CURRENCY_CONVERSION_FEE", "quantity": -1.5, "currency": "GBP"}]}}},
    {"order": {"status": "CANCELLED", "side": "BUY", "ticker": "AAPL_US_EQ"}, "fill": None},
], "nextPagePath": "/api/v0/equity/history/orders?limit=50&cursor=111"}
ORDERS_P2 = {"items": [
    {"order": {"status": "FILLED", "side": "SELL", "ticker": "GOOGL_US_EQ", "createdAt": "2026-11-05T15:00:00Z",
               "instrument": {"ticker": "GOOGL_US_EQ", "isin": "US02079K3059", "name": "Alphabet", "currency": "USD"}},
     "fill": {"filledAt": "2026-11-05T15:00:01Z", "quantity": -4, "price": 280,
              "walletImpact": {"currency": "GBP", "netValue": 881.3,
                               "taxes": [{"name": "CURRENCY_CONVERSION_FEE", "quantity": -1.3, "currency": "GBP"}]}}},
], "nextPagePath": None}

DIVIDENDS = {"items": [{"ticker": "VWRPl_EQ", "amount": 12.5, "amountInEuro": 14.4, "currency": "GBP",
                        "paidOn": "2026-09-20T00:00:00Z", "instrument": {"isin": "IE00BK5BQT80"}}],
             "nextPagePath": None}
TRANSACTIONS = {"items": [
    {"type": "DEPOSIT", "amount": 15000, "currency": "GBP", "dateTime": "2026-10-20T09:00:00Z"},
    {"type": "INTEREST_ON_FREE_CASH", "amount": 3.21, "currency": "GBP", "dateTime": "2026-10-31T00:00:00Z"},
], "nextPagePath": None}


def http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status}", response=resp)


def fake_get(routes: dict):
    calls = []

    def _get(acct, path):
        calls.append(path)
        for prefix, value in routes.items():
            if path.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        raise http_error(404)

    _get.calls = calls
    return _get


def clear_caches():
    for fn in (t212.account_currency, t212.account_summary, t212.account_holdings,
               t212.account_trades, t212.account_dividends, t212.account_transactions,
               t212._same_account_as_statement, t212._reads_positions):
        fn.cache_clear()


ENV = {"T212_ISA_API_KEY": "k1", "T212_ISA_API_SECRET": "s1",
       "T212_INVEST_API_KEY": "k2", "T212_INVEST_API_SECRET": "s2"}


class Base(unittest.TestCase):
    def setUp(self):
        clear_caches()
        cache.clear()
        for p in (mock.patch.object(fx, "_live_rates", return_value=RATES),
                  # a local data/trading212_statement.json must not leak into these tests
                  mock.patch.object(t212, "_statement", return_value=None)):
            p.start()
            self.addCleanup(p.stop)


class FxTest(Base):
    def test_pence(self):
        self.assertEqual(fx.unit("GBX"), (0.01, "GBP"))
        self.assertEqual(fx.unit("GBp"), (0.01, "GBP"))
        self.assertEqual(fx.unit("GBP"), (1.0, "GBP"))
        self.assertAlmostEqual(fx.to_eur(600, "GBX"), 6 * 1.2)
        self.assertAlmostEqual(fx.from_eur(12, "GBP"), 10)


class AccountsTest(unittest.TestCase):
    def setUp(self):
        # only the environment counts here, not accounts added in Settings
        p = mock.patch("backend.connections.t212_accounts", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def test_labels_from_env(self):
        env = {**ENV, "T212_GIA_API_KEY": "no-secret"}
        with mock.patch.dict(os.environ, env, clear=True):
            accts = t212.accounts()
        self.assertEqual([a.label for a in accts], ["T212 Invest", "T212 ISA"])
        self.assertEqual([a.is_isa for a in accts], [False, True])

    def test_plain_key_and_name_override(self):
        env = {"T212_API_KEY": "k", "T212_API_SECRET": "s",
               "T212_MY_ISA_API_KEY": "k", "T212_MY_ISA_API_SECRET": "s", "T212_MY_ISA_NAME": "Stocks ISA"}
        with mock.patch.dict(os.environ, env, clear=True):
            labels = {a.label: a.is_isa for a in t212.accounts()}
        self.assertEqual(labels, {"Trading212": False, "Stocks ISA": True})


class TickerTest(unittest.TestCase):
    def test_yf_ticker(self):
        self.assertEqual(t212.yf_ticker("AAPL_US_EQ"), "AAPL")
        self.assertEqual(t212.yf_ticker("BRK_B_US_EQ"), "BRK-B")
        self.assertEqual(t212.yf_ticker("VWRPl_EQ"), "VWRP.L")
        self.assertEqual(t212.yf_ticker("ZPRRd_EQ"), "ZPRR.DE")
        self.assertEqual(t212.yf_ticker("MCp_EQ"), "MC.PA")
        self.assertEqual(t212.yf_ticker("FB_US_EQ"), "META")
        # the ISIN table wins over symbology
        self.assertEqual(t212.yf_ticker("ASMLa_EQ", "NL0010273215"), "ASML.AS")


class HoldingsTest(Base):
    def test_positions_endpoint(self):
        acct = t212.Account("T212 ISA", "k", "s")
        with mock.patch.object(t212, "_get", fake_get({"/equity/positions": POSITIONS})):
            hs = {h.ticker: h for h in t212.account_holdings(acct)}
        self.assertEqual(set(hs), {"VWRP.L", "RMV.L", "GOOGL"})
        rmv = hs["RMV.L"]
        self.assertEqual((rmv.currency, rmv.price, rmv.buy_price), ("GBP", 6.25, 6.0))  # pence -> pounds
        self.assertAlmostEqual(rmv.broker_profit_eur, 100 * 1.2)                       # GBP P/L -> EUR
        self.assertEqual(rmv.wrapper, "ISA")
        self.assertEqual(hs["GOOGL"].currency, "USD")

    def test_legacy_portfolio_fallback(self):
        acct = t212.Account("Trading212", "k", "s")
        legacy = [{"ticker": "AAPL_US_EQ", "quantity": 2, "averagePrice": 150, "currentPrice": 200,
                   "ppl": 80, "fxPpl": -5}]
        routes = {"/equity/positions": http_error(404), "/equity/portfolio": legacy,
                  "/equity/account/summary": {"currency": "EUR"}}
        with mock.patch.object(t212, "_get", fake_get(routes)), \
             mock.patch.object(t212, "_instrument_meta", return_value={
                 "AAPL_US_EQ": {"name": "Apple", "isin": "US0378331005", "currencyCode": "USD"}}):
            (h,) = t212.account_holdings(acct)
        self.assertEqual((h.ticker, h.price, h.broker_profit_eur, h.wrapper), ("AAPL", 200.0, 75.0, ""))

    def test_server_error_is_not_swallowed(self):
        acct = t212.Account("T212 ISA", "k", "s")
        with mock.patch.object(t212, "_get", fake_get({"/equity/positions": http_error(401)})):
            with self.assertRaises(requests.HTTPError):
                t212.account_holdings(acct)


class FailingAccountTest(Base):
    def test_one_bad_key_keeps_the_others(self):
        good, bad = t212.Account("T212 ISA", "good", "s"), t212.Account("Trading212", "bad", "s")

        def _get(acct, path):
            if acct.key == "bad":
                raise http_error(401)
            return {"currency": "GBP"} if path.startswith("/equity/account") else POSITIONS

        with mock.patch.object(t212, "_get", _get), \
             mock.patch.object(t212, "accounts", return_value=[bad, good]):
            self.assertEqual(len(t212.get_holdings()), 3)
            status = {r["label"]: r for r in t212.status()}
        self.assertFalse(status["Trading212"]["ok"])
        self.assertIn("401", status["Trading212"]["error"])


class HistoryTest(Base):
    def test_orders_paginate_and_fold_fees(self):
        acct = t212.Account("T212 Invest", "k", "s")
        get = fake_get({"/equity/history/orders?limit=50&cursor": ORDERS_P2,
                        "/api/v0/equity/history/orders?limit=50&cursor": ORDERS_P2,
                        "/equity/history/orders": ORDERS_P1})
        with mock.patch.object(t212, "_get", get):
            trades = t212.account_trades(acct)
        self.assertEqual(len(trades), 2)
        buy, sell = trades
        self.assertEqual((buy.side, buy.quantity, buy.amount, buy.currency), ("BUY", 4, 1000.0, "GBP"))
        self.assertEqual((sell.side, sell.quantity, sell.amount), ("SELL", 4, 880.0))
        self.assertEqual(buy.ticker, "GOOGL")
        (ev,) = realized_events(trades)
        self.assertAlmostEqual(ev["realized"], -120.0)
        self.assertAlmostEqual(compute_positions(trades)["US02079K3059"].realized, -120.0)

    def test_dividends_and_interest(self):
        acct = t212.Account("T212 Invest", "k", "s")
        routes = {"/equity/history/dividends": DIVIDENDS, "/equity/history/transactions": TRANSACTIONS}
        with mock.patch.object(t212, "_get", fake_get(routes)), \
             mock.patch.object(t212, "accounts", return_value=[acct]):
            (d,) = t212.get_dividends()
            interest = t212.get_interest()
        self.assertEqual((d["date"], d["amount"], d["currency"]), ("2026-09-20", 12.5, "GBP"))
        self.assertAlmostEqual(d["amount_eur"], 15.0)
        self.assertEqual([round(i["amount_eur"], 2) for i in interest], [3.85])

    def test_old_dividends_path(self):
        acct = t212.Account("T212 Invest", "k", "s")
        get = fake_get({"/equity/history/dividends": http_error(404), "/history/dividends": DIVIDENDS})
        with mock.patch.object(t212, "_get", get):
            self.assertEqual(len(t212.account_dividends(acct)), 1)


# an imported statement for a closed EU account (all positions sold)
CLOSED_STATEMENT = {
    "positions": [],
    "trades": [
        {"date": "2024-03-01", "isin": "NL0010273215", "symbol": "ASML", "name": "ASML",
         "side": "buy", "quantity": 2, "amount_eur": 1200.0},
        {"date": "2025-06-01", "isin": "NL0010273215", "symbol": "ASML", "name": "ASML",
         "side": "sell", "quantity": 2, "amount_eur": 1300.0},
    ],
    "dividends": [{"date": "2024-08-01", "isin": "NL0010273215", "symbol": "ASML", "amount_eur": 3.0}],
    "transactions": [{"date": "2024-09-01", "kind": "interest", "amount_eur": 2.5}],
}


class ClosedAccountTest(Base):
    """A closed account's statement stays next to the live accounts."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(t212, "_statement", return_value=CLOSED_STATEMENT)
        p.start()
        self.addCleanup(p.stop)
        self.acct = t212.Account("T212 Invest", "k", "s")
        routes = {"/equity/positions": POSITIONS, "/equity/history/orders": {"items": []},
                  "/equity/history/dividends": DIVIDENDS, "/equity/history/transactions": TRANSACTIONS,
                  "/equity/account/info": {"currencyCode": "GBP"}}
        for p in (mock.patch.object(t212, "_get", fake_get(routes)),
                  mock.patch.object(t212, "accounts", return_value=[self.acct])):
            p.start()
            self.addCleanup(p.stop)

    def test_history_is_kept_with_live_accounts(self):
        self.assertEqual(set(t212.trade_sources()), {"T212 Invest", "Trading212"})
        statement_trades = t212.trade_sources()["Trading212"][1]()
        self.assertEqual([t.side for t in statement_trades], ["buy", "sell"])
        self.assertEqual({d["account"] for d in t212.get_dividends()}, {"Trading212", "T212 Invest"})
        self.assertIn(("Trading212", 2.5), {(i["account"], i["amount_eur"]) for i in t212.get_interest()})
        # closed: nothing held there, the live positions are all still shown
        self.assertEqual({h.broker for h in t212.get_holdings()}, {"T212 Invest"})
        self.assertEqual(len(t212.get_holdings()), len(POSITIONS))

    def test_label_clash_with_a_plain_live_key(self):
        plain = t212.Account("Trading212", "k", "s")
        with mock.patch.object(t212, "accounts", return_value=[plain]):
            self.assertEqual(set(t212.trade_sources()), {"Trading212", "Trading212 (statement)"})


def _order(day: str, side: str, qty: float, net: float) -> dict:
    return {"order": {"status": "FILLED", "side": side, "ticker": "ASML_EQ", "createdAt": day + "T10:00:00Z",
                      "instrument": {"ticker": "ASML_EQ", "isin": "NL0010273215", "name": "ASML", "currency": "EUR"}},
            "fill": {"filledAt": day + "T10:00:01Z", "quantity": qty if side == "BUY" else -qty,
                     "walletImpact": {"currency": "EUR", "netValue": net}}}


SAME_STATEMENT = {**CLOSED_STATEMENT, "trades": CLOSED_STATEMENT["trades"] + [
    {"date": "2025-01-10", "isin": "NL0010273215", "symbol": "ASML", "name": "ASML",
     "side": "buy", "quantity": 1, "amount_eur": 650.0}]}
SAME_ORDERS = {"items": [_order("2024-03-01", "BUY", 2, 1200.0), _order("2025-01-10", "BUY", 1, 650.0),
                         _order("2025-06-01", "SELL", 3, 1950.0)], "nextPagePath": None}


class SameAccountTest(Base):
    """An old key and an uploaded statement for the same account are counted once."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(t212, "_statement", return_value=SAME_STATEMENT)
        p.start()
        self.addCleanup(p.stop)
        self.old = t212.Account("Trading212", "k", "s")

    def run_with(self, positions):
        routes = {"/equity/positions": positions, "/equity/history/orders": SAME_ORDERS,
                  "/equity/history/dividends": DIVIDENDS, "/equity/history/transactions": TRANSACTIONS,
                  "/equity/account/info": {"currencyCode": "EUR"}}
        with mock.patch.object(t212, "_get", fake_get(routes)), \
                mock.patch.object(t212, "accounts", return_value=[self.old]):
            return (set(t212.trade_sources()), {d["account"] for d in t212.get_dividends()},
                    {i["account"] for i in t212.get_interest()}, t212.replaced_by_statement())

    def test_closed_account_key_gives_way_to_the_statement(self):
        sources, divs, interest, replaced = self.run_with(http_error(403))
        self.assertEqual(sources, {"Trading212"})
        self.assertEqual(divs, {"Trading212"})
        self.assertEqual(replaced, ["Trading212"])
        with mock.patch.object(t212, "accounts", return_value=[self.old]), \
                mock.patch.object(t212, "_get", fake_get({"/equity/history/orders": SAME_ORDERS,
                                                          "/equity/positions": http_error(403)})):
            trades = t212.trade_sources()["Trading212"][1]()
        self.assertEqual(len(trades), 3)  # the statement's three, not six

    def test_live_key_makes_the_statement_redundant(self):
        sources, divs, interest, replaced = self.run_with([])
        self.assertEqual(sources, {"Trading212"})
        self.assertEqual(replaced, [])
        self.assertEqual(interest, {"Trading212"})


if __name__ == "__main__":
    unittest.main()
