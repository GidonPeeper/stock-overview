"""Trading 212 history CSV and Trade Republic statement PDF importers (made-up data)."""

import contextlib
import unittest
from unittest import mock

from backend import fx
from backend.importers import traderepublic_pdf, trading212_csv

HEADER = ("Action,Time (UTC),ISIN,Ticker,Name,Notes,ID,No. of shares,Price / share,"
          "Currency (Price / share),Exchange rate,Result,Currency (Result),Total,Currency (Total),"
          "Withholding tax,Currency (Withholding tax),Currency conversion fee,"
          "Currency (Currency conversion fee),Merchant name,Merchant category\n")


def t212_csv(*rows: str) -> str:
    return HEADER + "\n".join(rows) + "\n"


EARLIER = {   # an activity statement up to 2026-09-03 with one open position
    "overview": {"period": ["2025-05-20", "2026-09-03"]},
    "trades": [{"date": "2025-06-01 10:00", "symbol": "META", "isin": "US30303M1027", "name": "Meta",
                "ticker": "META", "side": "BUY", "quantity": 4.0, "amount_eur": 2000.0,
                "fx_fee_eur": 3.0, "return_eur": None}],
    "positions": [{"symbol": "META", "isin": "US30303M1027", "name": "Meta", "ticker": "META",
                   "quantity": 4.0, "value_eur": 2400.0, "return_eur": 400.0}],
    "cash": {"total_eur": 10.0}, "transactions": [], "dividends": [], "warnings": [],
    "_source": "Trading 212 activity statement",
}

LATER = t212_csv(
    # overlaps the earlier statement: must not be added twice
    "Market buy,2025-06-01 10:00:00+00:00,US30303M1027,META,Meta,,EOF1,4.0,550,USD,1.1,,,2000.00,EUR,,,3.00,EUR,,",
    "Interest on cash,2026-09-02 00:10:00+00:00,,,,,i1,,,,,,,0.50,EUR,,,,,,",
    # after it: a partial sell, a dividend, interest in GBP and a card payment
    "Market sell,2026-09-09 14:00:00+00:00,US30303M1027,META,Meta,,EOF2,1.0,650,USD,1.16,60.00,EUR,560.00,EUR,,,0.80,EUR,,",
    "Dividend (Dividend),2026-09-10 11:00:00+00:00,US30303M1027,META,Meta,,,3.0,0.5,USD,,,,1.20,EUR,0.20,USD,,,,",
    "Interest on cash,2026-09-11 00:10:00+00:00,,,,,i2,,,,,,,0.10,GBP,,,,,,",
    "Card debit,2026-09-12 12:00:00+00:00,,,,,c1,,,,,,,-5.00,GBP,,,,,Shop,Food",
)


