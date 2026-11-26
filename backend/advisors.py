"""Ways to save: money-saving checks run on your own numbers.

Each check reads data the app already has (holdings per account, the UK
tax-year summary, the dividend forecast, and the loans, income and cash
accounts you enter in Settings) and returns a tip only when there is money to
save. Every tip says how much (per year, or once), how that was worked out,
which data it used, any deadline, and what to do.

  * Missing data: a check that needs something you have not entered is
    skipped and adds a one-line prompt ("Add your income in Settings to ...").
    A check that runs and finds nothing goes on the `all_clear` list.
  * Amounts: tax works in pounds, so checks compute in GBP. The API returns
    EUR like every other endpoint (`saving_eur`). In text, a computed amount
    is written as a token, {{1234.56}} = an amount in EUR, which the browser
    shows in the viewer's currency. Fixed rules (the £20,000 ISA allowance)
    are written in pounds.
  * The country chosen in Settings (fallback TAX_RESIDENCE in .env, default
    UK): any other country turns the UK-only checks off; fund fees and
    loan-versus-cash still run.
  * UK_RESIDENT_FROM (env, optional, YYYY-MM-DD): enables the check for the
    4-year foreign income and gains regime for new UK residents.

Rules of thumb with the working shown. Not tax or financial advice.
"""

from __future__ import annotations

import functools
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import paye as _paye
from .fx import from_eur, to_eur
from .instruments import is_valid_isin

# ===========================================================================
#  UK RATES AND ALLOWANCES, 2026/27. UPDATE AFTER EACH BUDGET.
#
#  Checked on gov.uk on 2026-11-14 and again on 2026-11-26, after the Budget
#  of 28 October 2026. Its rates annex leaves every 2026/27 figure below
#  unchanged (and every 2027/28 one too, except savings rates):
#  https://www.gov.uk/government/publications/budget-2026-overview-of-tax-legislation-and-rates-ootlar/annex-a-rates-and-allowances
#  What that Budget did change, as far as these checks go:
#    * tax on savings interest rises 4 points from 6 April 2027 (to 24/44/49%);
#    * the planned cut to the cash ISA limit was dropped: cash can still use
#      the whole £20,000 ISA allowance
#      (https://www.gov.uk/government/publications/budget-2026-document/budget-2026-html).
#  The same figures for the ISA, CGT and dividend allowances live in uktax.py;
#  tests/test_advisors.py fails if the two drift apart.
# ===========================================================================
TAX_YEAR = "2026/27"
RATES_CHECKED = "2026-11-26"

# https://www.gov.uk/individual-savings-accounts/how-isas-work
ISA_ALLOWANCE = 20_000
# https://www.gov.uk/capital-gains-tax/allowances
CGT_EXEMPT = 3_000
# https://www.gov.uk/capital-gains-tax/rates (18% within the basic-rate band, else 24%)
CGT_RATE = {"basic": 0.18, "higher": 0.24, "additional": 0.24}
# https://www.gov.uk/tax-on-dividends
DIVIDEND_ALLOWANCE = 500
DIVIDEND_RATE = {"basic": 0.1075, "higher": 0.3575, "additional": 0.3935}
# https://www.gov.uk/income-tax-rates (England, Wales and Northern Ireland)
PERSONAL_ALLOWANCE = 12_570
HIGHER_FROM = 50_270
ADDITIONAL_FROM = 125_140
INCOME_RATE = {"basic": 0.20, "higher": 0.40, "additional": 0.45}
# https://www.gov.uk/income-tax-rates/income-over-100000
# (the personal allowance falls by £1 for every £2 above this)
TAPER_FROM = 100_000
# https://www.gov.uk/apply-tax-free-interest-on-savings/how-much-is-tax-free
SAVINGS_ALLOWANCE = {"basic": 1_000, "higher": 500, "additional": 0}
# (same page) up to £5,000 of interest is tax-free when other income is low:
# every £1 of other income above the personal allowance cuts it by £1
STARTING_RATE_FOR_SAVINGS = 5_000
SAVINGS_RATE = {"basic": 0.20, "higher": 0.40, "additional": 0.45}
SAVINGS_RATE_RISE_APRIL_2027 = 0.04  # Budget 2026 rates annex (link above)
# https://www.gov.uk/tax-on-your-private-pension/annual-allowance
PENSION_ANNUAL_ALLOWANCE = 60_000
# https://www.gov.uk/guidance/pension-schemes-work-out-your-tapered-annual-allowance
# (threshold income over £200,000 and adjusted income over £260,000: the
#  allowance falls £1 for every £2 above £260,000, to no less than £10,000)
TAPER_THRESHOLD_INCOME = 200_000
TAPER_ADJUSTED_INCOME = 260_000
TAPERED_ALLOWANCE_MIN = 10_000
# https://www.gov.uk/government/publications/increasing-normal-minimum-pension-age
PENSION_ACCESS_AGE = "55 (57 from 6 April 2028)"
# https://www.gov.uk/tax-on-your-private-pension/pension-tax-relief
# (relief at source adds 20%; higher and additional rate claim the rest)
# https://www.gov.uk/scottish-income-tax (different bands: 19% to 48%)
# https://www.gov.uk/government/publications/foreign-income-and-gains-fig-regime-self-assessment-helpsheet-hs266/hs266-foreign-income-and-gains-fig-regime-2026
# (first 4 tax years after 10 years abroad; a claim costs the personal
#  allowance and the CGT annual exempt amount for that year)
FIG_YEARS = 4
# https://www.gov.uk/hmrc-internal-manuals/investment-funds/ifm12146
# (gains on a non-reporting offshore fund are taxed as income)
REPORTING_FUNDS_LIST = ("https://www.gov.uk/government/publications/"
                        "offshore-funds-list-of-reporting-funds")
# ===========================================================================

# Assumptions (not tax rules): change freely.
ASSUMED_GROWTH = 0.05      # yearly growth used to value tax-free growth
CASH_BUFFER_MONTHS = 3     # months of payments kept as cash before repaying a loan
MIN_SAVING_GBP = 5         # tips worth less than this are not shown
RATE_MARGIN_PCT = 0.25     # loan vs cash: ignore rate gaps smaller than this
MAX_ITEMS = 4              # holdings listed per tip

DISCLAIMER = ("Not tax or financial advice: rough estimates from your own data. "
              "Check the rules or ask an adviser before you act.")

