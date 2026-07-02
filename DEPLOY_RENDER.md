# Deploy to Render (free, always-on, phone-friendly)

The dashboard runs 24/7 on Render's free tier at a fixed `…onrender.com` URL,
behind your login. **The repo can be public** — your keys go in as environment
variables and your statement files as Render **Secret Files**, so nothing
private is ever in git.

Free-tier notes: the service sleeps after ~15 min idle; the next visit takes
~30–60 s to wake (plus a one-off rebuild of price history). Caches/history are
rebuilt automatically.

## 1. Push the code to GitHub (public or private — no private data is in it)
```bash
gh repo create stock-overview --public --source=. --push
```

## 2. Create the Render service
1. Sign up at https://render.com (GitHub login is easiest).
2. **New → Blueprint**, pick the repo — Render reads `render.yaml`.
3. Set the environment variables (Environment tab):
   - `T212_API_KEY`, `T212_API_SECRET` — your read-only Trading 212 key/secret
   - `DASHBOARD_USER`, `DASHBOARD_PASSWORD` — your phone login
   - `DASHBOARD_SECRET` — any long random string (keeps you logged in)

## 3. Add your private data as Secret Files
Still on the service's **Environment** tab → **Secret Files** → add three files
(paste the contents of your local files), named exactly:

- `degiro_account.csv`
- `trades_trade_republic.json`
- `income_trade_republic.json`

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
When you buy/sell, update the three Secret Files in Render (and push any code
changes). Trading 212 stays live automatically.

## Notes
- Do **not** IP-restrict the Trading 212 key (Render's outbound IP changes).
- Keep `.env` and your real `data/*.csv|json` files out of git (they already are).
