"""Take-home pay for UK salaries: income tax, National Insurance and pension.

Income entered "before tax" in Settings is run through this module, so the
cash-flow views show what actually lands in your bank account.

Rules for England, Wales and Northern Ireland, tax years 2026/27 onwards
(thresholds are frozen until April 2031):
  https://www.gov.uk/income-tax-rates
  https://www.gov.uk/income-tax-rates/income-over-100000
  https://www.gov.uk/national-insurance-rates-letters  (employee, category A)

Pension: `pension_pct` is the % of gross pay deducted on the payslip.
  sacrifice         salary sacrifice: lowers pay before tax and NI
  net_pay           taken before income tax, not before NI
  relief_at_source  taken from pay after tax (NEST and most auto-enrolment
                    schemes); the provider adds 25% on top (basic-rate relief),
                    higher-rate relief is claimed on the tax return
`employer_pct` is what the employer pays in; it is shown, not deducted.

Not covered: Scottish rates, student loans, benefits in kind, other tax codes.
"""

from __future__ import annotations

from datetime import date

PERSONAL_ALLOWANCE = 12_570
TAPER_FROM = 100_000           # allowance falls £1 for every £2 above this
BASIC_BAND = 37_700            # taxable income taxed at 20%
ADDITIONAL_FROM = 125_140      # taxable income above this is taxed at 45%
RATES = (0.20, 0.40, 0.45)

NI_PRIMARY = 12_570            # employee NI starts here (per year)
NI_UPPER = 50_270              # 8% below, 2% above
NI_MAIN, NI_ABOVE = 0.08, 0.02
NI_MONTHLY = (1_048, 4_189)    # HMRC's monthly thresholds (rounded), as payroll uses

PENSION_TYPES = ("sacrifice", "net_pay", "relief_at_source")
RULES_NOTE = ("UK income tax and National Insurance, 2026/27 rates (England, Wales and "
              "Northern Ireland), tax code 1257L.")


def income_tax(taxable_pay: float, allowance_income: float | None = None) -> float:
    """Yearly income tax. `allowance_income` is the income used to taper the
    personal allowance (defaults to `taxable_pay`)."""
    ani = taxable_pay if allowance_income is None else allowance_income
    allowance = max(0.0, PERSONAL_ALLOWANCE - max(0.0, ani - TAPER_FROM) / 2)
    taxable = max(0.0, taxable_pay - allowance)
    basic = min(taxable, BASIC_BAND)
    higher = min(max(taxable - BASIC_BAND, 0.0), ADDITIONAL_FROM - BASIC_BAND)
    additional = max(taxable - ADDITIONAL_FROM, 0.0)
    return basic * RATES[0] + higher * RATES[1] + additional * RATES[2]


def national_insurance(ni_pay: float, periods: int = 1) -> float:
    """Employee NI on pay for `periods` equal pay periods per year. NI is worked
    out per pay period, so pass the per-period pay and periods=12 for a month."""
    lower, upper = NI_MONTHLY if periods == 12 else (NI_PRIMARY / periods, NI_UPPER / periods)
    return (max(min(ni_pay, upper) - lower, 0.0) * NI_MAIN
            + max(ni_pay - upper, 0.0) * NI_ABOVE)


def _split(gross: float, pct: float, kind: str, employer_pct: float = 0.0) -> dict:
    """Pension amounts for one item: what comes off before tax, before NI, and
    after tax, and what reaches the pension pot (yours incl. relief, employer's)."""
    d = max(gross * max(pct, 0.0) / 100, 0.0)          # deducted on the payslip
    kind = kind if kind in PENSION_TYPES else "sacrifice"
    ras = kind == "relief_at_source"
    return {"deducted": d,
            "pre_tax": 0.0 if ras else d,
            "pre_ni": d if kind == "sacrifice" else 0.0,
            "from_net": d if ras else 0.0,
            "relief": d / 0.8 - d if ras else 0.0,     # basic-rate relief the provider claims
            "ras_gross": d / 0.8 if ras else 0.0,
            "employer": max(gross * max(employer_pct, 0.0) / 100, 0.0)}


