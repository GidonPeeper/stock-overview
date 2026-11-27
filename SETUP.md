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
Stop it with Ctrl+C. Keep the Terminal window open while you use the app.

If `./run.sh` says the port is in use, the app is probably running already:
open the address it shows. To use another port (Mac and Linux):
`PORT=8001 ./run.sh`.

Prefer Docker? `docker compose up -d`, then open the same address.

## 2. Add your accounts

You first see sample data. It disappears as soon as you add something of your
own. Open **Settings**:

| You use | What to do |
|---|---|
| Trading 212 | Add an account → Trading 212 → paste a read-only API key |
| Trade Republic | Add an account → Trade Republic → upload the **Transaction export**. Or choose **Sign in (unofficial)**: phone number and PIN, then approve in the Trade Republic app |
| DEGIRO | **Statements → DeGiro**: upload the **Account statement** as PDF or CSV (all dates). Read in English, Dutch, German, French, Spanish, Italian, Portuguese, Czech and Polish |
| Interactive Brokers, eToro, Bitvavo, Starling, Wise | Add an account, then follow the steps shown |
| Any other broker | Add an account → **Another broker or app** → upload its positions CSV, then check which column is which |
| Bank, savings and pension accounts | **Money and things you own**: type in the balance, and the interest rate if you know it |
| Loans, income, monthly payments, things you own | **Money and things you own**. They make up your net worth and monthly budget on the Money tab |

A new statement replaces the one you uploaded before, so always export all
dates. If you upload the wrong file, the app says which file it expected.

## 3. Make it yours (optional)

Open **Settings → App and data → Your country** and pick where you live. Your
country sets the tax overview on the Money tab, the news, and which tax cards
show. The app then suggests a currency and time zone: press **Use these** to
take them, then **Save**.

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

## 5. Back up, update, move or start over

Everything you add is in the `data` folder: statements, balances, keys and
your choices in Settings. Your login, if you set one, is in `.env`. Never
share either: they hold your statements and keys.

| To | Do this |
|---|---|
| Back up | Copy the `data` folder (and `.env`) somewhere safe |
| Restore | Stop the app, copy the backed-up files back into `data`, start it again |
| Update | Stop the app, run `git pull` in the folder, start it again. It installs what is new by itself |
| Move to another computer | Do step 1 there, stop the app, copy your `data` folder and `.env` into the new folder, start it again |
| Remove one account | **Settings → Connected accounts → ✕**. A DEGIRO, Trade Republic or Trading 212 statement has no ✕: stop the app and delete its file from `data` (`degiro_account.csv`, `trades_trade_republic.json` and `income_trade_republic.json`, or `trading212_statement.json`) |
| Start over with the examples | Stop the app, move everything out of `data` except the files with `.sample.` in their name, start it again |

## Good to know

- **Prices** come from Yahoo Finance. A share Yahoo does not know shows no
  gain or loss (valued at what you paid). Without internet the app still
  opens and shows everything at what you paid.
- **Tax figures** are rules of thumb, not tax advice.

## On your phone, or away from home (optional)

**Same Wi-Fi** (Mac and Linux; `run.bat` serves only its own computer).
Without a password the app shows nothing to other devices, so set one first:

1. In the app's folder, copy `.env.sample` to a new file named `.env`
   (`cp .env.sample .env`). Open `.env` in a text editor.
2. Fill in `DASHBOARD_USER` (a username), `DASHBOARD_PASSWORD` (a long
   password) and `DASHBOARD_SECRET` (a long random text; make one with
   `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`). Save.
3. Stop the app and start it with `HOST=0.0.0.0 ./run.sh`. It prints the
   address to open on your phone, such as `http://192.168.1.20:8000`. If your
   computer asks whether Python may accept incoming connections, choose
   **Allow**.
4. Open that address on your phone and sign in.

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
   | `UK_RESIDENT_FROM` | leave it empty, unless you moved to the UK during this tax year: then that date, `YYYY-MM-DD` |

4. Open your `…onrender.com` address, sign in, and add your accounts.
   **Settings → App and data → Where your data is saved** should say **Cloud
   backup on**. On a phone: **Share → Add to Home Screen**.

The free plan is slow for the first ~10 minutes after a start, and one
always-on service uses about 744 of Render's 750 free hours a month. Details:
[DEPLOY_RENDER.md](DEPLOY_RENDER.md). A tunnel to your own computer is another
route: [HOSTING.md](HOSTING.md).