class Trading212CsvTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(fx, "_live_rates", return_value={"EUR": 1.0, "GBP": 1.2, "USD": 0.9})
        p.start()
        self.addCleanup(p.stop)

    def test_convert(self):
        st, report = trading212_csv.convert(LATER)
        self.assertEqual(st["overview"]["period"], ["2025-06-01", "2026-09-12"])
        buy, sell = st["trades"]
        self.assertEqual((buy["side"], buy["amount_eur"], buy["fx_fee_eur"]), ("BUY", 2000.0, 3.0))
        self.assertEqual((sell["side"], sell["amount_eur"], sell["return_eur"]), ("SELL", 560.0, 60.0))
        self.assertEqual(st["dividends"][0]["amount_eur"], 1.2)
        kinds = [(t["kind"], t["amount_eur"]) for t in st["transactions"]]
        self.assertEqual(kinds, [("interest", 0.5), ("interest", 0.12), ("withdrawal", -6.0)])
        # 4 bought, 1 sold: 3 left, cost shrinks with the sale
        (pos,) = st["positions"]
        self.assertAlmostEqual(pos["quantity"], 3.0)
        self.assertAlmostEqual(pos["value_eur"] - pos["return_eur"], 1500.0)
        self.assertEqual(report["warnings"], [])

    def test_merge_adds_only_later_rows_and_carries_positions(self):
        later, _ = trading212_csv.convert(LATER)
        st, report = trading212_csv.merge(EARLIER, later)
        self.assertEqual(report["merged_after"], "2026-09-03")
        self.assertEqual(report["added"], {"trades": 1, "dividends": 1, "transactions": 2})
        self.assertEqual([t["side"] for t in st["trades"]], ["BUY", "SELL"])
        self.assertEqual(st["overview"]["period"], ["2025-05-20", "2026-09-12"])
        # the statement's own cost (2400 - 400 = 2000 for 4) is kept: 3 left cost 1500
        (pos,) = st["positions"]
        self.assertAlmostEqual(pos["quantity"], 3.0)
        self.assertAlmostEqual(pos["value_eur"] - pos["return_eur"], 1500.0)
        self.assertEqual(st["cash"], {})

    def test_closing_the_account_leaves_no_positions(self):
        later, _ = trading212_csv.convert(t212_csv(
            "Market sell,2026-09-14 09:00:00+00:00,US30303M1027,META,Meta,,EOF3,4.0,650,USD,1.16,"
            "200.00,EUR,2200.00,EUR,,,3.00,EUR,,"))
        st, _ = trading212_csv.merge(EARLIER, later)
        self.assertEqual(st["positions"], [])

    def test_sell_without_buy_is_flagged(self):
        st, report = trading212_csv.convert(t212_csv(
            "Market sell,2026-09-14 09:00:00+00:00,US30303M1027,META,Meta,,EOF3,4.0,650,USD,1.16,"
            "200.00,EUR,2200.00,EUR,,,,,,"))
        self.assertIn("US30303M1027", report["warnings"][0])
        self.assertEqual(st["positions"], [])

    def test_rejects_other_csv(self):
        self.assertFalse(trading212_csv.looks_like("Symbol,Quantity\nAAPL,1\n"))
        with self.assertRaises(ValueError):
            trading212_csv.convert("Symbol,Quantity\nAAPL,1\n")


    def test_number_that_is_not_a_number_stops_the_import(self):
        # "1,000.00" in Total (a thousands comma): before, it silently became
        # 0, so a 1000 EUR buy cost nothing. Now the import stops and says so.
        bad = t212_csv("Market buy,2026-09-14 09:00:00+00:00,US30303M1027,META,Meta,,EOF3,2.0,500,"
                       "USD,1.1,,,\"1,000.00\",EUR,,,,,,")
        with self.assertRaisesRegex(ValueError, "Total"):
            trading212_csv.convert(bad)

    def test_rows_that_move_shares_without_a_trade_are_flagged(self):
        # 4 bought, then a 2-for-1 split adds 4 more: the split row is not a
        # buy or sell, so the holdings would show 4 instead of 8. Before: no
        # warning at all ("all checks passed").
        st, report = trading212_csv.convert(t212_csv(
            "Market buy,2026-09-01 09:00:00+00:00,US30303M1027,META,Meta,,B1,4.0,500,USD,1.1,,,"
            "1800.00,EUR,,,,,,",
            "Stock split open,2026-09-20 09:00:00+00:00,US30303M1027,META,Meta,,S1,4.0,250,USD,,,,"
            ",EUR,,,,,,"))
        self.assertTrue(any("Stock split open" in w for w in report["warnings"]))
        self.assertFalse(st["all_checks_ok"])


