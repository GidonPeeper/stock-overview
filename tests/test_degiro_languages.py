"""DEGIRO statements in other languages than Dutch (made-up rows and numbers,
in the formats confirmed in backend/degiro_lang.py)."""

import asyncio
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import degiro_lang as lang
from backend import fx, income
from backend.connectors import degiro
from backend.importers import degiro_pdf
from backend.positions import compute_positions

EN = "Date,Time,Value date,Product,ISIN,Description,FX,Change,,Balance,,Order Id\n"
NL = "Datum,Tijd,Valutadatum,Product,ISIN,Omschrijving,FX,Mutatie,,Saldo,,Order Id\n"
DE = "Datum,Uhrzeit,Valutadatum,Produkt,ISIN,Beschreibung,FX,Änderung,,Saldo,,Order-ID\n"
ES = "Fecha,Hora,Fecha valor,Producto,ISIN,Descripción,Tipo,Variación,,Saldo,,ID Orden\n"
IT = "Data,Ora,Data Valore,Prodotto,ISIN,Descrizione,Borsa,Variazioni,,Saldo,,ID Ordine\n"
PT = "Data,Hora,Data Valor,Produto,ISIN,Descrição,T.,Mudança,,Saldo,,ID da Ordem\n"
CS = "Datum,Čas,Datum,Produkt,ISIN,Popis,Kurz,Pohyb,,Zůstatek,,ID objednávky\n"
PL = "Data,Czas,Data,Produkt,ISIN,Opis,Kurs,Zmiana,,Saldo,,Identyfikator zlecenia\n"
RATES = {"EUR": 1.0, "USD": 0.9, "GBP": 1.2, "CHF": 1.0}


def row(date, product, isin, desc, ccy, change, oid="", fx_rate="", balance="0"):
    q = (lambda s: f'"{s}"' if "," in s else s)
    return (f"{date},10:00,{date},{product},{isin},{q(desc)},{q(fx_rate)},{ccy},{q(change)},"
            f"{ccy},{q(balance)},{oid}\n")


