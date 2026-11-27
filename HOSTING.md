# Secure remote access (phone) via Cloudflare Tunnel + Access

Goal: reach the dashboard from anywhere **without** copying your statements or
broker keys to a server. The app keeps running on your Mac; Cloudflare
exposes it over HTTPS and only lets *you* in.

```
phone ──HTTPS──▶ Cloudflare (Access login: you@example.com)
                      │  (encrypted tunnel, no open ports)
                      ▼
                 cloudflared ──▶ http://localhost:8000  (your Mac)
                                     app + data stay here
```

## Two layers of protection
1. **Cloudflare Access** — a login wall locked to your Google account.
2. **App password** — `DASHBOARD_PASSWORD` in `.env` (defence in depth). The app
   refuses to show anything through the tunnel without it. To rely on
   Cloudflare Access alone, set `ALLOW_NO_PASSWORD=1` and
   `ALLOWED_HOSTS=<your address, e.g. dash.example.com>` instead.

Broker keys are read-only, so even a total breach cannot move money.

## Prerequisites
- A free **Cloudflare account**
- A **domain** added to Cloudflare (a cheap one is fine; needed for Access).
  Without a domain you can only use a temporary `trycloudflare.com` quick tunnel
  — OK for a quick test *with the app password set*, but it has no Access wall,
  so don't rely on it.

## Steps
1. Turn on the app password. Copy `.env.sample` to `.env` and fill in:
   ```
   DASHBOARD_USER=you
   DASHBOARD_PASSWORD=<a long random passphrase>
   DASHBOARD_SECRET=<a long random text: it keeps you signed in across restarts>
   ```
2. Install the connector and authenticate:
   ```bash
   brew install cloudflared
   cloudflared tunnel login          # pick your domain in the browser
   cloudflared tunnel create stock-overview
   cloudflared tunnel route dns stock-overview stocks.YOURDOMAIN
   ```
3. Create `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: stock-overview
   credentials-file: /Users/<you>/.cloudflared/<TUNNEL-ID>.json
   ingress:
     - hostname: stocks.YOURDOMAIN
       service: http://localhost:8000
     - service: http_status:404
   ```
4. Run the app (this computer only: no `HOST=0.0.0.0`) and the tunnel:
   ```bash
   ./run.sh                                      # terminal 1
   cloudflared tunnel run stock-overview         # terminal 2
   ```
   (Later: `cloudflared service install` to run the tunnel on boot.)
5. In the **Cloudflare Zero Trust** dashboard → **Access → Applications** →
   *Add a self-hosted application* for `stocks.YOURDOMAIN`, then add a policy:
   **Allow** where **emails == your email address**. Choose email OTP or Google
   as the login method.

Now `https://stocks.YOURDOMAIN` on your phone shows a Cloudflare login, then the
app password, then your dashboard — and nothing is reachable when your Mac is off.