FUND_FEES_FILE = Path(__file__).with_name("fund_fees.json")

_FUND_WORDS = ("ETF", "UCITS", "FUND", "ISHARES", "VANGUARD", "SPDR", "XTRACKERS",
               "AMUNDI", "LYXOR", "WISDOMTREE", "INVESCO QQQ", "INDEX TRUST")
_NOT_FUND_WORDS = ("ETC", "ETN")  # exchange-traded commodities/notes are not funds


# --------------------------------------------------------------- inputs
@dataclass
class Inputs:
    rows: list[dict]                   # per-account holdings (main._open_holdings_rows)
    tax: dict | None = None            # uktax.summary()
    fin: dict | None = None            # finances.load()
    cash: dict | None = None           # cash.load()
    dividends: dict | None = None      # reports.income_projection(); None = not ready
    foreign_gains_gbp: float = 0.0     # realised gains on non-UK assets this tax year, outside ISAs
    today: date = field(default_factory=date.today)
    residence: str = "UK"
    resident_from: date | None = None  # UK_RESIDENT_FROM
    demo: bool = False                 # demo mode: sample finances/cash count as data


def residence() -> str:
    """Same reading as the tax-loss card (blank or unknown values mean UK)."""
    from .harvest import residence as _shared
    return _shared()


# --------------------------------------------------------------- helpers
def _gbp(eur: float) -> float:
    return from_eur(eur, "GBP")


def _m(gbp: float) -> str:
    """Token for an amount in text, in EUR; the browser formats it."""
    return "{{%.2f}}" % to_eur(gbp, "GBP")


def _pct(x: float) -> str:
    return f"{x * 100:g}%"


def _band(income: float | None) -> str:
    if income is None or income <= HIGHER_FROM:
        return "basic"
    return "higher" if income <= ADDITIONAL_FROM else "additional"


def _personal_allowance(income: float) -> float:
    return max(0.0, PERSONAL_ALLOWANCE - max(0.0, income - TAPER_FROM) / 2)


def _annual_allowance(threshold_income: float, adjusted_income: float) -> float:
    """Pension annual allowance after the taper for high incomes."""
    if threshold_income <= TAPER_THRESHOLD_INCOME or adjusted_income <= TAPER_ADJUSTED_INCOME:
        return PENSION_ANNUAL_ALLOWANCE
    return max(PENSION_ANNUAL_ALLOWANCE - (adjusted_income - TAPER_ADJUSTED_INCOME) / 2,
               TAPERED_ALLOWANCE_MIN)


def _tax_year(d: date) -> tuple[date, date]:
    y = d.year if d >= date(d.year, 4, 6) else d.year - 1
    return date(y, 4, 6), date(y + 1, 4, 5)


def _is_isa(r: dict) -> bool:
    return (r.get("wrapper") or "").upper() == "ISA"


def _is_fund(r: dict) -> bool:
    name = f" {(r.get('name') or '').upper()} "
    if any(f" {w} " in name for w in _NOT_FUND_WORDS):
        return False
    return any(w in name for w in _FUND_WORDS) or (r.get("isin") or "") in _fund_isins()


def _country(r: dict) -> str:
    isin = (r.get("isin") or "").upper()
    return isin[:2] if is_valid_isin(isin) else ""


def _label(r: dict) -> str:
    return f"{r.get('name') or r.get('ticker')} ({r.get('broker')})"


@functools.lru_cache(maxsize=1)
def _fund_table() -> list[dict]:
    try:
        return json.loads(FUND_FEES_FILE.read_text()).get("groups", [])
    except (OSError, ValueError):
        return []


@functools.lru_cache(maxsize=1)
def _fund_isins() -> frozenset[str]:
    return frozenset({f["isin"] for g in _fund_table() for f in g["funds"]} |
                     {g["cheaper"]["isin"] for g in _fund_table()})


def _bank_accounts(c: "_Ctx") -> list[dict]:
    """Cash and savings accounts with an interest rate entered in Settings
    (not pensions). An account that updates itself counts once its rate has
    been entered: until then its rate is unknown, not 0%."""
    return [a for a in ((c.cash or {}).get("accounts") or [])
            if a.get("type") in ("cash", "savings") and (not a.get("auto") or a.get("rate_known"))]


def _interest_gbp(accounts: list[dict]) -> float:
    return _gbp(sum(float(a.get("balance_eur") or 0) * float(a.get("rate") or 0) / 100
                    for a in accounts))