# ---- Trading 212 uploads: a new file is joined to the saved statement ----
BASE = {   # an activity statement for 2025 (AAPL was bought in 2024, before it starts)
    "overview": {"period": ["2025-01-01", "2025-12-31"]},
    "trades": [{"date": "2025-03-01 10:00", "symbol": "META", "isin": "US30303M1027", "name": "Meta",
                "ticker": "META", "side": "BUY", "quantity": 4.0, "amount_eur": 2000.0, "return_eur": None},
               {"date": "2025-11-01 10:00", "symbol": "META", "isin": "US30303M1027", "name": "Meta",
                "ticker": "META", "side": "SELL", "quantity": 1.0, "amount_eur": 600.0, "return_eur": 100.0}],
    "positions": [{"symbol": "META", "isin": "US30303M1027", "name": "Meta", "ticker": "META",
                   "quantity": 3.0, "value_eur": 1800.0, "return_eur": 300.0},
                  {"symbol": "AAPL", "isin": "US0378331005", "name": "Apple", "ticker": "AAPL",
                   "quantity": 2.0, "value_eur": 380.0, "return_eur": 80.0}],
    "cash": {}, "transactions": [], "dividends": [], "warnings": [],
    "_source": "Trading 212 activity statement",
}
AAPL_2024 = ("Market buy,2024-06-01 10:00:00+00:00,US0378331005,AAPL,Apple,,A1,2.0,165,USD,1.1,,,"
             "300.00,EUR,,,,,,")
META_2025 = ("Market buy,2025-03-01 10:00:00+00:00,US30303M1027,META,Meta,,M1,4.0,550,USD,1.1,,,"
             "2000.00,EUR,,,,,,")


class Trading212UploadTest(unittest.TestCase):
    """POST /api/upload/t212_statement with a statement already saved."""

    def setUp(self):
        import json
        import os
        import tempfile
        from pathlib import Path

        from starlette.testclient import TestClient

        from backend import datafiles, github_sync, main

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        for p in (mock.patch.object(datafiles, "_SEARCH_DIRS", [self.data]),
                  mock.patch.object(datafiles, "DATA_DIR", self.data),
                  mock.patch.object(github_sync, "push_vault_async", lambda *a, **k: None),
                  mock.patch.object(main, "_DASH_PASS", ""),
                  mock.patch.object(fx, "_live_rates", return_value={"EUR": 1.0, "GBP": 1.2, "USD": 0.9}),
                  mock.patch("backend.connections.t212_accounts", return_value=[]),
                  mock.patch.dict(os.environ, {k: "" for k in os.environ if k.startswith("T212_")})):
            p.start()
            self.addCleanup(p.stop)
        datafiles.refresh()
        self.addCleanup(datafiles.refresh)
        (self.data / "trading212_statement.json").write_text(json.dumps(BASE))
        self.client = TestClient(main.app, base_url="http://localhost:8000")
        self.json = json

    def upload(self, csv_text: str):
        return self.client.post("/api/upload/t212_statement",
                                files={"file": ("export.csv", csv_text.encode(), "text/csv")})

    def saved(self) -> dict:
        return self.json.loads((self.data / "trading212_statement.json").read_text())

    def test_an_older_export_is_joined_not_swapped_in(self):
        # The 2024 export (2024-06-01 .. 2025-03-01) ends before the saved
        # statement does. Before: it replaced the statement, so the META sale
        # of 2025-11-01 and everything after 2025-03-01 was lost.
        r = self.upload(t212_csv(AAPL_2024, META_2025))
        self.assertEqual(r.status_code, 200, r.text)
        st = self.saved()
        self.assertEqual(st["overview"]["period"], ["2024-06-01", "2025-12-31"])
        self.assertEqual([(t["date"][:10], t["side"]) for t in st["trades"]],
                         [("2024-06-01", "BUY"), ("2025-03-01", "BUY"), ("2025-11-01", "SELL")])
        self.assertEqual({p["isin"]: p["quantity"] for p in st["positions"]},
                         {"US30303M1027": 3.0, "US0378331005": 2.0})
        self.assertEqual(r.json()["_report"]["warnings"], [])

    def test_a_file_inside_the_saved_dates_changes_nothing(self):
        # Only 2025-03-01: before, this one-day file replaced the whole year.
        r = self.upload(t212_csv(META_2025))
        self.assertEqual(r.status_code, 400)
        self.assertIn("Nothing was changed", r.json()["detail"])
        self.assertEqual(self.saved(), BASE)

    def test_a_gap_between_files_is_reported(self):
        # The saved statement ends 2025-12-31, the export starts 2026-03-02:
        # January and February are missing. Before: joined without a word.
        r = self.upload(t212_csv(
            "Interest on cash,2026-03-02 00:10:00+00:00,,,,,i1,,,,,,,0.50,EUR,,,,,,"))
        self.assertEqual(r.status_code, 200, r.text)
        warnings = " ".join(r.json()["_report"]["warnings"])
        self.assertIn("2026-01-01 to 2026-03-01", warnings)


