# Deploy to Render (free, always-on, phone-friendly)

The dashboard runs 24/7 on Render's free tier at a fixed `…onrender.com` URL,
behind your login. The code repo can be public: keys go in as environment
variables, and your data is saved encrypted in a **private** repo of your own.
[SETUP.md](SETUP.md) walks through it step by step; this page has the details.

Free-tier notes: the service sleeps after 15 min without visits, and waking it
takes about a minute. The app copes with that (it opens from the last numbers
saved on your phone while the server wakes up), and it keeps itself awake for
free — see [Speed](#speed-sleep-keep-alive-and-caching) below.

## 1. Code and data repos
- **Code:** fork this repo (or push your clone). Public is fine: it holds no data.
- **Data:** a separate **private** repo, created with a README (GitHub's API
  cannot write to an empty repo). The app keeps one encrypted file there,
  `data/vault.enc` on a `data-sync` branch. It is encrypted, but file names and
  sizes can be read, so keep the repo private.

## 2. Create the Render service
1. Sign up at https://render.com (GitHub login is easiest).
2. **New → Blueprint**, pick the code repo — Render reads `render.yaml`.
3. Fill in the environment variables it asks for:
   - `DASHBOARD_USER`, `DASHBOARD_PASSWORD` — your login. Required: on a host
     the app shows nothing until a password is set.
   - `DASHBOARD_SECRET` — a long random string. It keeps you logged in and
     encrypts your saved data (`DATA_KEY`, if set, is used for the data instead).
     Never change it, or the saved data can no longer be read.
   - `GITHUB_TOKEN` — fine-grained token, **Contents: Read and write** on the
     data repo only. Pick a long expiry and renew it before it ends.
   - `GITHUB_REPO` — the data repo, `owner/name`. Without it the app saves to
     the repo it was deployed from (Render's `RENDER_GIT_REPO_SLUG`).
   - `DISPLAY_CURRENCY` — `EUR`, `GBP` or `USD`, the currency the app opens in.
   - `TAX_RESIDENCE` — `UK` (default), `US` or `NL`; anything else (`NONE`)
     hides the tax cards.
   - Optional: `DASHBOARD_TZ` (default `Europe/London`), `UK_RESIDENT_FROM`.

Broker and bank keys are added in the app (Settings → Add an account), or as
env vars: `T212_<NAME>_API_KEY` + `T212_<NAME>_API_SECRET` per Trading 212
account (a name containing `ISA` counts as tax-free).

## 3. Deploy, then install on your phone
1. **Apply** → wait a few minutes for the build.
2. Open the `…onrender.com` URL in **Safari** → sign in → **Save Password**.
3. **Share → Add to Home Screen** → app icon. Next time, tap the password field
   and unlock with **Face ID**.
4. Settings → Cloud persistence should say **Cloud sync ON**.

## How saving works
Render wipes its disk on every restart and deploy. Every upload, balance or
key added in the app is encrypted (with `DATA_KEY`/`DASHBOARD_SECRET`) and
stored on the data repo's `data-sync` branch, and loaded back on every start.
The branch keeps one commit, replaced on each save, so the repo does not grow.
If GitHub cannot be reached at start-up, saving pauses (so older data never
overwrites newer data) and resumes once it can.

**Moving saved data to another repo.** Set `GITHUB_REPO` to the new private
repo (created with a README) and give the token Contents read & write on both
repos. On the next start the app copies the data over, reads it back, and
deletes the old `data-sync` branch.

**Alternatives** (private code repo only):
- *Committed vault:* `.venv/bin/python -m backend.vault` writes
  `data/vault.enc` from your local `data/` files; `git add -f data/vault.enc`,
  commit and push. Each deploy decrypts it with `DASHBOARD_SECRET`.
- *Secret Files:* on the service's **Environment** tab → **Secret Files**, add
  `degiro_account.csv`, `trades_trade_republic.json`,
  `income_trade_republic.json` or `trading212_statement.json` (the app checks
  the service root, `/etc/secrets/` and `data/`).

## Updating your data later
Open the app's **Settings** page on your phone or laptop and upload the fresh
exports — the DeGiro statement PDF, the Trade Republic transaction CSV, and
(without an API key) the Trading 212 activity-statement PDF. They are converted
and validated on the server and saved as above. Each upload replaces the
previous one for that broker, so export all dates each time. Trading 212 stays
live automatically when the API key is set.

## Speed: sleep, keep-alive and caching

**What you see when the server is asleep.** The phone app opens straight
away from the last numbers it saved, marked "Updating…", and swaps in fresh
numbers when the server is up. (A small service worker, `frontend/sw.js`,
keeps a copy of the page itself; no portfolio data is stored in it. Signing
out clears the saved numbers.)

**Keep it awake (on by default, free).** Render sleeps a free service after
15 minutes without traffic. On Render the app pings its own address
(`RENDER_EXTERNAL_URL`, set by Render) every 10 minutes, so it stays awake.
Set `KEEPALIVE_SELF=0` to turn this off, for example when you run a second free
service (see the budget below). `/healthz` shows `up_minutes`: if it keeps
growing, the service has not slept. Other ways, should the built-in ping ever
stop working:

- *External monitor* (simplest, more punctual): a free account at
  cron-job.org → Create cronjob → URL
  `https://<your-service>.onrender.com/healthz` → every 10 minutes → Create.
  UptimeRobot (free, every 5 minutes) works the same way.
- *GitHub Actions* (included, off by default): in the repo's Settings →
  Secrets and variables → Actions, add the secret `KEEPALIVE_URL` =
  `https://<your-service>.onrender.com/healthz` and the variable
  `KEEPALIVE` = `on`. See `.github/workflows/keep-alive.yml`. GitHub sometimes
  starts scheduled runs late, so the service may still sleep now and then.
  Actions do not run while the GitHub account has a billing problem.

`/healthz` needs no login and returns no data. Mind the budget: Render gives
750 free instance hours per workspace per month, and one service running all
month uses about 744. So keep only one free service awake. Pings themselves
do no work. While nobody uses the app it refreshes its data every 20 minutes
(every 4 minutes while you use it), so the next visit finds it ready.

**Paid alternative.** Render's Starter plan ($7/month, 0.5 CPU, 512 MB) never
sleeps and has 5× the CPU of the free plan (0.1 CPU), so pages also compute
faster. Change `plan: free` to `plan: starter` in `render.yaml`.

**What the server caches** (in memory; a restart starts empty and warms
itself up in the background):

| Data | Fresh for | Then served while refreshing in the background |
|---|---|---|
| Live quotes (fetched in parallel) | 60 s | up to 1 h (a new day always fetches) |
| Holdings from all brokers | 30 s | up to 1 h |
| Trade lists, realised P/L, income, UK tax, annual report | 5 min | up to 1 h |
| FX rates | 1 h | up to 6 h |
| Week/month/year price history | 5 min | up to 24 h |
| Performance chart (daily history since the first trade) | 3 h | up to 7 days; rebuilt at once when trades change |
| Index strip · sparklines · S&P benchmark | 5 min · 30 min · 1 h | up to 6–24 h |
| Earnings dates · dividend history · correlation | 6–12 h | 4× as long |

Anything built from your own data is recomputed straight away after an
upload or any other edit, so new numbers never wait for a timer. If Yahoo or a
broker fails, the last good value is kept.

## Bank links (Enable Banking)
EU bank balances (Rabobank, ING, ABN AMRO, bunq, EU Revolut, …) can update on
their own. Link banks from the `https://…onrender.com` address: Enable Banking
only sends you back to an https URL, so it does not work from `http://localhost`.

1. In Enable Banking's Control Panel, register a **Production** application
   with the redirect URL `https://<your-service>.onrender.com/enablebanking/callback`
   (Settings → Add an account → your bank → *Bank link* shows it, ready to copy).
2. Click **Activate by linking accounts** and approve your own accounts.
3. Give the app its key, one of:
   - in Settings: the application ID and the downloaded `.pem` file (stored
     encrypted in the vault — set `GITHUB_TOKEN` so it survives restarts), or
   - on Render: env var `ENABLEBANKING_APP_ID`, and the `.pem` as a **Secret File**
     with `ENABLEBANKING_KEY_FILE=/etc/secrets/<file name>` (or paste it into
     `ENABLEBANKING_PRIVATE_KEY`).
4. Pick the bank and press **Link**. On iPhone the bank may send you back in
   Safari: the return page needs no login, then just reopen the home-screen app.

Balances are read every 6 hours (banks allow about 4 reads a day) and saved
with the connection, so a restart does not spend extra reads. Renew each bank
when the app says so (usually every 180 days).

## Notes
- Do **not** IP-restrict the Trading 212 key (Render's outbound IP changes).
- Keep `.env` and your real `data/*.csv|json` files out of git (they already are).