class _Ctx:
    """Shared, derived values plus the three output lists."""

    def __init__(self, inp: Inputs):
        self.inp = inp
        self.uk = inp.residence == "UK"
        self.tips: list[dict] = []
        self.prompts: dict[str, dict] = {}
        self.clear: list[str] = []
        self.start, self.end = _tax_year(inp.today)
        self.days_left = (self.end - inp.today).days

        fin = inp.fin if inp.fin and (inp.demo or not inp.fin.get("sample")) else None
        cash = inp.cash if inp.cash and (inp.demo or not inp.cash.get("sample")) else None
        self.fin, self.cash = fin, cash

        income = (fin or {}).get("income") or []
        fixed = [i for i in income if i.get("fixed", True) is not False]

        def yearly(rows: list[dict], *keys: str) -> float:
            return _gbp(sum(float(i.get(k) or 0) for i in rows for k in keys) * 12)

        # The income that sets your tax band and personal allowance (adjusted
        # net income): pay minus the pension paid from it, as finances.py
        # worked it out from Settings. Salary sacrifice and net pay count the
        # amount taken from pay; relief at source also the 25% the provider adds.
        # https://www.gov.uk/guidance/adjusted-net-income
        cut = ("pension_monthly_eur", "pension_relief_monthly_eur")
        self.income_gross = round(yearly(income, "monthly_eur"), 2) if income else None
        self.income = round(self.income_gross - yearly(income, *cut), 2) if income else None
        self.income_fixed = round(yearly(fixed, "monthly_eur") - yearly(fixed, *cut), 2) \
            if income else None
        # Pension paid in already this year (yours, its relief, your employer's)
        # and the parts the tapered annual allowance adds back.
        self.pension_paid = yearly(income, "pension_pot_monthly_eur")
        self.employer_pension = yearly(income, "employer_pension_monthly_eur")
        self.sacrificed = yearly([i for i in income
                                  if (i.get("pension_type") or "sacrifice") == "sacrifice"],
                                 "pension_monthly_eur")
        self.band = _band(self.income)

        self.taxable = [r for r in inp.rows if not _is_isa(r)]
        self.taxable_gbp = sum(_gbp(r["value_eur"]) for r in self.taxable)
        self.tax = inp.tax
        isa = (inp.tax or {}).get("isa") or {}
        self.isa_left = isa.get("left_gbp") if isa.get("tracked") else None

        # projected dividends per account row (GBP/yr), split by quantity
        self.div_by_row: dict[int, float] | None = None
        if inp.dividends is not None:
            per_ticker = {h["ticker"]: float(h.get("annual_eur") or 0)
                          for h in inp.dividends.get("holdings", [])}
            qty: dict[str, float] = {}
            for r in inp.rows:
                qty[r["ticker"]] = qty.get(r["ticker"], 0.0) + float(r.get("quantity") or 0)
            self.div_by_row = {}
            for i, r in enumerate(inp.rows):
                annual = per_ticker.get(r["ticker"], 0.0)
                if annual and qty.get(r["ticker"]):
                    self.div_by_row[i] = _gbp(annual * float(r["quantity"]) / qty[r["ticker"]])

    # -- output helpers
    def rate_note(self, kind: str) -> str:
        rate = {"cgt": CGT_RATE, "div": DIVIDEND_RATE, "income": INCOME_RATE,
                "savings": SAVINGS_RATE}[kind][self.band]
        if self.income is None:
            return f"{_pct(rate)} (basic rate; more if you pay higher-rate tax)"
        return f"{_pct(rate)} ({self.band} rate)"

    def prompt(self, pid: str, text: str, link: str = "/settings") -> None:
        self.prompts.setdefault(pid, {"id": pid, "text": text, "link": link})

    def need_income(self) -> None:
        self.prompt("income", "Add your income in Settings (Loans, income & payments) to see "
                    "pension tax relief and to price these tips at your own tax rate.",
                    "/settings#fin")

    def deadline(self) -> dict:
        return {"deadline": self.end.isoformat(),
                "deadline_label": f"before 5 April {self.end.year}",
                "days_left": self.days_left,
                "urgency": "high" if self.days_left <= 30 else
                           "medium" if self.days_left <= 90 else "low"}

    def add(self, tid: str, title: str, explanation: str, saving_gbp: float, period: str,
            how: str, data_used: list[str], action: str, items: list[dict] | None = None,
            deadline: bool = False, link: str | None = None, in_total: bool = True,
            isa_gbp: float = 0.0) -> None:
        """`isa_gbp`: how much of this year's ISA allowance the tip uses."""
        if saving_gbp < MIN_SAVING_GBP:
            return
        tip = {"id": tid, "title": title, "explanation": explanation,
               "saving_gbp": round(saving_gbp, 2),
               "saving_eur": round(to_eur(saving_gbp, "GBP"), 2),
               "period": period, "how": how, "data_used": data_used, "action": action,
               "items": items or [], "deadline": None, "deadline_label": None,
               "days_left": None, "urgency": "low", "link": link,
               "in_total": in_total}  # False: advice to not do something
        if deadline:
            tip.update(self.deadline())
        if isa_gbp > 0:
            tip["_isa_gbp"] = isa_gbp
        self.tips.append(tip)

    def isa_room(self) -> float:
        """ISA allowance left this tax year; the full £20,000 when not tracked
        (no more can go in within one tax year)."""
        return self.isa_left if self.isa_left is not None else ISA_ALLOWANCE

    def tax_free_interest(self) -> float:
        """Interest you can earn before tax: the personal savings allowance,
        plus (when your income is known) any unused personal allowance and
        the starting rate for savings."""
        psa = SAVINGS_ALLOWANCE[self.band]
        if self.income is None:
            return psa
        pa = _personal_allowance(self.income)
        starting = max(STARTING_RATE_FOR_SAVINGS - max(self.income - pa, 0.0), 0.0)
        return max(pa - self.income, 0.0) + starting + psa


# ================================================================ checks
def check_isa_allowance(c: _Ctx) -> None:
    """Bed & ISA: move holdings from taxable accounts into the ISA before 5 April."""
    if c.tax is None:
        return
    if c.isa_left is None:
        c.prompt("isa", "Connect your ISA account in Settings (a Trading 212 read-only API key) "
                 "to track how much of your £20,000 ISA allowance is left.")
        return
    left = c.isa_left
    if left < 1:
        c.clear.append("ISA allowance: used in full this tax year.")
        return
    if c.taxable_gbp < 1:
        c.clear.append("ISA allowance: nothing held outside the ISA to move.")
        return

    move = min(left, c.taxable_gbp)
    div_yield = 0.0
    if c.div_by_row is not None and c.taxable_gbp:
        div_yield = sum(c.div_by_row.get(i, 0.0) for i, r in enumerate(c.inp.rows)
                        if not _is_isa(r)) / c.taxable_gbp
    div_rate, cgt_rate = DIVIDEND_RATE[c.band], CGT_RATE[c.band]
    saving = move * (div_yield * div_rate + ASSUMED_GROWTH * cgt_rate)

    # Which holdings to move first: smallest gain per pound moved, so the sale
    # stays inside the CGT allowance. Losers are left to the tax-loss check.
    gain_room = (c.tax or {}).get("exempt_left_gbp", 0.0)
    items, filled, gain_used = [], 0.0, 0.0
    candidates = sorted((r for r in c.taxable if r["profit_eur"] >= 0 and r["value_eur"] > 0),
                        key=lambda r: r["profit_eur"] / r["value_eur"])
    for r in candidates:
        if filled >= move - 1 or len(items) >= MAX_ITEMS:
            break
        value, gain = _gbp(r["value_eur"]), _gbp(r["profit_eur"])
        take = min(value, move - filled)
        if gain > 0:
            take = min(take, value * max(gain_room - gain_used, 0) / gain)
        if take < 50:
            continue
        g = gain * take / value
        items.append({"label": _label(r), "detail": f"move {_m(take)} (gain {_m(g)})",
                      "amount_eur": round(to_eur(take, "GBP"), 2)})
        filled += take
        gain_used += g

    explanation = (f"You have {_m(left)} of your £20,000 ISA allowance left, and it doesn't "
                   f"carry over after 5 April. You hold {_m(c.taxable_gbp)} outside the ISA, "
                   "where dividends and gains can be taxed. Inside the ISA they're tax-free for good.")
    action = ("Before 5 April, sell holdings outside the ISA and buy them back inside it "
              "(\"Bed & ISA\"). The 30-day rule doesn't apply to buying back inside an ISA."
              + (" The holdings below fit within your CGT allowance." if items else ""))
    if items and filled < move - 1:
        action += (f" Moving more than {_m(filled)} would realise gains above your "
                   "£3,000 CGT allowance this year.")
    how = (f"{_m(move)} moved (the smaller of allowance left and holdings outside the ISA) × "
           f"(dividend yield {div_yield * 100:.2f}% × dividend tax {c.rate_note('div')} + "
           f"{_pct(ASSUMED_GROWTH)} assumed growth × CGT {c.rate_note('cgt')}). Tax you avoid "
           "each year from then on; your £500 dividend and £3,000 CGT allowances lower it "
           "while your taxable holdings are small.")
    c.add("isa_allowance", "Use your ISA allowance before 5 April", explanation, saving, "year",
          how, ["ISA deposits this tax year", "holdings outside the ISA",
                "dividend forecast" if c.div_by_row is not None else "dividend forecast (loading)"],
          action, items, deadline=True, isa_gbp=move)