class Trading212CombineTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(fx, "_live_rates", return_value={"EUR": 1.0, "GBP": 1.2, "USD": 0.9})
        p.start()
        self.addCleanup(p.stop)

    def test_a_later_statement_keeps_its_own_positions_table(self):
        later = {**BASE, "overview": {"period": ["2025-06-01", "2026-06-30"]},
                 "trades": [BASE["trades"][1]],
                 "positions": [{**BASE["positions"][0], "value_eur": 2100.0, "return_eur": 600.0},
                               BASE["positions"][1]]}
        st, report = trading212_csv.combine(BASE, later, {"warnings": []})
        self.assertEqual(st["overview"]["period"], ["2025-01-01", "2026-06-30"])
        self.assertEqual(len(st["trades"]), 2)           # the sale once, not twice
        self.assertEqual(st["positions"], later["positions"])
        self.assertEqual(report["warnings"], [])

    def test_files_of_two_accounts_are_refused(self):
        other = trading212_csv.convert(t212_csv(
            "Market buy,2025-12-01 10:00:00+00:00,US0378331005,AAPL,Apple,,X1,1.0,190,USD,1.1,,,"
            "170.00,EUR,,,,,,",
            "Market buy,2026-02-01 10:00:00+00:00,US0378331005,AAPL,Apple,,X2,1.0,190,USD,1.1,,,"
            "170.00,EUR,,,,,,"))[0]
        # 2025-12-01 .. 2025-12-31 is in both, with different trades in each
        other["trades"][0]["isin"] = "IE00B4ND3602"
        with self.assertRaisesRegex(ValueError, "different accounts"):
            trading212_csv.combine({**BASE, "trades": BASE["trades"] + [
                {**BASE["trades"][0], "date": "2025-12-15 10:00"}]}, other)


def _line(*words):
    return {"top": 0, "words": [{"text": w, "x0": 10.0 * i, "x1": 10.0 * i + 8, "top": 0, "bottom": 8}
                                for i, w in enumerate(words)]}


class Trading212PdfTest(unittest.TestCase):
    def test_statement_of_two_accounts_is_refused(self):
        # All accounts are ticked by default. Two overviews with different
        # figures: before, the rows of both were mixed into one account and
        # only the second overview's totals were kept.
        from backend.importers import trading212_pdf

        page = lambda n, deposits, value: {"page": n, "lines": [  # noqa: E731
            _line("Overview"), _line("Deposits", deposits), _line("Account", "value", value)]}
        with self.assertRaisesRegex(ValueError, "more than one Trading 212 account"):
            trading212_pdf.parse([page(1, "€1,000.00", "€1,200.00"), page(2, "£500.00", "£480.00")])
        # the same overview twice (a repeated page) is still one account
        st = trading212_pdf.parse([page(1, "€1,000.00", "€1,200.00"), page(2, "€1,000.00", "€1,200.00")])
        self.assertEqual(st["overview"]["deposits"], 1000.0)
        # a figure the checks do not use may repeat (start and end values)
        trading212_pdf.parse([page(1, "€1,000.00", "€900.00"), page(2, "€1,000.00", "€1,200.00")])


# ---- Trade Republic transaction export (made-up rows, real column layout) ----
TR_HEADER = ["datetime", "date", "account_type", "category", "type", "asset_class", "name", "symbol",
             "shares", "price", "amount", "fee", "tax", "currency", "original_amount",
             "original_currency", "fx_rate", "description", "transaction_id"]


