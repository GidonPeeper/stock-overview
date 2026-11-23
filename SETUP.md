# Run your own copy

This gives you your own dashboard at your own web address, free. It takes about
20 minutes. You need a GitHub account and a Render account (sign up with GitHub).

Your copy is yours alone: nobody else, including the person who shared this
repo, can see your data. The app only reads your accounts; it can never trade
or move money.

## 1. Copy the code

1. Open https://github.com/GidonPeeper/stock-overview and press **Fork**, then
   **Create fork**. Your fork is public, but it only holds code.
2. Later, to get updates: open your fork on GitHub and press **Sync fork**.
   Render then installs the update by itself.

## 2. Make a private place for your data

Render wipes its disk on every restart. The app therefore saves your data,
encrypted, in a private GitHub repo of your own.

1. On GitHub: **New repository**. Name it `stock-overview-data`, choose
   **Private**, and tick **Add a README file** (the repo must not be empty).
2. Make a key the app can save with: GitHub → **Settings → Developer settings →
   Personal access tokens → Fine-grained tokens → Generate new token**.
   - Repository access: **Only select repositories** → `stock-overview-data`.
   - Permissions → Repository permissions → **Contents: Read and write**.
   - Pick a long expiry. Copy the token (it is shown once).

## 3. Put it online on Render

1. At https://render.com: **New → Blueprint**, pick your fork, press **Connect**.
2. Fill in the settings it asks for:

   | Setting | What to enter |
   |---|---|
   | `DASHBOARD_USER` | a username for your login |
   | `DASHBOARD_PASSWORD` | a long password |
   | `DASHBOARD_SECRET` | a long random text, 40+ characters (a password manager can make one). It encrypts your saved data: keep a copy and never change it |
   | `GITHUB_TOKEN` | the token from step 2 |
   | `GITHUB_REPO` | `your-github-name/stock-overview-data` |
   | `DISPLAY_CURRENCY` | `EUR`, `GBP` or `USD` |
   | `TAX_RESIDENCE` | `UK`, `US` or `NL` for that country's tax tips; `NONE` to hide the tax cards |
   | `UK_RESIDENT_FROM` | leave empty (only for people who moved to the UK this tax year) |

3. Press **Apply**. The first start takes a few minutes.
4. Optional, under the service's **Environment** tab: `DASHBOARD_TZ` = your time
   zone, e.g. `Europe/Amsterdam` (default `Europe/London`). It sets when "today"
   starts.

## 4. Add your accounts

Open your `…onrender.com` address and sign in. You first see sample data;
it disappears as soon as you add something of your own. Go to **Settings**:

| You use | What to do |
|---|---|
| Trading 212 | Add an account → Trading 212 → paste a read-only API key |
| DEGIRO | Upload the **Account statement** PDF (all dates) |
| Trade Republic | Upload the **transaction export** CSV |
| Interactive Brokers, eToro, Bitvavo, Starling, Wise | Add an account, then follow the steps shown |
| Any other broker | Upload its positions CSV |
| Bank and savings accounts | Type in the balance |

Settings → **Cloud persistence** should say **Cloud sync ON**. If not, check
`GITHUB_TOKEN` and `GITHUB_REPO`.

## 5. On your phone

Open the address in Safari (iPhone) or Chrome (Android), sign in, save the
password, then **Share → Add to Home Screen**.

## Good to know

- **Free plan limits.** The server is slow for the first ~10 minutes after a
  start or an update, while it loads prices. It keeps itself awake, which uses
  about 744 of Render's 750 free hours a month, so run only one free service.
- **Prices** come from Yahoo Finance. A share Yahoo does not know shows no
  gain or loss (valued at what you paid).
- **Tax figures** are rules of thumb, not tax advice.
- **Lost `DASHBOARD_SECRET`?** Your saved data cannot be read any more. Upload
  your statements again.