def check_cgt_allowance(c: _Ctx) -> None:
    """Realise gains up to the £3,000 annual exempt amount before it resets."""
    if c.tax is None:
        return
    left = float(c.tax.get("exempt_left_gbp") or 0)
    gains = [r for r in c.taxable if r["profit_eur"] > 0]
    if left < 1:
        c.clear.append("CGT allowance: already used this tax year.")
        return
    if not gains:
        c.clear.append("CGT allowance: no gains outside the ISA to use it on.")
        return
    total = sum(_gbp(r["profit_eur"]) for r in gains)
    use = min(left, total)
    saving = use * CGT_RATE[c.band]

    items, need = [], use
    for r in sorted(gains, key=lambda r: -r["profit_eur"]):
        if need < 1 or len(items) >= MAX_ITEMS:
            break
        gain, value = _gbp(r["profit_eur"]), _gbp(r["value_eur"])
        take = min(gain, need)
        sell = value * take / gain
        items.append({"label": _label(r),
                      "detail": f"sell {_m(sell)} to realise {_m(take)} of gain",
                      "amount_eur": round(to_eur(sell, "GBP"), 2)})
        need -= take
    if need >= 1:
        items.append({"label": "Other holdings with gains",
                      "detail": f"realise the remaining {_m(need)} of gain", "amount_eur": None})

    explanation = (f"Each tax year the first £3,000 of gains is tax-free. You have {_m(left)} "
                   f"of it left and {_m(total)} of gains sitting in accounts outside the ISA. "
                   "Selling some now and buying back resets your cost, so that slice of "
                   "gain is never taxed. The allowance doesn't carry over.")
    action = ("Before 5 April, sell the amounts below. Buy back inside your ISA straight away "
              "(this is the Bed & ISA tip), or outside it after 30 days: buying the same "
              "share back within 30 days cancels the effect. Losses sold in the same tax "
              "year cancel these gains first, so sell losing positions (see Tax-loss "
              "harvesting) after 5 April.")
    how = (f"{_m(use)} of gain (allowance left, or all your gains if less) × CGT "
           f"{c.rate_note('cgt')}. One-off. Gains use each broker's average cost at today's "
           "exchange rate, so HMRC's figure can differ a little.")
    c.add("cgt_allowance", "Use your £3,000 CGT allowance before it resets", explanation,
          saving, "once", how, ["gains and losses this tax year", "unrealised gains outside the ISA"],
          action, items, deadline=True)


def check_dividend_allowance(c: _Ctx) -> None:
    """Dividends outside the ISA above the £500 allowance."""
    if c.div_by_row is None:
        c.prompt("dividends", "The dividend forecast is still loading. Open this tab again "
                 "in a minute to check your dividend allowance.", "")
        return
    payers = [(i, r, c.div_by_row[i]) for i, r in enumerate(c.inp.rows)
              if not _is_isa(r) and c.div_by_row.get(i, 0) > 0]
    total = sum(d for *_, d in payers)
    if total <= DIVIDEND_ALLOWANCE:
        c.clear.append(f"Dividend allowance: about {_m(total)} a year expected outside the ISA, "
                       "inside the £500 allowance.")
        return
    rate = DIVIDEND_RATE[c.band]
    tax_now = (total - DIVIDEND_ALLOWANCE) * rate

    # Move the highest yielders first, as far as this year's ISA allowance reaches.
    room = start_room = c.isa_room()
    moved_div, items = 0.0, []
    for i, r, d in sorted(payers, key=lambda p: -p[2] / max(_gbp(p[1]["value_eur"]), 1)):
        value = _gbp(r["value_eur"])
        share = min(1.0, room / value) if value else 0.0
        if share <= 0:
            break
        moved_div += d * share
        room -= value * share
        if len(items) < MAX_ITEMS:
            items.append({"label": _label(r),
                          "detail": f"about {_m(d)} a year in dividends "
                                    f"({d / value * 100:.1f}% yield)" if value else "",
                          "amount_eur": round(to_eur(d, "GBP"), 2)})
    tax_after = max(total - moved_div - DIVIDEND_ALLOWANCE, 0) * rate
    saving = tax_now - tax_after

    explanation = (f"You can expect about {_m(total)} a year in dividends outside the ISA. "
                   f"Above the £500 allowance they're taxed at {c.rate_note('div')}: about "
                   f"{_m(tax_now)} a year. Dividends inside an ISA are tax-free.")
    action = ("Hold your biggest dividend payers in the ISA first (sell outside, buy back "
              "inside, before 5 April while you have allowance left), and keep growth "
              "shares that pay little outside.")
    how = (f"Forecast dividends outside the ISA ({_m(total)}) − £500 allowance, × {c.rate_note('div')}"
           f" = {_m(tax_now)}. Saving = that tax minus what's left after moving the highest "
           "yielders into the ISA" + (f" (limited by the {_m(c.isa_left)} ISA allowance left)."
                                      if c.isa_left is not None else
                                      " (at most the £20,000 a year the ISA allowance takes).")
           + " The forecast is last year's dividends per share × shares you hold.")
    c.add("dividend_allowance", "Shelter dividends above the £500 allowance", explanation,
          saving, "year", how, ["dividend forecast", "holdings outside the ISA"], action, items,
          deadline=c.isa_left is not None, isa_gbp=start_room - room)