def tr_csv(*rows: dict, sep: str = ",") -> str:
    lines = [sep.join(TR_HEADER)]
    for i, r in enumerate(rows):
        day = r.get("date", "2026-01-05")
        full = {"datetime": f"{day}T10:00:00.000000Z", "date": day, "account_type": "DEFAULT",
                "currency": "EUR", "transaction_id": f"t{i}", **r}
        lines.append(sep.join(str(full.get(c, "")) for c in TR_HEADER))
    return "\n".join(lines) + "\n"


def tr_trade(day, typ, isin, shares, amount, fee="", tax="", asset="STOCK", cat="TRADING"):
    return {"date": day, "category": cat, "type": typ, "asset_class": asset, "name": "Made-up AG",
            "symbol": isin, "shares": shares, "amount": amount, "fee": fee, "tax": tax}


def tr_cash(day, typ, amount, tax="", isin="", shares="", asset=""):
    return {"date": day, "category": "CASH", "type": typ, "asset_class": asset, "symbol": isin,
            "name": "Made-up AG" if isin else "", "shares": shares, "amount": amount, "tax": tax}


ISIN = "DE0005557508"


class TradeRepublicCsvTest(unittest.TestCase):
    def convert(self, *rows, sep=","):
        from backend.importers import traderepublic_csv
        return traderepublic_csv.convert(tr_csv(*rows, sep=sep))

    def test_semicolon_export_is_read(self):
        # Some exports are ;-separated. Before: "Not a Trade Republic export".
        trades, _, _ = self.convert(tr_trade("2026-01-05", "BUY", ISIN, "15", "-264.96", "-1.00"), sep=";")
        self.assertEqual(trades["trades"][0]["amount_eur"], 265.96)

    def test_etf_distribution_is_income(self):
        # A distributing ETF pays 11.46, 2.25 tax withheld: 9.21 received.
        # Before: DISTRIBUTION rows were not income at all (0).
        _, income, _ = self.convert(tr_cash("2026-01-05", "DISTRIBUTION", "11.46", "-2.25", ISIN, "24.9", "FUND"))
        self.assertEqual([(i["type"], i["amount_eur"]) for i in income["income"]], [("dividend", 9.21)])

    def test_interest_is_net_of_tax(self):
        # 4.77 interest, 1.34 tax withheld: 3.43 received, the same figure as
        # the statement PDF and the live sync. Before: 4.77 (gross).
        _, income, report = self.convert(tr_cash("2026-01-05", "INTEREST_PAYMENT", "4.77", "-1.34"))
        self.assertEqual(income["income"][0]["amount_eur"], 3.43)
        self.assertEqual(report["cash_balance_eur"], 3.43)

    def test_tax_on_a_buy_is_part_of_its_cost_and_tax_on_a_sale_leaves_the_cash(self):
        # Buy: 100.00 + 1.00 fee + 0.30 transaction tax = 101.30 paid (before: 101.00).
        # Sell: 150.00 - 1.00 fee = 149.00 proceeds before tax; 12.50 tax
        # withheld, so cash grows by 136.50 (before: 149.00).
        trades, _, report = self.convert(
            tr_trade("2026-01-05", "BUY", ISIN, "2", "-100.00", "-1.00", "-0.30"),
            tr_trade("2026-02-05", "SELL", ISIN, "-2", "150.00", "-1.00", "-12.50"))
        self.assertEqual([t["amount_eur"] for t in trades["trades"]], [101.30, 149.00])
        self.assertEqual(report["cash_balance_eur"], round(-101.30 + 136.50, 2))

    def test_matured_bond_and_private_market_fund_are_trades(self):
        # 20 of a bond bought, then paid back (CASH FINAL_MATURITY); 100 units of
        # a private-market fund bought from cash. Before: the bond stayed held
        # forever and the fund never appeared.
        trades, _, report = self.convert(
            tr_trade("2025-12-15", "BUY", "DE0001141794", "20", "-19.79", "-1.00", asset="BOND"),
            tr_cash("2026-04-05", "FINAL_MATURITY", "20.00", isin="DE0001141794", shares="20", asset="BOND"),
            {**tr_cash("2026-04-05", "REDEMPTION", "", isin="DE0001141794", shares="-20", asset="BOND"),
             "category": "CORPORATE_ACTION"},
            tr_cash("2026-03-02", "PRIVATE_MARKET_BUY", "-100.00", isin="LU0000000001", shares="100",
                    asset="PRIVATE_FUND"))
        self.assertEqual(report["open_positions"], {"LU0000000001": 100.0})
        self.assertEqual(report["warnings"], [])

    def test_split_adds_the_new_shares_at_no_cost(self):
        # 0.045566 shares for 40.00 (+2.00 fees), then a 10-for-1 split adds
        # 0.410094: 0.45566 shares, still costing 42.00. Before: 0.045566.
        from backend.positions import Trade, compute_positions
        trades, _, _ = self.convert(
            tr_trade("2024-12-18", "BUY", "US64110L1061", "0.017085", "-15.00", "-1.00"),
            tr_trade("2024-12-18", "BUY", "US64110L1061", "0.028481", "-25.00", "-1.00"),
            {**tr_trade("2025-11-17", "SPLIT", "US64110L1061", "0.410094", ""), "category": "CORPORATE_ACTION"})
        pos = compute_positions([Trade(t["date"], t["isin"], t["name"], t["side"], t["quantity"],
                                       t["amount_eur"]) for t in trades["trades"]])["US64110L1061"]
        self.assertAlmostEqual(pos.quantity, 0.45566)
        self.assertAlmostEqual(pos.cost, 42.0)

    def test_crypto_is_priced_by_its_coin(self):
        # Crypto rows carry the coin ("BTC"), not an ISIN. Before: no ticker, no price.
        trades, _, _ = self.convert(tr_trade("2026-01-05", "BUY", "BTC", "0.0011", "-32.97", "-1.00",
                                             asset="CRYPTO"))
        self.assertEqual(trades["trades"][0]["ticker"], "BTC-EUR")

    def test_shares_moving_without_a_trade_are_reported(self):
        # A free crypto delivery and an unknown corporate action. Before: silently left out.
        _, _, report = self.convert(
            {**tr_trade("2026-01-19", "FREE_RECEIPT", "ETH", "0.000491", "", asset="CRYPTO"),
             "category": "DELIVERY"},
            {**tr_trade("2026-02-01", "SPIN_OFF", ISIN, "3", ""), "category": "CORPORATE_ACTION"})
        text = " ".join(report["warnings"])
        self.assertIn("ETH", text)
        self.assertIn("CORPORATE_ACTION SPIN_OFF", text)


