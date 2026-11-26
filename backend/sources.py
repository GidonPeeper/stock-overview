"""Every account the dashboard reads, in one place.

Trading 212 can contribute several accounts (e.g. "T212 ISA" and "T212 Invest");
DeGiro contributes one; Trade Republic one per account (its live sign-in and
the uploaded export of the same account count once, connectors/traderepublic.py).
Realized P/L is computed per account, so the same share held in an ISA and in
a normal account stays in two separate cost pools, as HMRC treats them.
"""

from __future__ import annotations

from typing import Callable

from . import connections
from .connectors import degiro, traderepublic, trading212
from .connectors.base import Holding
from .fx import to_eur
from .positions import Trade


def holdings() -> list[Holding]:
    return (trading212.get_holdings() + degiro.get_holdings() + traderepublic.get_holdings()
            + connections.holdings())  # accounts added in Settings (IBKR, CSV, ...)


def trade_sources() -> dict[str, tuple[bool, Callable[[], list[Trade]]]]:
    """{account label: (is_isa, get_trades)}. A connector that cannot even
    list its accounts is left out and reported (problems()); the others count."""
    out: dict = {}
    for name, part in (("Trading 212", lambda: trading212.trade_sources()),
                       ("DeGiro", lambda: {"DeGiro": (False, degiro.get_trades)}),
                       # the upload and/or the live sign-in, once
                       ("Trade Republic", lambda: traderepublic.trade_sources()),
                       ("Accounts added in Settings", lambda: connections.trade_sources())):
        try:
            got = part()
        except Exception as exc:
            _note(f"sources:{name}", name, f"{_why(exc, 'Its accounts')}. Profit from selling "
                                           "and the chart leave it out for now.")
            continue
        _clear(f"sources:{name}")
        out.update(got)
    return out


def all_trades() -> list[Trade]:
    out: list[Trade] = []
    for _, get in trade_sources().values():
        try:
            out.extend(get())
        except Exception:
            continue
    return out


def in_eur(trades: list[Trade]) -> list[Trade]:
    """Same trades with every cash amount converted to EUR (today's rate for
    non-EUR accounts, e.g. a GBP Trading 212 account)."""
    return [
        t if t.currency == "EUR" else Trade(
            when=t.when, key=t.key, name=t.name, side=t.side, quantity=t.quantity,
            amount=to_eur(t.amount, t.currency), currency="EUR", ticker=t.ticker)
        for t in trades
    ]


def all_trades_eur() -> list[Trade]:
    return in_eur(all_trades())


# ---------------------------------------------------------- one source fails
# Each source is read on its own: one that fails (a broker API down, a damaged
# statement file) never takes the others down. It keeps its last good holdings
# when it had any, and `problems()` says so in plain words, so the dashboard
# can tell the totals leave something out or use older numbers. Never a
# silent 0.
import logging as _logging
import threading as _threading
import time as _time

_log = _logging.getLogger("stock_overview.sources")
_plock = _threading.Lock()
_problems: dict[str, dict] = {}        # "holdings:<source>" / "trades:<account>" -> problem
_last_good: dict[tuple, tuple] = {}    # (source, demo) -> (holdings, read at)


def _holding_sources() -> list[tuple[str, Callable[[], list[Holding]]]]:
    return [("Trading 212", lambda: trading212.get_holdings()),
            ("DeGiro", lambda: degiro.get_holdings()),
            ("Trade Republic", lambda: traderepublic.get_holdings()),
            ("Accounts added in Settings", lambda: connections.holdings())]


def _why(exc: BaseException, what: str = "it") -> str:
    """A plain sentence (no trailing full stop) on why a source failed."""
    from .connectors.live import ConnectorError
    from .datafiles import DataFileError
    if isinstance(exc, (DataFileError, ConnectorError)):
        return str(exc).rstrip(". ")          # already plain words
    return f"{what} could not be read just now ({type(exc).__name__})"


def _note(key: str, source: str, message: str) -> None:
    with _plock:
        first = key not in _problems
        _problems[key] = {"source": source, "message": message, "at": _time.time()}
    if first:
        _log.warning("%s: %s", source, message)


def _clear(key: str) -> None:
    with _plock:
        _problems.pop(key, None)


