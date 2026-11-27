"""Interest rates of accounts that update themselves (backend/interest.py):
the rate implied by the interest paid, each provider's data, and how a typed
rate, a found rate and a missing one reach the cash totals, the advisors and
the MCP tool.

Rates are AERs: ((1 + interest / average balance) ^ (365 / days) - 1) x 100.
Fixtures are made-up accounts shaped like each provider's documentation (the
page is named above each one). No network: requests are canned.
Run: .venv/bin/python -m unittest discover -s tests -t .
"""

import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import requests

from backend import cash, fx, interest
from backend.connectors import ibkr_flex, registry, starling, traderepublic_live as trl
from backend.connectors import trading212 as t212
from tests.test_connections import RATES, TempStore, response

TODAY = date(2026, 11, 27)


def daily(start: date, n: int, amount: float) -> list[tuple[date, float]]:
    return [(date.fromordinal(start.toordinal() + i), amount) for i in range(n)]


# ------------------------------------------------------------ the maths
class ImpliedRateTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(fx, "_live_rates", return_value=RATES)
        p.start()
        self.addCleanup(p.stop)

    def test_steady_balance_monthly_credits(self):
        # 10,000 from August, interest paid on the 1st for the month before
        # (Starling's Easy Saver): 33.37 for Aug (31 days, on 10,000), 32.40 for
        # Sep (30 days, on 10,033.37), 33.59 for Oct (31 days, on 10,065.77).
        # Oct alone: (1 + 33.59 / 10,065.77) ^ (365/31) - 1 = 4.00%.
        # All three agree, so they are combined: 99.36 interest on an average of
        # (10,000 x 31 + 10,033.37 x 30 + 10,065.77 x 31) / 92 = 10,033.04 over
        # 92 days: (1 + 99.36 / 10,033.04) ^ (365/92) - 1 = 3.99%.
        credits = [("2026-09-01", 33.37), ("2026-10-01", 32.40), ("2026-11-01", 33.59)]
        r = interest.implied(credits, [], 10099.36, TODAY, currency="GBP", basis="gross")
        self.assertEqual(r["status"], "ok")
        self.assertEqual((r["pct"], r["from"], r["to"], r["days"], r["interest"]),
                         (3.99, "2026-08-01", "2026-10-31", 92, 99.36))
        self.assertAlmostEqual(r["average"], 10033.04, places=2)
        self.assertEqual(r["frequency"], "monthly")
        self.assertNotIn("previous_pct", r)
        self.assertEqual(interest.note(r, TODAY),
                         "from the interest paid for 1 Aug–31 Oct (before tax)")

    def test_money_moved_mid_period(self):
        # Sep: 2,000 all month -> 6.46 paid 1 Oct: (1 + 6.46/2,000)^(365/30) - 1 = 4.00%.
        # Oct: 2,006.46 for 1-15 Oct, then 8,000 paid in on 16 Oct: 10,006.46 for
        # 16-31 Oct. Average (2,006.46 x 15 + 10,006.46 x 16) / 31 = 6,135.49;
        # 20.47 paid 1 Nov: (1 + 20.47 / 6,135.49) ^ (365/31) - 1 = 4.00%.
        # Both months together: 26.93 on (2,000 x 30 + 6,135.49 x 31) / 61 =
        # 4,101.64 over 61 days: (1 + 26.93 / 4,101.64) ^ (365/61) - 1 = 3.99%.
        # (Today's balance would give 20.47 x 12 / 10,026.93 = 2.45%: wrong.)
        credits = [("2026-10-01", 6.46), ("2026-11-01", 20.47)]
        r = interest.implied(credits, [("2026-10-16", 8000.0)], 10026.93, TODAY, currency="GBP")
        self.assertEqual((r["status"], r["pct"], r["from"], r["to"]), ("ok", 3.99, "2026-09-01", "2026-10-31"))
        self.assertAlmostEqual(r["average"], 4101.64, places=2)
        only_oct = interest.implied([("2026-10-01", 6.46), ("2026-11-01", 20.47)], [("2026-10-16", 8000.0)],
                                    10026.93, TODAY, currency="GBP", since="2026-09-15")
        self.assertEqual((only_oct["pct"], only_oct["from"]), (4.0, "2026-10-01"))   # Sep not complete

    def test_daily_credits(self):
        # 5,000 paid in on 19 Aug; 0.54 interest every day from 20 Aug to 27 Nov
        # (Trading 212 pays daily, for the day before). The balance grows by the
        # interest: on day d it is 5,000 + 0.54 x (credits so far).
        # 30-day blocks ending at the last credit; all agree, so 90 days are used
        # (the 10 days before them would make 100 > 95): credits 30 Aug-27 Nov =
        # 48.60, accrual days 29 Aug-26 Nov with 10..99 credits paid, average
        # 5,000 + 0.54 x 54.5 = 5,029.43: (1 + 48.60 / 5,029.43) ^ (365/90) - 1 = 3.98%.
        credits = daily(date(2026, 8, 20), 100, 0.54)
        r = interest.implied(credits, [("2026-08-19", 5000.0)], 5054.00, TODAY, currency="GBP")
        self.assertEqual((r["status"], r["frequency"], r["days"]), ("ok", "daily", 90))
        self.assertEqual((r["from"], r["to"], r["interest"], r["credits"]), ("2026-08-29", "2026-11-26", 48.6, 90))
        self.assertAlmostEqual(r["average"], 5029.43, places=2)
        self.assertEqual(r["pct"], 3.98)

    def test_rate_change_uses_the_latest_period(self):
        # Sep: 10,000 at 4.5% -> 36.24 paid 1 Oct: (1 + 36.24/10,000)^(365/30) - 1 = 4.50%.
        # Oct: 10,036.24 at 4.0% -> 33.49 paid 1 Nov: (1 + 33.49/10,036.24)^(365/31) - 1 = 4.00%.
        # 4.50 is 0.50 away from 4.00 (more than 0.16): the rate changed, Oct only.
        credits = [("2026-10-01", 36.24), ("2026-11-01", 33.49)]
        r = interest.implied(credits, [], 10069.73, TODAY, currency="GBP", basis="gross")
        self.assertEqual((r["pct"], r["from"], r["to"], r["previous_pct"]),
                         (4.0, "2026-10-01", "2026-10-31", 4.5))
        self.assertIn("it was about 4.5% before", interest.note(r, TODAY))

    def test_too_little_data(self):
        # one payment: no period yet
        r = interest.implied([("2026-11-01", 30.0)], [], 10030.0, TODAY, currency="GBP")
        self.assertEqual(r["status"], "too_little")
        self.assertIn("Only one interest payment", r["text"])
        # a near-empty account: 50 GBP = 60 EUR on average, under the 100 EUR minimum
        r = interest.implied([("2026-10-01", 0.17), ("2026-11-01", 0.17)], [], 50.34, TODAY, currency="GBP")
        self.assertEqual(r["status"], "too_little")
        self.assertIn("balance was too small", r["text"])
        # 300 GBP at 1.5%: 0.37 + 0.38 = 0.75 in two months, under the 1.00 minimum
        r = interest.implied([("2026-10-01", 0.37), ("2026-11-01", 0.38)], [], 300.75, TODAY, currency="GBP")
        self.assertEqual(r["status"], "too_little")
        self.assertIn("Only 0.75 GBP of interest", r["text"])
        # daily interest for 20 days only (the account is new): under 28 days
        r = interest.implied(daily(date(2026, 11, 8), 20, 0.54), [("2026-11-07", 5000.0)], 5010.80,
                             TODAY, currency="GBP")
        self.assertEqual(r["status"], "too_little")
        self.assertIn("Only 20 days", r["text"])

    def test_no_interest_means_zero_when_money_sat_there(self):
        # 2,000 all along, nothing paid in 125 days: the account earns 0%
        r = interest.implied([], [("2026-11-05", -40.0)], 1960.0, TODAY, currency="GBP")
        self.assertEqual((r["status"], r["pct"], r.get("zero")), ("ok", 0.0, True))
        self.assertEqual(interest.note(r, TODAY), "no interest paid 25 Jul–27 Nov")
        # but money that arrived 20 days ago says nothing yet
        r = interest.implied([], [("2026-11-07", 2000.0)], 2000.0, TODAY, currency="GBP")
        self.assertEqual(r["status"], "none")
        self.assertIn("If it pays none, enter 0%", r["text"])

    def test_stopped_and_unclear(self):
        r = interest.implied([("2026-07-01", 30.0), ("2026-08-01", 30.0)], [], 10000.0, TODAY,
                             currency="GBP", since="2026-06-01")
        self.assertEqual(r["status"], "stopped")
        self.assertIn("1 Aug 2026", r["text"])
        # a deposit of 8,000 on 16 Oct with only 1,000 there now: the history
        # goes below zero, so a movement is missing and no rate is claimed
        r = interest.implied([("2026-10-01", 3.0), ("2026-11-01", 3.0)], [("2026-10-16", 8000.0)], 1000.0,
                             TODAY, currency="GBP")
        self.assertEqual(r["status"], "unclear")

    def test_period_text(self):
        self.assertEqual(interest.period_text("2026-10-01", "2026-10-31", TODAY), "1–31 Oct")
        self.assertEqual(interest.period_text("2026-10-28", "2026-11-26", TODAY), "28 Oct–26 Nov")
        self.assertEqual(interest.period_text("2025-12-15", "2026-01-12", TODAY), "15 Dec 2025–12 Jan 2026")
        self.assertEqual(interest.period_text("2025-10-01", "2025-10-31", TODAY), "1–31 Oct 2025")