class TradeRepublicCsvMatchesLiveTest(unittest.TestCase):
    """The same events, once as export rows and once as live timeline
    records (traderepublic_live.build), give the same trades and income."""

    def test_same_trades_and_income(self):
        from backend.connectors import traderepublic_live as live
        from backend.importers import traderepublic_csv

        trades, income, _ = traderepublic_csv.convert(tr_csv(
            tr_trade("2026-01-05", "BUY", ISIN, "10", "-500.00", "-1.00"),
            tr_trade("2026-02-05", "SELL", ISIN, "-4", "260.00", "-1.00", "-12.50"),
            tr_cash("2026-03-05", "DIVIDEND", "8.00", "-2.11", ISIN, "6", "STOCK"),
            tr_cash("2026-03-31", "INTEREST_PAYMENT", "4.77", "-1.34"),
            tr_cash("2026-04-01", "BENEFITS_SAVEBACK", "4.51"),
            tr_trade("2026-04-02", "BUY", ISIN, "0.09", "-4.51")))
        # what the live sync stores for the same events (cash as booked)
        rec = lambda i, ts, kind, **kw: (i, {"id": i, "ts": ts, "kind": kind, "isin": ISIN,  # noqa: E731
                                              "name": "Made-up AG", "currency": "EUR", **kw})
        events = dict([
            rec("1", "2026-01-05 10:00:00", "buy", shares=10, amount=501.00, fee=1.0, tax=0.0),
            rec("2", "2026-02-05 10:00:00", "sell", shares=4, amount=246.50, fee=1.0, tax=12.50),
            rec("3", "2026-03-05 10:00:00", "dividend", amount=5.89),
            rec("4", "2026-03-31 10:00:00", "interest", amount=3.43),
            rec("5", "2026-04-01 10:00:00", "saveback", shares=0.09, amount=4.51)])
        l_trades, l_divs, l_interest, _, unread = live.build(events)
        self.assertEqual(unread, 0)
        self.assertEqual(sorted((t["side"], round(t["quantity"], 6), t["amount_eur"]) for t in trades["trades"]),
                         sorted((t.side, round(t.quantity, 6), t.amount) for t in l_trades))
        by_type = lambda t: round(sum(i["amount_eur"] for i in income["income"] if i["type"] == t), 2)  # noqa: E731
        self.assertEqual(by_type("dividend"), round(sum(d["amount"] for d in l_divs), 2))
        self.assertEqual(by_type("interest"), round(sum(i["amount"] for i in l_interest), 2))


