"""Invariants of the core numbers, on many made-up cases.

1. Seeded random trade sequences (hundreds of fractional trades, full exits,
   re-entries, sales bigger than the holding, several trades at one moment,
   date-only, "T" and " " times, newest-first listings) run through
   positions.compute_positions / realized_events and through a plain
   re-implementation below that uses exact fractions. They must agree.
   A cash check that needs no average cost at all: when no sale exceeds the
   holding, realized - remaining cost = sum of sales - sum of buys.

2. The totals identities on random holding sets and on the bundled sample
   data (prices mocked). The rounding rule they rely on:
     every amount is computed unrounded and rounded to the cent once, where
     it is returned. A total therefore equals its exact sum rounded, and
     differs from the sum of its n rounded parts by at most n x 0.005.
   With that rule:
     total value = sum of rows = sum per account = sum per sector = sum of merged holdings
     total cost = sum of rows' cost
     unrealized = value - cost (a broker's own P/L, Trading 212's, wins for its
       row: the row's cost is value - broker P/L, so the identity still holds)
     total return = unrealized + realized + dividends + interest + fees
     today's total = sum of the rows' today = the Performance card's "today"

No network: quotes, history and FX are mocked; stdlib random with fixed seeds.
"""

import itertools
import json
import random
import unittest
from collections import defaultdict
from datetime import date, datetime, timedelta
from fractions import Fraction
from unittest import mock

from backend import cache, fx, periods
from backend.connectors.base import Holding
from backend.positions import Trade, compute_positions, realized_events

DUST = Fraction(1, 10**9)
RATES = {"EUR": 1.0, "USD": 0.8, "GBP": 1.25, "CHF": 1.0}


# ------------------------------------------------- 1. the average-cost engine
def reference(listed: list[Trade]) -> dict[str, dict]:
    """Average cost, written out plainly with exact fractions.

    Order: by time (date-only = midnight; "T" or " " between date and time).
    Trades at the same moment keep the order listed, unless that order would
    sell more than is held at some point and the moment has a buy: then the
    moment's buys go first. A sale meets at most the shares held; only that
    share of its proceeds counts, and the holding never goes below zero."""
    def moment(t):
        return datetime.fromisoformat(t.when.replace("Z", ""))

    state = defaultdict(lambda: {"q": Fraction(0), "c": Fraction(0), "realized": Fraction(0),
                                 "matched_proceeds": Fraction(0), "oversold": False,
                                 "events": []})
    for _, group in itertools.groupby(sorted(listed, key=moment), key=moment):
        group = list(group)
        for key in dict.fromkeys(t.key for t in group):
            mine = [t for t in group if t.key == key]
            q, short = state[key]["q"], False
            for t in mine:
                q += Fraction(t.quantity) if t.side == "BUY" else -Fraction(t.quantity)
                short = short or q < -DUST
            if short and any(t.side == "BUY" for t in mine):
                mine = [t for t in mine if t.side == "BUY"] + [t for t in mine if t.side != "BUY"]
            s = state[key]
            for t in mine:
                n, amount = Fraction(t.quantity), Fraction(t.amount)
                if t.side == "BUY":
                    s["q"] += n
                    s["c"] += amount
                    continue
                if n > s["q"] + DUST:
                    s["oversold"] = True
                if s["q"] > DUST:
                    sold = min(n, s["q"])
                    proceeds = amount * sold / n
                    cost = s["c"] * sold / s["q"]
                    s["realized"] += proceeds - cost
                    s["matched_proceeds"] += proceeds
                    s["c"] -= cost
                    s["events"].append(proceeds - cost)
                s["q"] = max(s["q"] - n, Fraction(0))
    return state