class _Statement(unittest.TestCase):
    """Runs the connector and income on a CSV text, with fixed FX rates."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(fx, "_live_rates", return_value=RATES)
        p.start()
        self.addCleanup(p.stop)

    def load(self, text):
        path = Path(self.tmp.name) / "degiro_account.csv"
        path.write_text(text, encoding="utf-8")
        with mock.patch.object(degiro, "_csv_file", return_value=path), \
             mock.patch.object(income, "_resolve_data", return_value=(path, True)):
            trades = degiro.get_trades()
            return trades, compute_positions(trades), income._degiro_rows()


class FillWordingTest(unittest.TestCase):
    def test_each_language(self):
        cases = {
            "Koop 3 @ 600 EUR": ("BUY", "3", "600", "EUR", "nl"),
            "Verkoop 1.000 @ 6,325 EUR": ("SELL", "1.000", "6,325", "EUR", "nl"),
            "Buy 2 ACME CORP. - C@682.568 USD": ("BUY", "2", "682.568", "USD", "en"),
            "Sell 4 Acme Plc@496 GBX (GB00B0000001)": ("SELL", "4", "496", "GBX", "en"),
            "Buy 3 Acme Core S&P 500 UCITS ETF USD (Acc)@502.48 EUR (IE00B0000001)":
                ("BUY", "3", "502.48", "EUR", "en"),
            "Buy 5 3M Lookalike@128 USD": ("BUY", "5", "128", "USD", "en"),
            "Kauf 1.000 zu je 0,9 EUR": ("BUY", "1.000", "0,9", "EUR", "de"),
            "Verkauf 114 zu je 6,6 EUR (DE000A0000A1)": ("SELL", "114", "6,6", "EUR", "de"),
            "Kauf 1’105 zu je 14.2055 EUR (LU0000000001)": ("BUY", "1’105", "14.2055", "EUR", "de"),
            "Achat 6 Acme Uranium UCITS ETF@53,25 EUR (IE0000000002)": ("BUY", "6", "53,25", "EUR", "fr"),
            "Vente 1 046,5 Acme@1,5 EUR": ("SELL", "1 046,5", "1,5", "EUR", "fr"),
            "Compra 95 ACME@21,31 EUR (ES0000000001)": ("BUY", "95", "21,31", "EUR", "es"),
            "Venta 10 ACME@22 EUR (ES0000000001)": ("SELL", "10", "22", "EUR", "es"),
            "Acquisto 2 Acme All-World - (USD) Accumulating@95,61 EUR (IE0000000003)":
                ("BUY", "2", "95,61", "EUR", "it"),
            "Vendita 282 ACME ESG@27,07 EUR": ("SELL", "282", "27,07", "EUR", "it"),
            "Venda 1 Acme Inc@9,05 USD (US0000000001)": ("SELL", "1", "9,05", "USD", "pt"),
            "Nákup 100 ACME - TD@16,29 EUR (BE0000000001)": ("BUY", "100", "16,29", "EUR", "cs"),
            "Prodej 100 ACME@16,29 EUR (BE0000000001)": ("SELL", "100", "16,29", "EUR", "cs"),
            "Kupno 7 ACME SA@12,5 PLN": ("BUY", "7", "12,5", "PLN", "pl"),
            "Sprzedaż 7 ACME SA@13 PLN": ("SELL", "7", "13", "PLN", "pl"),
        }
        for desc, want in cases.items():
            with self.subTest(desc):
                f = lang.fill(desc)
                self.assertEqual((f.side, f.qty, f.price, f.ccy, f.lang), want)

    def test_not_fills(self):
        for desc in ("Money Market fund conversion: Buy 0.5 at 1 GBP", "FX Credit", "Dividend",
                     "Geldmarktfonds Umwandlung: Kauf 0,02 zu je 0 EUR", "PRODUCT CHANGE: Buy 10 A@0 EUR",
                     "Verkaufsprovision", "Buyback"):
            with self.subTest(desc):
                self.assertIsNone(lang.fill(desc))

    def test_corporate_actions(self):
        self.assertEqual(lang.corporate("ISIN CHANGE: Sell 7 Acme Old@29.52 USD (US0000000001)"), "isin_change")
        self.assertEqual(lang.corporate("ISIN-ÄNDERUNG: Kauf 3 zu je 60,5 USD"), "isin_change")
        self.assertEqual(lang.corporate("SPLIT ADJUSTMENT: 30 Acme @ 0.6748 EUR"), "split")
        self.assertEqual(lang.corporate("PRODUCT CHANGE: Buy 10 Acme@0 EUR"), "trade")
        self.assertEqual(lang.corporate("DESLISTAMIENTO : Venta 9 Acme@0 EUR"), "trade")
        self.assertIsNone(lang.corporate("Buy 10 Acme@1 EUR"))
        f = lang.find_fill("PRODUCTWIJZIGING : Koop 500 @ 0,039 EUR")
        self.assertEqual((f.side, f.qty, f.ccy), ("BUY", "500", "EUR"))
        self.assertIsNone(lang.find_fill("Money Market fund conversion: Buy 0.5 at 1 GBP"))
        self.assertEqual(lang.split_qty("SPLIT ADJUSTMENT: 30 Acme @ 0.6748 EUR"), "30")


class NumbersTest(unittest.TestCase):
    def test_own_form_decides_where_certain(self):
        for s, want in (("1.234,56", 1234.56), ("1,234.56", 1234.56), ("-1365.14", -1365.14),
                        ("-1800,00", -1800.0), ("1’667.31", 1667.31), ("9 526,72", 9526.72),
                        ("0,039", 0.039), ("1.234.567", 1234567.0), ("0", 0.0), ("", 0.0)):
            with self.subTest(s):
                self.assertAlmostEqual(lang.to_float(s, ","), want)
                self.assertAlmostEqual(lang.to_float(s, "."), want)

    def test_file_mark_decides_ambiguous(self):
        self.assertEqual(lang.to_float("1.000", ","), 1000.0)
        self.assertEqual(lang.to_float("1.000", "."), 1.0)
        self.assertEqual(lang.to_float("682.568", "."), 682.568)

    def test_decimal_mark_by_majority(self):
        self.assertEqual(lang.decimal_mark(["1.000", "21,31", "0,5"], "."), ",")
        self.assertEqual(lang.decimal_mark(["682.568", "-106.55"], ","), ".")
        self.assertEqual(lang.decimal_mark(["1.000", "3"], ","), ",")   # nothing certain

    def test_no_break_space_thousands(self):
        self.assertEqual(lang.to_float("9\u00a0526,72", "."), 9526.72)
        self.assertEqual(lang.to_float("9\u202f526,72", "."), 9526.72)
        f = lang.fill("Vente 1\u202f046,5 Acme@1,5 EUR")
        self.assertEqual(lang.to_float(f.qty, ","), 1046.5)

    def test_plain_drops_thousands_keeps_mark(self):
        self.assertEqual(lang.plain("-1.116,03", ","), "-1116,03")
        self.assertEqual(lang.plain("1,365.14", "."), "1365.14")
        self.assertEqual(lang.plain("9 526,72", ","), "9526,72")


class EnglishStatementTest(_Statement):
    def test_gbp_account_buy_and_sell(self):
        """A UK account books in GBP: the cost is the GBP debit plus fees."""
        oid1, oid2 = "aaaa1111-0000-0000-0000-000000000001", "aaaa1111-0000-0000-0000-000000000002"
        text = EN + "".join([
            row("10-06-2024", "ACME INC", "US0000000001", "FX Debit", "USD", "-100.00", oid2, "1.2500"),
            row("10-06-2024", "ACME INC", "US0000000001", "FX Credit", "GBP", "80.00", oid2),
            row("10-06-2024", "ACME INC", "US0000000001", "DEGIRO Transaction and/or third party fees",
                "GBP", "-1.00", oid2),
            row("10-06-2024", "ACME INC", "US0000000001", "Sell 2 Acme Inc@50 USD (US0000000001)",
                "USD", "100.00", oid2),
            row("04-03-2024", "ACME INC", "US0000000001", "FX Credit", "USD", "400.00", oid1, "1.2500"),
            row("04-03-2024", "ACME INC", "US0000000001", "FX Debit", "GBP", "-320.00", oid1),
            row("04-03-2024", "ACME INC", "US0000000001", "DEGIRO Transaction and/or third party fees",
                "GBP", "-2.00", oid1),
            row("04-03-2024", "ACME INC", "US0000000001", "Buy 10 Acme Inc@40 USD (US0000000001)",
                "USD", "-400.00", oid1),
            row("05-06-2024", "ACME INC", "US0000000001", "Dividend", "USD", "5.00"),
            row("05-06-2024", "ACME INC", "US0000000001", "Dividend Tax", "USD", "-0.75"),
            row("02-01-2024", "", "", "DEGIRO Exchange Connection Fee 2024 (Nasdaq - NDQ)", "GBP", "-2.50"),
            row("01-02-2024", "", "", "Flatex Interest Income", "EUR", "0.40"),
            row("01-01-2024", "", "", "Deposit", "GBP", "1000.00"),
        ])
        trades, pos, inc = self.load(text)
        buy = next(t for t in trades if t.side == "BUY")
        self.assertEqual((buy.quantity, buy.amount), (10.0, round(322.00 * 1.2, 2)))
        p = pos["US0000000001"]
        self.assertAlmostEqual(p.quantity, 8.0)
        self.assertAlmostEqual(p.realized, 79.0 * 1.2 - 322.0 * 1.2 * 2 / 10, places=2)
        kinds = {r["kind"]: 0 for r in inc}
        for r in inc:
            kinds[r["kind"]] += round(r["amount_eur"], 4)
        self.assertAlmostEqual(kinds["dividends"], 4.25 * 0.9)
        self.assertAlmostEqual(kinds["fees"], -2.50 * 1.2)
        self.assertAlmostEqual(kinds["interest"], 0.40)

    def test_eur_account_corporate_actions(self):
        oid = "bbbb2222-0000-0000-0000-000000000001"
        text = EN + "".join([
            row("20-05-2024", "ACME NEW", "US0000000003", "ISIN CHANGE: Buy 10 Acme New@41 USD (US0000000003)",
                "USD", "-410.00"),
            row("20-05-2024", "ACME OLD", "US0000000002", "ISIN CHANGE: Sell 10 Acme Old@41 USD (US0000000002)",
                "USD", "410.00"),
            row("01-04-2024", "BETA", "NL0000000001", "PRODUCT CHANGE: Sell 5 Beta@0 EUR", "EUR", "0.00"),
            row("01-04-2024", "BETA NEW", "NL0000000002", "PRODUCT CHANGE: Buy 5 Beta New@0 EUR", "EUR", "0.00"),
            row("02-02-2024", "ACME OLD", "US0000000002", "FX Debit", "EUR", "-370.00", oid),
            row("02-02-2024", "ACME OLD", "US0000000002", "FX Credit", "USD", "400.00", oid, "1.0800"),
            row("02-02-2024", "ACME OLD", "US0000000002", "Buy 10 Acme Old@40 USD (US0000000002)",
                "USD", "-400.00", oid),
            row("01-02-2024", "BETA", "NL0000000001", "Buy 5 Beta@20 EUR (NL0000000001)", "EUR", "-100.00",
                "bbbb2222-0000-0000-0000-000000000002"),
        ])
        trades, pos, _ = self.load(text)
        acme = pos["US0000000003"]                 # re-keyed, cost basis kept, nothing sold
        self.assertEqual((acme.quantity, acme.sells, round(acme.avg_cost, 2)), (10.0, 0, 37.0))
        self.assertNotIn("US0000000002", pos)
        self.assertAlmostEqual(pos["NL0000000002"].quantity, 5.0)
        self.assertAlmostEqual(pos["NL0000000001"].quantity, 0.0)


class DutchAccountEnglishSiteTest(_Statement):
    def test_dot_amounts_with_dutch_rows(self):
        """English website, Dutch account: "-1500.00" amounts next to "Koop 1.000 @ 1,5 EUR"."""
        text = EN + row("06-07-2023", "ACME", "IE0000000001", "Koop 1.000 @ 1,5 EUR", "EUR", "-1500.00",
                        "cccc3333-0000-0000-0000-000000000001")
        trades, _, _ = self.load(text)
        self.assertEqual((trades[0].quantity, trades[0].amount), (1000.0, 1500.0))

    def test_dutch_csv_unchanged(self):
        text = NL + row("02-01-2024", "ACME", "NL0000000001", "Koop 1.000 @ 0,5 EUR", "EUR", "-500,00",
                        "dddd4444-0000-0000-0000-000000000001")
        trades, _, _ = self.load(text)
        self.assertEqual((trades[0].side, trades[0].quantity, trades[0].amount), ("BUY", 1000.0, 500.0))


class OtherLanguagesTest(_Statement):
    def check(self, header, buy, sell, ccy="EUR", buy_amt="-1000,00", sell_amt="600,00", extra=()):
        text = header + "".join([
            row("10-06-2024", "ACME", "XS0000000001", sell, ccy, sell_amt, "e2"),
            row("01-03-2024", "ACME", "XS0000000001", buy, ccy, buy_amt, "e1"),
            *extra,
        ])
        trades, pos, inc = self.load(text)
        self.assertEqual([t.side for t in trades], ["SELL", "BUY"])
        return trades, pos["XS0000000001"], inc

    def test_german(self):
        _, p, inc = self.check(DE, "Kauf 100 zu je 10 EUR (XS0000000001)",
                               "Verkauf 50 zu je 12 EUR (XS0000000001)", buy_amt="-1000.00", sell_amt="600.00",
                               extra=[row("05-05-2024", "ACME", "XS0000000001", "Dividende", "EUR", "8.00"),
                                      row("05-05-2024", "ACME", "XS0000000001", "Dividendensteuer", "EUR", "-2.00")])
        self.assertEqual((p.quantity, round(p.realized, 2)), (50.0, 100.0))
        self.assertAlmostEqual(sum(r["amount_eur"] for r in inc), 6.0)

    def test_swiss_german(self):
        _, p, _ = self.check(DE, "Kauf 1’105 zu je 10.5 CHF (XS0000000001)", "Verkauf 105 zu je 11 CHF",
                             ccy="CHF", buy_amt="-11602.50", sell_amt="1155.00")
        self.assertAlmostEqual(p.quantity, 1000.0)

    def test_french_english_headers(self):
        _, p, inc = self.check(EN, "Achat 100 Acme SA@10 EUR (XS0000000001)", "Vente 50 Acme SA@12 EUR",
                               extra=[row("05-05-2024", "ACME", "XS0000000001", "Impôts sur dividende", "EUR", "-1,00")])
        self.assertEqual((p.quantity, round(p.realized, 2)), (50.0, 100.0))
        self.assertEqual([r["kind"] for r in inc], ["dividends"])

    def test_spanish(self):
        _, p, inc = self.check(ES, "Compra 100 ACME@10 EUR (XS0000000001)", "Venta 50 ACME@12 EUR (XS0000000001)",
                               buy_amt="-1000.00", sell_amt="600.00",
                               extra=[row("05-05-2024", "ACME", "XS0000000001", "Retención del dividendo", "EUR", "-1.00"),
                                      row("02-01-2024", "", "", "Comisión de conectividad con el mercado 2024 (X)",
                                          "EUR", "-2.50")])
        self.assertEqual((p.quantity, round(p.realized, 2)), (50.0, 100.0))
        self.assertEqual(sorted(r["kind"] for r in inc), ["dividends", "fees"])

    def test_italian(self):
        _, p, inc = self.check(IT, "Acquisto 100 Acme@10 EUR (XS0000000001)", "Vendita 50 Acme@12 EUR",
                               extra=[row("05-05-2024", "ACME", "XS0000000001", "Ritenuta sul dividendo", "EUR", "-1,00"),
                                      row("02-01-2024", "", "", "DEGIRO Costi di connessione 2024 (X)", "EUR", "-2,50")])
        self.assertEqual((p.quantity, round(p.realized, 2)), (50.0, 100.0))
        self.assertEqual(sorted(r["kind"] for r in inc), ["dividends", "fees"])

    def test_portuguese(self):
        _, p, inc = self.check(PT, "Compra 100 Acme@10 EUR (XS0000000001)", "Venda 50 Acme@12 EUR (XS0000000001)",
                               extra=[row("05-05-2024", "ACME", "XS0000000001", "Imposto sobre dividendo", "EUR", "-1,00")])
        self.assertEqual((p.quantity, round(p.realized, 2)), (50.0, 100.0))
        self.assertEqual([r["kind"] for r in inc], ["dividends"])

    def test_czech(self):
        _, p, inc = self.check(CS, "Nákup 100 ACME@10 EUR (XS0000000001)", "Prodej 50 ACME@12 EUR (XS0000000001)",
                               buy_amt="-1000.00", sell_amt="600.00",
                               extra=[row("05-05-2024", "ACME", "XS0000000001", "Daň z dividendy", "EUR", "-1.00")])
        self.assertEqual((p.quantity, round(p.realized, 2)), (50.0, 100.0))
        self.assertEqual([r["kind"] for r in inc], ["dividends"])

    def test_polish(self):
        _, p, _ = self.check(PL, "Kupno 100 ACME@10 EUR", "Sprzedaż 50 ACME@12 EUR")
        self.assertEqual((p.quantity, round(p.realized, 2)), (50.0, 100.0))


class CsvQuirksTest(_Statement):
    def test_wrapped_order_id_is_joined(self):
        text = EN + "".join([
            row("02-02-2024", "ACME", "US0000000001", "FX Debit", "EUR", "-370.00", "ffff6666-0000-0000-"),
            ",,,,,,,,,,,0000-000000000001\n",
            row("02-02-2024", "ACME", "US0000000001", "FX Credit", "USD", "400.00",
                "ffff6666-0000-0000-0000-000000000001", "1.0800"),
            row("02-02-2024", "ACME", "US0000000001", "Buy 10 Acme@40 USD", "USD", "-400.00",
                "ffff6666-0000-0000-0000-000000000001"),
        ])
        trades, _, _ = self.load(text)
        self.assertEqual([(t.quantity, t.amount) for t in trades], [(10.0, 370.0)])

    def test_fill_without_order_id_is_kept(self):
        text = EN + "".join([
            row("02-02-2024", "ACME", "US0000000001", "FX Debit", "EUR", "-370.00"),
            row("02-02-2024", "ACME", "US0000000001", "FX Credit", "USD", "400.00", "", "1.0800"),
            row("02-02-2024", "ACME", "US0000000001", "Buy 10 Acme@40 USD", "USD", "-400.00"),
        ])
        trades, _, inc = self.load(text)
        self.assertEqual([(t.quantity, t.amount) for t in trades], [(10.0, 370.0)])
        self.assertEqual(inc, [])


class IncomeWordingTest(unittest.TestCase):
    def test_kinds(self):
        want = {
            "Dividend Tax": "dividends", "Fund Distribution": "dividends", "Dividendensteuer": "dividends",
            "Impôts sur dividende": "dividends", "Retención del dividendo": "dividends",
            "Ritenuta sul dividendo": "dividends", "Imposto sobre dividendo": "dividends",
            "Daň z dividendy": "dividends", "Dywidenda": "dividends",
            "Flatex Interest": "interest", "Interest": "interest", "Interesse": "interest",
            "DEGIRO Money Market fund Compensation": "interest",
            "DEGIRO Exchange Connection Fee 2024 (London Stock Exchange (LSE) - LSE)": "fees",
            "Giro Exchange Connection Fee 2024": "fees", "ADR/GDR Pass-Through Fee": "fees",
            "Einrichtung von Handelsmodalitäten 2020 (NASDAQ - NDQ)": "fees",
            "Frais de connexion aux places boursières 2025 (X)": "fees",
            "Custo de Conectividade DEGIRO 2025 (Nasdaq - NDQ)": "fees",
            "FX Credit": None, "FX Debit": None, "Deposit": None, "flatex Deposit": None,
            "Money Market fund conversion: Buy 0.5 at 1 GBP": None,
            "DIVIDENDO FLEXIBLE: Venta 9 ACME-RTS@0 EUR (ES0000000001)": None,
        }
        self.assertEqual({d: income._degiro_kind(d) for d in want}, want)


class CheckStatementTest(unittest.TestCase):
    def test_reads_known_languages(self):
        text = EN + row("02-02-2024", "ACME", "US0000000001", "Buy 10 Acme@40 USD", "USD", "-400.00", "o1")
        self.assertEqual(degiro.check_statement(text), {"fills": 1, "orders": 1})

    def test_unknown_language_is_refused_by_name(self):
        text = EN + row("02-02-2024", "ACME", "US0000000001", "Köp 10 Acme@40 USD", "USD", "-400.00", "o1")
        with self.assertRaisesRegex(ValueError, "language the app does not read yet.*Köp 10 Acme"):
            degiro.check_statement(text)

    def test_no_trades(self):
        text = EN + row("02-02-2024", "", "", "Deposit", "EUR", "100.00")
        with self.assertRaisesRegex(ValueError, "No buys or sells"):
            degiro.check_statement(text)

    def test_other_files(self):
        with self.assertRaisesRegex(ValueError, "Transactions export"):
            degiro.check_statement("Datum,Tijd,Product,ISIN,Beurs,Uitvoeringsplaats,Aantal,Koers,,"
                                   "Lokale waarde,,Waarde,,Wisselkoers,Transactiekosten,,Totaal,,Order ID\n")
        with self.assertRaisesRegex(ValueError, "doesn't look like a DEGIRO account statement"):
            degiro.check_statement("Action,Time,ISIN,Ticker\nBuy,2024,US0000000001,ACME\n")
        with self.assertRaisesRegex(ValueError, "No statement table found"):
            degiro.check_statement(EN, pdf=True)

    def test_upload_refuses_instead_of_saving_nothing(self):
        from fastapi import HTTPException
        from starlette.datastructures import UploadFile

        from backend import datafiles, main
        text = EN + row("02-02-2024", "ACME", "US0000000001", "Köp 10 Acme@40 USD", "USD", "-400.00", "o1")
        upload = UploadFile(io.BytesIO(text.encode()), filename="Account.csv")
        with mock.patch.object(datafiles, "save_upload") as save:
            with self.assertRaises(HTTPException) as e:
                asyncio.run(main.upload_statement("degiro", upload))
        self.assertEqual(e.exception.status_code, 400)
        self.assertIn("language the app does not read yet", e.exception.detail)
        save.assert_not_called()


# ---- PDF ----------------------------------------------------------------------

_X = {"date": 30, "time": 80, "value": 110, "product": 160, "isin": 260, "desc": 330,
      "fx": 500, "change": 540, "balance": 600}


class _Page:
    def __init__(self, lines):
        self.words = [{"text": t, "x0": x, "top": 10 * n} for n, cells in enumerate(lines, 1)
                      for x, t in cells]

    def extract_words(self, **_):
        return self.words

    def close(self):
        pass


class _Pdf:
    def __init__(self, pages):
        self.pages = pages


def _header(titles):
    cells, keys = [], list(_X)
    for key, title in zip(keys, titles):
        for i, word in enumerate(title.split()):
            cells.append((_X[key] + 25 * i, word))
    return cells


def _data(date, time, isin, desc, ccy="", change="", bal_ccy="", balance="", fx_rate="", product=""):
    cells = [(_X["date"], date), (_X["time"], time), (_X["value"], date)]
    if product:
        cells.append((_X["product"], product))
    if isin:
        cells.append((_X["isin"], isin))
    cells += [(_X["desc"] + 6 * i, w) for i, w in enumerate(desc.split())]
    if fx_rate:
        cells.append((_X["fx"], fx_rate))
    if ccy:
        cells += [(_X["change"], ccy)] + [(_X["change"] + 25 + 8 * i, w) for i, w in enumerate(change.split())]
    if bal_ccy:
        cells += [(_X["balance"], bal_ccy)] + [(_X["balance"] + 25 + 8 * i, w) for i, w in enumerate(balance.split())]
    return cells


def _convert(lines):
    with mock.patch("pdfplumber.open", return_value=contextlib.nullcontext(_Pdf([_Page(lines)]))):
        return degiro_pdf.convert(b"%PDF-")


class PdfLanguagesTest(unittest.TestCase):
    def test_english_statement(self):
        """English rows, English numbers, a fill wrapped onto a second line."""
        lines = [
            _header(lang.PDF_HEADERS["en"]),
            _data("16-03-2024", "20:35", "US0000000001", "FX Credit", "USD", "1,365.14", "USD", "0.00",
                  fx_rate="1.1897"),
            _data("16-03-2024", "20:35", "US0000000001", "FX Debit", "EUR", "-1,147.45", "EUR", "852.55"),
            _data("16-03-2024", "20:35", "US0000000001", "Buy 2 ACME MOTORS INC.", "USD", "-1,365.14", "USD",
                  "-1,365.14"),
            [(_X["desc"], "-"), (_X["desc"] + 6, "C@682.57"), (_X["desc"] + 12, "USD"),
             (_X["desc"] + 18, "(US0000000001)")],
            _data("01-03-2024", "09:00", "", "Deposit", "EUR", "2,000.00", "EUR", "2,000.00"),
        ]
        text, report = _convert(lines)
        self.assertEqual(report["fills"], 1)
        self.assertEqual(report["balance_mismatches"], 0)
        self.assertEqual(report["fill_amount_errors"], [])
        self.assertIn('Buy 2 ACME MOTORS INC. - C@682.57 USD (US0000000001),,USD,-1365.14,USD,-1365.14,'
                      '20240316-2035-US0000000001', text)
        self.assertEqual(degiro.check_statement(text, pdf=True), {"fills": 1, "orders": 1})

    def test_dutch_rows_english_headers_unchanged(self):
        lines = [
            _header(lang.PDF_HEADERS["en"]),
            _data("02-01-2024", "10:00", "NL0000000001", "Koop 3 @ 600 EUR", "EUR", "-1.800,00", "EUR", "3.200,00"),
        ]
        text, report = _convert(lines)
        self.assertIn('Koop 3 @ 600 EUR,,EUR,"-1800,00",EUR,"3200,00",20240102-1000-NL0000000001', text)

    def test_header_in_other_languages(self):
        for code in ("nl", "de", "es", "it", "cs"):
            with self.subTest(code):
                lines = [_header(lang.PDF_HEADERS[code]),
                         _data("02-01-2024", "10:00", "NL0000000001", "Koop 3 @ 600 EUR", "EUR", "-1.800,00",
                               "EUR", "3.200,00")]
                _, report = _convert(lines)
                self.assertEqual(report["fills"], 1)

    def test_german_statement_without_value_date_column(self):
        titles = [t for i, t in enumerate(lang.PDF_HEADERS["de"]) if i != 2]
        lines = [[(_X[k] + 25 * j, w) for k, t in zip([k for k in _X if k != "value"], titles)
                  for j, w in enumerate(t.split())]]
        lines.append([(_X["date"], "03-08-2017"), (_X["time"], "15:32"), (_X["product"], "ACME"),
                      (_X["isin"], "US0000000001")]
                     + [(_X["desc"] + 6 * i, w) for i, w in enumerate("Kauf 1 zu je 177,5 USD".split())]
                     + [(_X["change"], "USD"), (_X["change"] + 25, "-177,50"),
                        (_X["balance"], "USD"), (_X["balance"] + 25, "-177,50")])
        text, report = _convert(lines)
        self.assertEqual(report["fills"], 1)
        self.assertIn('"Kauf 1 zu je 177,5 USD",,USD,"-177,50",USD,"-177,50",20170803-1532-US0000000001', text)

    def test_space_grouped_amounts(self):
        """French layout: "9 526,72" comes out of the PDF as two words."""
        lines = [
            _header(lang.PDF_HEADERS["en"]),
            _data("08-10-2025", "16:58", "IE0000000002", "Achat 6 Acme Uranium ETF@53,25 EUR (IE0000000002)",
                  "EUR", "-319,50", "EUR", "9 526,72"),
            _data("01-10-2025", "09:00", "", "Dépôt flatex", "EUR", "9 846,22", "EUR", "9 846,22"),
        ]
        text, report = _convert(lines)
        self.assertIn(",EUR,-319,50,EUR,9526,72,", text.replace('"', ""))
        self.assertEqual(report["balance_mismatches"], 0)
        self.assertEqual(report["fill_amount_errors"], [])

    def test_unknown_header_reads_nothing(self):
        lines = [[(30, "Datum"), (80, "Tid"), (160, "Produkt"), (260, "ISIN"), (330, "Beskrivning")],
                 _data("02-01-2024", "10:00", "NL0000000001", "Köp 3 ACME@600 SEK", "SEK", "-1 800,00")]
        text, _ = _convert(lines)
        with self.assertRaisesRegex(ValueError, "No statement table found"):
            degiro.check_statement(text, pdf=True)


if __name__ == "__main__":
    unittest.main()
