"""MCP server (backend/mcp_server.py, backend/mcp_tools.py): the protocol
over HTTP and stdio, every tool on the bundled sample data, errors, and the
/mcp endpoint's token check.

No network: Yahoo is replaced by a fake with fixed quotes (as in
test_cache.py), FX rates are fixed, and only the bundled sample files are
read, even on a computer that has its own data in data/.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    import mcp  # noqa: F401  (the official MCP SDK, requirements.txt)
except ImportError:  # pragma: no cover
    mcp = None

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "t" * 40
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
           "Authorization": f"Bearer {TOKEN}", "MCP-Protocol-Version": "2025-11-25"}
TOOL_NAMES = {"get_portfolio_summary", "list_holdings", "get_performance", "get_realized_gains",
              "get_income", "get_net_worth", "get_risk_analysis", "get_tax_overview",
              "get_data_status", "get_watchlist", "get_quote"}
# Arguments for tools that need some (the rest run without any).
TOOL_ARGS = {"get_quote": {"query": "NVDA"}}
RATES = {"EUR": 1.0, "USD": 0.9, "GBP": 1.2, "CHF": 1.05}   # EUR per unit


def sample_world(case) -> None:
    """Start patches on a TestCase class: sample data only, fake Yahoo with
    fixed quotes (ASML down 40%, so tax-loss harvesting has a candidate),
    fixed FX, no files written, no background market threads."""
    from backend import advisors, cache, datafiles, fx, harvest, market, prices, sectors, trend
    from tests.test_cache import fake_yf

    tmp = tempfile.TemporaryDirectory()
    case.addClassCleanup(tmp.cleanup)
    empty = Path(tmp.name) / "nothing-of-mine"
    empty.mkdir()
    env = {k: "" for k in os.environ if k.startswith("T212_")}
    env.update({"WARMUP": "0", "TICKER_LOOKUP": "0", "TAX_RESIDENCE": "UK", "DISPLAY_CURRENCY": "EUR",
                "RENDER": "", "RENDER_EXTERNAL_URL": "", "MCP_TOKEN": TOKEN, "MCP_ALLOWED_ORIGINS": ""})
    for p in (mock.patch.object(datafiles, "_SEARCH_DIRS", [empty]),
              mock.patch.dict(os.environ, env),
              mock.patch.object(fx, "_live_rates", return_value=dict(RATES)),
              mock.patch.object(sectors, "get_sectors", side_effect=lambda ts: {t: "Technology" for t in ts if t}),
              mock.patch.object(harvest, "_sector_map", return_value={}),
              mock.patch.object(harvest, "_start_corr"),
              mock.patch.object(trend, "series", return_value=[])):
        p.start()
        case.addClassCleanup(p.stop)
    datafiles.refresh()
    case.addClassCleanup(datafiles.refresh)

    def clear():
        cache.clear()
        for d in (prices._CACHE, prices._FAILED, prices._SCALE, market._spark_cache):
            d.clear()
    clear()
    case.addClassCleanup(clear)

    from backend import sources
    quotes = {"NVDA": (150.0, 148.0, "USD"), "BRK-B": (500.0, 495.0, "USD")}
    for h in sources.holdings():
        if h.ticker and h.ticker not in quotes:
            up = 0.6 if h.ticker == "ASML.AS" else 1.2
            quotes[h.ticker] = (round(h.buy_price * up, 2), round(h.buy_price * up * 0.98, 2), h.currency)
    yf = fake_yf(quotes)

    def no_download(*a, **k):
        raise RuntimeError("no network in tests")

    yf.download = no_download
    p = mock.patch.dict(sys.modules, {"yfinance": yf})
    p.start()
    case.addClassCleanup(p.stop)
    cache.clear()
    assert advisors  # imported so its module-level state exists before the first call


@unittest.skipIf(mcp is None, "the MCP SDK is not installed (pip install -r requirements.txt)")
class ToolsTest(unittest.TestCase):
    """The tool functions themselves, on the sample data."""

    @classmethod
    def setUpClass(cls):
        sample_world(cls)
        from backend import mcp_tools
        cls.t = mcp_tools

    def test_every_tool_runs_and_says_it_is_sample_data(self):
        for fn in self.t.TOOLS:
            with self.subTest(tool=fn.__name__):
                out = fn(**TOOL_ARGS.get(fn.__name__, {}))
                self.assertIs(out["sample_data"], True)
                self.assertIn("SAMPLE DATA", out["warning"])
                json.loads(self.t.to_text(out))   # serialisable
                self.assertNotIn("_mkt", self.t.to_text(out))

    def test_summary_numbers_and_currency(self):
        eur = self.t.get_portfolio_summary(currency="EUR")
        gbp = self.t.get_portfolio_summary(currency="GBP")
        self.assertEqual((eur["currency"], gbp["currency"]), ("EUR", "GBP"))
        self.assertAlmostEqual(gbp["value"], eur["value"] / RATES["GBP"], delta=0.02)
        self.assertAlmostEqual(eur["unrealized_gain"], eur["value"] - eur["cost"], delta=0.02)
        self.assertAlmostEqual(sum(a["value"] for a in eur["accounts"]), eur["value"], delta=0.05)
        self.assertEqual(len(eur["top_holdings"]), 5)
        self.assertEqual(eur["holdings_count"], len(self.t.list_holdings(limit=200)["holdings"]))

    def test_holdings_sorted_filtered_and_limited(self):
        out = self.t.list_holdings(limit=3)
        values = [h["value"] for h in out["holdings"]]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(out["shown"], 3)
        self.assertGreater(out["total_matches"], 3)
        worst = self.t.list_holdings(sort_by="gain", ascending=True, limit=1)["holdings"][0]
        self.assertEqual(worst["ticker"], "ASML.AS")          # the one down 40%
        apple = self.t.list_holdings(search="apple")
        self.assertTrue(apple["holdings"])
        self.assertTrue(all("apple" in h["name"].lower() for h in apple["holdings"]))
        degiro = self.t.list_holdings(account="degiro")
        self.assertEqual({h["account"] for h in degiro["holdings"]}, {"DeGiro"})
        none = self.t.list_holdings(account="no such bank")
        self.assertEqual(none["holdings"], [])
        self.assertIn("DeGiro", none["available_accounts"])

    def test_performance_periods_and_range(self):
        month = self.t.get_performance(period="month", points=5)
        self.assertLessEqual(len(month["series"]), 5)
        self.assertEqual(month["series"][0]["gain_since_start"], 0.0)
        self.assertEqual(month["start_date"][-2:], "01")
        self.assertIn("dashboard_card", month)
        self.assertAlmostEqual(month["value_change"], month["end_value"] - month["start_value"], delta=0.02)
        custom = self.t.get_performance(start_date="2000-01-01", end_date="2000-02-01")
        self.assertIn("message", custom)                     # no history that far back
        everything = self.t.get_performance(period="all")
        self.assertNotIn("dashboard_card", everything)

    def test_performance_as_the_first_question(self):
        """Sample data: the demo trendline exists even before any other tool ran."""
        out = self.t.get_performance(period="year")
        self.assertNotIn("message", out)
        self.assertGreater(len(out["series"]), 2)

    def test_bad_arguments_raise_input_errors(self):
        with self.assertRaises(self.t.ToolInputError):
            self.t.get_performance(start_date="last tuesday")
        with self.assertRaises(self.t.ToolInputError):
            self.t.get_performance(start_date="2026-02-01", end_date="2026-01-01")
        with self.assertRaises(self.t.ToolInputError):
            self.t.get_portfolio_summary(currency="JPY")

    def test_income_by_year_matches_the_app_total(self):
        from backend import income
        out = self.t.get_income()
        by_year = {y["year"]: y["dividends"] + y["interest"] for y in out["by_year"]}
        for year, amount in income.by_year().items():
            self.assertAlmostEqual(by_year[year], amount, delta=0.02)
        self.assertIn("next_12_months_dividends", out)

    def test_net_worth_adds_up(self):
        n = self.t.get_net_worth()
        self.assertAlmostEqual(n["net_worth"], n["investments"] + n["cash_and_savings"]
                               + n["things_owned"] - n["debt"], delta=0.05)
        self.assertTrue(n["cash_accounts"] and n["loans"])

    def test_tax_overview_has_card_candidate_tips_and_disclaimers(self):
        tax = self.t.get_tax_overview()
        self.assertEqual(tax["uk_tax_year"]["tax_year"][:2], "20")
        harvest = tax["tax_loss_harvesting"]
        self.assertEqual([c["ticker"] for c in harvest["candidates"]], ["ASML.AS"])
        self.assertIn("not tax advice", tax["disclaimer"])
        for tip in tax["ways_to_save"]["tips"]:
            self.assertNotRegex(json.dumps(tip), r"\{\{")       # amounts written out
        with mock.patch.dict(os.environ, {"TAX_RESIDENCE": "NONE"}):
            off = self.t.get_tax_overview()
        self.assertIs(off["uk_tax_year"]["applies"], False)

    def test_data_status_and_watchlist(self):
        st = self.t.get_data_status()
        self.assertEqual(st["settings"]["tax_residence"], "UK")
        self.assertFalse(any(s["added"] for s in st["statements"]))
        self.assertIn("how_to_add_data", st)
        self.assertNotIn("path", self.t.to_text(st))          # no file paths
        wl = self.t.get_watchlist()["watchlist"]
        self.assertEqual(next(w for w in wl if w["ticker"] == "NVDA")["price"], 150.0)

    def test_get_quote(self):
        out = self.t.get_quote("NVDA")
        self.assertEqual((out["ticker"], out["price"], out["price_currency"]), ("NVDA", 150.0, "USD"))
        self.assertEqual((out["today_change"], out["currency"]), (2.0, "EUR"))
        self.assertAlmostEqual(out["price_in_currency"], 150.0 * RATES["USD"], places=2)
        self.assertIsNone(out["you_hold"])                       # on the watchlist, not held
        held = self.t.get_quote("ASML.AS", currency="GBP")
        self.assertEqual((held["you_hold"]["quantity"], held["you_hold"]["accounts"]), (2.0, ["DeGiro"]))
        self.assertNotIn("price_in_currency", self.t.get_quote("ASML.AS", currency="EUR"))   # already euros
        for bad in ("", "  ", "x " * 40):
            with self.subTest(bad=bad), self.assertRaises(self.t.ToolInputError):
                self.t.get_quote(bad)
        with self.assertRaises(self.t.ToolInputError) as ctx:
            self.t.get_quote("some company")                     # a name is searched for: this Yahoo has no search
        self.assertIn("Yahoo Finance", str(ctx.exception))

    def test_real_data_is_not_marked_as_sample(self):
        from backend import datafiles
        with mock.patch.object(datafiles, "demo", return_value=False):
            out = self.t.envelope({"x": 1}, "EUR")
        self.assertEqual(out, {"sample_data": False, "currency": "EUR", "x": 1})


@unittest.skipIf(mcp is None, "the MCP SDK is not installed (pip install -r requirements.txt)")
class HttpProtocolTest(unittest.TestCase):
    """JSON-RPC over the web app's /mcp endpoint (Streamable HTTP)."""

    @classmethod
    def setUpClass(cls):
        sample_world(cls)
        from starlette.testclient import TestClient

        from backend import main
        cls.main = main
        cls.client = TestClient(main.app, base_url="http://localhost:8000")
        cls.client.__enter__()          # runs the app's lifespan (starts the MCP handler)
        cls.addClassCleanup(cls.client.__exit__, None, None, None)

    def post(self, body, headers=None):
        return self.client.post("/mcp", content=json.dumps(body), headers={**HEADERS, **(headers or {})})

    def rpc(self, method, params=None, id=1):
        body = {"jsonrpc": "2.0", "id": id, "method": method}
        if params is not None:
            body["params"] = params
        r = self.post(body)
        self.assertEqual(r.status_code, 200, r.text)
        msg = r.json()
        self.assertEqual((msg["jsonrpc"], msg["id"]), ("2.0", id))
        return msg

    def call(self, name, arguments=None):
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}})["result"]

    def test_handshake(self):
        res = self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "0"}})["result"]
        self.assertEqual(res["protocolVersion"], "2025-11-25")
        self.assertEqual(res["serverInfo"]["name"], "stock-overview")
        self.assertIn("tools", res["capabilities"])
        self.assertIn("sample_data", res["instructions"])
        r = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self.rpc("ping", id=7)["result"], {})

    def test_tools_list_schemas(self):
        tools = self.rpc("tools/list")["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, TOOL_NAMES)
        for t in tools:
            with self.subTest(tool=t["name"]):
                self.assertRegex(t["name"], r"^[A-Za-z0-9_-]{1,64}$")
                self.assertGreater(len(t["description"]), 40)
                self.assertTrue(t.get("title"))
                schema = t["inputSchema"]
                self.assertEqual(schema["type"], "object")
                self.assertIsInstance(schema.get("properties", {}), dict)
                self.assertTrue(set(schema.get("required", [])) <= set(schema.get("properties", {})))
                for name, prop in schema.get("properties", {}).items():
                    self.assertTrue("type" in prop or "anyOf" in prop or "enum" in prop, name)
                    self.assertTrue(prop.get("description"), f"{t['name']}.{name} has no description")
                self.assertIs(t["annotations"]["readOnlyHint"], True)
                self.assertIs(t["annotations"]["destructiveHint"], False)

    def test_every_tool_over_the_protocol(self):
        for name in sorted(TOOL_NAMES):
            with self.subTest(tool=name):
                res = self.call(name, TOOL_ARGS.get(name))
                self.assertFalse(res.get("isError"), res)
                data = json.loads(res["content"][0]["text"])
                self.assertIs(data["sample_data"], True)

    def test_arguments_reach_the_tool(self):
        res = self.call("list_holdings", {"limit": 2, "currency": "USD"})
        data = json.loads(res["content"][0]["text"])
        self.assertEqual((data["currency"], len(data["holdings"])), ("USD", 2))

    def test_unknown_tool_and_bad_arguments_are_errors(self):
        res = self.call("place_order", {"ticker": "AAPL"})
        self.assertTrue(res["isError"])
        self.assertIn("Unknown tool", res["content"][0]["text"])
        for name, args in (("list_holdings", {"limit": 0}), ("list_holdings", {"sort_by": "color"}),
                           ("get_portfolio_summary", {"currency": "JPY"}),
                           ("get_performance", {"start_date": "yesterday"})):
            with self.subTest(tool=name, args=args):
                res = self.call(name, args)
                self.assertTrue(res["isError"], res)
                self.assertTrue(res["content"][0]["text"])
        msg = self.post({"jsonrpc": "2.0", "id": 9, "method": "no/such/method"}).json()
        self.assertEqual(msg["error"]["code"], -32601)

    def test_prompt(self):
        prompts = self.rpc("prompts/list")["result"]["prompts"]
        self.assertEqual([p["name"] for p in prompts], ["portfolio_review"])
        got = self.rpc("prompts/get", {"name": "portfolio_review"})["result"]
        self.assertIn("get_portfolio_summary", got["messages"][0]["content"]["text"])

    def test_2026_07_28_request_without_handshake(self):
        meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
                "io.modelcontextprotocol/clientCapabilities": {}}
        r = self.post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "get_data_status", "arguments": {}, "_meta": meta}},
                      {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call",
                       "Mcp-Name": "get_data_status"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["result"].get("isError"))
        self.assertNotIn("mcp-session-id", r.headers)

    # ------------------------------------------------------- access control
    def test_off_without_mcp_token(self):
        with mock.patch.dict(os.environ, {"MCP_TOKEN": ""}):
            r = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r.status_code, 404)

    def test_missing_or_wrong_token_is_refused(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        r = self.client.post("/mcp", content=json.dumps(body),
                             headers={k: v for k, v in HEADERS.items() if k != "Authorization"})
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.headers["www-authenticate"].startswith("Bearer"))
        for auth in (f"Bearer {TOKEN[:-1]}x", f"Bearer {TOKEN}x", f"Basic {TOKEN}", TOKEN, "Bearer "):
            with self.subTest(auth=auth):
                r = self.post(body, {"Authorization": auth})
                self.assertEqual(r.status_code, 401)
                self.assertNotIn("result", r.text)

    def test_token_is_compared_in_constant_time(self):
        from backend import mcp_server
        with mock.patch.object(mcp_server.secrets, "compare_digest",
                               wraps=mcp_server.secrets.compare_digest) as cmp:
            self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        cmp.assert_called_once_with(TOKEN.encode(), TOKEN.encode())

    def test_short_token_keeps_the_endpoint_closed(self):
        with mock.patch.dict(os.environ, {"MCP_TOKEN": "short"}):
            r = self.post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                          {"Authorization": "Bearer short"})
        self.assertEqual(r.status_code, 503)

    def test_origin_check(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(self.post(body, {"Origin": "https://evil.example"}).status_code, 403)
        for ok in ("https://claude.ai", "http://localhost:6274", "http://localhost:8000"):
            with self.subTest(origin=ok):
                self.assertEqual(self.post(body, {"Origin": ok}).status_code, 200)
        with mock.patch.dict(os.environ, {"MCP_ALLOWED_ORIGINS": "https://my.example, https://x.example"}):
            self.assertEqual(self.post(body, {"Origin": "https://my.example"}).status_code, 200)

    def test_get_and_delete_are_not_allowed(self):
        self.assertEqual(self.client.get("/mcp", headers=HEADERS).status_code, 405)
        self.assertEqual(self.client.delete("/mcp", headers=HEADERS).status_code, 405)

    def test_dashboard_login_does_not_apply_to_mcp_but_still_guards_the_api(self):
        with mock.patch.object(self.main, "_DASH_PASS", "a password"):
            self.assertEqual(self.client.get("/api/portfolio").status_code, 401)
            self.assertEqual(self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code, 200)
            r = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Authorization": "Bearer nope"})
            self.assertEqual(r.status_code, 401)


# A stand-in for yfinance that writes to stdout (with print and straight to
# file descriptor 1) when imported and when used, then fails.
NOISY_YFINANCE = '''
import os, sys
print("NOISE-PRINT on import")
os.write(1, b"NOISE-FD1 on import\\n")

def __getattr__(name):
    print("NOISE-PRINT on use:", name)
    os.write(1, b"NOISE-FD1 on use\\n")
    raise RuntimeError("no network in tests")
'''


@unittest.skipIf(mcp is None, "the MCP SDK is not installed (pip install -r requirements.txt)")
class StdioTest(unittest.TestCase):
    """`python -P -m backend.mcp_server` as MCP.md tells Claude to start it:
    from another folder, with PYTHONPATH. Its stdout must carry nothing but
    protocol messages, even when a library prints."""

    def test_stdout_carries_only_protocol_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "fake"
            fake.mkdir()
            (fake / "yfinance.py").write_text(NOISY_YFINANCE)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("T212_", "GITHUB_", "RENDER", "DASHBOARD_", "MCP_", "ENABLEBANKING"))}
            env.update({"PYTHONPATH": os.pathsep.join([str(fake), str(ROOT)]), "WARMUP": "0",
                        "TICKER_LOOKUP": "0"})
            proc = subprocess.Popen([sys.executable, "-P", "-m", "backend.mcp_server"], cwd=tmp, env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            watchdog = threading.Timer(90, proc.kill)
            watchdog.start()
            try:
                requests = [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "0"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "list_holdings", "arguments": {"limit": 1}}},
                ]
                for msg in requests:
                    proc.stdin.write((json.dumps(msg) + "\n").encode())
                proc.stdin.flush()
                lines = [proc.stdout.readline() for _ in range(3)]
                proc.stdin.close()
                rest, err = proc.stdout.read(), proc.stderr.read()
                proc.wait(timeout=30)
            finally:
                watchdog.cancel()
                proc.kill()
                proc.stdout.close()
                proc.stderr.close()

        out = b"".join(lines) + rest
        messages = [json.loads(line) for line in out.decode().splitlines()]   # every line is JSON
        self.assertEqual(len(messages), 3, out)
        for m in messages:
            self.assertEqual(m["jsonrpc"], "2.0")
            self.assertTrue("result" in m or "error" in m)
        by_id = {m["id"]: m for m in messages}
        self.assertEqual(set(by_id), {1, 2, 3})
        self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "stock-overview")
        self.assertEqual({t["name"] for t in by_id[2]["result"]["tools"]}, TOOL_NAMES)
        self.assertFalse(by_id[3]["result"].get("isError"), by_id[3])
        self.assertNotIn(b"NOISE", out)
        # the noise did happen, and went to stderr
        self.assertIn(b"NOISE-FD1", err)
        self.assertIn(b"NOISE-PRINT", err)
        self.assertEqual(proc.returncode, 0, err[-2000:])


class OriginRuleTest(unittest.TestCase):
    @unittest.skipIf(mcp is None, "the MCP SDK is not installed")
    def test_rules(self):
        from backend.mcp_server import _origin_ok
        with mock.patch.dict(os.environ, {"MCP_ALLOWED_ORIGINS": ""}):
            self.assertTrue(_origin_ok("https://dash.example.com", "dash.example.com"))   # same site
            self.assertTrue(_origin_ok("https://claude.ai", "dash.example.com"))
            self.assertTrue(_origin_ok("http://127.0.0.1:3000", "dash.example.com"))
            self.assertFalse(_origin_ok("https://dash.example.com.evil.io", "dash.example.com"))
            self.assertFalse(_origin_ok("null", "dash.example.com"))


if __name__ == "__main__":
    unittest.main()
