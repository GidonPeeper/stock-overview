# Ask Claude about your portfolio

Stock Overview is an **MCP server** (Model Context Protocol). Connect it to
Claude and ask questions about your money in plain language. Claude looks up
the answer in your own dashboard data.

Works with Claude Code, Claude Desktop and claude.ai.

## Before you connect: privacy

- **Claude can read your financial data**: holdings, balances, loans, income,
  budget and tax figures. It reads only when a question needs it.
- What Claude reads becomes part of your conversation, like text you type.
- **Read-only.** The tools cannot trade, move money, change settings, upload
  files or connect accounts.
- Until you add your own accounts, every answer uses the sample portfolio and
  says so.

## Questions you can ask

- How did my portfolio do this month? And this year?
- What are my 10 biggest positions? Which ones are losing money?
- How much did I get in dividends this year? What do I expect next year?
- What is my net worth? How much is left of my budget each month?
- How risky is my portfolio? Am I too concentrated in one stock or currency?
- What should I look at for tax before 5 April?
- What is the price of Nvidia? How did the S&P 500 do today?
- Give me a portfolio review. (Uses the built-in `portfolio_review` prompt.)

## Set it up

Start the app once first (`./run.sh`, see [SETUP.md](SETUP.md)), so everything
is installed.

**Shortcut (Mac or Linux):** run `./run.sh claude` in the folder. It prints
the commands below with your own path already filled in.

Otherwise, replace `/path/to/stock-overview` below with the full path of your
copy (run `pwd` inside the folder to see it).

### Claude Code

```bash
claude mcp add stock-overview --scope user \
  -e PYTHONPATH=/path/to/stock-overview \
  -- /path/to/stock-overview/.venv/bin/python -P -m backend.mcp_server
```

Check it with `claude mcp get stock-overview` (it should say **Connected**).
Remove it with `claude mcp remove stock-overview --scope user`.

### Claude Desktop

1. Open **Settings > Developer > Edit Config**. This opens
   `claude_desktop_config.json`
   (Mac: `~/Library/Application Support/Claude/`,
   Windows: `%APPDATA%\Claude\`).
2. Add this, with your own path:

   ```json
   {
     "mcpServers": {
       "stock-overview": {
         "command": "/path/to/stock-overview/.venv/bin/python",
         "args": ["-P", "-m", "backend.mcp_server"],
         "env": { "PYTHONPATH": "/path/to/stock-overview" }
       }
     }
   }
   ```

   On Windows the command is `C:\\path\\to\\stock-overview\\.venv\\Scripts\\python.exe`
   (JSON needs the double backslashes, in `PYTHONPATH` too).
3. Quit Claude Desktop completely and open it again.

If it does not connect, read the log: `~/Library/Logs/Claude/mcp-server-stock-overview.log`
(Windows: `%APPDATA%\Claude\logs`).

Both of these start the server on your own computer. They read your `data/`
folder directly; the web dashboard does not need to be running.

### A hosted copy (by URL)

Use this when your dashboard runs online (for example on Render). The
address is `https://<your dashboard>/mcp`. It stays off until you set a token.

1. Make a token: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
2. On your host, add the environment variable `MCP_TOKEN` with that token
   (on Render: the service's **Environment** page). Wait for the restart.
3. Connect:
   - **Claude Code**:

     ```bash
     claude mcp add --transport http stock-overview https://<your dashboard>/mcp \
       --header "Authorization: Bearer <your token>"
     ```

   - **claude.ai**: **Customize > Connectors > Add > Add custom connector**.
     Enter a name and the URL. Choose **No sign-in**. Under **Request headers**
     pick `authorization` and enter `Bearer <your token>` (with the space).
     Request headers are in beta and not every account has them yet. Without
     them, claude.ai cannot send the token: use Claude Code or Claude Desktop instead.

Keep the token secret: anyone who has it can read your data. To shut someone
out, change `MCP_TOKEN` and restart. The token must be at least 32 characters.
Your dashboard password is separate and is not used here.

## The tools

| Tool | What it answers |
|---|---|
| `get_portfolio_summary` | Value, cost, gains, dividends, interest, fees, total return, today's change, per account and sector |
| `list_holdings` | Your positions, largest first; filter by account or name, sort by gain or today's change |
| `get_performance` | Gain and return for today, this week/month/year, the last 30 days or 12 months, all time, or any dates |
| `get_realized_gains` | Profit or loss from what you sold, per account and per year |
| `get_income` | Dividends and interest per year and account, and the next 12 months' expected dividends |
| `get_net_worth` | Investments, cash, things you own and loans; your monthly budget |
| `get_risk_analysis` | Volatility, beta, Sharpe, drawdown, concentration, currencies, sectors, health score |
| `get_tax_overview` | UK tax-year figures, tax-loss candidates and money-saving tips (estimates, not advice) |
| `get_country_tax_overview` | How tax on investments works in your country: rules, figures, official sources, the date they were checked (general information, not advice) |
| `get_data_status` | Which accounts are connected, sample data or not, how fresh the prices are |
| `get_watchlist` | The shares you watch |
| `get_quote` | The price of any share, fund, index, currency or crypto, held or not: today's change, market open or closed, key facts, and what you hold of it |

Amounts are in the currency chosen in Settings unless you ask Claude for
another one (EUR, GBP or USD). Tax figures follow the country chosen in
Settings.

## Test it yourself

```bash
cd /path/to/stock-overview && .venv/bin/python -m backend.mcp_server
```

It waits quietly for Claude to talk to it. That means it works. Press Ctrl+C to stop.
