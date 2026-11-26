"""Trade Republic: the live sign-in and the uploaded export, counted once.

Two ways in, for the same account:
  * live   -- the in-app sign-in (connectors/traderepublic_live.py, added in
              Settings): positions and cash every few minutes, the full history
              from Trade Republic's timeline.
  * upload -- `data/trades_trade_republic.json` (+ income_trade_republic.json),
              made from the app's "Transaction export" CSV or the account
              statement PDF (backend/importers/traderepublic_*.py), or by hand.
              Holdings are rebuilt from its trades. Amounts are exact EUR.

Live wins once it works; the upload stays as the fallback (plan()):
  * no sign-in, or it never worked          -> the upload counts fully
  * signed in, history still being read     -> live positions; the upload's
                                               trades and income (assumed the
                                               same account until we can check)
  * history read, same account as the upload (most of the upload's trades,
    same ISIN on about the same day, are in it) -> live only
  * history read, a different account       -> both, the upload as
                                               "Trade Republic (upload)" if
                                               the sign-in is called
                                               "Trade Republic"
So realized P/L, dividends and the performance chart never count one account
twice. Cash and income of a sign-in flow through connections.py (cash.py,
income.py); this module handles holdings, trades and the upload's income.
"""

from __future__ import annotations

import functools
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from .base import Holding
from ..datafiles import resolve as _resolve_data
from ..instruments import prefetch, price_currency, ticker_for
from ..positions import Trade, compute_positions

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
LABEL = "Trade Republic"
UPLOAD_LABEL = "Trade Republic (upload)"
CONNECTOR = "traderepublic"


# ------------------------------------------------------------------ upload
def _data_file() -> Path:
    return _resolve_data("trades_trade_republic.json")[0]


def _income_file() -> Path:
    return _resolve_data("income_trade_republic.json")[0]


@functools.lru_cache(maxsize=4)
def _read_json(path: str, mtime: float) -> dict:
    data = json.loads(Path(path).read_text())
    return data if isinstance(data, dict) else {}


def _load(path: Path) -> dict:
    try:
        return _read_json(str(path), path.stat().st_mtime)
    except (OSError, ValueError):
        return {}


def _file_raw() -> list[dict]:
    return [t for t in _load(_data_file()).get("trades", []) if isinstance(t, dict)]


def _file_income_raw() -> list[dict]:
    return [r for r in _load(_income_file()).get("income", []) if isinstance(r, dict)]


def get_trades() -> list[Trade]:
    """The uploaded file's trades (exact EUR), whether or not they count; use
    trade_sources() for the ones that do."""
    raw = _file_raw()
    if not raw:
        return []
    prefetch({t["isin"]: "EUR" for t in raw if not t.get("ticker")})
    return [
        Trade(
            when=t["date"],
            key=t["isin"],
            name=t["name"],
            side=str(t["side"]).upper(),   # a hand-made JSON may say "buy"; anything but BUY is a sale
            quantity=float(t["quantity"]),
            amount=float(t["amount_eur"]),   # exact EUR
            currency="EUR",
            ticker=t.get("ticker") or ticker_for(t["isin"], "EUR"),
        )
        for t in raw
    ]


def _file_holdings(label: str) -> list[Holding]:
    holdings: list[Holding] = []
    for p in compute_positions(get_trades()).values():
        if not p.is_open:
            continue
        holdings.append(
            Holding(
                broker=label,
                ticker=p.ticker,
                name=p.name,
                isin=p.key,
                quantity=round(p.quantity, 6),
                buy_price=round(p.avg_cost, 4),  # EUR cost per share
                currency=price_currency(p.ticker),  # USD for US listings, EUR for Xetra/Euronext
                buy_currency="EUR",
            )
        )
    return holdings


# -------------------------------------------------------------------- live
def _live() -> list[tuple[dict, object]]:
    """[(connection, latest snapshot or None)] for every Trade Republic sign-in."""
    try:
        from .. import connections
        conns = [c for c in connections.load_all() if c.get("connector") == CONNECTOR]
        return [(c, connections.snapshot(c)) for c in conns]
    except Exception:
        return []


def _day(s: str) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def same_account(file_trades: list[dict], live_trades: list[Trade]) -> bool:
    """Most of the upload's trades (same ISIN, same day give or take one) are
    in the live history. An upload without trades counts as the same."""
    want = {(t.get("isin"), _day(t.get("date", ""))) for t in file_trades if t.get("isin")}
    want = {k for k in want if k[1]}
    if not want:
        return True
    have = {(t.key, _day(t.when)) for t in live_trades}
    common = sum(1 for isin, d in want
                 if any((isin, d + timedelta(days=k)) in have for k in (-1, 0, 1)))
    small = min(len(want), len({k for k in have if k[1]}))
    return small > 0 and common >= min(3, small) and common >= 0.5 * small


def plan() -> dict:
    """Which source counts for what (see the module docstring):
    {"file": "all" | "history" | "none", "file_label", "working": [(conn,
    snap)], "complete": [(conn, snap)]}."""
    lives = _live()
    working = [(c, s) for c, s in lives if s is not None]
    complete = [(c, s) for c, s in working if getattr(s, "history_complete", False)]
    raw, income = _file_raw(), _file_income_raw()
    if not raw and not income:
        file = "none"
    elif not working:
        file = "all"
    elif any(same_account(raw, s.trades) for _, s in complete):
        file = "none"
    elif complete and len(complete) == len(working):
        file = "all"          # every signed-in account is another one
    else:
        file = "history"      # still reading the live history: assume the same account
    # the upload is renamed when a counted sign-in is called "Trade Republic"
    counted = working if file == "all" else complete
    taken = file != "none" and any(c.get("label") == LABEL for c, _ in counted)
    return {"file": file, "file_label": UPLOAD_LABEL if taken else LABEL,
            "working": working, "complete": complete}


def _live_trades(conn_id: str) -> list[Trade]:
    for c, s in _live():
        if c["id"] == conn_id and s is not None and getattr(s, "history_complete", False):
            return list(s.trades)
    return []


# -------------------------------------------------------------- public API
def get_holdings() -> list[Holding]:
    p = plan()
    out: list[Holding] = _file_holdings(p["file_label"]) if p["file"] == "all" else []
    for c, s in p["working"]:
        out.extend(replace(h, broker=c["label"], wrapper="") for h in s.holdings)
    return out


def trade_sources() -> dict:
    """{account label: (is_isa, get_trades)}: the upload (if it counts) and
    each signed-in account whose history has been read."""
    p = plan()
    out: dict = {}
    if p["file"] != "none":
        out[p["file_label"]] = (False, lambda: get_trades())
    for c, _ in p["complete"]:
        out[c["label"]] = (False, functools.partial(_live_trades, c["id"]))
    if not out:
        out[LABEL] = (False, lambda: get_trades())
    return out


def income_rows() -> tuple[str, list[dict]]:
    """(label, the upload's dividend/interest rows) when the upload's income
    counts, else (label, []). A sign-in's income comes through connections.py."""
    p = plan()
    if p["file"] == "none":
        return p["file_label"], []
    return p["file_label"], _file_income_raw()


def file_note() -> str:
    """Why the uploaded file is not counted (Settings), or ""."""
    if (_file_raw() or _file_income_raw()) and plan()["file"] == "none":
        return "Not counted: your Trade Republic sign-in covers the same account."
    return ""
