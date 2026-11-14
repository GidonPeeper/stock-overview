"""Shared plumbing for the live, read-only connectors added in Settings.

A live connector turns the credentials of one connection into a `Snapshot`:
bank balances, positions, trades and income. It never writes anything back to
the bank or broker. Every problem is raised as a `ConnectorError` whose message
is plain language and safe to show in the browser: it never contains the key,
the request URL (IBKR puts its token in the URL) or a raw exception text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

from .base import Holding
from ..positions import Trade

# (connect, read) seconds. A slow institution must never hold up the dashboard;
# connections.py also waits only a few seconds before using the last result.
TIMEOUT = (5, 20)


class ConnectorError(Exception):
    """A plain-language problem with one connection (safe to show)."""

    def __init__(self, message: str, *, status: int | str | None = None,
                 pending: bool = False):
        super().__init__(message)
        self.status = status
        # True when the institution accepted the request but the data is not
        # ready yet (IBKR builds its report on demand). Not a failure.
        self.pending = pending


@dataclass
class Snapshot:
    """What one connection reported at one moment."""
    summary: str = ""                                    # "2 accounts", "12 positions"
    cash: list[dict] = field(default_factory=list)       # {name, currency, balance, type?}
    holdings: list[Holding] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    dividends: list[dict] = field(default_factory=list)  # {date, ticker, isin, amount, currency}
    interest: list[dict] = field(default_factory=list)   # {date, amount, currency}


def http_message(name: str, status: int, hints: dict[int, str] | None = None) -> str:
    hint = (hints or {}).get(status, "")
    if status == 401:
        msg = f"{name} did not accept the key (HTTP 401). Check you copied all of it, or make a new one."
    elif status == 403:
        msg = f"{name} refused access (HTTP 403). The key may be missing a read permission."
    elif status == 404:
        msg = f"{name} could not find that (HTTP 404)."
    elif status == 429:
        msg = f"{name} says there were too many requests. Try again in a minute."
    elif status >= 500:
        msg = f"{name} is having problems right now (HTTP {status}). Try again later."
    else:
        msg = f"{name} answered with an error (HTTP {status})."
    return f"{msg} {hint}".strip()


def request(name: str, url: str, *, headers: dict | None = None, params: dict | None = None,
            auth=None, hints: dict[int, str] | None = None,
            timeout=TIMEOUT) -> requests.Response:
    """GET `url`. Network and HTTP problems become a ConnectorError without
    the URL or any credential in it (`from None` drops the original exception,
    whose text includes the URL)."""
    try:
        resp = requests.get(url, headers=headers, params=params, auth=auth, timeout=timeout)
    except requests.Timeout:
        raise ConnectorError(f"{name} did not answer in time. Try again later.") from None
    except requests.RequestException:
        raise ConnectorError(f"Could not reach {name}. Check the internet connection.") from None
    if resp.status_code >= 400:
        raise ConnectorError(http_message(name, resp.status_code, hints),
                             status=resp.status_code) from None
    return resp


def get_json(name: str, url: str, **kw):
    resp = request(name, url, **kw)
    try:
        return resp.json()
    except ValueError:
        raise ConnectorError(f"{name} sent an answer this app could not read.") from None


def num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