def annual(items: list[dict]) -> dict:
    """Yearly take-home for a stack of gross incomes, all from employment.

    items: [{"gross": yearly £, "pension_pct": %, "pension_type": ..., "employer_pct": %}, ...]
    Tax is worked out on the total; each item's share is what it adds on top
    of the items before it, so list the salary first: a bonus or commission
    then shows its tax at your top rate."""
    out_items = []
    gross = pre_tax = pre_ni = from_net = ras = 0.0
    tax_before = ni_before = 0.0
    for it in items:
        g = max(float(it.get("gross") or 0), 0.0)
        p = _split(g, float(it.get("pension_pct") or 0), it.get("pension_type") or "sacrifice",
                   float(it.get("employer_pct") or 0))
        gross += g
        pre_tax += p["pre_tax"]
        pre_ni += p["pre_ni"]
        from_net += p["from_net"]
        ras += p["ras_gross"]
        taxable = gross - pre_tax
        tax = income_tax(taxable, allowance_income=taxable - ras)
        ni = national_insurance(gross - pre_ni)
        it_tax, it_ni = tax - tax_before, ni - ni_before
        tax_before, ni_before = tax, ni
        net = g - it_tax - it_ni - p["deducted"]
        out_items.append({"gross": round(g, 2), "income_tax": round(it_tax, 2),
                          "ni": round(it_ni, 2), "pension": round(p["deducted"], 2),
                          "pension_relief": round(p["relief"], 2),
                          "employer_pension": round(p["employer"], 2),
                          "pension_into_pot": round(p["deducted"] + p["relief"] + p["employer"], 2),
                          "net": round(net, 2),
                          "top_rate": round(marginal_rate(gross - pre_tax, gross - pre_ni,
                                                          taxable - ras) * 100, 1)})
    total = {k: round(sum(i[k] for i in out_items), 2)
             for k in ("gross", "income_tax", "ni", "pension", "net")}
    return {"items": out_items, "total": total}


def marginal_rate(taxable_pay: float, ni_pay: float, allowance_income: float) -> float:
    """Income tax + NI on the next £1 of pay."""
    step = 100.0
    t = (income_tax(taxable_pay + step, allowance_income + step)
         - income_tax(taxable_pay, allowance_income))
    n = national_insurance(ni_pay + step) - national_insurance(ni_pay)
    return (t + n) / step


# ------------------------------------------------------------ this tax year
def tax_year_start(today: date) -> date:
    y = today.year if (today.month, today.day) >= (4, 6) else today.year - 1
    return date(y, 4, 6)


def _month_no(ym: str, start: date) -> int | None:
    """Tax month (1 = April) of a 'YYYY-MM' string, or None if outside the year."""
    try:
        y, m = (int(x) for x in ym[:7].split("-"))
    except (ValueError, AttributeError):
        return None
    n = (y * 12 + m) - (start.year * 12 + 4) + 1
    return n if 1 <= n <= 12 else None


def payslips(gross_year: float, started: str | None, pension_pct: float = 0.0,
             pension_type: str = "sacrifice", today: date | None = None,
             employer_pct: float = 0.0) -> list[dict] | None:
    """Monthly take-home for this tax year when the job started after April.

    PAYE is cumulative: with tax code 1257L the tax-free allowance for the
    months before you started is still unused, so the first payslips carry
    less tax. National Insurance is worked out per month. Returns None when
    there is nothing special to show (started in April or earlier, or no date)."""
    today = today or date.today()
    start = tax_year_start(today)
    first = _month_no(started or "", start)
    if first is None or first == 1:
        return None
    monthly = gross_year / 12
    p = _split(monthly, pension_pct, pension_type, employer_pct)
    taxable_month = monthly - p["pre_tax"]
    ni_month = national_insurance(monthly - p["pre_ni"], periods=12)
    out, tax_paid, pay_to_date = [], 0.0, 0.0
    for n in range(first, 13):
        pay_to_date += taxable_month
        free = PERSONAL_ALLOWANCE * n / 12
        band = BASIC_BAND * n / 12
        top = ADDITIONAL_FROM * n / 12
        taxable = max(pay_to_date - free, 0.0)
        due = (min(taxable, band) * RATES[0]
               + min(max(taxable - band, 0.0), top - band) * RATES[1]
               + max(taxable - top, 0.0) * RATES[2])
        tax = max(due - tax_paid, 0.0)
        tax_paid += tax
        y = start.year + (n + 2) // 12
        m = (n + 2) % 12 + 1
        out.append({"month": f"{y}-{m:02d}", "income_tax": round(tax, 2),
                    "ni": round(ni_month, 2), "pension": round(p["deducted"], 2),
                    "net": round(monthly - tax - ni_month - p["deducted"], 2)})
    return out