def check_reporting_funds(c: _Ctx) -> None:
    """Non-UK funds without UK reporting status: gains taxed as income."""
    funds = [r for r in c.taxable if _is_fund(r) and _country(r) not in ("", "GB")]
    likely = [r for r in funds if _country(r) not in ("IE", "LU")]
    usual = [r for r in funds if _country(r) in ("IE", "LU")]
    if usual:
        c.clear.append(f"Reporting funds: {len(usual)} Irish/Luxembourg fund(s) outside the ISA. "
                       "These are usually UK reporting funds; check HMRC's list to be sure.")
    if not likely:
        return
    diff = INCOME_RATE[c.band] - CGT_RATE[c.band]
    value = sum(_gbp(r["value_eur"]) for r in likely)
    gain = sum(max(_gbp(r["profit_eur"]), 0) for r in likely)
    saving = value * ASSUMED_GROWTH * diff
    items = [{"label": _label(r), "detail": f"ISIN {r['isin']} · worth {_m(_gbp(r['value_eur']))}",
              "amount_eur": r["value_eur"]} for r in likely[:MAX_ITEMS]]
    explanation = ("Funds from outside the UK, Ireland and Luxembourg (such as US-listed ETFs) "
                   "usually aren't UK 'reporting funds'. When you sell one, HMRC taxes the "
                   f"whole gain as income ({c.rate_note('income')}) instead of capital gains "
                   f"({c.rate_note('cgt')}), and the £3,000 CGT allowance doesn't help."
                   + (f" On today's gain of {_m(gain)} that's {_m(gain * diff)} extra." if gain > 1 else ""))
    action = ("Check each fund on HMRC's reporting funds list. If it's not there, hold that kind "
              "of fund in your ISA (no tax either way), or switch to a UK-reporting version "
              "(often the Irish 'UCITS' one) for new money.")
    how = (f"{_m(value)} in these funds × {_pct(ASSUMED_GROWTH)} assumed growth × "
           f"{_pct(diff)} (income tax minus CGT) = extra tax on each year's growth. "
           "Funds are spotted by name and ISIN country; individual shares are not affected.")
    c.add("reporting_funds", "Funds whose gains may be taxed as income", explanation, saving,
          "year", how, ["holdings outside the ISA (fund name, ISIN)"], action, items,
          link=REPORTING_FUNDS_LIST)


def check_fund_fees(c: _Ctx) -> None:
    """Funds with a cheaper equivalent (curated table in fund_fees.json)."""
    by_key: dict[str, tuple[dict, dict]] = {}
    for g in _fund_table():
        for f in g["funds"]:
            by_key[f["isin"]] = (f, g)
            for t in f.get("tickers", []):
                by_key[t] = (f, g)
    items, saving = [], 0.0
    for r in c.inp.rows:
        hit = by_key.get(r.get("isin") or "") or by_key.get(r.get("ticker") or "")
        if not hit:
            continue
        f, g = hit
        cheap = g["cheaper"]
        gap = f["ter_pct"] - cheap["ter_pct"]
        if gap <= 0:
            continue
        s = _gbp(r["value_eur"]) * gap / 100
        if s < MIN_SAVING_GBP:
            continue  # too small to be worth a trade
        saving += s
        items.append({"label": _label(r),
                      "detail": f"{f['ter_pct']}% a year → {cheap['name']} ({cheap['yahoo']}, "
                                f"{cheap['ter_pct']}%): saves {_m(s)} a year. {cheap['note'].capitalize()}.",
                      "amount_eur": round(to_eur(s, "GBP"), 2), "_s": s})
    if not items:
        return
    items.sort(key=lambda x: -x.pop("_s"))
    explanation = ("Some of your funds have a cheaper fund that tracks the same index. The fee "
                   "comes out of your returns every year, so a lower one is money in your pocket.")
    action = ("Inside an ISA you can switch with no tax. Outside it, selling realises gains: "
              "switch only within your £3,000 CGT allowance, or simply put new money into the "
              "cheaper fund. Watch the trading cost of the switch."
              if c.uk else
              "Selling to switch can realise taxable gains: check that first, or put new money "
              "into the cheaper fund. Watch the trading cost of the switch.")
    how = ("Value of each holding × (its yearly fee − the cheaper fund's fee). Fees from the "
           "fund providers via justETF, checked " + _fees_checked() + ".")
    c.add("fund_fees", "Cheaper funds that track the same index", explanation, saving, "year",
          how, ["holdings (ISIN, value)", "fund fee table"], action, items[:MAX_ITEMS])


def _fees_checked() -> str:
    try:
        return json.loads(FUND_FEES_FILE.read_text()).get("checked", "")
    except (OSError, ValueError):
        return ""