def random_trades(rng: random.Random, clean: bool = False) -> list[Trade]:
    """A made-up trade history in the order a statement might list it.
    clean: no sale bigger than the holding (exits sell exactly what is held)."""
    keys = [f"XX00000000{k}" for k in range(rng.randint(1, 3))]
    held = {k: 0.0 for k in keys}
    trades: list[Trade] = []
    day = date(2020, 1, 1)
    for _ in range(rng.randint(40, 300)):
        day += timedelta(days=rng.choice([1, 1, 2, 5]))
        kind = rng.choice(["date", "space", "iso"])
        n_at_once = rng.choice([1, 1, 1, 2, 3])    # several trades at one moment
        minute = rng.randint(60, 1380)
        stamp = {"date": day.isoformat(),
                 "space": f"{day.isoformat()} {minute // 60:02d}:{minute % 60:02d}",
                 "iso": f"{day.isoformat()}T{minute // 60:02d}:{minute % 60:02d}:00Z"}[kind]
        group = []
        for _ in range(n_at_once):
            k = rng.choice(keys)
            price = rng.uniform(1, 500)
            r = rng.random()
            if held[k] <= 0 or r < 0.45:
                qty = rng.randint(1, 50000) / 1000          # fractional shares
                side = "BUY"
                held[k] += qty
            else:
                side = "SELL"
                if r < 0.65:
                    qty = round(held[k] * rng.uniform(0.05, 0.95), 3) or 0.001
                elif clean:
                    qty = held[k]                            # exact full exit
                elif r < 0.85:
                    qty = round(held[k], 6)                  # full exit: dust or a hair over
                elif r < 0.95:
                    qty = round(held[k] * 1.25 + 1, 3)       # more than is held
                else:
                    qty = rng.randint(1, 5000) / 1000        # from nothing recorded
                qty = qty or 0.001                           # never a sale of nothing
                held[k] = max(held[k] - qty, 0.0)
            group.append(Trade(stamp, k, k, side, qty, round(qty * price, 2)))
        if rng.random() < 0.5:
            group.reverse()                                  # listed newest first
        trades.extend(group)
    order = rng.choice(["oldest first", "newest first", "shuffled"])
    if order == "newest first":
        trades.reverse()
    elif order == "shuffled":
        moments = [list(g) for _, g in itertools.groupby(trades, key=lambda t: t.when)]
        rng.shuffle(moments)                                 # moments move, ties keep their order
        trades = [t for g in moments for t in g]
    return trades