def tr_row(date, typ, text, amount, balance, page=1):
    return {"date": date, "type": typ, "text": text, "money": [amount, balance], "page": page}


class TradeRepublicPdfTest(unittest.TestCase):
    def convert(self, rows):
        with mock.patch.object(traderepublic_pdf, "_rows", return_value=rows), \
             mock.patch("pdfplumber.open", return_value=contextlib.nullcontext(object())):
            return traderepublic_pdf.convert(b"%PDF-")

    def test_bookings(self):
        trades, income, report = self.convert([
            tr_row("2025-05-19", "Overboeking", "Incoming transfer from A", 2000.0, 2000.0),
            tr_row("2025-05-29", "Handel", "Buy trade NL0010273215 ASML HOLDING EO -,09, quantity: 2.972048",
                   2000.0, 0.0),
            tr_row("2025-08-06", "Inkomsten", "Cash Dividend for ISIN NL0010273215", 4.05, 4.05),
            tr_row("2025-09-01", "Rente", "Interest payment", 0.95, 5.0),
            tr_row("2025-09-26", "Handel", "Sell trade NL0010273215 ASML HOLDING EO -,09, quantity: 2.972048",
                   2406.95, 2411.95),
            tr_row("2025-09-27", "Overboeking", "Outgoing transfer for A", 2411.95, 0.0),
        ])
        buy, sell = trades["trades"]
        self.assertEqual((buy["side"], buy["quantity"], buy["amount_eur"], buy["fee_eur"]),
                         ("BUY", 2.972048, 2000.0, 1.0))
        self.assertAlmostEqual(buy["price"], 1999.0 / 2.972048, places=3)
        self.assertEqual((sell["side"], sell["amount_eur"]), ("SELL", 2406.95))
        self.assertEqual([(i["type"], i["amount_eur"]) for i in income["income"]],
                         [("dividend", 4.05), ("interest", 0.95)])
        self.assertEqual((report["deposits_eur"], report["withdrawals_eur"]), (2000.0, 2411.95))
        self.assertEqual(report["open_positions"], {})
        self.assertEqual(report["problems"], [])

    def test_balance_mismatch_is_reported(self):
        _, _, report = self.convert([
            tr_row("2025-05-19", "Overboeking", "Incoming transfer", 100.0, 100.0),
            tr_row("2025-05-20", "Handel", "Buy trade NL0010273215 ASML, quantity: 0.1", 70.0, 20.0),
        ])
        self.assertEqual(len(report["problems"]), 1)
        self.assertTrue(report["warnings"])

    def test_dutch_and_german_quantity_words(self):
        trades, _, _ = self.convert([
            tr_row("2025-05-19", "Overboeking", "Incoming transfer", 500.0, 500.0),
            tr_row("2025-05-20", "Handel", "Kauf US0378331005 APPLE INC., Anzahl: 2", 401.0, 99.0),
        ])
        self.assertEqual(trades["trades"][0]["quantity"], 2.0)


if __name__ == "__main__":
    unittest.main()