def check_pension(c: _Ctx) -> None:
    """Tax relief on pension contributions at your marginal rate (60% in the taper band)."""
    if c.income is None:
        c.need_income()
        return
    inc = c.income
    if inc <= HIGHER_FROM:
        c.clear.append("Pension tax relief: at your income it's the standard 20%.")
        return
    # Room left in the annual allowance: tapered for high incomes (threshold
    # income = income after pension, salary sacrifice added back; adjusted
    # income = pay before pension + employer payments), minus what is already
    # paid in through the pay entered in Settings.
    allowance = _annual_allowance(inc + c.sacrificed, c.income_gross + c.employer_pension)
    room = max(allowance - c.pension_paid, 0.0)
    if room < 1:
        c.clear.append("Pension tax relief: the pension payments in Settings already use your "
                       f"{_m(allowance)} annual allowance.")
        return
    # What you could afford: income minus the monthly payments in Settings, if any.
    totals = (c.fin or {}).get("totals") or {}
    spare = (_gbp(max(float(totals.get("free_cashflow") or 0), 0)) * 12
             if totals.get("monthly_payments") else float("inf"))
    cap = min(room, spare)
    if cap < room:
        cap_note = (f" (capped at your spare cash flow of {_m(spare)} a year: income minus "
                    "monthly payments in Settings)")
    elif room == PENSION_ANNUAL_ALLOWANCE:
        cap_note = " (capped at the £60,000 annual allowance)"
    else:
        cap_note = (f" (capped at the {_m(room)} left of your "
                    + ("tapered " if allowance < PENSION_ANNUAL_ALLOWANCE else "")
                    + f"{_m(allowance)} annual allowance"
                    + (f", after the {_m(c.pension_paid)} already going in" if c.pension_paid >= 1 else "")
                    + ")")
    income_is = ("Your income after the pension you already pay is about"
                 if c.income_gross - inc >= 1 else "Your income is about")
    if inc > TAPER_FROM:
        above_add = max(inc - ADDITIONAL_FROM, 0)
        taper = min(inc, ADDITIONAL_FROM) - TAPER_FROM
        pay = min(above_add + taper, cap)
        relief = min(above_add, pay) * INCOME_RATE["additional"] + max(pay - above_add, 0) * 0.60
        title = "Pension payments get about 60% tax relief above £100,000"
        explanation = (f"{income_is} {_m(inc)}. Between £100,000 and £125,140 you lose "
                       "£1 of tax-free personal allowance for every £2 you earn, so that slice is "
                       "taxed at about 60%. Paying into a pension lowers the income that counts, "
                       "and you get the allowance back.")
        if c.income_fixed is not None and c.income_fixed <= TAPER_FROM:
            explanation += (" Your fixed income is below £100,000: this applies in years your "
                            "variable income (bonus, commission) takes you over.")
        how = (f"Gross pension payment of {_m(pay)} brings your income down to £100,000"
               + (cap_note if pay == cap else "")
               + ". Relief: "
               + (f"{_m(min(above_add, pay))} at 45% + " if above_add else "")
               + f"{_m(max(pay - above_add, 0))} at about 60% (40% tax + 20% from the "
               "allowance you get back).")
    else:
        pay = min(inc - HIGHER_FROM, cap)
        relief = pay * INCOME_RATE["higher"]
        title = "Higher-rate tax relief on pension payments"
        explanation = (f"{income_is} {_m(inc)}, so part of it is taxed at 40%. Pension "
                       "payments from that slice get 40% tax relief: each £100 in your pension "
                       "costs you £60.")
        how = (f"Gross pension payment of {_m(pay)} (your income above £50,270"
               + (cap_note if pay == cap else "") + ") × 40%.")
    explanation += " Scotland has different bands, so the numbers differ there."
    how += (" Uses the income you entered in Settings × 12, minus the pension paid from it, as "
            "your yearly income before tax. Your and your employer's pension payments entered "
            "there count towards the annual allowance; payments made elsewhere and unused "
            "allowance carried forward from earlier years aren't known here.")
    action = ("Pay more into your pension: ask your employer about salary sacrifice, or pay "
              "into a personal pension (the provider adds 20%; claim the rest on your Self "
              "Assessment). Before 5 April for this tax year. The money is locked until age "
              f"{PENSION_ACCESS_AGE}.")
    c.add("pension_relief", title, explanation, relief, "year", how,
          ["income (Settings)"], action, deadline=True)


def check_loans_vs_cash(c: _Ctx) -> None:
    """Pay off loans that cost more than cash earns; don't overpay cheap ones."""
    loans = [l for l in ((c.fin or {}).get("loans") or []) if float(l.get("balance_eur") or 0) > 0]
    if not loans:
        return
    accounts = [a for a in _bank_accounts(c) if float(a.get("balance_eur") or 0) > 0]
    if not accounts:
        c.prompt("cash", "Add your bank and savings accounts (with interest rates) in Settings "
                 "to compare them with your loans and check tax on interest.", "/settings#cash")
        return
    best = max(accounts, key=lambda a: float(a.get("rate") or 0))
    best_rate = float(best.get("rate") or 0)
    total_cash = sum(float(a["balance_eur"]) for a in accounts)
    payments = float(((c.fin or {}).get("totals") or {}).get("monthly_payments") or 0)
    spare = _gbp(total_cash - CASH_BUFFER_MONTHS * payments)

    after_tax = best_rate
    if c.uk:
        if _interest_gbp(accounts) > c.tax_free_interest():
            after_tax = best_rate * (1 - SAVINGS_RATE[c.band])
    cash_says = (f"your cash earns at most {best_rate:g}% ({best['name']})"
                 + (f", about {after_tax:.2f}% after tax" if after_tax < best_rate else ""))
    optimiser = any(float(a.get("rate") or 0) < best_rate - 0.01 for a in accounts)

    for loan in sorted(loans, key=lambda l: -float(l.get("rate") or 0)):
        r = float(loan.get("rate") or 0)
        bal = _gbp(float(loan["balance_eur"]))
        fx_note = (" The loan and that account are in different currencies, so exchange-rate "
                   "moves change the maths." if loan.get("currency", "EUR") != best.get("currency", "EUR")
                   else "")
        if r > after_tax + RATE_MARGIN_PCT and spare > 1:
            amount = min(bal, spare)
            spare -= amount
            saving = amount * (r - after_tax) / 100
            c.add(f"loan_payoff:{loan['name']}", f"Pay down {loan['name']} with spare cash",
                  f"{loan['name']} costs {r:g}% a year, while {cash_says}. Paying off "
                  f"{_m(amount)} saves the difference." + fx_note,
                  saving, "year",
                  f"{_m(amount)} (the smaller of the loan and your cash above "
                  f"{CASH_BUFFER_MONTHS} months of payments) × ({r:g}% − {after_tax:.2f}%).",
                  ["loans (Settings)", "cash accounts and rates (Settings)", "monthly payments"],
                  f"Overpay {_m(amount)} on {loan['name']} and keep {CASH_BUFFER_MONTHS} months "
                  "of payments as a buffer. Check for early-repayment fees first; some student "
                  "loans have income-linked repayments or are written off after a set time.")
        elif r + RATE_MARGIN_PCT < after_tax and spare > 1:
            amount = min(bal, spare)
            keep = amount * (after_tax - r) / 100
            c.add(f"loan_keep:{loan['name']}", f"Don't overpay {loan['name']}",
                  f"{loan['name']} costs only {r:g}% a year, while {cash_says}. Keeping "
                  f"{_m(amount)} in savings earns more than paying the loan off early." + fx_note,
                  keep, "year",
                  f"{_m(amount)} (the smaller of the loan and your cash above "
                  f"{CASH_BUFFER_MONTHS} months of payments) × ({after_tax:.2f}% − {r:g}%).",
                  ["loans (Settings)", "cash accounts and rates (Settings)", "monthly payments"],
                  "Make the normal payments only and keep spare cash in your best savings account."
                  + (" The Cash Optimizer on the Overview tab shows where your cash earns most."
                     if optimiser else ""),
                  link="/#cashopt" if optimiser else None, in_total=False)
        else:
            c.clear.append(f"{loan['name']}: its rate ({r:g}%) is close to what cash earns, "
                           "so paying early or not makes little difference.")


