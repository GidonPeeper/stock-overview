"""Tax-loss harvesting: positions outside tax-free accounts that are down enough
to sell for a tax loss, what that saves, when you may buy back, and what to buy
meanwhile so you stay invested.

Works per account (a sale happens in one account), on the rows from
main._open_holdings_rows(). Amounts come in as EUR and go out in the tax
currency (GBP for the UK, USD for the US).

The country is the one chosen in Settings (prefs.py; TAX_RESIDENCE in .env is
the fallback): UK, US or NL; any other country (or none) turns the card off.

Settings (.env, all optional):
  CGT_RATE              your rate in %, default 24 (UK) / 15 (US)
  HARVEST_MIN_LOSS_PCT  smallest loss worth flagging, % of cost (default 10)
  HARVEST_MIN_LOSS      smallest loss worth flagging, in GBP/USD (default 250)
  CGT_LOSSES_BROUGHT_FORWARD  UK losses from earlier years you reported, in GBP

UK rules, checked on gov.uk on 2026-11-14:
  * Rates from 6 April 2026: 18% on gains inside the basic-rate band, 24% above
    it and for higher/additional-rate taxpayers. Annual exempt amount £3,000.
    https://www.gov.uk/capital-gains-tax/rates
    https://www.gov.uk/capital-gains-tax/allowances
  * A loss "is deducted from the gains you made in the same tax year" (in full,
    even below the £3,000). Earlier years' losses are used only while "your total
    taxable gain is still above the tax-free allowance". A loss can be claimed
    "up to 4 years after the end of the tax year" of the sale.
    https://www.gov.uk/capital-gains-tax/losses
  * 30-day rule, TCGA 1992 s106A(5): a sale is matched with shares "of the same
    class", "acquired by the same person in the same capacity" "within the 30
    days after the disposal". So a rebuy on day 31 or later is safe.
    https://www.gov.uk/hmrc-internal-manuals/capital-gains-manual/cg51560
  * Bed & ISA: ISA shares are held "in a capacity other than that in which he
    holds any other securities of the same class" (ISA Regulations 1998, reg
    34(2)), so buying back inside an ISA straight away is not matched.
    https://www.legislation.gov.uk/uksi/1998/1870/regulation/34
  * Pooling: "all shares of the same class, in the same company, are held
    together in a 'Section 104 holding'" — across brokers. A sale from one
    account uses the average cost of all non-ISA accounts.
    https://www.gov.uk/government/publications/shares-and-capital-gains-tax-hs284-self-assessment-helpsheet
  * Anti-avoidance (TCGA s16A): HMRC's guidance treats a sale followed by a
    repurchase after more than 30 days, with real market risk in between, as
    outside the rule (CG-APP9, examples 8-9).
    https://www.gov.uk/hmrc-internal-manuals/capital-gains-manual/cg-app9
US (basic): wash sale = buying "substantially identical" stock "within 30 days
  before or after the sale", an IRA purchase included (IRS Pub. 550). Net losses
  offset up to $3,000 of other income a year; the rest carries forward
  (https://www.irs.gov/taxtopics/tc409). https://www.irs.gov/publications/p550
NL: box 3 taxes a fictitious return on what you own on 1 January, or (if lower)
  your actual return including unrealised price changes — never the sale
  itself, so harvesting saves nothing. Checked 2026-11-26: the actual return
  counts "koerswinst of -verlies op aandelen" over the year, and a negative
  year cannot be carried to another year.
  https://www.belastingdienst.nl/wps/wcm/connect/nl/box-3/content/wat-is-mijn-werkelijk-rendement
  Announced, not yet law in force: a new box 3 on actual returns, "waarschijnlijk
  1 januari 2028" (same page). As proposed it taxes shares on yearly price
  changes, sold or not (not checked on an official page): re-check when it passes.
The same-day rule and the exact UK matching order are applied to this year's
gains in uktax.match_disposals(); the cost of each candidate is the broker's
average cost per account (pooled across accounts), which can differ from the
section 104 pool after a sale and rebuy within 30 days.
"""

from __future__ import annotations

import functools
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import sectors, sources, uktax
from .fx import from_eur, to_eur
from .positions import realized_events

