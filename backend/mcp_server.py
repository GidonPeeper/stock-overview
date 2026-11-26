"""MCP server: lets Claude (or any MCP client) read this dashboard.

Two ways in (MCP.md has the setup steps):

  * stdio, for a copy on your own computer. Claude Desktop or Claude Code
    starts `python -m backend.mcp_server` itself. It runs in that process
    against the same data/ folder; no web server is needed. While it serves,
    stdout carries only protocol messages: the MCP SDK points file
    descriptor 1 at stderr and talks over a private copy, so a print() or a
    library writing to stdout cannot break the connection. The web app's
    background warmer and keep-alive are not started.
  * Streamable HTTP at /mcp on the running web app, for a hosted copy. Off
    (404) unless MCP_TOKEN is set; every request must then carry
    `Authorization: Bearer <MCP_TOKEN>`. The dashboard's login cookie is not
    used here, so main.py lets /mcp past its login check. Stateless, JSON
    responses; the SDK serves both the older initialize-based protocol
    versions and the 2026-07-28 per-request one.

Read-only: the tools (backend/mcp_tools.py) only read data.
"""

import functools
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response

from . import mcp_tools

log = logging.getLogger("stock_overview.mcp")

PATH = "/mcp"
MIN_TOKEN_LENGTH = 32

INSTRUCTIONS = """\
Read-only access to the user's Stock Overview dashboard: investments across their brokers, \
cash, loans, income and the tax figures the app computes. These tools cannot trade, move \
money or change anything.
- If a result says "sample_data": true, the user has not added their own accounts yet. The \
numbers are the app's demo portfolio: say so, and never present them as the user's.
- Amounts are in the currency the result names (the dashboard's display currency unless you \
pass `currency`). Dates are ISO; percentages are plain numbers (12.5 = 12.5%).
- Start with get_portfolio_summary for general questions. Use get_data_status when numbers \
look wrong or stale.
- Tax and money-saving figures are the app's estimates, not advice: keep their caveats."""

_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                             idempotent_hint=True, open_world_hint=False)

# name -> (title, description). Written for the model that picks the tool.
_DESCRIPTIONS = {
    "get_portfolio_summary": (
        "Portfolio summary",
        "Headline numbers for the user's whole portfolio across all brokers: value, cost, "
        "unrealized and realized gains, dividends, interest, fees, total return, today's change, "
        "the split per account and per sector, and the 5 largest holdings. Start here for "
        "'how is my portfolio doing?'."),
    "list_holdings": (
        "Holdings",
        "The user's open positions (shares, ETFs, funds): quantity, price, value, cost, "
        "unrealized gain, today's change and weight in the portfolio. Merged across accounts "
        "(by ISIN) unless `account` is given. Filter by account or by name/ticker/ISIN text; "
        "sort by value, gain, gain_pct or today; `limit` caps the list (default 20)."),
    "get_performance": (
        "Performance over a period",
        "How the portfolio did over a period: gain in money, time-weighted return %, start and "
        "end value, money added, highest and lowest value, and a value series thinned to "
        "`points` points. Periods: today, week, month, year (calendar: since midnight, Monday, "
        "the 1st, 1 January), last_30_days, last_12_months, all, or start_date/end_date. For "
        "calendar periods it also gives the figure on the dashboard's period card."),
    "get_realized_gains": (
        "Realized gains",
        "Profit or loss already locked in by selling: total, per account, per calendar year, and "
        "the fully closed positions (largest gain first, or losses first with ascending=true)."),
    "get_income": (
        "Dividends and interest",
        "Dividends and interest received: all-time, this calendar year, per year and per account, "
        "fees charged outside trades, and the dividends expected over the next 12 months."),
    "get_net_worth": (
        "Net worth and budget",
        "Net worth = investments + cash and savings accounts + things owned - loans, with the "
        "accounts, loans, things owned and the monthly budget (income after tax, payments, what "
        "is left) the user entered, plus a financial-independence estimate."),
    "get_risk_analysis": (
        "Risk and diversification",
        "Volatility, beta vs the S&P 500, Sharpe ratio, max drawdown, concentration (top 3, "
        "effective number of positions), currency and sector exposure, a 0-100 health score with "
        "its parts, and simple market and currency what-if scenarios."),
    "get_tax_overview": (
        "Tax overview",
        "Tax figures the app already computes, not advice: the UK tax-year card (gains, losses, "
        "allowances left, dividends, ISA allowance, 30-day rule), positions that could be sold for "
        "a tax loss, and 'ways to save' tips with amounts, working and deadlines. Depends on the "
        "tax residence set in the app (UK by default)."),
    "get_country_tax_overview": (
        "How tax works in a country",
        "General information, not advice: how tax on investments works in the user's country "
        "(or another, by two-letter code). The tax year and key dates, then rules per topic "
        "(gains, dividends, interest, funds, wealth tax, allowances, tax-free accounts, losses, "
        "foreign shares, reporting, crypto) with figures, official source links, the date they "
        "were checked, announced changes and what is not covered. Says when the app has no "
        "overview for that country yet."),
    "get_data_status": (
        "Data status",
        "Which statements and accounts feed the dashboard, whether it shows sample data, "
        "connection errors, how fresh the prices are, and the settings (currency, tax residence, "
        "time zone, today's date). Use it when numbers look wrong or stale."),
    "get_watchlist": (
        "Watchlist",
        "Shares the user watches but may not own: price, today's change and the change over "
        "about a month."),
}


_stdio = False   # set by main()