def check_savings_interest(c: _Ctx) -> None:
    """Interest above the Personal Savings Allowance is taxed: use a cash ISA."""
    accounts = _bank_accounts(c)
    if not accounts:
        c.prompt("cash", "Add your bank and savings accounts (with interest rates) in Settings "
                 "to compare them with your loans and check tax on interest.", "/settings#cash")
        return
    interest = _interest_gbp(accounts)
    psa = SAVINGS_ALLOWANCE[c.band]
    free = c.tax_free_interest()
    unused_pa = c.income is not None and c.income < _personal_allowance(c.income)
    allowance = (f"£{psa:,} personal savings allowance" if free <= psa else
                 f"{_m(free)} of tax-free interest (the £{psa:,} personal savings allowance plus "
                 "the starting rate for savings" + (" and your unused personal allowance"
                                                    if unused_pa else "")
                 + ", as your other income is low)")
    if interest <= free:
        c.clear.append(f"Savings interest: about {_m(interest)} a year, inside your {allowance}.")
        return
    rate = SAVINGS_RATE[c.band]
    tax = (interest - free) * rate
    balance = sum(_gbp(float(a.get("balance_eur") or 0)) for a in accounts)
    avg_rate = interest / balance if balance else 0
    needed = (interest - free) / avg_rate if avg_rate else 0
    movable = min(needed, c.isa_room())
    saving = tax * (movable / needed) if needed else 0
    explanation = (f"Your cash earns about {_m(interest)} a year in interest. Above your "
                   f"{allowance} it's taxed at {c.rate_note('savings')}: "
                   f"about {_m(tax)} a year. From April 2027 that rate goes up by 4 points. "
                   "Interest in a cash ISA is tax-free.")
    action = (f"Move about {_m(movable)} into a cash ISA at a similar rate (it uses the same "
              "£20,000 yearly ISA allowance as investing), before 5 April for this year's "
              "allowance. Premium Bonds are another tax-free option.")
    how = (f"({_m(interest)} interest − {_m(free)} tax-free) × {c.rate_note('savings')} = "
           f"{_m(tax)}. Moving {_m(movable)} at your average rate ({avg_rate * 100:.2f}%) "
           "takes that interest out of tax"
           + ((", limited by the ISA allowance left." if c.isa_left is not None else
               ", limited by the £20,000 a year the ISA allowance takes.") if movable < needed else "."))
    c.add("savings_interest", "Tax on savings interest above your allowance", explanation,
          saving, "year", how, ["cash accounts and rates (Settings)", "income (Settings)"],
          action, deadline=True, link="/#cashopt", isa_gbp=movable)


def check_fig_regime(c: _Ctx) -> None:
    """New UK residents: the 4-year foreign income and gains (FIG) regime."""
    arrived = c.inp.resident_from
    if arrived is None:
        return
    year_no = _tax_year(c.inp.today)[0].year - _tax_year(arrived)[0].year + 1
    if not 1 <= year_no <= FIG_YEARS:
        return
    if c.income is None:
        c.need_income()
        return
    foreign = {i for i, r in enumerate(c.inp.rows)
               if not _is_isa(r) and _country(r) not in ("", "GB")}
    divs = c.div_by_row or {}
    div_all = sum(d for i, d in divs.items() if not _is_isa(c.inp.rows[i]))
    div_foreign = sum(d for i, d in divs.items() if i in foreign)
    gains = max(float((c.tax or {}).get("net_gain_gbp") or 0), 0)
    foreign_gain = min(max(c.inp.foreign_gains_gbp, 0.0), gains)
    unrealised = sum(max(_gbp(c.inp.rows[i]["profit_eur"]), 0) for i in foreign)
    cgt, div_rate = CGT_RATE[c.band], DIVIDEND_RATE[c.band]

    # Tax this year without a claim minus tax with one. A claim keeps the £500
    # dividend allowance but loses the personal allowance and the £3,000 CGT
    # allowance (HS266).
    cgt_saved = (max(gains - CGT_EXEMPT, 0) - (gains - foreign_gain)) * cgt
    div_saved = (max(div_all - DIVIDEND_ALLOWANCE, 0)
                 - max(div_all - div_foreign - DIVIDEND_ALLOWANCE, 0)) * div_rate
    # Extra income tax without a personal allowance (part of it can move into
    # the 40% band, so not simply allowance × your rate).
    lost_pa = (_paye.income_tax(c.income, allowance_income=float("inf"))
               - _paye.income_tax(c.income))
    base = cgt_saved + div_saved - lost_pa
    with_reset = base + unrealised * cgt   # also realise non-UK gains tax-free
    if with_reset < MIN_SAVING_GBP:
        c.clear.append(f"Foreign income and gains regime (UK year {year_no} of {FIG_YEARS}): "
                       "claiming would cost more in lost allowances than it saves at today's numbers.")
        return
    reset = base < MIN_SAVING_GBP
    saving = with_reset if reset else base
    explanation = (f"You became UK resident on {arrived.day} {arrived:%B %Y}, so this is UK tax year "
                   f"{year_no} of {FIG_YEARS}. If you weren't UK resident in the 10 tax years "
                   "before, you can claim to pay no UK tax on foreign dividends and gains for the "
                   "year. The price: you lose your personal allowance and the £3,000 CGT "
                   "allowance for that year.")
    if reset:
        explanation += (f" It pays off if you also realise the {_m(unrealised)} of gains on "
                        "non-UK holdings this year, which resets their cost tax-free.")
    how = (f"Tax this year without a claim minus with one: CGT on foreign gains realised "
           f"({_m(foreign_gain)} of your {_m(gains)}) {_m(cgt_saved)}, dividend tax on foreign "
           f"dividends ({_m(div_foreign)} a year) {_m(div_saved)}, minus the extra income tax "
           f"from losing your {_m(_personal_allowance(c.income))} personal allowance "
           f"({_m(lost_pa)})"
           + (f", plus {_pct(cgt)} of the {_m(unrealised)} unrealised gain on non-UK holdings "
              "outside the ISA, which a sale this year would reset" if reset else "")
           + ". The lost £3,000 CGT allowance is inside the CGT figure. 'Foreign' = ISIN not "
           "starting with GB, a rough stand-in for HMRC's rules on where an asset is.")
    action = ("Check you qualify (10 years abroad before arriving), then tick the foreign "
              "income and gains boxes on the SA109 pages of your Self Assessment for this year."
              + (" Realise gains before 5 April; buy back after 30 days or inside the ISA."
                 if reset else "") + " Worth a quick check with a tax adviser.")
    c.add("fig_regime", "New to the UK: no tax on foreign income and gains", explanation, saving,
          "once", how, ["UK_RESIDENT_FROM", "income (Settings)", "holdings outside the ISA",
                        "dividend forecast", "sales this tax year"],
          action, deadline=True,
          link="https://www.gov.uk/government/publications/foreign-income-and-gains-fig-regime-"
               "self-assessment-helpsheet-hs266/hs266-foreign-income-and-gains-fig-regime-2026")