class AverageCostInvariantsTest(unittest.TestCase):
    SEEDS = range(300)

    def test_engine_matches_exact_reference(self):
        for seed in self.SEEDS:
            trades = random_trades(random.Random(seed))
            got = compute_positions(trades)
            want = reference(trades)
            scale = 1e-9 * (1 + sum(abs(t.amount) for t in trades))
            events = defaultdict(list)
            for ev in realized_events(trades):
                events[ev["key"]].append(ev)
            for key, w in want.items():
                p = got[key]
                msg = f"seed {seed}, {key}"
                self.assertAlmostEqual(p.quantity, float(w["q"]), delta=1e-6, msg=msg)
                self.assertAlmostEqual(p.cost, float(w["c"]), delta=scale, msg=msg)
                self.assertAlmostEqual(p.realized, float(w["realized"]), delta=scale, msg=msg)
                self.assertGreaterEqual(p.quantity, 0.0, msg=msg)
                self.assertEqual(p.is_open, w["q"] > Fraction(1, 10**4), msg=msg)
                # per-sale events add up to the position's realized P/L
                evs = events.get(key, [])
                self.assertEqual(len(evs), len(w["events"]), msg=msg)
                self.assertAlmostEqual(sum(e["realized"] for e in evs), p.realized,
                                       delta=scale, msg=msg)
                self.assertAlmostEqual(sum(e["proceeds"] for e in evs),
                                       float(w["matched_proceeds"]), delta=scale, msg=msg)

    def test_cash_check_without_average_cost(self):
        # When no sale exceeds the holding, every euro in and out is
        # accounted for: realized - remaining cost = sales - buys.
        checked = 0
        for seed in self.SEEDS:
            trades = random_trades(random.Random(seed), clean=True)
            want = reference(trades)
            got = compute_positions(trades)
            for key, w in want.items():
                if w["oversold"]:
                    continue
                buys = sum(Fraction(t.amount) for t in trades if t.key == key and t.side == "BUY")
                sells = sum(Fraction(t.amount) for t in trades if t.key == key and t.side == "SELL")
                p = got[key]
                scale = 1e-9 * (1 + float(buys + sells))
                self.assertAlmostEqual(p.realized - p.cost, float(sells - buys), delta=scale,
                                       msg=f"seed {seed}, {key}")
                checked += 1
        self.assertGreater(checked, 50)

    def test_listing_order_of_separate_moments_does_not_matter(self):
        for seed in range(50):
            trades = random_trades(random.Random(seed))
            moments = [list(g) for _, g in itertools.groupby(
                sorted(trades, key=lambda t: t.when.replace("T", " ")),
                key=lambda t: t.when)]
            random.Random(seed + 1000).shuffle(moments)
            a = compute_positions(trades)
            b = compute_positions([t for g in moments for t in g])
            for key in a:
                self.assertAlmostEqual(a[key].realized, b[key].realized, places=6)
                self.assertAlmostEqual(a[key].quantity, b[key].quantity, places=9)

    def test_accounts_keep_separate_pools_but_can_be_pooled(self):
        # The same share at two brokers: per account, each pool realizes
        # against its own cost; pooled (UK tax) against the blended cost.
        a = [Trade("2024-01-02 10:00", "S", "S", "BUY", 10, 1000.0),
             Trade("2024-03-01 10:00", "S", "S", "SELL", 10, 1500.0)]
        b = [Trade("2024-02-01 10:00", "S", "S", "BUY", 10, 2000.0)]
        self.assertAlmostEqual(compute_positions(a)["S"].realized, 500.0)   # 1500 - 1000
        self.assertAlmostEqual(compute_positions(b)["S"].realized, 0.0)
        owner = {id(t): "A" for t in a} | {id(t): "B" for t in b}
        evs = realized_events(a + b, owner)
        # pooled: 20 for 3000 (avg 150), sell 10 for 1500: 1500 - 1500 = 0
        self.assertEqual([(e["account"], round(e["realized"], 2)) for e in evs], [("A", 0.0)])


# ------------------------------------------------- 2. totals identities
def random_world(rng: random.Random) -> tuple[list[Holding], dict]:
    """Random holdings at up to three accounts, some shares at several,
    some broker-priced (with the broker's own P/L), some with no quote."""
    holdings, quotes = [], {}
    for n in range(rng.randint(1, 9)):
        isin = f"XX{n:010d}"
        ticker = rng.choice(["", f"T{n}", f"T{n}.L", f"T{n}.AS"])
        ccy = "GBP" if ticker.endswith(".L") else "EUR" if ticker.endswith(".AS") else "USD"
        if ticker and rng.random() < 0.85:
            price = round(rng.uniform(0.5, 900), 4)
            prev = price if rng.random() < 0.3 else round(price * rng.uniform(0.9, 1.1), 4)
            quotes[ticker] = {"price": price, "prev_close": prev, "currency": ccy}
        for broker in rng.sample(["Acct A", "Acct B", "Acct C"], rng.randint(1, 3)):
            qty = round(rng.uniform(0.001, 400), 6)
            buy = round(rng.uniform(0.5, 900), 4)
            if rng.random() < 0.3:     # broker-priced, with its own P/L in EUR
                own = round(rng.uniform(0.5, 900), 4)
                holdings.append(Holding(broker, ticker, f"Name {n}", qty, buy, ccy, isin=isin,
                                        price=own, broker_profit_eur=round(rng.uniform(-500, 500), 2)))
            else:
                holdings.append(Holding(broker, ticker, f"Name {n}", qty, buy, ccy, isin=isin,
                                        buy_currency=rng.choice([None, "EUR"])))
    return holdings, quotes