# ------------------------------------------------------------- Starling
class FixedNow(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


PERSONAL, SAVER = "6a1c2f4e-0000-4000-8000-000000000001", "6a1c2f4e-0000-4000-8000-000000000002"
CAT_PERSONAL, CAT_HOLIDAY = "7b2d3a5f-0000-4000-8000-000000000001", "7b2d3a5f-0000-4000-8000-000000000003"
CAT_SAVER = "7b2d3a5f-0000-4000-8000-000000000002"

# GET /api/v2/accounts -- developer.starlingbank.com/docs (API reference, the
# OpenAPI spec /api/openapi.json): {accounts: [AccountV2: accountUid,
# accountType PRIMARY|ADDITIONAL|LOAN|FIXED_TERM_DEPOSIT|SAVINGS,
# defaultCategory, currency, createdAt, name]}
ACCOUNTS = {"accounts": [
    {"accountUid": PERSONAL, "accountType": "PRIMARY", "defaultCategory": CAT_PERSONAL,
     "currency": "GBP", "createdAt": "2021-03-01T10:00:00.000Z", "name": "Personal"},
    {"accountUid": SAVER, "accountType": "SAVINGS", "defaultCategory": CAT_SAVER,
     "currency": "GBP", "createdAt": "2025-02-01T10:00:00.000Z", "name": "Easy Saver"}]}


# GET /api/v2/accounts/{accountUid}/balance -- same reference: clearedBalance,
# effectiveBalance, pendingTransactions, acceptedOverdraft, amount,
# totalClearedBalance, totalEffectiveBalance, each {currency, minorUnits};
# the totals include Spaces.
def balance(main: int, total: int) -> dict:
    gbp = lambda m: {"currency": "GBP", "minorUnits": m}  # noqa: E731
    return {"clearedBalance": gbp(main), "effectiveBalance": gbp(main), "pendingTransactions": gbp(0),
            "acceptedOverdraft": gbp(0), "amount": gbp(main), "totalClearedBalance": gbp(total),
            "totalEffectiveBalance": gbp(total)}


# GET /api/v2/feed/account/{accountUid}/settled-transactions-between
#     ?minTransactionTimestamp=...&maxTransactionTimestamp=... (scope
#     transaction:read) -> {feedItems: [FeedItem]} -- same reference. Interest
#     paid: source INTEREST_PAYMENT; a move to a Space: INTERNAL_TRANSFER,
#     counterPartyType CATEGORY (as in the SDK's test/responses/v2-get-feed-items.json).
def item(n, cat, when, pence, direction, source, cp_type, cp_uid, cp_name, spending):
    return {"feedItemUid": f"8c3e4b6a-0000-4000-8000-{n:012d}", "categoryUid": cat,
            "amount": {"currency": "GBP", "minorUnits": pence},
            "sourceAmount": {"currency": "GBP", "minorUnits": pence}, "direction": direction,
            "updatedAt": when, "transactionTime": when, "settlementTime": when, "source": source,
            "status": "SETTLED", "counterPartyType": cp_type, "counterPartyUid": cp_uid,
            "counterPartyName": cp_name, "reference": "", "country": "GB",
            "spendingCategory": spending, "hasAttachment": False, "hasReceipt": False}


PERSONAL_FEED = {"feedItems": [
    item(1, CAT_PERSONAL, "2026-11-03T09:00:00.000Z", 20000, "OUT", "INTERNAL_TRANSFER", "CATEGORY",
         CAT_HOLIDAY, "Holiday", "SAVING"),
    item(2, CAT_HOLIDAY, "2026-11-03T09:00:00.000Z", 20000, "IN", "INTERNAL_TRANSFER", "CATEGORY",
         CAT_PERSONAL, "Personal", "SAVING"),
    item(3, CAT_PERSONAL, "2026-11-05T12:30:00.000Z", 4000, "OUT", "MASTER_CARD", "MERCHANT",
         "9d4f5c7b-0000-4000-8000-000000000001", "Corner Shop", "GROCERIES"),
    item(4, CAT_PERSONAL, "2026-11-06T08:00:00.000Z", 4000, "IN", "FASTER_PAYMENTS_IN", "SENDER",
         "9d4f5c7b-0000-4000-8000-000000000002", "A. Friend", "INCOME"),
    item(5, CAT_PERSONAL, "2026-11-10T18:00:00.000Z", 200000, "OUT", "INTERNAL_TRANSFER", "CATEGORY",
         CAT_SAVER, "Easy Saver", "SAVING"),
]}
SAVER_FEED = {"feedItems": [
    item(6, CAT_SAVER, "2026-09-01T03:00:00.000Z", 3337, "IN", "INTEREST_PAYMENT", "STARLING",
         "9d4f5c7b-0000-4000-8000-000000000003", "Starling Bank", "INTEREST_PAYMENTS"),
    item(7, CAT_SAVER, "2026-10-01T03:00:00.000Z", 3240, "IN", "INTEREST_PAYMENT", "STARLING",
         "9d4f5c7b-0000-4000-8000-000000000003", "Starling Bank", "INTEREST_PAYMENTS"),
    item(8, CAT_SAVER, "2026-11-01T03:00:00.000Z", 3359, "IN", "INTEREST_PAYMENT", "STARLING",
         "9d4f5c7b-0000-4000-8000-000000000003", "Starling Bank", "INTEREST_PAYMENTS"),
    item(9, CAT_SAVER, "2026-11-10T18:00:00.000Z", 200000, "IN", "INTERNAL_TRANSFER", "CATEGORY",
         CAT_PERSONAL, "Personal", "SAVING"),
]}


def starling_routes(feed_status: int = 200) -> dict:
    feed = (lambda body: body) if feed_status == 200 else (lambda body: response(feed_status))
    return {
        # Personal: 1,300 in the main account + 200 in the Holiday Space
        f"/api/v2/accounts/{PERSONAL}/balance": balance(130000, 150000),
        # Easy Saver: 10,000 + the interest above + 2,000 moved in on 10 Nov
        f"/api/v2/accounts/{SAVER}/balance": balance(1209936, 1209936),
        f"/api/v2/feed/account/{PERSONAL}/settled-transactions-between": feed(PERSONAL_FEED),
        f"/api/v2/feed/account/{SAVER}/settled-transactions-between": feed(SAVER_FEED),
        "/api/v2/accounts": ACCOUNTS,
    }


class StarlingInterestTest(TempStore):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(starling, "datetime", FixedNow)
        p.start()
        self.addCleanup(p.stop)

    def test_easy_saver_rate_and_current_account_zero(self):
        fake = self.http(starling_routes())
        res = starling.interest({"token": "tok"})["accounts"]
        saver = res[SAVER]
        # 10,000 in Aug, Sep, Oct with 33.37, 32.40, 33.59 paid on the 1st:
        # 3.99%, worked out in ImpliedRateTest.test_steady_balance_monthly_credits.
        # The 2,000 from Personal on 10 Nov counts (the other account's category).
        self.assertEqual((saver["status"], saver["pct"], saver["from"], saver["to"], saver["basis"]),
                         ("ok", 3.99, "2026-08-01", "2026-10-31", "gross"))
        # Personal: about 3,500 until 10 Nov and no interest at all: 0%. The
        # 200 moved to its own Space (both sides in the feed) changes nothing.
        personal = res[PERSONAL]
        self.assertEqual((personal["status"], personal["pct"], personal.get("zero")), ("ok", 0.0, True))
        feed_call = next(c for c in fake.calls if "settled-transactions-between" in c["url"])
        self.assertIn("minTransactionTimestamp=2026-07-25T00:00:00.000Z", feed_call["url"])
        self.assertIn("maxTransactionTimestamp=2026-11-27T12:00:00.000Z", feed_call["url"])

    def test_interest_sources(self):
        self.assertTrue(starling.is_interest({"direction": "IN", "source": "INTEREST_V2_PAYMENT"}))
        self.assertTrue(starling.is_interest({"direction": "IN", "source": "INTERNAL_TRANSFER",
                                              "spendingCategory": "INTEREST_GAINED"}))
        self.assertFalse(starling.is_interest({"direction": "OUT", "source": "LOAN_INTEREST_CHARGE"}))

    def test_missing_permission_keeps_balances(self):
        self.http(starling_routes(feed_status=403))
        snap = starling.fetch({"token": "tok"})                     # balances: as before
        self.assertEqual([c["balance"] for c in snap.cash], [1500.0, 12099.36])
        res = starling.interest({"token": "tok"})
        self.assertEqual(res["status"], "permission")
        self.assertIn("transaction:read", res["text"])
        self.assertTrue(any("account:read, balance:read and transaction:read" in s for s in res["steps"]))


# ---------------------------------------------------------- Trading 212
def t212_routes(transactions):
    # docs.trading212.com/api: GET /equity/account/summary {id, currency,
    # totalValue, cash {availableToTrade, inPies, reservedForOrders},
    # investments {...}}; history endpoints {items, nextPagePath}. Transactions
    # {amount, currency, dateTime, reference, type WITHDRAW | DEPOSIT | FEE |
    # TRANSFER | INTEREST_ON_FREE_CASH | LENDING_INTEREST}.
    def _get(acct, path):
        if path.startswith("/equity/account/summary"):
            return {"id": 1, "currency": "GBP", "totalValue": 2543.5,
                    "cash": {"availableToTrade": 1543.5, "inPies": 0, "reservedForOrders": 0},
                    "investments": {"currentValue": 1000, "realizedProfitLoss": 0, "totalCost": 1000,
                                    "unrealizedProfitLoss": 0}}
        if path.startswith("/equity/history/transactions"):
            if isinstance(transactions, Exception):
                raise transactions
            return {"items": transactions, "nextPagePath": None}
        if path.startswith("/equity/history/orders"):
            return {"items": [
                {"order": {"status": "FILLED", "side": "BUY", "ticker": "VWRPl_EQ",
                           "createdAt": "2026-11-27T10:00:00Z",
                           "instrument": {"ticker": "VWRPl_EQ", "isin": "IE00BK5BQT80",
                                          "name": "Vanguard FTSE All-World", "currency": "GBP"}},
                 "fill": {"filledAt": "2026-11-27T10:00:01Z", "quantity": 8, "price": 125,
                          "walletImpact": {"currency": "GBP", "netValue": 1000.0, "taxes": []}}}],
                "nextPagePath": None}
        if path.startswith("/equity/history/dividends"):
            return {"items": [{"ticker": "VWRPl_EQ", "amount": 12.5, "amountInEuro": 14.4, "currency": "GBP",
                               "paidOn": "2026-11-27T00:00:00Z", "instrument": {"isin": "IE00BK5BQT80"}}],
                    "nextPagePath": None}
        raise AssertionError(path)
    return _get


def http_error(status):
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(response=resp)


class Trading212InterestTest(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(fx, "_live_rates", return_value=RATES),):
            p.start()
            self.addCleanup(p.stop)
        self.acct = t212.Account("T212 Invest", "key-made-up", "secret-made-up")
        for fn in (t212.account_summary, t212.account_trades, t212.account_dividends,
                   t212.account_transactions, t212.account_currency):
            fn.cache_clear()
            self.addCleanup(fn.cache_clear)

    def test_rate_from_daily_interest_and_rebuilt_cash(self):
        # 3,000 paid in on 19 Aug; 0.31 interest each day 20 Aug-27 Nov (100 days).
        # Today (27 Nov) 1,000 went into a fund, a 12.50 dividend came in and 500
        # was withdrawn: cash now 3,000 + 31.00 - 1,000 + 12.50 - 500 = 1,543.50.
        # Rebuilt, the cash was 3,000 + 0.31 x (credits so far) before today.
        # 90 days used: 27.90 interest on 3,000 + 0.31 x 54.5 = 3,016.90 on average:
        # (1 + 27.90 / 3,016.90) ^ (365/90) - 1 = 3.80%. (A buy counted as money
        # in would give about 2,000 more on every day, and about 2.5%.)
        txs = [{"amount": 3000, "currency": "GBP", "dateTime": "2026-08-19T09:00:00Z",
                "reference": "d-1", "type": "DEPOSIT"},
               {"amount": 500, "currency": "GBP", "dateTime": "2026-11-27T11:00:00Z",
                "reference": "w-1", "type": "WITHDRAW"}]
        txs += [{"amount": 0.31, "currency": "GBP", "dateTime": f"{d.isoformat()}T01:30:00Z",
                 "reference": f"i-{i}", "type": "INTEREST_ON_FREE_CASH"}
                for i, (d, _) in enumerate(daily(date(2026, 8, 20), 100, 0.31))]
        with mock.patch.object(t212, "_get", t212_routes(txs)):
            r = t212.cash_interest(self.acct, today="2026-11-27")
        self.assertEqual((r["status"], r["pct"], r["days"], r["frequency"], r["basis"]),
                         ("ok", 3.8, 90, "daily", "paid"))
        self.assertEqual(interest.note(r, TODAY), "from the interest paid for 29 Aug–26 Nov (as paid out)")

    def test_key_without_history_permission(self):
        with mock.patch.object(t212, "_get", t212_routes(http_error(403))):
            r = t212.cash_interest(self.acct, today="2026-11-27")
        self.assertEqual(r["status"], "permission")
        self.assertIn("history permissions", r["text"])


# ------------------------------------------------------------------ IBKR
# Activity Flex Query, section "Interest Details (Tiers)" -- ibkrguides.com/
# reportingreference/reportguide/interest%20details%20(tiers)fq.htm (Currency,
# Interest Type, Value Date, Tier Break, Balance Threshold, Principal, Rate,
# Total Interest; one row per value date per tier). XML names as in ibflex
# (github.com/csingley/ibflex, Types.py: TierInterestDetail).
FLEX_TIERS = b"""<FlexQueryResponse queryName="Dashboard" type="AF"><FlexStatements count="1">
<FlexStatement accountId="U7654321" fromDate="20251114" toDate="20261113" period="Last365CalendarDays">
<AccountInformation accountId="U7654321" currency="GBP" />
<CashReport>
 <CashReportCurrency accountId="U7654321" currency="GBP" endingCash="12500" levelOfDetail="Currency" />
 <CashReportCurrency accountId="U7654321" currency="USD" endingCash="900" levelOfDetail="Currency" />
</CashReport>
<TierInterestDetails>
 <TierInterestDetail accountId="U7654321" currency="GBP" fxRateToBase="1" interestType="Credit Interest"
   valueDate="20261112" tierBreak="I" balanceThreshold="8000" securitiesPrincipal="8000" commoditiesPrincipal="0"
   ibuklPrincipal="0" totalPrincipal="8000" rate="0" securitiesInterest="0" commoditiesInterest="0"
   ibuklInterest="0" totalInterest="0" code="" fromAcct="" toAcct="" />
 <TierInterestDetail accountId="U7654321" currency="GBP" fxRateToBase="1" interestType="Credit Interest"
   valueDate="20261112" tierBreak="II" balanceThreshold="8000" securitiesPrincipal="4000" commoditiesPrincipal="0"
   ibuklPrincipal="0" totalPrincipal="4000" rate="3.2" securitiesInterest="0.35" commoditiesInterest="0"
   ibuklInterest="0" totalInterest="0.35" code="" fromAcct="" toAcct="" />
 <TierInterestDetail accountId="U7654321" currency="GBP" fxRateToBase="1" interestType="Credit Interest"
   valueDate="20261113" tierBreak="I" balanceThreshold="8000" securitiesPrincipal="8000" commoditiesPrincipal="0"
   ibuklPrincipal="0" totalPrincipal="8000" rate="0" securitiesInterest="0" commoditiesInterest="0"
   ibuklInterest="0" totalInterest="0" code="" fromAcct="" toAcct="" />
 <TierInterestDetail accountId="U7654321" currency="GBP" fxRateToBase="1" interestType="Credit Interest"
   valueDate="20261113" tierBreak="II" balanceThreshold="8000" securitiesPrincipal="4500" commoditiesPrincipal="0"
   ibuklPrincipal="0" totalPrincipal="4500" rate="3.2" securitiesInterest="0.39" commoditiesInterest="0"
   ibuklInterest="0" totalInterest="0.39" code="" fromAcct="" toAcct="" />
 <TierInterestDetail accountId="U7654321" currency="USD" fxRateToBase="0.79" interestType="Debit Interest"
   valueDate="20261113" tierBreak="I" balanceThreshold="100000" securitiesPrincipal="-300" commoditiesPrincipal="0"
   ibuklPrincipal="0" totalPrincipal="-300" rate="6.8" securitiesInterest="-0.06" commoditiesInterest="0"
   ibuklInterest="0" totalInterest="-0.06" code="" fromAcct="" toAcct="" />
</TierInterestDetails>
</FlexStatement></FlexStatements></FlexQueryResponse>"""


class IbkrRateTest(unittest.TestCase):
    def test_stated_tier_rates(self):
        snap = ibkr_flex.parse(ET.fromstring(FLEX_TIERS), "IBKR")
        cash_rows = {c["currency"]: c for c in snap.cash}
        gbp = cash_rows["GBP"]["interest"]
        # latest value date 13 Nov: 8,000 at 0% and 4,500 at 3.2%:
        # (8,000 x 0 + 4,500 x 3.2) / 12,500 = 1.152 -> 1.15% on the whole 12,500
        self.assertEqual((gbp["status"], gbp["source"], gbp["pct"]), ("ok", "provider", 1.15))
        self.assertEqual(gbp["note"], "stated by IBKR for 13 Nov (0% on 8,000, 3.2% on 4,500 GBP), before tax")
        # USD: only interest charged on a short balance, nothing paid
        self.assertEqual(cash_rows["USD"]["interest"]["status"], "none")


# --------------------------------------------------------- Trade Republic
class TradeRepublicRateTest(unittest.TestCase):
    # timeline item as in pytr's tests/events/zinsen.json (github.com/pytr-org/pytr)
    ITEM = {"id": "a7e1c3d2-0000-4000-8000-000000000001", "timestamp": "2026-11-01T06:46:37.846+0000",
            "title": "Zinsen", "subtitle": "2 % p.a.",
            "amount": {"currency": "EUR", "value": 4.87, "fractionDigits": 2},
            "status": "EXECUTED", "eventType": "INTEREST_PAYOUT", "source": "timelineTransaction"}

    def test_stated_rate_from_the_payout(self):
        rec = trl.list_record(self.ITEM, "interest")
        self.assertEqual((rec["amount"], rec["rate"]), (4.87, 2.0))
        _, _, paid, _, _ = trl.build({rec["id"]: rec})
        rate = trl.cash_rate(paid, today=datetime(2026, 11, 27))
        self.assertEqual((rate["status"], rate["source"], rate["pct"]), ("ok", "provider", 2.0))
        self.assertEqual(rate["note"], "stated by Trade Republic with the interest paid on 1 Nov, before tax")

    def test_no_stated_rate(self):
        rec = trl.list_record({**self.ITEM, "subtitle": None}, "interest")
        self.assertNotIn("rate", rec)
        _, _, paid, _, _ = trl.build({rec["id"]: rec})
        self.assertEqual(trl.cash_rate(paid, today=datetime(2026, 11, 27))["status"], "unsupported")
        # a stated rate more than 45 days old is not used
        _, _, old, _, _ = trl.build({"x": trl.list_record(self.ITEM, "interest")})
        self.assertEqual(trl.cash_rate(old, today=datetime(2027, 1, 30))["status"], "unsupported")


# ------------------------------------------------- reaching the cash card
class CashAccountsTest(TempStore):
    """cash.load() with a Starling connection: the rate found, a typed rate
    winning and being cleared, and a token without the permission."""

    def setUp(self):
        super().setUp()
        d = Path(tempfile.mkdtemp())
        for p in (mock.patch.object(cash, "DATA_DIR", d),
                  mock.patch.object(cash, "resolve", side_effect=lambda n: (d / n, (d / n).exists())),
                  mock.patch.object(cash, "_broker_cash", return_value=[]),
                  mock.patch.object(starling, "datetime", FixedNow),
                  mock.patch.object(interest, "ENABLED", True),
                  mock.patch.object(interest, "FIRST_WAIT", 5.0)):
            p.start()
            self.addCleanup(p.stop)
        interest.clear()
        self.addCleanup(interest.clear)
        from backend import connections
        connections.add(connections.new({"id": "starling", "name": "Starling Bank"}, "api", "starling",
                                        "Starling", "", {"token": "made-up-token"}))

    def accounts(self, out):
        return {a["name"]: a for a in out["accounts"]}

    def test_found_rate_typed_rate_wins_and_clears(self):
        self.http(starling_routes())
        acc = self.accounts(cash.load())
        saver, personal = acc["Starling Easy Saver"], acc["Starling Personal"]
        self.assertEqual((saver["rate"], saver["rate_known"], saver["rate_source"]), (3.99, True, "paid"))
        self.assertIn("from the interest paid for 1 Aug–31 Oct", saver["rate_note"])
        self.assertEqual(saver["type"], "savings")                         # from its name
        self.assertEqual((personal["rate"], personal["rate_known"]), (0.0, True))
        self.assertIn("no interest paid", personal["rate_note"])
        for a in acc.values():                                             # internal fields dropped
            self.assertFalse({"ref", "feed", "interest", "connector"} & set(a))
        # 12,099.36 GBP x 1.2 = 14,519.23 EUR at 3.99% = 579.32 a year
        self.assertEqual(saver["interest_eur_yr"], round(14519.23 * 3.99 / 100, 2))

        out = cash.set_auto("Starling Easy Saver", 4.5, "savings")          # typed: wins
        saver = self.accounts(out)["Starling Easy Saver"]
        self.assertEqual((saver["rate"], saver["rate_source"], saver["rate_note"]), (4.5, "typed", "entered by you"))
        self.assertEqual(saver["rate_auto"]["pct"], 3.99)                  # still shown in Settings
        out = cash.set_auto("Starling Easy Saver", None)                    # cleared: automatic again
        saver = self.accounts(out)["Starling Easy Saver"]
        self.assertEqual((saver["rate"], saver["rate_source"]), (3.99, "paid"))

    def test_missing_permission(self):
        self.http(starling_routes(feed_status=403))
        out = cash.load()
        acc = self.accounts(out)
        self.assertEqual(acc["Starling Easy Saver"]["balance"], 12099.36)   # balances still work
        why = acc["Starling Easy Saver"]["rate_auto"]
        self.assertFalse(acc["Starling Easy Saver"]["rate_known"])
        self.assertEqual(why["status"], "permission")
        self.assertIn("transaction:read", why["text"])
        self.assertTrue(why["steps"])
        # unknown, not 0%: both accounts are left out of the interest totals
        self.assertEqual(out["interest_eur_yr"], 0.0)
        self.assertEqual(out["rate_unknown_eur"], round(12099.36 * 1.2, 2) + round(1500 * 1.2, 2))
        from backend import connections
        self.assertEqual(connections.load_all()[0]["status"], "ok")        # the connection is fine

    def test_advisors_and_mcp_use_the_same_rates(self):
        self.http(starling_routes())
        out = cash.load()
        from backend import advisors, mcp_tools

        ctx = type("Ctx", (), {"cash": out})()
        self.assertEqual({a["name"] for a in advisors._bank_accounts(ctx)},
                         {"Starling Easy Saver", "Starling Personal"})
        saver = self.accounts(out)["Starling Easy Saver"]
        self.assertIn("from the interest paid", mcp_tools._rate_source(saver))
        unknown = {"auto": True, "rate_known": False, "rate": 0.0,
                   "rate_auto": {"status": "permission", "text": "Add transaction:read."}}
        self.assertEqual(mcp_tools._rate_source(unknown), "not known: Add transaction:read.")
        self.assertEqual(mcp_tools._rate_source({"rate": 2.0}), "entered by you")


class NotesAndRoutingTest(unittest.TestCase):
    def test_providers_without_an_api_rate_say_why(self):
        for cid in ("wise", "etoro", "bitvavo", "enablebanking", "traderepublic"):
            self.assertIn("Enter it yourself", registry.connector(cid).interest_note, cid)

    def test_trading212_rows_are_routed_and_cleaned(self):
        acct = t212.Account("T212 Invest", "key-made-up", "secret-made-up")
        rows = [{"name": "T212 Invest cash", "institution": "Trading 212", "balance": 100.0,
                 "currency": "GBP", "type": "cash", "auto": True, "feed": "trading212", "ref": "T212 Invest"}]
        found = {"status": "ok", "source": "paid", "pct": 3.8, "from": "2026-08-29", "to": "2026-11-26",
                 "basis": "paid"}
        with mock.patch.object(t212, "accounts", return_value=[acct]), \
                mock.patch.object(t212, "cash_interest", return_value=found), \
                mock.patch.object(interest, "ENABLED", True), mock.patch.object(interest, "FIRST_WAIT", 5.0):
            interest.clear()
            interest.attach(rows)
        interest.clear()
        self.assertEqual(rows[0]["rate_auto"]["pct"], 3.8)
        self.assertNotIn("feed", rows[0])
        self.assertNotIn("ref", rows[0])


class LookupTest(unittest.TestCase):
    def setUp(self):
        interest.clear()
        self.addCleanup(interest.clear)
        p = mock.patch.object(interest, "ENABLED", True)
        p.start()
        self.addCleanup(p.stop)

    def test_background_first_then_cached(self):
        go, calls = threading.Event(), []

        def compute():
            calls.append(1)
            go.wait(5)
            return {"status": "ok", "pct": 1.0}

        self.assertEqual(interest.lookup("k1", compute), {"status": "pending"})   # never waits
        self.assertEqual(interest.public({"status": "pending"})["status"], "checking")
        go.set()
        interest._running.get("k1") and interest._running["k1"].result(5)
        self.assertEqual(interest.lookup("k1", compute)["pct"], 1.0)
        self.assertEqual(interest.lookup("k1", compute)["pct"], 1.0)
        self.assertEqual(len(calls), 1)                                          # cached

    def test_failure_is_a_note_and_retried_sooner(self):
        def boom():
            raise requests.ConnectionError("down")

        with mock.patch.object(interest, "FIRST_WAIT", 5.0):
            res = interest.lookup("k2", boom)
        self.assertEqual(res["status"], "error")
        self.assertLessEqual(interest._results["k2"][1] - __import__("time").time(), interest.RETRY)

    def test_new_credentials_are_checked_again(self):
        self.assertNotEqual(interest.creds_hash({"token": "old"}), interest.creds_hash({"token": "new"}))


if __name__ == "__main__":
    unittest.main()
