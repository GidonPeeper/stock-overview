# Maintaining Wimm

The jobs someone with code knowledge has to do so the app stays right, each
with the files to change and how to check the result. Users do not need this
page: for them there is the check-up in Settings and
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).

Run the tests after every change (no network or keys needed):

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

## How the app knows its rules are old

Some of what the app shows was true on a date. `backend/rules.py` holds those
dates and tells the user when one has passed, on the card that uses it and on
the check-up (`backend/checkup.py`). Nothing stops working; the app says what
is old until someone updates it.

| Rules | Date kept in | Notice when |
|---|---|---|
| UK tax figures | `rules.UK_TAX_YEAR`, `rules.UK_RULES_CHECKED` | a later UK tax year has started (6 April) |
| Country pages | `backend/countries/<CC>.json`: `tax_year`, `checked_on` | the page's tax year ended, or it was checked over ~13 months ago |
| Broker steps | `backend/broker_catalog.json`: `researched_on` | checked over 18 months ago |
| The app itself | git | a newer commit is on `main` (only if the user switched on the check) |

## Every year: the UK tax year (6 April)

Also after each UK Budget (usually in the autumn), which can change next year's
figures.

1. **Read the official figures** for the new tax year:
   - the Budget's *Overview of tax legislation and rates*, Annex A
     (gov.uk/government/publications, search "OOTLAR"; last year's link is in
     `backend/advisors.py`);
   - https://www.gov.uk/income-tax-rates and `/income-tax-rates/income-over-100000`;
   - https://www.gov.uk/capital-gains-tax/allowances and `/capital-gains-tax/rates`;
   - https://www.gov.uk/tax-on-dividends;
   - https://www.gov.uk/individual-savings-accounts;
   - https://www.gov.uk/apply-tax-free-interest-on-savings;
   - https://www.gov.uk/tax-on-your-private-pension/annual-allowance;
   - https://www.gov.uk/guidance/rates-and-thresholds-for-employers-2027-to-2028
     (change the years) for National Insurance and the monthly payroll thresholds.
2. **Change the figures that changed**, each next to the link it came from:
   - `backend/advisors.py`, the block "UK RATES AND ALLOWANCES" (ISA, CGT,
     dividends, income tax bands, savings, pension);
   - `backend/uktax.py`: `CGT_ANNUAL_EXEMPT_GBP`, `DIVIDEND_ALLOWANCE_GBP`,
     `ISA_ALLOWANCE_GBP`;
   - `backend/paye.py`: allowance, bands, rates, NI thresholds, `NI_MONTHLY`;
   - `backend/harvest.py`: `DEFAULT_RATE_PCT` and the rules text at the top;
   - `backend/countries/GB.json`: `tax_year` (label `2027/28`, return date),
     the figures in `topics`, `checked_on`.
3. **Change the year in one place:** `backend/rules.py`, `UK_TAX_YEAR = "2027/28"`
   and `UK_RULES_CHECKED` = the day you checked. The cards then stop showing
   "These figures use the 2026/27 rules".
