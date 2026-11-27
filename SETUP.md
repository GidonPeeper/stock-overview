# Set up Stock Overview

Stock Overview runs on your own computer. Your data stays in one folder on
that computer and is sent nowhere. The app only reads your accounts: it can
never trade or move money.

## 1. Start it

You need [Python 3.10 or newer](https://www.python.org/downloads/) and
[Git](https://git-scm.com/downloads).

**Mac or Linux**, in Terminal:

```bash
git clone https://github.com/GidonPeeper/stock-overview.git
cd stock-overview
./run.sh
```

**Windows**: run the same `git clone` line, then double-click `run.bat` in the
folder.

The first start takes about a minute. Your browser then opens
http://127.0.0.1:8000. Next time, run `./run.sh` (or `run.bat`) again.
Stop it with Ctrl+C.

Prefer Docker? `docker compose up -d`, then open the same address.

## 2. Add your accounts

You first see sample data. It disappears as soon as you add something of your
own. Open **Settings**:

| You use | What to do |
|---|---|
| Trading 212 | Add an account → Trading 212 → paste a read-only API key |
| Trade Republic | Add an account → Trade Republic → upload the **Transaction export**. Or choose **Sign in (unofficial)**: phone number and PIN, then approve in the Trade Republic app |
| DEGIRO | Upload the **Account statement** as PDF or CSV (all dates). Read in English, Dutch, German, French, Spanish, Italian, Portuguese, Czech and Polish |
| Interactive Brokers, eToro, Bitvavo, Starling, Wise | Add an account, then follow the steps shown |
| Any other broker | Upload its positions CSV |
| Bank and savings accounts | Type in the balance |

## 3. Make it yours (optional)

Open **Settings → App and data → Your country** and pick where you live. Your
country sets the tax overview on the Money tab, the news, and which tax cards
show. The app then suggests a currency and time zone: keep them or change them.

The `.env` settings below are only the defaults until you choose in Settings.
To set them, copy `.env.sample` to `.env`, change what you need, then restart:

| Setting | What it does |
|---|---|
| `DISPLAY_CURRENCY` | `EUR`, `GBP` or `USD` |
| `TAX_RESIDENCE` | `UK`, `US` or `NL` for that country's tax tips; `NONE` hides the tax cards |
| `DASHBOARD_TZ` | your time zone, e.g. `Europe/Amsterdam`. It sets when "today" starts |

## 4. Ask Claude about your portfolio (optional)

Claude can answer questions from your own numbers, such as "how did my
portfolio do this month?". Run `./run.sh claude` for the command to connect
it, or copy it from **Settings → App and data**. Claude can only read.
Details: [MCP.md](MCP.md).

## Good to know

- **Your data** is the `data` folder. Back it up with **Settings → App and
  data → Download a backup**. Never share that file: it holds your statements
  and keys. To move to a new computer, restore it there.
- **Updates:** run `./run.sh update` (Windows: `run.bat update`), then start
  the app again.
- **Something wrong?** **Settings → App and data → Check-up** says what needs
  attention and what to do. More help: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
- **Prices** come from Yahoo Finance. A share Yahoo does not know shows no
  gain or loss (valued at what you paid).
- **Tax figures** are rules of thumb, not tax advice.

## On your phone, or away from home (optional)

**Same Wi-Fi.** Set a login in `.env` (`DASHBOARD_USER`, `DASHBOARD_PASSWORD`
and `DASHBOARD_SECRET`, a long random text), start with `HOST=0.0.0.0 ./run.sh`,
and open `http://<your computer's name or IP>:8000` on your phone. Without a
password the app shows nothing to other devices.

**Anywhere, always on.** Put your own copy online for free on Render:

1. **Copy the code.** Fork this repo on GitHub. The fork is public, but it
   only holds code. To update later, press **Sync fork**.
2. **Make a private place for your data.** Render wipes its disk on every
   restart, so the app saves your data, encrypted, in a private repo of yours.
   - On GitHub: **New repository**, name it `stock-overview-data`, choose
     **Private**, tick **Add a README file**.
   - GitHub → **Settings → Developer settings → Personal access tokens →
     Fine-grained tokens → Generate new token**. Repository access: only
     `stock-overview-data`. Permissions: **Contents: Read and write**. Copy
     the token.
3. **Start it.** At https://render.com: **New → Blueprint**, pick your fork,
   and fill in:

   | Setting | What to enter |
   |---|---|
   | `DASHBOARD_USER` | a username |
   | `DASHBOARD_PASSWORD` | a long password |
   | `DASHBOARD_SECRET` | a long random text, 40+ characters. It encrypts your saved data: keep a copy and never change it |
   | `GITHUB_TOKEN` | the token from step 2 |
   | `GITHUB_REPO` | `your-github-name/stock-overview-data` |
   | `DISPLAY_CURRENCY`, `TAX_RESIDENCE` | as in step 3 above |

4. Open your `…onrender.com` address, sign in, and add your accounts.
   Settings should say **Cloud sync ON**. On a phone: **Share → Add to Home
   Screen**.

The free plan is slow for the first ~10 minutes after a start, and one
always-on service uses about 744 of Render's 750 free hours a month. Details:
[DEPLOY_RENDER.md](DEPLOY_RENDER.md). A tunnel to your own computer is another
route: [HOSTING.md](HOSTING.md).
