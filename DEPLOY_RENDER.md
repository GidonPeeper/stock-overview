# Deploy to Render (free, always-on, phone-friendly)

The dashboard runs 24/7 on Render's free tier at a fixed `…onrender.com` URL,
behind your login. **The repo can be public** — your keys go in as environment
variables and your statement files as Render **Secret Files**, so nothing
private is ever in git.

Free-tier notes: the service sleeps after 15 min without visits, and waking it
takes about a minute. The app copes with that (it opens from the last numbers
saved on your phone while the server wakes up), and you can keep it awake for
free — see [Speed](#speed-sleep-keep-alive-and-caching) below.

## 1. Push the code to GitHub (public or private — no private data is in it)
```bash
gh repo create stock-overview --public --source=. --push
```

## 2. Create the Render service
1. Sign up at https://render.com (GitHub login is easiest).
2. **New → Blueprint**, pick the repo — Render reads `render.yaml`.
3. Set the environment variables (Environment tab):
   - `T212_INVEST_API_KEY` + `T212_INVEST_API_SECRET`, `T212_ISA_API_KEY` + `T212_ISA_API_SECRET`
     — one read-only key pair per Trading 212 account (generate each one while that
     account is selected in the app). A single account can use plain `T212_API_KEY` + `T212_API_SECRET`.
   - `DISPLAY_CURRENCY` — `GBP` or `EUR`, the currency the dashboard opens in
   - `DASHBOARD_USER`, `DASHBOARD_PASSWORD` — your phone login
   - `DASHBOARD_SECRET` — any long random string (keeps you logged in)

## 3. Private data — the vault (recommended, zero-touch)

Your statement files can ship inside the public repo as **encrypted ciphertext**
(`data/vault.enc`), decrypted on boot with a key derived from the
`DASHBOARD_SECRET` (or `DATA_KEY`) env var — which only exists on the host and
in your local `.env`, never in git. With the vault present, every deploy
self-hydrates your real data automatically — no uploads, no secret files.

After updating a statement locally, rebuild and push it:
```bash
.venv/bin/python -m backend.vault && git add data/vault.enc && git commit -m "update data" && git push
```

## 3b. Alternative: Secret Files
Still on the service's **Environment** tab → **Secret Files** → add the files
(paste the contents of your local files), named exactly:

- `degiro_account.csv`
- `trades_trade_republic.json`
- `income_trade_republic.json`
- `trading212_statement.json` (only without a Trading 212 API key)

Render mounts Secret Files at the service root and `/etc/secrets/` — the app
checks both automatically (plus `data/`), so a plain filename is all you need.
Without these it runs on bundled sample data; the in-app banner shows which
sources are live vs sample, so you can see at a glance what's missing.

## 4. Deploy, then install on your phone
1. **Apply** → wait a few minutes for the build.
2. Open the `…onrender.com` URL in **Safari** → sign in → **Save Password**.
3. **Share → Add to Home Screen** → app icon. Next time, tap the password field
   and unlock with **Face ID**.

## Updating your data later
Easiest: open the app's **Settings** page on your phone and upload the fresh
exports — the DeGiro statement PDF, the Trade Republic transaction CSV, and
(without an API key) the Trading 212 activity-statement PDF. They are converted
and validated on the server and, with `GITHUB_TOKEN` set, re-encrypted into the
vault automatically. Trading 212 stays live automatically when the API key is set.

## Speed: sleep, keep-alive and caching

**What you see when the server is asleep.** The phone app opens straight
away from the last numbers it saved, marked "Updating…", and swaps in fresh
numbers when the server is up. (A small service worker, `frontend/sw.js`,
keeps a copy of the page itself; no portfolio data is stored in it. Signing
out clears the saved numbers.)

**Keep it awake (optional, free).** Render sleeps a free service after 15
minutes without traffic. A ping every 10 minutes stops that:

- *GitHub Actions* (included, off by default): in the repo's Settings →
  Secrets and variables → Actions, add the secret `KEEPALIVE_URL` =
  `https://<your-service>.onrender.com/healthz` and the variable
  `KEEPALIVE` = `on`. See `.github/workflows/keep-alive.yml`. GitHub sometimes
  starts scheduled runs late, so the service may still sleep now and then.
- *External monitor* (more punctual): a free HTTP monitor such as UptimeRobot
  or cron-job.org, every 5–10 minutes, on the same `/healthz` URL.

`/healthz` needs no login and returns no data. Mind the budget: Render gives
750 free instance hours per workspace per month, and one service running all
month uses about 744. So keep only one free service awake. Pings do not make
the app call Yahoo or your brokers; it only refreshes data while you use it.

**Paid alternative.** Render's Starter plan ($7/month, 0.5 CPU, 512 MB) never
sleeps and has 5× the CPU of the free plan (0.1 CPU), so pages also compute
faster. Change `plan: free` to `plan: starter` in `render.yaml`.

**What the server caches** (in memory; a restart starts empty and warms
itself up in the background):

| Data | Fresh for | Then served while refreshing in the background |
|---|---|---|
| Live quotes (fetched in parallel) | 60 s | up to 15 min |
| Holdings from all brokers | 30 s | up to 10 min |
| Trade lists, realised P/L, income, UK tax, annual report | 5 min | up to 1 h |
| FX rates | 1 h | up to 6 h |
| Week/month/year price history | 5 min | up to 24 h |
| Index strip · sparklines · S&P benchmark | 5 min · 30 min · 1 h | up to 6–24 h |
| Earnings dates · dividend history · correlation | 6–12 h | 4× as long |

Anything built from your own data is recomputed straight away after an
upload or any other edit, so new numbers never wait for a timer. If Yahoo or a
broker fails, the last good value is kept.

## Notes
- Do **not** IP-restrict the Trading 212 key (Render's outbound IP changes).
- Keep `.env` and your real `data/*.csv|json` files out of git (they already are).