UK_CHECKS = (check_isa_allowance, check_cgt_allowance, check_dividend_allowance,
             check_reporting_funds, check_pension, check_savings_interest, check_fig_regime)
ANYWHERE_CHECKS = (check_fund_fees, check_loans_vs_cash)


def _count_isa_once(c: _Ctx) -> dict[str, float]:
    """Several tips spend the same ISA allowance (Bed & ISA, sheltering
    dividends, a cash ISA). Each tip shows what it saves on its own; the totals
    give each pound of allowance to one tip only, best saving per pound first.
    Returns {tip id: EUR amount counted in the totals} for tips counted in part."""
    left = c.isa_room()
    users = sorted((t for t in c.tips if t.get("_isa_gbp")),
                   key=lambda t: -t["saving_gbp"] / t["_isa_gbp"])
    out = {}
    for t in users:
        use = t.pop("_isa_gbp")
        share = min(max(left, 0.0) / use, 1.0)
        left -= use
        if share < 1:
            out[t["id"]] = round(t["saving_eur"] * share, 2)
            t["how"] += (" Other tips here use the same ISA allowance, so the total at the top "
                         f"counts {share:.0%} of this saving.")
    return out


# ============================================================== public
def build(inp: Inputs) -> dict:
    """Run every check on `inp`. Pure apart from FX rates, so it's testable."""
    c = _Ctx(inp)
    checks = (UK_CHECKS + ANYWHERE_CHECKS) if c.uk else ANYWHERE_CHECKS
    for check in checks:
        try:
            check(c)
        except Exception:  # one broken check must not hide the others
            continue
    counts = _count_isa_once(c)
    c.tips.sort(key=lambda t: -t["saving_gbp"])
    counted = [t for t in c.tips if t["in_total"]]
    per_year = sum(counts.get(t["id"], t["saving_eur"]) for t in counted if t["period"] == "year")
    once = sum(counts.get(t["id"], t["saving_eur"]) for t in counted if t["period"] == "once")
    return {
        "residence": inp.residence,
        "tax_year": (inp.tax or {}).get("tax_year") or TAX_YEAR,
        "rates_checked": RATES_CHECKED,
        "tips": c.tips,
        "total_per_year_eur": round(per_year, 2),
        "total_once_eur": round(once, 2),
        "prompts": list(c.prompts.values()),
        "all_clear": c.clear,
        "note": None if c.uk else ("UK tax checks are off because the country chosen in Settings "
                                   "is not the UK."),
        "assumptions": (f"Growth {_pct(ASSUMED_GROWTH)} a year; {CASH_BUFFER_MONTHS} months of "
                        "payments kept as cash; income entered in Settings is before tax. "
                        "Without your income, tax is priced at the basic rate."),
        "disclaimer": DISCLAIMER,
    }


# -------------------------------------------------- gathering live inputs
_warm = threading.Lock()


def _dividend_forecast(rows: list[dict], wait: float = 0.3) -> dict | None:
    """The dividend forecast if it's cached (or quick); otherwise None while
    a background thread fills the cache for the next request."""
    from . import reports

    merged: dict[str, dict] = {}
    for r in rows:  # same grouping as main._merge_holdings, so the cache key matches
        k = r.get("isin") or r.get("ticker") or r.get("name")
        m = merged.setdefault(k, {"name": r["name"], "ticker": r["ticker"],
                                  "currency": r["currency"], "quantity": 0.0, "value_eur": 0.0})
        m["quantity"] += float(r.get("quantity") or 0)
        m["value_eur"] += float(r.get("value_eur") or 0)
    holdings = list(merged.values())
    if not _warm.acquire(blocking=False):
        return None  # another request is already building it
    out: dict = {}

    def run():
        try:
            out["v"] = reports.income_projection(holdings)
        except Exception:
            out["v"] = None
        finally:
            _warm.release()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(wait)  # a warm cache answers in milliseconds
    return out.get("v")


def _foreign_gains_gbp(since: date, end: date) -> float:
    """Net gains realised this tax year on non-UK assets, outside ISAs."""
    from . import sources, uktax

    # Matched across taxable accounts with HMRC's rules, as in uktax.summary().
    pooled = []
    for _, (is_isa, get_trades) in sources.trade_sources().items():
        if is_isa:
            continue
        try:
            pooled.extend(sources.in_eur(get_trades()))
        except Exception:
            continue
    total = 0.0
    for ev in uktax.match_disposals(pooled, resident_from=uktax._resident_from()):
        key = (ev.get("key") or "").upper()
        try:
            d = date.fromisoformat(ev["when"][:10])
        except (ValueError, TypeError):
            continue
        if since <= d <= end and is_valid_isin(key) and not key.startswith("GB"):
            total += _gbp(ev["realized"])
    return round(total, 2)


def collect(rows: list[dict], demo: bool = False, today: date | None = None) -> dict:
    """Gather the live inputs (all local or cached) and run the checks."""
    from . import cash, finances, uktax

    today = today or date.today()
    res = residence()
    inp = Inputs(rows=rows, today=today, residence=res, demo=demo)
    try:
        inp.fin = finances.load()
    except Exception:
        pass
    try:
        inp.cash = cash.load()
    except Exception:
        pass
    inp.dividends = _dividend_forecast(rows)
    if res == "UK":
        try:
            inp.tax = uktax.summary(today)
        except Exception:
            pass
        inp.resident_from = uktax._resident_from()
        if inp.resident_from:
            start, end = _tax_year(today)
            inp.foreign_gains_gbp = _foreign_gains_gbp(max(start, inp.resident_from), end)
    return build(inp)