@asynccontextmanager
async def _preload(_server):
    """Load the dashboard once the transport is up (on stdio that is after
    stdout is protected), so the first question is quick and a broken
    install shows in the log at start."""
    if _stdio:
        # The SDK has pointed fd 1 at stderr by now. Python-level prints go
        # to stderr directly too, so none sit in sys.stdout's buffer and get
        # flushed onto the wire after the SDK restores fd 1 at exit.
        sys.stdout.flush()
        sys.stdout = sys.stderr
    try:
        await anyio.to_thread.run_sync(mcp_tools._app)
    except Exception:
        log.exception("could not load the dashboard; tool calls will fail")
    yield {}


server = MCPServer(
    "stock-overview", title="Stock Overview", version="1.0.0",
    instructions=INSTRUCTIONS, log_level="WARNING", lifespan=_preload,
    subscriptions=False,   # the tool list never changes: no long-lived listen streams
)


def _as_tool(fn):
    """The tool as the SDK calls it: same signature (so the same input
    schema), result as compact JSON text, bad input as a tool error."""
    @functools.wraps(fn)
    def tool(**kwargs):
        mcp_tools.touch()
        try:
            return mcp_tools.to_text(fn(**kwargs))
        except mcp_tools.ToolInputError as exc:
            raise ToolError(str(exc)) from None
    return tool


for _fn in mcp_tools.TOOLS:
    _title, _desc = _DESCRIPTIONS[_fn.__name__]
    server.add_tool(_as_tool(_fn), name=_fn.__name__, title=_title, description=_desc,
                    annotations=_READ_ONLY, structured_output=False)


@server.prompt(name="portfolio_review", title="Portfolio review",
               description="A plain-English review of the portfolio: performance, risk, income "
                           "and tax points worth a look.")
def portfolio_review() -> str:
    return (
        "Please review my investment portfolio with the stock-overview tools.\n"
        "1. get_data_status. If sample_data is true, tell me the review uses demo numbers.\n"
        "2. get_portfolio_summary.\n"
        "3. get_performance for period 'month' and for period 'year'.\n"
        "4. get_risk_analysis.\n"
        "5. get_income.\n"
        "6. get_tax_overview.\n"
        "Then write a short review in plain English: how the portfolio did, what stands out "
        "(concentration, currencies, big winners and losers), income, and at most three things "
        "worth looking at, with any tax deadlines. Give facts and questions to consider, not buy "
        "or sell advice."
    )


# ------------------------------------------------------- Streamable HTTP
_DEFAULT_ORIGINS = ("https://claude.ai", "https://claude.com")


def _origin_ok(origin: str, host: str | None) -> bool:
    """Browsers send Origin; MCP clients that are not browsers usually don't.
    A present Origin must be this site itself, localhost, Claude, or listed
    in MCP_ALLOWED_ORIGINS (comma-separated). Guards against DNS rebinding."""
    o = origin.strip().rstrip("/").lower()
    allowed = set(_DEFAULT_ORIGINS) | {x.strip().rstrip("/").lower()
                                       for x in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if x.strip()}
    if o in allowed:
        return True
    parts = urlsplit(o)
    if parts.hostname in ("localhost", "127.0.0.1", "::1"):
        return True
    return bool(host) and parts.netloc == host.strip().lower()


def _error(status: int, message: str, headers: dict | None = None) -> Response:
    """A JSON-RPC error without an id, as the transport spec allows."""
    return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": message}},
                        status_code=status, headers=headers)


class _Endpoint:
    """ASGI app at /mcp: token and Origin checks, then the SDK's handler."""

    manager = None   # set while the web app runs (http_lifespan)

    async def __call__(self, scope, receive, send):
        token = os.getenv("MCP_TOKEN", "")
        if not token:
            return await Response("Not Found", status_code=404)(scope, receive, send)
        if len(token) < MIN_TOKEN_LENGTH:
            return await _error(503, f"MCP_TOKEN is too short: use at least {MIN_TOKEN_LENGTH} "
                                     "random characters (see MCP.md)")(scope, receive, send)
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin and not _origin_ok(origin, headers.get("host")):
            return await _error(403, "Origin not allowed")(scope, receive, send)
        scheme, _, given = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(
                given.strip().encode(), token.encode()):
            challenge = 'Bearer realm="stock-overview"' + (', error="invalid_token"' if given else "")
            return await _error(401, "Missing or wrong bearer token",
                                {"WWW-Authenticate": challenge})(scope, receive, send)
        if scope["method"] != "POST":
            # No server-initiated stream and no sessions: a GET would hang
            # open forever. Both protocol eras allow 405 here.
            return await Response("Method Not Allowed", status_code=405,
                                  headers={"Allow": "POST"})(scope, receive, send)
        if self.manager is None:
            return await _error(503, "MCP endpoint is starting")(scope, receive, send)
        await self.manager.handle_request(scope, receive, send)


endpoint = _Endpoint()


def mount(app) -> None:
    """Add the /mcp route to the web app (call before the catch-all static mount)."""
    app.add_route(PATH, endpoint, include_in_schema=False)


@asynccontextmanager
async def http_lifespan():
    """Run the SDK's request handler for as long as the web app runs. A new
    one per start-up (the SDK runs each only once)."""
    server.streamable_http_app(
        stateless_http=True, json_response=True,
        # Host/Origin are checked in _Endpoint: a hosted copy's host name is
        # not known in advance, so the SDK's localhost-only check stays off.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    manager = server.session_manager
    async with manager.run():
        endpoint.manager = manager
        try:
            yield
        finally:
            endpoint.manager = None


# ------------------------------------------------------------------ stdio
def main() -> None:
    global _stdio
    _stdio = True
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    server.run("stdio")


if __name__ == "__main__":
    main()