class _Identities:
    """Checks shared by the random and the sample-data cases."""

    def check_identities(self, main, msg=""):
        rows, total_value, total_cost, by_broker = main._open_holdings_rows()
        out = main.portfolio()
        json.dumps(out, allow_nan=False)        # no NaN or infinity anywhere
        t = out["totals"]
        n = max(len(rows), 1)
        exact = [r["_exact"] for r in rows]

        # exact sums
        self.assertAlmostEqual(sum(e["value_eur"] for e in exact), total_value, places=6, msg=msg)
        self.assertAlmostEqual(sum(e["cost_eur"] for e in exact), total_cost, places=6, msg=msg)
        self.assertAlmostEqual(sum(e["profit_eur"] for e in exact), total_value - total_cost,
                               places=6, msg=msg)
        # rounded once, from the exact sums
        self.assertEqual(t["value"], round(total_value, 2), msg)
        self.assertEqual(t["cost"], round(total_cost, 2), msg)
        self.assertEqual(t["unrealized"], round(total_value - total_cost, 2), msg)
        # each displayed part within half a cent of its exact amount
        for r in rows:
            for k in ("value_eur", "cost_eur", "profit_eur"):
                self.assertLessEqual(abs(r[k] - r["_exact"][k]), 0.005 + 1e-9, msg)
        # total = sum of rows = per account = per sector = merged holdings
        tol = lambda parts: 0.005 * max(parts, 1) + 1e-6
        self.assertLessEqual(abs(t["value"] - sum(r["value_eur"] for r in rows)), tol(n), msg)
        self.assertLessEqual(abs(t["value"] - sum(b["value"] for b in out["by_broker"])),
                             tol(len(out["by_broker"])), msg)
        self.assertLessEqual(abs(t["value"] - sum(s["value"] for s in out["by_sector"])),
                             tol(len(out["by_sector"])), msg)
        self.assertLessEqual(abs(t["value"] - sum(h["value_eur"] for h in out["holdings"])),
                             tol(len(out["holdings"])), msg)
        for b in out["by_broker"]:
            mine = sum(r["_exact"]["value_eur"] for r in rows if r["broker"] == b["broker"])
            self.assertEqual(b["value"], round(mine, 2), msg)
        # a merged holding is its rows' exact sum, rounded once
        for h in out["holdings"]:
            mine = [r for r in rows if (r["isin"] or r["ticker"] or r["name"]) ==
                    (h["isin"] or h["ticker"] or h["name"])]
            v = sum(r["_exact"]["value_eur"] for r in mine)
            c = sum(r["_exact"]["cost_eur"] for r in mine)
            p = sum(r["_exact"]["profit_eur"] for r in mine)
            self.assertEqual(h["value_eur"], round(v, 2), msg)
            self.assertEqual(h["profit_eur"], round(p, 2), msg)
            self.assertEqual(h["profit_pct"], round(p / c * 100 if c else 0, 2), msg)
            self.assertAlmostEqual(h["quantity"], round(sum(r["quantity"] for r in mine), 4),
                                   places=6, msg=msg)
        # unrealized = value - cost, within the rounding of the three figures
        self.assertLessEqual(abs(t["unrealized"] - (t["value"] - t["cost"])), 0.01 + 1e-9, msg)
        # total return = unrealized + realized + dividends + interest + fees
        self.assertLessEqual(abs(t["total_return"] - (t["unrealized"] + t["realized"] + t["dividends"]
                                                      + t["interest"] + t["fees"])), 0.01 + 1e-9, msg)
        self.assertEqual(t["all_time"], round(total_value - total_cost + t["realized"], 2), msg)
        # today: the rows' exact sum, once; the merged holdings add up to it
        daily_exact = sum(e["daily_eur"] for r, e in zip(rows, exact) if r["daily_eur"] is not None)
        self.assertEqual(t["daily"], round(daily_exact, 2), msg)
        merged_daily = [h["daily_eur"] for h in out["holdings"] if h["daily_eur"] is not None]
        self.assertLessEqual(abs(t["daily"] - sum(merged_daily)), tol(len(merged_daily)), msg)
        # a row with no price at all is valued at cost and flagged
        for r in rows:
            if r["_mkt"]["price"] is None and not r["_mkt"]["own_price"]:
                self.assertAlmostEqual(r["_exact"]["value_eur"], r["_exact"]["cost_eur"], places=6,
                                       msg=msg)
                self.assertFalse(r["priced_live"], msg)
                self.assertIsNone(r["daily_eur"], msg)
        return out


