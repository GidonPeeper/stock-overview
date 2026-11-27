"""The interest rate of each account that updates itself, found from the
account itself (Starling, Trading 212 cash, Interactive Brokers, ...).

cash.py applies this order for each such account:
  1. the rate typed in Settings: always wins, and clearing it goes back to 2-4
  2. a rate the provider states (IBKR's interest details, a Trade Republic
     payout note): source "provider"
  3. the rate implied by the interest the account actually paid: source "paid"
  4. none: `text` says why, for that account, and `steps` what to do.
No provider in this app states a rate in its API except those two (checked
in each connector's docs, Nov 2026), so 3 is the usual source.

The implied rate (`implied`)
    interest credited in a window / the average end-of-day balance in it,
    compounded to a year: ((1 + interest / average) ^ (365 / days) - 1) x 100.
    That is an AER, the figure banks quote, so it compares with a rate typed
    in. The daily balances are rebuilt backwards from today's balance and
    every movement since (interest included), so money moved in or out
    mid-period counts for the days it was there.

    Windows: credits a day or two apart (Trading 212) -> 30-day blocks ending at
    the latest credit, each credit covering the day before it. Monthly
    credits in the first week of the month (Starling's Easy Saver pays on the
    1st, IBKR on the 3rd business day) -> each covers the calendar month
    before. Anything else -> each credit covers the days since the previous
    one. The newest window is used, with older ones while their rate agrees
    (within 0.15 points, or 4% of the rate) up to 95 days; at the first that
    does not, the rate has changed and only the newer windows count.

    Minimums (else "too little" with the reason): two credits; 28 days; an
    average balance of at least EUR 100 (converted); interest of at least 1.00
    in the account's currency and 0.10 per credit (so rounding each credit to
    the cent moves the rate by under 5%); a result above 0% and at most 15%.
    A balance history that goes below zero means a movement is missing: the
    result is not used. No interest at all while the account held at least
    EUR 100 for 45 days or more is reported as 0% ("no interest paid ...").

    Tax: the figure is what was credited. Each connector says whether that is
    before tax (`basis` "gross"), after tax taken off ("net"), or not stated
    ("paid").

Every lookup that needs another call to the provider runs in the background
(`lookup`): a page gets the last result at once, or "checking" the first time.
Results are kept for 6 hours (30 minutes after an error) in memory only, keyed
by the connection and a hash of its credentials, so a new key is checked
straight away. A failure never touches the balances.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

log = logging.getLogger("stock_overview.interest")

LOOKBACK_DAYS = 125        # movements read: room for 3 calendar months + the current one
MAX_DAYS = 95              # longest window combined into one rate
MIN_DAYS = 28
MIN_BALANCE_EUR = 100.0
MIN_INTEREST = 1.00        # in the account's currency
PER_CREDIT = 0.10          # minimum interest per credit (rounding to the cent: < 5%)
MAX_RATE = 15.0
ZERO_DAYS = 45             # days with money in it before "no interest" means 0%
DAYS_A_YEAR = 365

TTL = 6 * 3600             # a result is reused this long
RETRY = 30 * 60            # after an error
FIRST_WAIT = 0.0           # seconds a page waits for a first result (tests set it)
# INTEREST_LOOKUP=0 turns the background reads off (the tests: no network).
ENABLED = os.getenv("INTEREST_LOOKUP", "1") != "0"

BASIS = {"gross": "before tax", "net": "after tax", "paid": "as paid out"}


# ------------------------------------------------------------------ dates
def _day(x) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def period_text(start, end, today: date | None = None) -> str:
    """'1–31 Oct', '28 Oct–26 Nov', '15 Dec 2025–12 Jan 2026'."""
    start, end = _day(start), _day(end)
    today = today or date.today()
    year = f" {end.year}" if end.year != today.year or start.year != end.year else ""
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}–{end.day} {end:%b}{year}"
    first = f"{start.day} {start:%b}" + (f" {start.year}" if start.year != end.year else "")
    return f"{first}–{end.day} {end:%b}{year}"


def _annual(interest: float, average: float, days: int) -> float:
    return ((1 + interest / average) ** (DAYS_A_YEAR / days) - 1) * 100


def _eur(amount: float, currency: str) -> float:
    try:
        from .fx import to_eur

        return to_eur(amount, currency)
    except Exception:
        return amount


# ------------------------------------------------------------ the maths
def implied(credits, flows, balance_now: float, today, *, currency: str = "EUR",
            basis: str = "paid", since=None) -> dict:
    """The rate implied by interest paid (see the module docstring).

    credits      [(date, amount)] interest paid into the account (> 0), in its currency
    flows        [(date, amount)] every other movement, signed (in +, out -)
    balance_now  the balance at the end of `today`, after all of them
    since        first day the movements are complete from (default: LOOKBACK_DAYS ago)

    -> {"status": "ok", "source": "paid", "pct", "from", "to", "days",
        "interest", "average", "currency", "credits", "frequency", "basis",
        "previous_pct"?, "zero"?} or {"status": "too_little" | "none" |
        "stopped" | "unclear", "text"}."""
    today = _day(today)
    since = _day(since) if since else today - timedelta(days=LOOKBACK_DAYS)
    per: dict[date, float] = {}
    moves: dict[date, float] = {}
    for d, a in credits:
        d, a = _day(d), float(a)
        if since <= d <= today and a > 0:
            per[d] = per.get(d, 0.0) + a
            moves[d] = moves.get(d, 0.0) + a
    for d, a in flows:
        d = _day(d)
        if since <= d <= today:
            moves[d] = moves.get(d, 0.0) + float(a)
    # end-of-day balances, walking back from today: B(d-1) = B(d) - moves(d)
    bal: dict[date, float] = {}
    b, d = float(balance_now), today
    while d >= since:
        bal[d] = b
        b -= moves.get(d, 0.0)
        d -= timedelta(days=1)

    def stats(start: date, end: date) -> tuple[int, float, float]:
        days = [bal[x] for x in (start + timedelta(n) for n in range((end - start).days + 1))]
        return len(days), sum(days) / len(days), min(days)

    out = {"currency": currency, "basis": basis}
    dates = sorted(per)
    if not dates:
        return _no_interest(bal, since, today, currency, out)
    if len(dates) < 2:
        return {**out, "status": "too_little",
                "text": f"Only one interest payment so far ({dates[0].day} {dates[0]:%b}): "
                        "the rate is worked out after the next one."}
    gaps = [(b2 - a2).days for a2, b2 in zip(dates, dates[1:])]
    gap = statistics.median(gaps)
    freq = "daily" if gap <= 3 else "monthly" if 25 <= gap <= 35 else "other"
    latest = dates[-1]
    stale = 10 if freq == "daily" else 45 if freq == "monthly" else int(gap * 1.5) + 10
    if (today - latest).days > stale:
        return {**out, "status": "stopped",
                "text": f"No interest has been paid since {latest.day} {latest:%b %Y}."}

    windows: list[tuple[date, date, float, int]] = []   # (first day, last day, interest, credits)
    months = [(d.year, d.month) for d in dates]
    if freq == "daily":
        started = dates[0] - timedelta(days=1) if (dates[0] - since).days > 3 else since
        for k in range(MAX_DAYS // 30 + 1):
            hi = latest - timedelta(days=30 * k)            # credits dated (hi-30, hi]
            lo = hi - timedelta(days=30)
            start, end = max(lo, started), hi - timedelta(days=1)
            if start > end or start < since:
                break
            got = [d for d in dates if lo < d <= hi]
            windows.append((start, end, sum(per[d] for d in got), len(got)))
            if start > lo:                                   # the account started earning here
                break
    elif freq == "monthly" and all(d.day <= 7 for d in dates) and len(set(months)) == len(months):
        for p in reversed(dates):
            end = p.replace(day=1) - timedelta(days=1)
            start = end.replace(day=1)
            if start < since:
                break
            windows.append((start, end, per[p], 1))
    else:
        for prev, p in reversed(list(zip(dates, dates[1:]))):
            start, end = prev, p - timedelta(days=1)
            if start < since:
                break
            windows.append((start, end, per[p], 1))
    if not windows:
        return {**out, "status": "too_little",
                "text": "The interest paid so far does not cover a whole period yet."}

    rated = []
    for start, end, interest, n in windows:
        days, avg, low = stats(start, end)
        rated.append({"start": start, "end": end, "interest": interest, "n": n, "days": days,
                      "avg": avg, "low": low,
                      "rate": _annual(interest, avg, days) if avg > 0 else None})
    first = rated[0]
    used, previous = [first], None
    if first["rate"] is not None:
        tol = max(0.15, 0.04 * first["rate"])
        for w in rated[1:]:
            if sum(u["days"] for u in used) + w["days"] > MAX_DAYS:
                break
            if w["rate"] is None or abs(w["rate"] - first["rate"]) > tol:
                previous = w["rate"]
                break
            used.append(w)

    days = sum(u["days"] for u in used)
    interest = round(sum(u["interest"] for u in used), 2)
    n = sum(u["n"] for u in used)
    average = sum(u["avg"] * u["days"] for u in used) / days
    low = min(u["low"] for u in used)
    start, end = used[-1]["start"], used[0]["end"]
    span = period_text(start, end, today)
    money = f"{interest:,.2f} {currency}"
    if low < -max(1.0, 0.01 * abs(average)):
        return {**out, "status": "unclear",
                "text": "The balance history could not be rebuilt from the account's movements "
                        f"for {span}, so the rate cannot be worked out reliably."}
    if days < MIN_DAYS:
        return {**out, "status": "too_little",
                "text": f"Only {days} days of interest so far: the rate is worked out after "
                        f"{MIN_DAYS} days."}
    if average <= 0 or _eur(average, currency) < MIN_BALANCE_EUR:
        return {**out, "status": "too_little",
                "text": f"The balance was too small in {span} (on average "
                        f"{max(average, 0):,.2f} {currency}) to work out a rate."}
    if interest < max(MIN_INTEREST, PER_CREDIT * n):
        return {**out, "status": "too_little",
                "text": f"Only {money} of interest was paid for {span}: too little to work out "
                        "a rate."}
    pct = _annual(interest, average, days)
    if not 0 < pct <= MAX_RATE:
        return {**out, "status": "unclear",
                "text": f"The interest paid for {span} works out at {pct:.1f}% a year, which "
                        "looks wrong, so it is not used."}
    res = {**out, "status": "ok", "source": "paid", "pct": round(pct, 2),
           "from": start.isoformat(), "to": end.isoformat(), "days": days,
           "interest": interest, "average": round(average, 2), "credits": n, "frequency": freq}
    if previous is not None:
        res["previous_pct"] = round(previous, 2)
    return res


def _no_interest(bal: dict, since: date, today: date, currency: str, out: dict) -> dict:
    """No interest credited at all: 0% when the account clearly held money
    long enough for interest to have been paid; else not known."""
    held = [d for d, b in bal.items() if _eur(b, currency) >= MIN_BALANCE_EUR]
    days = (today - since).days + 1
    if len(held) >= ZERO_DAYS and days >= 60:
        average = sum(bal.values()) / len(bal)
        return {**out, "status": "ok", "source": "paid", "pct": 0.0, "zero": True,
                "from": since.isoformat(), "to": today.isoformat(), "days": days,
                "interest": 0.0, "average": round(average, 2), "credits": 0,
                "frequency": "none"}
    return {**out, "status": "none",
            "text": f"No interest was paid into this account in the last {days} days. "
                    "If it pays none, enter 0%."}


# ------------------------------------------------------------- wording
def note(info: dict, today: date | None = None) -> str:
    """Where an automatic rate came from, in a few words."""
    if info.get("note"):
        return info["note"]
    if info.get("source") != "paid":
        return ""
    span = period_text(info["from"], info["to"], today)
    if info.get("zero"):
        return f"no interest paid {span}"
    text = f"from the interest paid for {span} ({BASIS.get(info.get('basis'), 'as paid out')})"
    if info.get("previous_pct") is not None:
        text += f"; it was about {info['previous_pct']:.1f}% before"
    return text


def public(info: dict | None, today: date | None = None) -> dict | None:
    """What the browser, the advisors and the MCP tool see."""
    if not info:
        return None
    status = info.get("status")
    if status == "ok":
        out = {"status": "ok", "pct": round(float(info["pct"]), 2), "source": info.get("source"),
               "note": note(info, today)}
        for k in ("from", "to", "interest", "average", "currency", "basis"):
            if info.get(k) is not None:
                out[k] = info[k]
        return out
    if status == "pending":
        return {"status": "checking", "text": "Checking the interest paid into this account…"}
    return {"status": status or "none", "text": info.get("text") or "",
            "steps": list(info.get("steps") or [])}


# ------------------------------------------------------------ background
_lock = threading.Lock()
_results: dict[str, tuple[dict, float]] = {}   # key -> (result, good until)
_running: dict[str, object] = {}               # key -> Future
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="interest")


def clear() -> None:
    with _lock:
        _results.clear()
        _running.clear()


def creds_hash(creds) -> str:
    raw = json.dumps(creds or {}, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _run(key: str, compute) -> None:
    try:
        res = compute()
        res = res if isinstance(res, dict) else {"status": "none"}
    except Exception as e:   # never more than a note on this one account
        log.info("interest lookup %s failed: %r", key.split(":")[0], e)
        res = {"status": "error",
               "text": "Could not read the interest paid just now. It is tried again later."}
    life = RETRY if res.get("status") == "error" else TTL
    with _lock:
        _results[key] = (res, time.time() + life)
        _running.pop(key, None)
        if len(_results) > 64:
            for k in sorted(_results, key=lambda k: _results[k][1])[:len(_results) - 64]:
                _results.pop(k, None)


def lookup(key: str, compute) -> dict:
    """The last result of `compute()` for `key`, refreshed in the background
    when older than its life; {"status": "pending"} until the first one."""
    now = time.time()
    with _lock:
        hit = _results.get(key)
        fut = _running.get(key)
        if (hit is None or now >= hit[1]) and fut is None and ENABLED:
            fut = _running[key] = _pool.submit(_run, key, compute)
    if hit is not None:
        return hit[0]
    if FIRST_WAIT and fut is not None:
        try:
            fut.result(timeout=FIRST_WAIT)
        except Exception:
            pass
        with _lock:
            hit = _results.get(key)
        if hit is not None:
            return hit[0]
    return {"status": "pending"}


# ------------------------------------------------------------ integration
def attach(rows: list[dict]) -> None:
    """Add `rate_auto` (public form) to each automatic cash row, and drop the
    internal fields used to find it. Never raises, never waits."""
    for r in rows:
        try:
            info = _find(r)
        except Exception as e:
            log.info("interest for %s: %r", r.get("institution"), e)
            info = None
        for k in ("ref", "feed", "interest", "connector"):
            r.pop(k, None)
        r["rate_auto"] = public(info)


def _find(row: dict) -> dict | None:
    if isinstance(row.get("interest"), dict):            # read with the balances
        return row["interest"]
    if row.get("feed") == "trading212":
        from .connectors import trading212

        acct = next((a for a in trading212.accounts() if a.label == row.get("ref")), None)
        if acct is None:
            return None
        key = f"trading212:{creds_hash([acct.key, acct.secret])}"
        return lookup(key, lambda: trading212.cash_interest(acct))
    from .connectors import registry

    spec = registry.connector(row.get("connector"))
    if spec is None:
        return None
    if spec.interest is not None:
        from . import connections

        conn = connections.get(row.get("connection") or "")
        if conn is None:
            return None
        creds = conn.get("credentials") or {}
        key = f"{spec.id}:{conn['id']}:{creds_hash(creds)}"
        res = lookup(key, lambda: spec.interest(creds, conn))
        if isinstance(res.get("accounts"), dict):
            return res["accounts"].get(row.get("ref")) or {
                "status": "none", "text": f"{spec.name} sent no interest details for this account."}
        return res
    if spec.interest_note:
        return {"status": "unsupported", "text": spec.interest_note}
    return None