RESIDENCES = ("UK", "US", "NL")
CURRENCY = {"UK": "GBP", "US": "USD"}
SYMBOL = {"GBP": "£", "USD": "$"}
# UK 24%: higher/additional rate, https://www.gov.uk/capital-gains-tax/rates
# US 15%: the middle long-term rate, https://www.irs.gov/taxtopics/tc409
DEFAULT_RATE_PCT = {"UK": 24.0, "US": 15.0}
DEFAULT_MIN_LOSS_PCT = 10.0
DEFAULT_MIN_LOSS = 250.0
UK_EXEMPT = uktax.CGT_ANNUAL_EXEMPT_GBP
US_ORDINARY_OFFSET = 3000  # $1,500 if married filing separately (irs.gov/taxtopics/tc409)
REBUY_DAYS = 30  # a rebuy within 30 days after the sale undoes the loss (CG51560; Pub. 550)
CLAIM_YEARS = 4  # UK: claim a loss within 4 years of the end of the tax year (gov.uk/capital-gains-tax/losses)

# Accounts whose name or wrapper says tax-free / tax-deferred.
SHELTERED = {"ISA", "IRA", "ROTH", "SIPP", "401K", "PENSION"}

_LIB_FILE = Path(__file__).with_name("replacements.json")


# ------------------------------------------------------------------ settings
def residence() -> str:
    """UK, US, NL, or OTHER for anywhere else (tax cards off): the country
    chosen in Settings, else TAX_RESIDENCE, else UK (prefs.py)."""
    from . import prefs

    r = {"GB": "UK"}.get(prefs.value("country"), prefs.value("country"))
    return r if r in RESIDENCES else "OTHER"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def _rate(res: str) -> float:
    pct = _env_float("CGT_RATE", DEFAULT_RATE_PCT.get(res, 0.0))
    return pct / 100 if pct > 1 else pct


def _sheltered_name(label: str) -> bool:
    return bool(set(re.split(r"[^A-Z0-9]+", (label or "").upper())) & SHELTERED)


def sheltered(row: dict) -> bool:
    """True for ISA / IRA / pension accounts: no tax on gains, so no loss to use."""
    return (row.get("wrapper") or "").upper() in SHELTERED or _sheltered_name(row.get("broker", ""))


