"""The rules the app carries, and when they go out of date.

Some of what the app shows was true on a date: the UK tax figures are for one
tax year, each country page describes one tax year and was checked on a day,
and the steps to export a statement were checked at each broker on a day.
This module holds those dates (or reads them from their files) and says, in
plain words, when one has passed. The cards that use the rules show the
notice, and the check-up (checkup.py) lists it. Nothing here changes a
number: the figures stay as they are until someone updates them.

  UK tax figures   UK_TAX_YEAR below is the one place that says which tax year
                   the figures in advisors.py, uktax.py, paye.py and harvest.py
                   are for. After updating those figures for a new tax year,
                   change UK_TAX_YEAR and UK_RULES_CHECKED (MAINTAINING.md).
  Country pages    backend/countries/<CC>.json: tax_year and checked_on.
  Broker steps     backend/broker_catalog.json: researched_on.
"""

from __future__ import annotations

from datetime import date

# The UK tax year the built-in UK figures are for, and the day they were last
# checked on gov.uk. Change both after a roll-over (see MAINTAINING.md).
UK_TAX_YEAR = "2026/27"
UK_RULES_CHECKED = "2026-11-26"

# A country page is old after about a year; the broker steps after about 18 months.
COUNTRY_PAGE_MAX_DAYS = 400
BROKER_STEPS_MAX_DAYS = 548


def _today(today: date | None) -> date:
    if today is not None:
        return today
    from . import clock
    return clock.today()


def day_text(d: date | str | None) -> str:
    """27 Nov 2026"""
    if isinstance(d, str):
        d = parse_day(d)
    return f"{d.day} {d.strftime('%b %Y')}" if d else ""


def parse_day(text) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except (TypeError, ValueError):
        return None


def uk_tax_year(day: date) -> str:
    """'2026/27' for any day from 6 April 2026 to 5 April 2027 (as uktax.tax_year)."""
    y = day.year if day >= date(day.year, 4, 6) else day.year - 1
    return f"{y}/{str(y + 1)[2:]}"


def _first_year(label: str) -> int | None:
    digits = str(label or "")[:4]
    return int(digits) if digits.isdigit() else None


def uk_notice(today: date | None = None) -> dict | None:
    """None while the UK figures are for the current UK tax year; else what
    the tax cards show: the figures are for an earlier year."""
    now = uk_tax_year(_today(today))
    have, current = _first_year(UK_TAX_YEAR), _first_year(now)
    if have is None or current is None or current <= have:
        return None
    return {"rules_year": UK_TAX_YEAR, "current_year": now, "checked_on": UK_RULES_CHECKED,
            "text": f"These figures use the {UK_TAX_YEAR} rules. The {now} tax year has started: "
                    "update the app, and check gov.uk for changes."}


def tax_year_end(tax_year: dict) -> date | None:
    """Last day of a country page's tax year ({"label": "2026/27", "starts":
    "04-06", "ends": "04-05"}), or None when it cannot be read."""
    try:
        first = _first_year(tax_year["label"])
        sm, sd = (int(x) for x in tax_year["starts"].split("-"))
        em, ed = (int(x) for x in tax_year["ends"].split("-"))
        if first is None:
            return None
        return date(first if (em, ed) >= (sm, sd) else first + 1, em, ed)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def country_notice(data: dict, today: date | None = None) -> dict | None:
    """None while a country page is current; else why it may be out of date:
    its tax year has ended, or it was checked more than about a year ago."""
    today = _today(today)
    label = str((data.get("tax_year") or {}).get("label") or "")
    end = tax_year_end(data.get("tax_year") or {})
    checked = parse_day(data.get("checked_on"))
    ended = end is not None and today > end
    old = checked is not None and (today - checked).days > COUNTRY_PAGE_MAX_DAYS
    if not ended and not old:
        return None
    if ended:
        text = (f"This page describes the rules for {label} and was checked on {day_text(checked)}; "
                "a new tax year has started. Rates and allowances may have changed: check the official "
                "pages linked below, and update the app.")
    else:
        text = (f"This page was checked on {day_text(checked)}, more than a year ago. Rules may have "
                "changed since: check the official pages linked below, and update the app.")
    return {"tax_year": label, "checked_on": data.get("checked_on"), "ended": ended, "old": old,
            "text": text}


def broker_steps_notice(researched_on, today: date | None = None) -> dict | None:
    """None while the broker steps are recent; else a notice that brokers may
    have moved their menus since they were checked."""
    checked = parse_day(researched_on)
    if checked is None:
        return None
    age = (_today(today) - checked).days
    if age <= BROKER_STEPS_MAX_DAYS:
        return None
    return {"checked_on": checked.isoformat(), "days": age,
            "text": f"These steps were checked on {day_text(checked)}, more than 18 months ago. "
                    "If a screen looks different, follow the broker's own help pages, "
                    "and update the app."}