class RandomTotalsTest(_Identities, unittest.TestCase):
    def setUp(self):
        from backend import main
        self.main = main
        cache.clear()
        self.addCleanup(cache.clear)
        inc = {"dividends": 12.34, "interest": 5.67, "total": 18.01, "fees": -3.21, "by_broker": {}}
        for p in (mock.patch.object(fx, "_live_rates", return_value=dict(RATES)),
                  mock.patch.object(main.realized, "summary",
                                    return_value={"total_realized_eur": 100.05}),
                  mock.patch.object(main.income, "summary", return_value=inc),
                  mock.patch.object(main, "_demo", return_value=False),
                  mock.patch.object(main.sectors, "get_sectors",
                                    side_effect=lambda ts: {t: ("Tech" if len(t) % 2 else "Energy")
                                                            for t in ts})):
            p.start()
            self.addCleanup(p.stop)

    def test_identities_hold_for_random_holdings(self):
        for seed in range(60):
            holdings, quotes = random_world(random.Random(seed))
            with mock.patch.object(self.main.sources, "holdings", return_value=holdings), \
                    mock.patch.object(self.main, "fetch_quotes", return_value=quotes):
                cache.clear()
                out = self.check_identities(self.main, msg=f"seed {seed}")
                closes = {tk: [(date(2025, 12, 30), q["prev_close"] * 0.9),
                               (date(2026, 11, 20), q["prev_close"])] for tk, q in quotes.items()}
                # no chart history here: this test is about today's figure
                with mock.patch.object(periods, "_closes", return_value=closes), \
                        mock.patch.object(periods, "quote_scale", return_value=1.0), \
                        mock.patch.object(self.main, "_history", return_value=[]):
                    prof = self.main.profit()
            if quotes:
                self.assertEqual(prof["periods"]["today"], out["totals"]["daily"], f"seed {seed}")


class SampleDataTotalsTest(_Identities, unittest.TestCase):
    """The bundled sample data (demo mode), Yahoo and FX mocked as in test_mcp."""

    @classmethod
    def setUpClass(cls):
        from tests.test_mcp import sample_world
        sample_world(cls)

    def test_identities_hold_on_the_sample_data(self):
        from backend import main, realized
        out = self.check_identities(main, msg="sample data")
        self.assertGreater(len(out["holdings"]), 3)
        rows = main._open_holdings_rows()[0]
        closes = {r["ticker"]: [(date(2025, 12, 30), 1.0), (date(2026, 11, 20), r["_mkt"]["prev"])]
                  for r in rows if r["ticker"] and r["_mkt"]["prev"]}
        with mock.patch.object(periods, "_closes", return_value=closes):
            cache.clear()
            self.assertEqual(main.profit()["periods"]["today"], main.portfolio()["totals"]["daily"])
        # realized: total = closed + trims = sum per account (each rounded once)
        r = realized.summary()
        self.assertLessEqual(abs(r["total_realized_eur"] - (r["closed_total_eur"] + r["trims_total_eur"])),
                             0.01 + 1e-9)
        self.assertLessEqual(abs(r["total_realized_eur"] - sum(r["by_broker"].values())),
                             0.005 * len(r["by_broker"]) + 1e-9)
        self.assertLessEqual(abs(r["closed_total_eur"] - sum(c["realized_eur"] for c in r["closed"])),
                             0.005 * max(len(r["closed"]), 1) + 1e-9)


if __name__ == "__main__":
    unittest.main()