def _day(when: str) -> date | None:
    try:
        return datetime.strptime((when or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _money(amount: float, ccy: str) -> str:
    return f"{SYMBOL.get(ccy, '')}{abs(amount):,.0f}"


# ------------------------------------------------------------- tax arithmetic
def uk_value(net_gain: float, loss: float, rate: float, exempt: float = UK_EXEMPT,
             brought_forward: float = 0.0) -> dict:
    """What realising `loss` (a positive amount) does to this UK tax year.

    `net_gain` is this year's gains minus losses so far. The new loss comes off
    this year's gains in full, even the part the exempt amount already covers
    ("wasted"). Losses from earlier years only bring gains down to the exempt
    amount, so any that are no longer needed stay available ("carried").
    """
    def taxable(net: float) -> tuple[float, float]:
        over = max(net - exempt, 0.0)
        bf_used = min(brought_forward, over)
        return over - bf_used, bf_used

    tax0, bf0 = taxable(net_gain)
    after = net_gain - loss
    tax1, bf1 = taxable(after)
    new_loss = max(-after, 0.0) - max(-net_gain, 0.0)   # this year's net loss grows
    carried = new_loss + (bf0 - bf1)
    saved = rate * (tax0 - tax1)
    return {
        "saved_now": round(saved, 2),
        "carried_forward": round(carried, 2),
        "wasted": round(max(loss - (tax0 - tax1) - carried, 0.0), 2),
        "tax_value": round(saved + rate * carried, 2),
        "taxable_before": round(tax0, 2),
        "taxable_after": round(tax1, 2),
    }


def us_value(net_gain: float, loss: float, rate: float,
             ordinary_offset: float = US_ORDINARY_OFFSET) -> dict:
    """US estimate: a loss offsets this year's net gains, then up to $3,000 of
    other income; the rest carries forward with no time limit."""
    room = max(net_gain, 0.0) + max(ordinary_offset + min(net_gain, 0.0), 0.0)
    used = min(loss, room)
    carried = loss - used
    return {
        "saved_now": round(rate * used, 2),
        "carried_forward": round(carried, 2),
        "wasted": 0.0,
        "tax_value": round(rate * loss, 2),
    }


def rebuy_from(sold_on: date) -> date:
    """First day a rebuy of the same shares (outside an ISA) no longer cancels
    the loss: day 31 after the sale (UK 30-day rule, US wash-sale window)."""
    return sold_on + timedelta(days=REBUY_DAYS + 1)


# ------------------------------------------------------------- replacements
@functools.lru_cache(maxsize=1)
def _library() -> dict:
    lib = json.loads(_LIB_FILE.read_text())
    funds: dict[str, tuple[dict, dict]] = {}
    for group in lib["funds"]:
        for m in group["members"]:
            for k in [m["ticker"], m.get("isin"), *m.get("also", [])]:
                if k:
                    funds[k] = (group, m)
    stocks = {k: s for s in lib["stocks"] for k in s["match"]}
    return {"funds": funds, "stocks": stocks, "sector_etfs": lib["sector_etfs"]}


def _member_keys(m: dict) -> set[str]:
    return {k for k in [m.get("ticker"), m.get("isin"), *m.get("also", [])] if k}


# Yahoo's sector names, in everyday words.
_SECTOR_WORDS = {"Consumer Cyclical": "consumer brands and shops", "Consumer Defensive": "everyday-goods companies",
                 "Financial Services": "banks and financial companies", "Basic Materials": "materials companies",
                 "Communication Services": "media and internet companies", "Healthcare": "healthcare companies",
                 "Technology": "tech companies", "Real Estate": "property companies"}


def _sector_etf(key: str, market: str) -> dict | None:
    etf = (_library()["sector_etfs"].get(key) or {}).get(market)
    if not etf:
        return None
    what = "the same industry" if key.startswith("_") else _SECTOR_WORDS.get(key, f"{key.lower()} shares")
    return {"kind": "similar", "ticker": etf["ticker"], "name": etf["name"],
            "reason": etf.get("reason") or
            f"Sector ETF: keeps you in {what} without betting on one company; it will track differently."}


def replacement_options(row: dict, res: str, sector: str | None = None, limit: int = 3) -> list[dict]:
    """Similar-but-not-identical things to buy right after selling `row`."""
    lib = _library()
    market = "us" if res == "US" else "uk"
    held = {k for k in (row.get("isin"), row.get("ticker")) if k}
    hit = next((lib["funds"][k] for k in (row.get("isin"), row.get("ticker")) if k in lib["funds"]), None)
    if hit:
        group, me = hit
        same, other = [], []
        for m in group["members"]:
            if (m.get("market") or "UK").lower() != market or _member_keys(m) & held:
                continue
            opt = {"kind": "similar", "ticker": m["ticker"], "name": m["name"]}
            if m["index"] == me["index"]:
                # Same index: fine for HMRC (a different fund is a different
                # security) but a share class of the same fund is too close, and
                # in the US two funds on one index may be "substantially identical".
                if market == "us" or m.get("provider") in {me.get("provider")} | {s["provider"] for s in same}:
                    continue
                same.append({**opt, "provider": m.get("provider"), "reason": f"Same index ({m['index']}), different provider: "
                             "a different fund, so buying it does not undo the loss."})
            else:
                other.append({**opt, "reason": m.get("note") or f"Different index ({m['index']}) "
                              "with almost the same companies; tracks very closely, not exactly."})
        opts = same[:2] + other[:1] if market == "uk" else other[:2]
        return opts[:limit] or [_generic_fund(market)]

    stock = next((lib["stocks"][k] for k in (row.get("isin"), row.get("ticker")) if k in lib["stocks"]), None)
    if stock:
        opts = [{"kind": "similar", **p} for p in stock["peers"] if p["ticker"] not in held]
        etf = _sector_etf(stock.get("etf") or stock["sector"], market)
        if etf:
            opts.append(etf)
        return opts[:limit]

    if sector == "ETF / Fund":
        return [_generic_fund(market)]
    etf = _sector_etf(sector, market) if sector else None
    return [etf or _sector_etf("_global", market)]


def _generic_fund(market: str) -> dict:
    if market == "us":
        return {"kind": "similar", "ticker": None, "name": "A fund on a different index",
                "reason": "Two funds on the same index may count as 'substantially identical', "
                          "so pick one that follows a different index."}
    return {"kind": "similar", "ticker": None, "name": "The same index from another provider",
            "reason": "A different fund is a different security, so buying it does not undo the loss."}


def _bed_and_isa(value_gbp: float, isa: dict | None) -> dict | None:
    """Buy the same shares back inside the ISA straight away (UK only)."""
    base = ("Sell here, then buy the same shares straight back inside your ISA. "
            "ISA shares are held separately, so the 30-day rule does not apply, "
            "and future gains are tax-free.")
    if not isa or not isa.get("tracked"):
        return {"kind": "bed_and_isa", "ticker": None, "name": "Buy it back in your ISA",
                "isa_left_gbp": None,
                "reason": base + f" Only if you have ISA allowance left (£{uktax.ISA_ALLOWANCE_GBP:,} a year)."}
    left = float(isa.get("left_gbp") or 0)
    if left <= 0:
        return None
    fits = left >= value_gbp
    return {"kind": "bed_and_isa", "ticker": None, "name": "Buy it back in your ISA",
            "isa_left_gbp": round(left, 2), "fits": fits,
            "reason": base + (f" You have £{left:,.0f} of ISA allowance left this tax year."
                              if fits else
                              f" Your £{left:,.0f} of ISA allowance left covers about "
                              f"{left / value_gbp:.0%} of it; buy a similar fund for the rest.")}


# --------------------------------------------------------------- correlation
# Optional and never blocking: 1-year daily-return correlation between what you
# sell and each swap, computed in a background thread and kept for a day.
_CORR: dict[tuple[str, str], tuple[float | None, float]] = {}
_CORR_TTL = 24 * 3600
_corr_lock = threading.Lock()
_corr_busy = False


def _cached_corr(pairs: set[tuple[str, str]]) -> dict[tuple[str, str], float | None]:
    now = time.time()
    out, missing = {}, []
    for p in pairs:
        hit = _CORR.get(p)
        if hit and now - hit[1] < _CORR_TTL:
            out[p] = hit[0]
        else:
            missing.append(p)
    if missing:
        _start_corr(missing)
    return out


def _start_corr(pairs: list[tuple[str, str]]) -> None:
    global _corr_busy
    with _corr_lock:
        if _corr_busy:
            return
        _corr_busy = True

    def job():
        global _corr_busy
        try:
            _compute_corr(pairs)
        except Exception:
            # retry in an hour rather than on every request
            for p in pairs:
                _CORR[p] = (None, time.time() - _CORR_TTL + 3600)
        finally:
            _corr_busy = False

    threading.Thread(target=job, daemon=True).start()


def _compute_corr(pairs: list[tuple[str, str]]) -> None:
    import yfinance as yf

    tickers = sorted({t for p in pairs for t in p})
    closes = yf.download(tickers, period="1y", interval="1d", progress=False,
                         auto_adjust=True)["Close"]
    rets = closes.pct_change(fill_method=None)
    now = time.time()
    for a, b in pairs:
        value = None
        try:
            both = rets[[a, b]].dropna()
            if len(both) >= 60:
                c = float(both[a].corr(both[b]))
                value = round(c, 2) if c == c else None
        except Exception:
            pass
        _CORR[(a, b)] = (value, now)


# ------------------------------------------------------------------ sectors
_warming: set[str] = set()


def _sector_map(tickers: list[str]) -> dict[str, str]:
    """Sectors from the on-disk cache only; unknown tickers are looked up in
    the background so the request never waits on Yahoo."""
    cache = sectors._load_cache()
    missing = [t for t in set(tickers) if t and t not in cache and t not in _warming]
    if missing:
        _warming.update(missing)
        threading.Thread(target=sectors.get_sectors, args=(missing,), daemon=True).start()
    return {t: cache[t] for t in tickers if t in cache}


# ---------------------------------------------------------------- candidates
def find_candidates(rows: list[dict], min_pct: float, min_loss_eur: float,
                    pooled: bool) -> tuple[list[dict], list[dict]]:
    """Taxable rows down at least `min_pct` % and `min_loss_eur`.

    With `pooled` (UK), the cost of each sale is the average cost across every
    taxable account holding the same share, as HMRC's section 104 pool works.
    Returns (candidates, skipped) where skipped explains rows that look like a
    loss in their own account but not once pooled."""
    taxable = [r for r in rows if not sheltered(r) and r.get("quantity") and r.get("priced_live", True)]
    isin_of = {r["ticker"]: r["isin"] for r in taxable if r.get("isin") and r.get("ticker")}

    def key(r):
        return r.get("isin") or isin_of.get(r.get("ticker")) or r.get("ticker") or r.get("name")

    pools: dict[str, list] = {}
    for r in taxable:
        p = pools.setdefault(key(r), [0.0, 0.0, []])
        p[0] += r["quantity"]
        p[1] += r["cost_eur"]
        p[2].append(r["broker"])

    out, skipped = [], []
    for r in taxable:
        qty, cost_all, accounts = pools[key(r)]
        others = [a for a in accounts if a != r["broker"]] if pooled else []
        cost = cost_all / qty * r["quantity"] if others else r["cost_eur"]
        loss = cost - r["value_eur"]
        if cost <= 0:
            continue
        if loss / cost * 100 >= min_pct and loss >= min_loss_eur:
            out.append({**r, "loss_eur": loss, "loss_pct": loss / cost * 100,
                        "pool_cost_eur": cost, "pooled_with": others})
            continue
        own = r["cost_eur"] - r["value_eur"]
        if others and r["cost_eur"] > 0 and own / r["cost_eur"] * 100 >= min_pct and own >= min_loss_eur:
            skipped.append({"name": r["name"], "account": r["broker"], "pooled_with": others,
                            "reason": f"Down in {r['broker']}, but HMRC averages its cost with "
                                      f"{', '.join(others)}, so selling here gives "
                                      f"{'a gain' if loss < 0 else 'only a small loss'}."})
    return out, skipped


# ------------------------------------------------------------------ the card
def suggest(rows: list[dict], *, today: date | None = None, res: str | None = None,
            min_pct: float | None = None, min_loss: float | None = None,
            uk: dict | None = None, corr: bool = True,
            sector_map: dict[str, str] | None = None) -> dict:
    """Everything the 'Tax-loss harvesting' card shows. `uk` (uktax.summary())
    and `sector_map` can be passed in; otherwise they are read here."""
    asked = today
    today = today or date.today()
    res = (res or residence()).upper()
    if res == "NL":
        return _netherlands()
    if res not in CURRENCY:
        msg = ("Tax-loss tips cover the UK, the US and the Netherlands. "
               "Choose your country in Settings.")
        return {"residence": res, "applies": False, "message": msg, "candidates": [],
                "totals": None, "skipped": [], "notes": [], "how_it_works": [msg], "disclaimer": ""}

    ccy = CURRENCY[res]
    rate = _rate(res)
    min_pct = _env_float("HARVEST_MIN_LOSS_PCT", DEFAULT_MIN_LOSS_PCT) if min_pct is None else min_pct
    min_loss = _env_float("HARVEST_MIN_LOSS", DEFAULT_MIN_LOSS) if min_loss is None else min_loss
    found, skipped = find_candidates(rows, min_pct, to_eur(min_loss, ccy), pooled=(res == "UK"))

    notes: list[str] = []
    recent_buys: dict[str, list[date]] = {}
    if res == "UK":
        if uk is None:
            try:
                uk = uktax.summary(today)
            except Exception:
                uk = None
                notes.append("Couldn't read this tax year's sales, so this assumes no gains yet.")
        uk = uk or {}
        net = float(uk.get("net_gain_gbp") or 0.0)
        gains = float(uk.get("gains_gbp") or 0.0)
        bf = _env_float("CGT_LOSSES_BROUGHT_FORWARD", 0.0)
        year_end = _day(uk.get("to", "")) or uktax.tax_year(today)[1]
        label = uk.get("tax_year") or uktax.tax_year(today)[2]
        isa = uk.get("isa")
        value_of = functools.partial(uk_value, rate=rate, brought_forward=bf)
    else:
        net, recent_buys = _us_year(today)
        gains, bf, isa = max(net, 0.0), 0.0, None
        year_end, label = date(today.year, 12, 31), str(today.year)
        value_of = functools.partial(us_value, rate=rate)

    if sector_map is None:
        sector_map = _sector_map([c["ticker"] for c in found])
    rebuy = rebuy_from(today)
    # Ranked by loss, which is the same as by stand-alone tax value (it grows
    # with the loss). Each line is valued as if the ones above it are sold too,
    # since they share this year's gains, so the lines add up to the total.
    found.sort(key=lambda c: -c["loss_eur"])
    running = net
    cands = []
    for c in found:
        loss = from_eur(c["loss_eur"], ccy)
        value = from_eur(c["value_eur"], ccy)
        v = value_of(running, loss)
        running -= loss
        options = replacement_options(c, res, sector_map.get(c["ticker"]))
        if res == "UK":
            bed = _bed_and_isa(value, isa)
            if bed:
                options.insert(0, bed)
        item = {
            "name": c["name"], "ticker": c["ticker"], "isin": c["isin"], "account": c["broker"],
            "quantity": c["quantity"], "value": round(value, 2), "loss": round(loss, 2),
            "loss_pct": round(c["loss_pct"], 1), "value_eur": c["value_eur"],
            "loss_eur": round(c["loss_eur"], 2), "pooled_with": c["pooled_with"],
            **v, "effect": _effect(v, ccy),
            "sell_by": year_end.isoformat(), "rebuy_from": rebuy.isoformat(),
            "options": options,
        }
        bought = [d for d in recent_buys.get(c["isin"] or c["ticker"], []) if d >= today - timedelta(days=REBUY_DAYS)]
        if bought:
            last = max(bought)
            item["warning"] = (f"You bought more on {last.isoformat()}. Selling at a loss before "
                               f"{rebuy_from(last).isoformat()} makes it a wash sale for those shares.")
        cands.append(item)
    if len(cands) > 1:
        notes.append("Each line assumes you also sell the ones above it, so they add up to the total.")

    if corr:
        pairs = {(c["ticker"], o["ticker"]) for c in cands for o in c["options"]
                 if c["ticker"] and o.get("ticker")}
        known = _cached_corr(pairs)
        for c in cands:
            for o in c["options"]:
                if (c["ticker"], o.get("ticker")) in known:
                    o["corr"] = known[(c["ticker"], o["ticker"])]

    total_loss = sum(c["loss"] for c in cands)
    totals = {"loss": round(total_loss, 2), **value_of(net, total_loss)} if cands else None
    out = {
        "residence": res, "applies": True, "currency": ccy, "today": today.isoformat(),
        "tax_year": label, "sell_by": year_end.isoformat(), "rebuy_from": rebuy.isoformat(),
        "rate_pct": round(rate * 100, 1),
        "thresholds": {"min_loss_pct": min_pct, "min_loss": min_loss},
        "realised": {"gains": round(gains, 2), "net": round(net, 2), "brought_forward": bf},
        "candidates": cands, "totals": totals, "skipped": skipped, "notes": notes,
        "how_it_works": _how(res, rate),
        "disclaimer": "Estimates, not tax advice. Check with HMRC or a tax adviser before you sell."
                      if res == "UK" else "Estimates, not tax advice. Check with a tax adviser before you sell.",
    }
    if res == "UK":
        from . import rules
        out["exempt"] = UK_EXEMPT
        out["outdated"] = rules.uk_notice(asked)   # UK figures for an earlier tax year
        out["claim_by"] = date(year_end.year + CLAIM_YEARS, 4, 5).isoformat()
    out["headline"], out["tip"] = _headline(out, ccy)
    if not cands:
        out["message"] = (f"Nothing to harvest: no position outside "
                          f"{'your ISA' if res == 'UK' else 'tax-advantaged accounts'} is down at least "
                          f"{min_pct:g}% and {_money(min_loss, ccy)}.")
    return out


def _effect(v: dict, ccy: str) -> str:
    """One plain sentence: what selling this one does."""
    m = functools.partial(_money, ccy=ccy)
    saved, carried, wasted = v["saved_now"], v["carried_forward"], v["wasted"]
    parts = [f"Saves about {m(saved)} this year" if saved >= 1 else "Saves nothing this year"]
    if carried >= 1:
        later = v["tax_value"] - saved
        parts.append(f"carries {m(carried)} forward (worth up to {m(later)} against future gains)")
    text = "; ".join(parts) if saved < 1 else " and ".join(parts)
    if wasted >= 1:
        text += f"; {m(wasted)} of it only cancels gains that are tax-free anyway"
    return text + "."


def _headline(out: dict, ccy: str) -> tuple[str, str | None]:
    t = out["totals"]
    if not t:
        return "", None
    net = out["realised"]["net"]
    m = functools.partial(_money, ccy=ccy)
    saves = f"saves about {m(t['saved_now'])}" if t["saved_now"] >= 1 else "saves nothing"
    if out["residence"] == "US":
        return (f"You've realised {m(net) + ' of' if net > 0 else 'no'} net gains this year. Harvesting all of "
                f"these now {saves} this year and carries {m(t['carried_forward'])} forward."), None
    exempt = m(UK_EXEMPT)
    if net <= 0:
        return (f"You have no net gains this tax year, so harvesting saves no tax now; it carries "
                f"{m(t['carried_forward'])} of losses forward to set against future gains."), None
    head = (f"You've realised {m(net)} of gains this tax year; {exempt} is tax-free anyway, so "
            f"harvesting all of these now {saves} this year and carries {m(t['carried_forward'])} forward.")
    tip = None
    if t["wasted"] >= 1:
        useful = max(net - UK_EXEMPT, 0.0)
        tip = (f"{m(t['wasted'])} of these losses would only cancel gains that are already tax-free. "
               + (f"About {m(useful)} of losses is enough this tax year; selling the rest after "
                  f"5 April keeps their full value." if useful >= 1 else
                  f"Your gains are under {exempt}, so selling after 5 April keeps the full loss, "
                  f"unless you expect more gains before then."))
    return head, tip


def _how(res: str, rate: float) -> list[str]:
    pct = f"{rate * 100:g}%"
    if res == "US":
        return [
            "Selling for less than you paid gives you a loss. It cancels gains from the same year, "
            "then up to $3,000 of other income; the rest carries forward with no time limit.",
            "Wash-sale rule: if you buy the same or a 'substantially identical' security in the 30 days "
            "before or after the sale — in any account, your IRA included — the loss doesn't count yet.",
            "To stay invested, buy something similar on a different index or a peer company, and switch "
            "back after 30 days if you like.",
            f"Tax saved is an estimate at {pct} (set CGT_RATE); short-term gains are taxed as income.",
        ]
    return [
        "Selling a share for less than you paid gives you a loss. HMRC lets you use it to cancel "
        "gains, so you pay less Capital Gains Tax.",
        "This year's losses first cancel this year's gains — all of them, even the £3,000 that is "
        "tax-free anyway. What is left carries forward.",
        "Losses from earlier years are only used to bring gains down to £3,000. Report a loss to HMRC "
        "within 4 years of the end of the tax year, or you can't use it.",
        "If you buy the same shares back outside an ISA within 30 days, HMRC ignores the sale. Wait "
        "until the rebuy date, buy them back inside your ISA, or buy something similar but not the same. "
        "Pause any regular buys of it outside your ISA too.",
        "If you hold the same share in more than one account outside your ISA, HMRC averages the cost "
        "across them, so the loss here uses that average.",
        f"Tax saved is an estimate at {pct}: the rate for higher-rate taxpayers (18% on gains inside "
        "the basic-rate band; set CGT_RATE). Amounts use today's exchange rate; HMRC uses the rate on "
        "each trade date.",
        "Selling costs fees and spread, and a similar fund will not move exactly the same while you wait.",
    ]


def _netherlands() -> dict:
    msg = ("In the Netherlands, box 3 taxes a fictitious return on what you own on 1 January (or your "
           "actual return, price changes included, if that is lower) — not the gains you realise when "
           "you sell. Selling at a loss saves no tax, so there is nothing to harvest.")
    return {"residence": "NL", "applies": False, "message": msg, "candidates": [], "totals": None,
            "skipped": [], "notes": [], "how_it_works": [msg],
            "disclaimer": "Not tax advice. Check with the Belastingdienst or a tax adviser."}


def _us_year(today: date) -> tuple[float, dict[str, list[date]]]:
    """US: net realised gain this calendar year in USD (taxable accounts), and
    recent buys in every account (an IRA buy also triggers a wash sale)."""
    start = date(today.year, 1, 1)
    net = 0.0
    buys: dict[str, list[date]] = {}
    for name, (is_free, get_trades) in sources.trade_sources().items():
        try:
            trades = get_trades()
        except Exception:
            continue
        for t in trades:
            d = _day(t.when)
            if t.side == "BUY" and d and today - timedelta(days=REBUY_DAYS) <= d <= today:
                for k in {t.key, t.ticker} - {""}:
                    buys.setdefault(k, []).append(d)
        if is_free or _sheltered_name(name):
            continue
        for ev in realized_events(sources.in_eur(trades)):
            d = _day(ev["when"])
            if d and start <= d <= today:
                net += ev["realized"]
    return from_eur(net, "USD"), buys