def _uncached_holdings_each() -> list[Holding]:
    from .datafiles import demo
    mode = demo()
    out: list[Holding] = []
    for name, get in _holding_sources():
        key = f"holdings:{name}"
        try:
            got = get()
        except Exception as exc:
            with _plock:
                prev = _last_good.get((name, mode))
            if prev:
                when = _time.strftime("%H:%M", _time.localtime(prev[1]))
                _note(key, name, f"{_why(exc, 'It')}. Showing its holdings as read at {when}.")
                out.extend(prev[0])
            else:
                _note(key, name, f"{_why(exc, 'It')}. Its holdings are left out of the totals for now.")
            continue
        with _plock:
            _last_good[(name, mode)] = (list(got), _time.time())
        _clear(key)
        out.extend(got)
    return out


_status_thread: list = [None]


def _t212_status() -> list[dict]:
    """trading212.status() (per-key health), never waited for: it can ask
    the API, which rate-limits hard. Read in the background, then refreshed
    at most once a minute while the last answer is served."""
    from . import cache

    def get():
        return cache.get("sources.t212_status", trading212.status, ttl=60, stale=3600, data=False)

    if cache.peek("sources.t212_status")[0]:
        return get()
    th = _status_thread[0]
    if th is None or not th.is_alive():
        def first():
            try:
                get()
            except Exception:
                pass            # tried again on a later page load
        th = _status_thread[0] = _threading.Thread(target=first, name="t212-status", daemon=True)
        th.start()
    return []


def problems() -> list[dict]:
    """What could not be read just now, in plain words: [{source, message}].
    Covers the sources above, Trading 212 keys and accounts added in Settings."""
    with _plock:
        # a note not repeated for 30 minutes is over (e.g. that account was removed)
        for k in [k for k, p in _problems.items() if _time.time() - p["at"] > 1800]:
            del _problems[k]
        out = [{"source": p["source"], "message": p["message"]} for p in _problems.values()]
    try:
        for row in _t212_status():
            if row.get("ok") is False and not row.get("note"):
                out.append({"source": row["label"], "message": f"{row.get('error') or 'could not be read'}. "
                            "Its holdings are left out of the totals for now; see Settings."})
    except Exception:
        pass
    try:
        for c in connections.load_all():
            if c.get("status") in ("error", "signin"):
                last = c.get("last_sync")
                msg = c.get("error") or "Sign in again in Settings."
                out.append({"source": c.get("label") or c["id"], "message": msg + (
                    f" Showing the numbers read at {str(last).replace('T', ' ')[:16]}." if last
                    else " It is left out of the totals for now.")})
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ caching
# Everything above is wrapped here, so a connector added to `holdings()` or
# `trade_sources()` is cached with no extra code (see backend/cache.py):
#   holdings     fresh 30 s, then served while a background refresh runs (1 h)
#   trade lists  per account, fresh 5 min, then served while refreshing (1 h)
# Any upload or edit (a write under /api/, or a changed data file) makes them
# recompute before they are served again. A failing broker keeps its last
# good result. Trade lists are shared: treat them as read-only.
import functools as _functools

from . import cache as _cache

_uncached_holdings = _uncached_holdings_each
_uncached_trade_sources = trade_sources


@_functools.wraps(holdings)
def holdings() -> list[Holding]:  # noqa: F811 - cached wrapper of the function above
    return _cache.get("sources.holdings", _uncached_holdings, ttl=30, stale=3600, copy=True)


def _cached_trades(label: str, get_trades: Callable[[], list[Trade]]) -> list[Trade]:
    key = f"trades:{label}"
    try:
        out = list(_cache.get(f"sources.trades:{label}", get_trades, ttl=300, stale=3600))
    except Exception as exc:
        _note(key, label, f"{_why(exc, 'Its trade history')}. "
                          "Profit from selling and the chart leave it out for now.")
        raise
    failed = _cache.last_error(f"sources.trades:{label}")
    if failed:
        _note(key, label, "Its trade history could not be refreshed just now; "
                          "using the copy read earlier.")
    else:
        _clear(key)
    return out


@_functools.wraps(_uncached_trade_sources)
def trade_sources() -> dict[str, tuple[bool, Callable[[], list[Trade]]]]:  # noqa: F811
    return {label: (is_isa, _functools.partial(_cached_trades, label, get))
            for label, (is_isa, get) in _uncached_trade_sources().items()}