4. **Check:**
   - `tests/test_advisors.py` fails if `advisors.py` and `uktax.py` disagree;
   - `tests/test_paye.py` holds hand-worked payslips: work the new examples out
     by hand (or with HMRC's calculator) and update the expected numbers;
   - `tests/test_uktax.py`, `tests/test_harvest.py`, `tests/test_countries.py`;
   - `tests/test_checkup.py` follows `UK_TAX_YEAR` by itself;
   - `python -c "from backend import rules; from datetime import date; print(rules.uk_notice(date(2027, 4, 6)))"`
     prints `None` once the year is updated.

## About once a year: a country page

Each file in `backend/countries/` describes one tax year. The format is in
`backend/countries/SCHEMA.md`.

1. Open the official pages listed in the file (`official`, and each topic's
   `sources`). Check every figure and sentence.
2. Change what changed; set `tax_year` to the new year and `checked_on` to today.
   Move items from `upcoming` into the topics once they are law.
3. Check: `.venv/bin/python -m unittest tests.test_countries` (every file must
   follow the schema), then open the Money tab with that country chosen.

## Every year or so: the broker steps

`backend/broker_catalog.json` has the steps to make a key or export a statement
at about 50 brokers and banks. Brokers move their menus.

1. For each institution, follow its `docs_url` (the broker's own help page)
   and compare the `steps`, `report_name` and `notes`.
2. Steps you could not confirm on the broker's own pages get `"verified": false`
   and the reason in `notes` (the app shows a warning).
3. Set the top-level `researched_on` to today (the app shows the date next to
   the steps, and the check-up warns 18 months later).
4. Check: `.venv/bin/python -m unittest tests.test_connections`, then go
   through Settings → Add an account for the brokers you changed.

## When a share has no price: the ticker table

`backend/instruments.py`, `TICKERS`, maps an ISIN to a Yahoo Finance ticker.
An ISIN not in the table is looked up on Yahoo by itself (kept in
`data/tickers_found.json`), so the table is only needed when that lookup picks
the wrong listing or finds nothing.

1. Find the ticker: search the ISIN or name on https://finance.yahoo.com, or
   with the app's own search. Prefer the listing in the currency the user
   trades in (`.AS`, `.DE`, `.PA`, `.MI` euros; `.L` pounds; none for US).
2. Add `"ISIN": "TICKER"` in the right group. `""` means "no usable price"
   (delisted).
3. Check: `.venv/bin/python -m unittest tests.test_core_numbers tests.test_connections`.

## Every few months: dependencies

`requirements.txt` lists minimum versions. `./run.sh` reinstalls when that
file changes, so users get new versions with the next update.

1. In a fresh folder: `python3 -m venv .venv && .venv/bin/pip install -U -r requirements.txt`.
2. Run the tests, start the app, look at every tab and at Settings, upload a
   sample statement (`data/*.sample.*` or a made-up PDF).
3. Most likely to break: **yfinance** (prices), **pdfplumber** (statement
   PDFs: `tests/test_importers_csv_pdf.py`), **fastapi/starlette** (the test
   client), **mcp** (`tests/test_mcp.py`).
4. Raise a minimum in `requirements.txt` only when the app needs it.

## When Yahoo changes something

Signs: users see "No live prices right now"; the check-up says "Prices are
not coming in" for everyone, not one share. The tests use a fake Yahoo, so
they do not notice.

1. First try a newer yfinance: `.venv/bin/pip install -U yfinance`, restart.
2. Check by hand:
   `.venv/bin/python -c "import yfinance as yf; t=yf.Ticker('AAPL'); t.history(period='1d'); print(t.history_metadata.get('regularMarketPrice'))"`
3. Where Yahoo is used: `backend/prices.py` (live prices, `_fetch_one`),
   `closes.py` and `trend.py` (history), `fx.py` (exchange rates), `market.py`
   (indices, sparklines, news), `sectors.py`, `lookup.py` (search),
   `instruments.py` (ISIN lookup). Fix the one that broke; keep the fake in
   `tests/test_cache.py` in line with the real answer.
4. If yfinance itself is fixed upstream, raise its minimum in `requirements.txt`.

## How a change reaches users

There are no releases: every commit on `main` is the new version.

| User runs it | They update with | They hear about it |
|---|---|---|
| on their own computer | `./run.sh update` (`git pull --ff-only`, then reinstall if `requirements.txt` changed) | the check-up, if they switched on the daily check |
| on Render, from a fork | **Sync fork** on GitHub; Render then deploys | the check-up, likewise |
| with Docker | `git pull`, `docker compose up -d --build` | the check-up, likewise |

So:

- **Keep `main` working.** Run the tests before you push. A broken `main`
  reaches everyone who updates.
- **Write commit subjects in plain words.** `./run.sh update` shows them to
  users as "What changed".
- **Never break old data.** Users keep their `data` folder for years: new code
  must read files written by old code. If the backup format must change, raise
  `backup.FORMAT` and keep reading the old one.
- **New data files** go in `vault.FILES` (`backend/vault.py`), so they are in
  the cloud copy, the backup and the cache checks automatically.
- **Say it in the app.** A change users must act on (a new setting, a renewed
  key) belongs in the check-up as a finding, not only in the notes.

## Adding a check to the check-up

`backend/checkup.py`: write `_check_something(ctx)` that returns findings made
with `_finding(id, level, title, why, action, target, target_label)`, and add it
to `CHECKS`. Rules: only what the app knows (no guesses, no slow network calls:
use the caches), plain words, and an action the user can do without a
terminal where possible. Test it with and without the condition in
`tests/test_checkup.py`. If the action needs help text, add a section to
`TROUBLESHOOTING.md` and link its anchor.
