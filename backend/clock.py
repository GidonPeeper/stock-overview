"""The dashboard's calendar day.

"Today" means since midnight where you live, not the last 24 hours, so a
market that has not opened yet today shows no change. The time zone is
DASHBOARD_TZ (default Europe/London).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("DASHBOARD_TZ") or "Europe/London")
    except Exception:
        return ZoneInfo("Europe/London")


def today() -> date:
    return datetime.now(tz()).date()


def local_date(moment) -> date | None:
    """Calendar date here of an epoch number or a datetime/Timestamp."""
    if moment is None:
        return None
    try:
        if hasattr(moment, "astimezone"):
            if getattr(moment, "tzinfo", None) is None:
                return None
            return moment.astimezone(tz()).date()
        return datetime.fromtimestamp(float(moment), tz()).date()
    except Exception:
        return None


def period_starts(day: date | None = None) -> dict[str, date]:
    """First day of this week (Monday), month and year."""
    d = day or today()
    return {"week": d - timedelta(days=d.weekday()),
            "month": d.replace(day=1),
            "year": d.replace(month=1, day=1)}
