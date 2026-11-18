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
