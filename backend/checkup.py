"""Check-up: is everything all right, and if not, what to do.

One list of findings for Settings → App and data → Check-up (and the badge on
the dashboard's settings button). Each finding is built from what the app
already knows: the last answers of brokers and banks, the files on disk, the
prices it holds, the saved choices and the dates of its own rules. A check
that cannot be made says nothing (never a guess). Nothing here asks a broker,
a bank or Yahoo: the slow inputs (the holdings, the trade history, the cash
list) are taken from the caches the dashboard keeps warm, and one that is not
ready within a moment is skipped this time.

A finding:
    id        stable name, e.g. "connection_error:c_12ab"
    level     "problem"    something is wrong and numbers are affected
              "attention"  something to do soon
              "info"       good to know
    title     what is wrong, in a few words
    why       why it matters
    action    the exact thing to do
    target    where it is done: a place in the app ("/settings#backup") or a
              document on GitHub (TROUBLESHOOTING.md)
    target_label  the text of that link

Add a check: write a function `_check_x(ctx) -> list[dict]` with `_finding()`
and add it to CHECKS. tests/test_checkup.py tests each one with and without
the condition that triggers it.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from . import rules, version

log = logging.getLogger("stock_overview.checkup")

LEVELS = ("problem", "attention", "info")

STALE_STATEMENT_DAYS = 60      # newest entry of an uploaded statement, while the account holds shares
STALE_CSV_DAYS = 90            # a position file uploaded for an account added in Settings
STALE_CONNECTION_DAYS = 3      # a live connection not updated for this long
BACKUP_EVERY_DAYS = 90         # on your own computer: download a backup at least this often
SHORT_SECRET = 32              # characters (DASHBOARD_SECRET, DATA_KEY)
SHORT_PASSWORD = 12            # characters, for a copy on the internet
SLOW_INPUT_SECONDS = 1.5       # wait at most this long for a cold cache


def _finding(id_: str, level: str, title: str, why: str, action: str,
             target: str = "", target_label: str = "") -> dict:
    assert level in LEVELS
    return {"id": id_, "level": level, "title": title, "why": why, "action": action,
            "target": target, "target_label": target_label}


def _help(anchor: str) -> str:
    return version.docs_url("TROUBLESHOOTING.md", anchor)


def _day(value) -> date | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).date()
    return rules.parse_day(value)


def _names(items: list[str], most: int = 3) -> str:
    items = [i for i in items if i]
    if len(items) <= most:
        return ", ".join(items[:-1]) + (" and " if len(items) > 1 else "") + items[-1] if items else ""
    return ", ".join(items[:most]) + f" and {len(items) - most} more"


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


# ---------------------------------------------------------------- context
class Context:
    """What the checks read. Slow inputs are gathered once, in parallel, each
    within SLOW_INPUT_SECONDS; one that is not ready is None (its checks are
    skipped). Tests pass the inputs in directly."""

    def __init__(self, holdings_rows: Callable[[], tuple] | None = None, vault_state: dict | None = None,
                 today: date | None = None, inputs: dict | None = None):
        from . import clock
        self.today = today or clock.today()
        self.now = time.time()
        self.vault_state = vault_state or {}
        self._inputs = dict(inputs or {})
        if inputs is None:
            self._gather(holdings_rows)

    def _gather(self, holdings_rows) -> None:
        from . import cash, realized
        jobs = {"rows": (lambda: holdings_rows()[0]) if holdings_rows else None,
                "realized": realized.summary, "cash": cash.load}
        got: dict = {}

        def run(name, fn):
            try:
                got[name] = fn()
            except Exception as e:          # a damaged file: its own check reports it
                got[name] = e
        threads = [threading.Thread(target=run, args=(n, f), name=f"checkup:{n}", daemon=True)
                   for n, f in jobs.items() if f is not None]
        for th in threads:
            th.start()
        end = time.monotonic() + SLOW_INPUT_SECONDS
        for th in threads:
            th.join(max(0.0, end - time.monotonic()))
        self._inputs.update(got)            # those still running fill the caches for next time

    def get(self, name: str):
        """The input, or None when it is not ready or failed."""
        v = self._inputs.get(name)
        return None if isinstance(v, BaseException) else v

    def error(self, name: str) -> BaseException | None:
        v = self._inputs.get(name)
        return v if isinstance(v, BaseException) else None


# ------------------------------------------------------------ the checks
def _check_demo(ctx: Context) -> list[dict]:
    from . import datafiles
    if not datafiles.demo():
        return []
    return [_finding("demo", "info", "You are looking at example data",
                     "The numbers on the dashboard are made up, so you can look around first.",
                     "Add your first account: a broker key, a statement file or a balance you type in. "
                     "The examples disappear as soon as you add something of your own.",
                     "/settings#add-account", "Add an account")]


def _check_trading212_keys(ctx: Context) -> list[dict]:
    from . import sources
    from .connectors import trading212
    try:
        rows = sources._t212_status()      # cached; never waits for Trading 212
        in_env = {a.label for a in trading212.accounts() if a.isa is None}
    except Exception:
        return []
    out = []
    for row in rows:
        if row.get("ok") is not False or row.get("note"):
            continue
        label, err = row.get("label", "Trading 212"), str(row.get("error") or "")
        where = ("in your .env file (or your host's settings), where the T212_… lines are"
                 if label in in_env else "in Settings → Connected accounts")
        if "401" in err:
            what = (f"Trading 212 refused the key for {label}: it is wrong, expired or was deleted. "
                    f"In Trading 212 make a new read-only API key (Settings → API), then replace the old one {where}.")
        elif "403" in err:
            what = (f"The key for {label} is missing a permission. In Trading 212 make a new API key with "
                    f"the read permissions ticked (account data, portfolio, history), then replace the old one {where}.")
        else:
            what = (f"Trading 212 did not answer ({err or 'no answer'}). This often passes by itself; "
                    f"if it lasts more than a day, make a new key in Trading 212 and replace the old one {where}.")
        out.append(_finding(f"t212_key:{label}", "problem", f"Trading 212 key not working: {label}",
                            "The holdings of this account are left out of your totals.", what,
                            "/settings#connected" if label not in in_env else _help("a-broker-key-is-refused"),
                            "Connected accounts" if label not in in_env else "What to do"))
    return out


def _check_connections(ctx: Context) -> list[dict]:
    from . import connections
    from .connectors import registry
    from .connectors.live import consent
    try:
        conns = connections.load_all()
    except Exception:
        return []
    out = []
    for c in conns:
        cid, label = c["id"], c.get("label") or c["id"]
        status, last = c.get("status"), c.get("last_sync")
        shown = f" Showing the numbers read on {rules.day_text(last)}." if last else " It is left out of your totals."
        if status == "signin":
            out.append(_finding(f"connection_signin:{cid}", "problem", f"Sign in to {label} again",
                                "Its sign-in has ended, so it no longer updates." + shown,
                                "Open Settings → Connected accounts and press “Sign in again” next to it.",
                                "/settings#connected", "Connected accounts"))
        elif status == "error":
            err = (c.get("error") or "").strip()
            out.append(_finding(f"connection_error:{cid}", "problem", f"{label} could not be updated",
                                (f"It said: {err}" if err else "It did not answer.") + shown,
                                "Open Settings → Connected accounts and press Refresh. If it keeps failing, "
                                "remove it and add it again with a new key (or renew the bank link).",
                                "/settings#connected", "Connected accounts"))
        info = consent(c.get("consent_expires")) if c.get("consent_expires") else None
        if info and info["expired"]:
            out.append(_finding(f"bank_link_expired:{cid}", "problem", f"The bank link to {label} has ended",
                                f"Banks give access for a limited time; this one ended on {rules.day_text(info['expires'])}, "
                                "so its balance no longer updates.",
                                "Open Settings → Connected accounts and press Renew next to it, then approve it in your bank's app.",
                                "/settings#connected", "Connected accounts"))
        elif info and info["warn"]:
            out.append(_finding(f"bank_link_ends:{cid}", "attention", f"The bank link to {label} ends soon",
                                f"It ends on {rules.day_text(info['expires'])} "
                                f"({_plural(info['days_left'], 'day')} from now); after that its balance stops updating.",
                                "Open Settings → Connected accounts and press Renew next to it, then approve it in your bank's app.",
                                "/settings#connected", "Connected accounts"))
        spec = registry.connector(c.get("connector"))
        live = spec is not None and spec.fetch is not None and c.get("method") in ("api", "aggregator")
        last_day = _day(last)
        busy = getattr(connections._inflight.get(cid), "done", lambda: True)() is False
        if (live and status == "ok" and last_day and not busy
                and (ctx.today - last_day).days > STALE_CONNECTION_DAYS):
            out.append(_finding(f"connection_old:{cid}", "attention", f"{label} last updated {rules.day_text(last_day)}",
                                "Its numbers may be out of date.",
                                "Open Settings → Connected accounts and press Refresh next to it.",
                                "/settings#connected", "Connected accounts"))
        if c.get("connector") == registry.GENERIC_CSV and last_day \
                and (ctx.today - last_day).days > STALE_CSV_DAYS:
            out.append(_finding(f"csv_old:{cid}", "attention", f"The {label} file is from {rules.day_text(last_day)}",
                                "Positions read from a file do not update themselves: anything bought or sold since "
                                "is missing.",
                                f"Export a new positions file from {c.get('institution_name') or label} and upload it in "
                                "Settings → Connected accounts.", "/settings#connected", "Connected accounts"))
    try:
        damaged = dict(connections.damaged)
    except Exception:
        damaged = {}
    for name in damaged:
        out.append(_finding(f"damaged:{name}", "problem", f"Your file {name} could not be read",
                            "The accounts or positions in it are left out until it is repaired.",
                            "Restore a backup in Settings → Back up and restore. The damaged file is kept next to the "
                            "new one (its name ends in .damaged-…) when you save again.",
                            "/settings#backup", "Back up and restore"))
    return out


def _check_source_problems(ctx: Context) -> list[dict]:
    """Sources that could not be read (a damaged statement, a broker down),
    other than Trading 212 keys and connections, which have their own checks."""
    from . import sources
    with sources._plock:
        items = [(k, dict(p)) for k, p in sources._problems.items() if ctx.now - p["at"] <= 1800]
    out = []
    for key, p in items:
        out.append(_finding(f"source:{key}", "problem", f"{p['source']}: could not be read",
                            p["message"],
                            "If it is a file you uploaded, upload it again (Settings → Statements). If it is a "
                            "connection, see Settings → Connected accounts. It often passes within minutes; "
                            "if not, see “A statement is refused” in the help.",
                            _help("a-statement-is-refused"), "Help"))
    return out


def _check_files(ctx: Context) -> list[dict]:
    from .datafiles import DataFileError
    out = []
    for name in ("cash",):
        err = ctx.error(name)
        if isinstance(err, DataFileError):
            out.append(_finding(f"damaged:{err.path.name}", "problem", f"Your file {err.path.name} could not be read",
                                str(err), "Restore a backup in Settings → Back up and restore.",
                                "/settings#backup", "Back up and restore"))
    try:
        from . import finances
        finances._load_raw()
    except DataFileError as err:
        out.append(_finding(f"damaged:{err.path.name}", "problem", f"Your file {err.path.name} could not be read",
                            str(err), "Restore a backup in Settings → Back up and restore.",
                            "/settings#backup", "Back up and restore"))
    except Exception:
        pass
    return out


def _check_incomplete_history(ctx: Context) -> list[dict]:
    summary = ctx.get("realized")
    if not summary or not summary.get("incomplete"):
        return []
    items = summary["incomplete"]
    names = [f"{i['name']} ({i['broker']})" for i in items]
    return [_finding("incomplete_history", "attention",
                     f"{_plural(len(items), 'sale')} without a recorded purchase",
                     f"You sold shares the app never saw you buy: {_names(names)}. Profit from selling counts only "
                     "the shares with a known purchase, so it is not complete.",
                     "Upload a statement that starts on the day you opened the account (for DEGIRO: Inbox → "
                     "Account statement, with the start date before your first trade). Shares moved in from "
                     "another broker have no purchase in this statement: that gap stays.",
                     "/settings#upload", "Statements")]


def _check_prices(ctx: Context) -> list[dict]:
    from . import prices
    rows = ctx.get("rows")
    if rows is None:
        return []
    out = []
    unpriced = [r for r in rows if not r.get("priced_live")]
    if unpriced:
        out.append(_finding("unpriced", "attention",
                            f"{_plural(len(unpriced), 'holding')} without a price",
                            f"{_names([r['name'] for r in unpriced])}: no live price was found, so "
                            f"{'it is' if len(unpriced) == 1 else 'they are'} counted at what you paid.",
                            "If the share was delisted or is an unusual fund, there is nothing to do. Otherwise "
                            "search for it with the search box at the top of the dashboard: if it shows a price "
                            "there under another name, report it (see the help) so the app can learn it.",
                            _help("a-holding-has-no-price"), "Help"))
    held = sorted({r["ticker"] for r in rows if r.get("ticker")})
    failed = [t for t in held if t in prices._FAILED
              and (t not in prices._CACHE or prices._FAILED[t] > prices._CACHE[t][2])]
    if held and failed and len(failed) == len(held):
        when = max(prices._FAILED[t] for t in failed)
        out.append(_finding("prices_down", "problem", "Prices are not coming in",
                            f"Yahoo Finance, where the prices come from, did not answer at "
                            f"{time.strftime('%H:%M', time.localtime(when))}. The dashboard shows the last prices it had.",
                            "This usually passes within an hour. If it lasts a day: check that this computer is "
                            "online, then update the app (Yahoo sometimes changes how it answers, and updates "
                            "follow that).", _help("prices-are-missing-or-old"), "Help"))
    elif failed:
        old = [t for t in failed if t in prices._CACHE and ctx.now - prices._CACHE[t][2] > 3600]
        if old:
            out.append(_finding("prices_old", "attention", f"{_plural(len(old), 'price')} could not be refreshed",
                                f"The last price of {_names(old)} is more than an hour old.",
                                "Usually this passes by itself. If it lasts a day, see the help.",
                                _help("prices-are-missing-or-old"), "Help"))
    return out


def _check_rates(ctx: Context) -> list[dict]:
    from . import fx
    rows = ctx.get("rows")
    if rows is None:
        return []
    need = {fx.unit(r.get("currency"))[1] for r in rows} - {fx.BASE_CURRENCY}
    out = []
    fixed = sorted(c for c in need if c in fx._FALLBACK and c not in fx._cache)
    if fixed:
        out.append(_finding("rates_fixed", "attention", "Live exchange rates are missing",
                            f"Amounts in {_names(fixed)} are converted with built-in rates, which can be a few "
                            "percent off.", "This usually passes within an hour. If it lasts a day, see the help.",
                            _help("prices-are-missing-or-old"), "Help"))
    none = sorted(c for c in need if c not in fx._FALLBACK and c not in fx._PEGGED
                  and c in fx._other and fx._other[c][0] is None)
    if none:
        out.append(_finding("rates_none", "problem", f"No exchange rate for {_names(none)}",
                            "Amounts in that currency are counted as if 1 unit were 1 euro, so totals are wrong.",
                            "Try again in an hour. If it stays, report it (see the help): the app needs to learn "
                            "where to find that rate.", _help("numbers-look-wrong"), "Help"))
    return out


def _check_cash_rates(ctx: Context) -> list[dict]:
    c = ctx.get("cash")
    if not c or not c.get("rate_unknown_eur") or c.get("sample"):
        return []
    from .cash import INTEREST_TYPES
    names = [a["name"] for a in c.get("accounts", []) if not a.get("rate_known") and a.get("type") in INTEREST_TYPES]
    return [_finding("cash_rate_unknown", "info", "Interest rate not set",
                     f"For {_names(names) or 'some accounts'} the app does not know the interest rate, so their "
                     "interest is left out of what your cash earns.",
                     "Enter the rate (0 if it pays nothing) in Settings → Bank, savings and pension accounts.",
                     "/settings#cash", "Your accounts")]


def _statement_dates(name: str, real_path: Path) -> tuple[date | None, date | None]:
    """(newest entry, when uploaded) of an uploaded statement."""
    import json
    newest = uploaded = None
    if name == "degiro_account.csv":
        from .connectors import degiro
        for r in degiro._rows(real_path):
            try:
                d = datetime.strptime(r[0].strip(), "%d-%m-%Y").date()
            except ValueError:
                continue
            newest = max(newest, d) if newest else d
    else:
        data = json.loads(real_path.read_text())
        dates = []
        period = ((data.get("overview") or {}).get("period") or [None, None])
        if len(period) == 2 and period[1]:
            dates.append(rules.parse_day(period[1]))
        for key in ("trades", "income", "dividends"):
            for row in data.get(key) or []:
                if isinstance(row, dict):
                    dates.append(rules.parse_day(row.get("date")))
        dates = [d for d in dates if d]
        newest = max(dates) if dates else None
        uploaded = rules.parse_day(data.get("_generated"))
    if uploaded is None and version.where() == "computer":
        uploaded = datetime.fromtimestamp(real_path.stat().st_mtime).date()   # the disk is kept: the upload time
    return newest, uploaded


_STATEMENTS = (  # file, broker, the labels of its holdings, how to export a new one
    ("degiro_account.csv", "DEGIRO", ("DeGiro",),
     "In DEGIRO open Inbox → Account statement, set the start date to your first trade and the end date to "
     "today, export it (PDF or CSV) and upload it in Settings → Statements."),
    ("trades_trade_republic.json", "Trade Republic", ("Trade Republic", "Trade Republic (upload)"),
     "In the Trade Republic app open Profile → Account statements, download the newest account statement "
     "(or the transaction export) and upload it in Settings → Statements."),
    ("trading212_statement.json", "Trading 212", ("Trading212", "Trading212 (statement)"),
     "In Trading 212 open Menu → History → Export, download the period since your last statement and upload "
     "it in Settings → Statements (it is added to the one you have)."),
)


def _check_statements(ctx: Context) -> list[dict]:
    from . import datafiles
    rows = ctx.get("rows")
    if rows is None:
        return []
    out = []
    for fname, broker, labels, how in _STATEMENTS:
        path, real = datafiles.resolve(fname)
        if not real or not any(r.get("broker") in labels for r in rows):
            continue          # no upload, or that account holds nothing now (closed)
        try:
            newest, uploaded = _statement_dates(fname, path)
        except Exception:
            continue
        if not newest or (ctx.today - newest).days <= STALE_STATEMENT_DAYS:
            continue
        up = f" It was uploaded on {rules.day_text(uploaded)}." if uploaded else ""
        out.append(_finding(f"statement_old:{fname}", "attention",
                            f"Your {broker} statement ends on {rules.day_text(newest)}",
                            f"Its newest entry is from {rules.day_text(newest)}.{up} Anything you bought, sold or "
                            "received since then is missing. (If nothing happened since, there is nothing to do.)",
                            how, "/settings#upload", "Statements"))
    return out


def _check_saving(ctx: Context) -> list[dict]:
    from . import appstate, datafiles, github_sync
    out = []
    on_host = version.where() == "host"
    vs = ctx.vault_state or {}
    err = vs.get("error")
    if vs.get("present") and err and not str(err).startswith("no key material"):
        out.append(_finding("vault_unreadable", "problem", "Your saved data could not be opened",
                            "The encrypted copy of your data was found but could not be unlocked. This happens "
                            "when DASHBOARD_SECRET (or DATA_KEY) was changed after it was saved.",
                            "Put the previous DASHBOARD_SECRET back in your host's settings (or .env). If it is "
                            "lost, see “I lost DASHBOARD_SECRET” in the help.",
                            _help("i-lost-dashboard_secret"), "Help"))
    if on_host:
        last = dict(github_sync.last_result or {})
        if not os.getenv("GITHUB_TOKEN") or not github_sync._repo():
            out.append(_finding("cloud_backup_off", "problem", "Cloud backup is off",
                                "This host wipes its disk on every restart or deploy, so what you add here "
                                "disappears at the next restart.",
                                "Create a private GitHub repository and a token that can write to it, and add "
                                "GITHUB_TOKEN and GITHUB_REPO in your host's settings (SETUP.md, “Anywhere, "
                                "always on”, steps 2 and 3). Meanwhile, download a backup after every change.",
                                version.docs_url("SETUP.md"), "SETUP.md"))
        elif last.get("ok") is False:
            out.append(_finding("cloud_backup_failing", "problem", "Cloud backup is not saving",
                                f"The last save failed: {last.get('error') or 'unknown error'}. Changes made since "
                                "then are lost at the next restart.",
                                "Check GITHUB_TOKEN (not expired, Contents read and write on that repository) and "
                                "GITHUB_REPO in your host's settings, then make a small change to try again. "
                                "Meanwhile, download a backup.", _help("the-hosted-copy-lost-data-after-a-restart"),
                                "Help"))
    elif datafiles.has_own_data():
        at = appstate.get("backup_downloaded_at")
        if not at:
            out.append(_finding("no_backup", "attention", "No backup downloaded yet",
                                "Your data lives only in the data folder on this computer. If the computer breaks "
                                "or the folder is deleted, it is gone.",
                                "Download a backup and keep it somewhere safe and private (a USB stick, your "
                                "cloud drive). Do it again every few months.",
                                "/settings#backup", "Download a backup"))
        elif (ctx.today - _day(at)).days > BACKUP_EVERY_DAYS:
            out.append(_finding("backup_old", "attention", f"Last backup: {rules.day_text(_day(at))}",
                                "Anything you added since then is not in a backup.",
                                "Download a new backup and keep it somewhere safe and private.",
                                "/settings#backup", "Download a backup"))
    return out


def _bound_to_network() -> bool:
    argv = sys.argv
    for i, a in enumerate(argv):
        host = argv[i + 1] if a == "--host" and i + 1 < len(argv) else (a[7:] if a.startswith("--host=") else None)
        if host is not None:
            return host not in ("127.0.0.1", "localhost", "::1")
    return False


def _check_safety(ctx: Context) -> list[dict]:
    out = []
    password = os.getenv("DASHBOARD_PASSWORD") or ""
    secret = os.getenv("DASHBOARD_SECRET") or ""
    on_host = version.where() == "host"
    if not password and os.getenv("ALLOW_NO_PASSWORD") == "1":
        out.append(_finding("no_password", "attention", "No password, and ALLOW_NO_PASSWORD is on",
                            "Anyone who can reach this app sees all your numbers, unless something else (such as "
                            "Cloudflare Access) asks who they are first.",
                            "Set DASHBOARD_USER and DASHBOARD_PASSWORD (in .env, or your host's settings) and "
                            "remove ALLOW_NO_PASSWORD, unless you are sure something else protects it.",
                            _help("i-forgot-the-dashboard-password"), "Help"))
    elif not password and _bound_to_network():
        out.append(_finding("network_no_password", "info", "Open to your network, but without a password",
                            "This app listens on your home network (HOST=0.0.0.0), so other devices can find it. "
                            "Without a password it refuses them and shows “Set a password first”.",
                            "To use it from your phone, set DASHBOARD_USER and DASHBOARD_PASSWORD in .env and "
                            "start it again. Otherwise start it with plain ./run.sh.",
                            _help("i-forgot-the-dashboard-password"), "Help"))
    if password and on_host and len(password) < SHORT_PASSWORD:
        out.append(_finding("short_password", "attention", "Your dashboard password is short",
                            f"This copy is on the internet and its password has {len(password)} characters, which "
                            "is easy to guess.",
                            f"Change DASHBOARD_PASSWORD in your host's settings to at least {SHORT_PASSWORD} "
                            "characters (a few random words work well).",
                            _help("i-forgot-the-dashboard-password"), "Help"))
    key = os.getenv("DATA_KEY") or ""
    if secret and len(secret) < SHORT_SECRET:
        encrypts = not key
        out.append(_finding("short_secret", "attention", "DASHBOARD_SECRET is short",
                            f"It has {len(secret)} characters. It signs your login"
                            + (" and encrypts your saved data" if encrypts else "")
                            + f"; at least {SHORT_SECRET} random characters are safe.",
                            ("First download a backup here. Then set a new, long DASHBOARD_SECRET, restart, log in "
                             "and restore the backup: that saves your data again with the new secret. Changing it "
                             "without these steps makes the saved copy unreadable."
                             if encrypts else
                             "Set a new, long DASHBOARD_SECRET and restart. You will need to log in again."),
                            "/settings#backup" if encrypts else _help("i-lost-dashboard_secret"),
                            "Back up and restore" if encrypts else "Help"))
    if key and len(key) < SHORT_SECRET:
        out.append(_finding("short_data_key", "attention", "DATA_KEY is short",
                            f"It has {len(key)} characters and encrypts your saved data.",
                            "First download a backup here. Then set a new, long DATA_KEY, restart and restore the "
                            "backup, which saves your data again with the new key.",
                            "/settings#backup", "Back up and restore"))
    from . import mcp_server
    token = os.getenv("MCP_TOKEN") or ""
    if token and len(token) < mcp_server.MIN_TOKEN_LENGTH:
        out.append(_finding("short_mcp_token", "attention", "The Claude connector is off: its token is too short",
                            f"MCP_TOKEN has {len(token)} characters; the connector refuses to run with fewer than "
                            f"{mcp_server.MIN_TOKEN_LENGTH}, so nobody can guess it.",
                            f"Set MCP_TOKEN to a random text of at least {mcp_server.MIN_TOKEN_LENGTH} characters, "
                            "restart, and give Claude the new token (MCP.md).",
                            version.docs_url("MCP.md"), "MCP.md"))
    return out


def _check_rules(ctx: Context) -> list[dict]:
    from . import catalog, countries, harvest, prefs
    out = []
    try:
        uk = harvest.residence() == "UK"
    except Exception:
        uk = False
    notice = rules.uk_notice(ctx.today) if uk else None
    if notice:
        out.append(_finding("uk_rules_old", "attention", f"The UK tax figures are for {notice['rules_year']}",
                            f"The {notice['current_year']} tax year has started, but the app's UK tax figures "
                            f"(allowances, rates, take-home pay) are for {notice['rules_year']}. The UK tax cards "
                            "say so too.",
                            "Update the app (see Version below). If the update still says this, nobody has "
                            "updated the figures yet: check gov.uk for the new rates, and report it.",
                            "/settings#version", "Version and updates"))
    try:
        code = prefs.value("country")
        data = countries.get(code) if code else None
    except Exception:
        data = None
    page = rules.country_notice(data, ctx.today) if data else None
    if page:
        out.append(_finding(f"country_page_old:{data['code']}", "attention",
                            f"The tax overview for {countries.in_name(data['code'])} may be out of date",
                            page["text"],
                            "Update the app (see Version below). The Money tab's tax overview links to the "
                            "official pages to check the current rules.", "/settings#version", "Version and updates"))
    try:
        researched = catalog.raw().get("researched_on")
    except Exception:
        researched = None
    steps = rules.broker_steps_notice(researched, ctx.today)
    if steps:
        out.append(_finding("broker_steps_old", "info",
                            f"The broker steps were checked on {rules.day_text(steps['checked_on'])}",
                            "Brokers move their menus now and then, so the steps for exporting a statement or "
                            "making a key may have changed.",
                            "Update the app. If a step does not match what you see, use the broker's own help "
                            "page (linked next to the steps).", "/settings#version", "Version and updates"))
    return out


def _check_version(ctx: Context) -> list[dict]:
    st = version.status()
    if st.get("newer"):
        return [_finding("update_available", "attention", "A newer version is available",
                         f"The project has a newer version (of {rules.day_text(st['latest_date'])}); this copy "
                         f"is of {rules.day_text(st['date'])}. Updates fix problems and keep the rules current.",
                         st["how_to_update"], "/settings#version", "Version and updates")]
    if st.get("check_on") and st.get("error"):
        return [_finding("update_check_failed", "info", "Could not check for a newer version",
                         f"The last check failed: {st['error']}.",
                         "Nothing to do: it is tried again tomorrow.", "/settings#version", "Version and updates")]
    return []


CHECKS: list[Callable[[Context], list[dict]]] = [
    _check_demo, _check_saving, _check_trading212_keys, _check_connections, _check_source_problems,
    _check_files, _check_prices, _check_rates, _check_statements, _check_incomplete_history,
    _check_cash_rates, _check_safety, _check_rules, _check_version,
]


def run(ctx: Context) -> dict:
    """Every finding, most important first: problems, then things to do, then
    good-to-know. `ok` is True when nothing needs doing (info aside)."""
    found: list[dict] = []
    seen: set[str] = set()
    for check in CHECKS:
        try:
            items = check(ctx) or []
        except Exception as e:      # one broken check never hides the others
            log.warning("check-up: %s failed: %r", check.__name__, e)
            continue
        for f in items:
            if f["id"] not in seen:
                seen.add(f["id"])
                found.append(f)
    found.sort(key=lambda f: LEVELS.index(f["level"]))
    counts = {lv: sum(1 for f in found if f["level"] == lv) for lv in LEVELS}
    return {"ok": not (counts["problem"] or counts["attention"]), "counts": counts, "findings": found,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "help": version.docs_url("TROUBLESHOOTING.md"),
            "version": {k: version.info().get(k) for k in ("short", "date", "source")}}
