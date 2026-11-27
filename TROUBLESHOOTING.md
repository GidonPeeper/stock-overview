# When something goes wrong

Start with the **check-up**: Settings → App and data → Check-up. It lists what
needs attention and what to do. A number on the settings button means it found
something. This page covers the rest: each problem as what you see, why, and
what to do.

**Contents:** [Starting](#starting) · [Prices](#prices) · [Accounts and statements](#accounts-and-statements) ·
[Numbers](#numbers-look-wrong) · [Passwords and keys](#passwords-and-keys) ·
[A copy on Render](#a-copy-on-render) · [Your data](#your-data) ·
[Updating](#updating) · [Reporting a bug](#reporting-a-bug)

## Starting

### The page does not open

- **You see:** "This site can't be reached" at http://127.0.0.1:8000.
- **Why:** the app is not running, or not yet. The first start takes about a minute.
- **Do:** look at the window where you started it. If it says
  `Wimm: http://127.0.0.1:8000`, wait a moment and reload. If the
  window is closed, start it again with `./run.sh` (Windows: `run.bat`). If it
  shows an error, find it below.

If the page says *"This dashboard has no password and was opened as …"*, you
used another address. Open http://127.0.0.1:8000 instead, or add that name to
`.env` as `ALLOWED_HOSTS=thatname`.

### Port already in use

- **You see:** `Port 8000 is already in use.`
- **Why:** something else uses that port, often Wimm itself, started
  earlier in another window.
- **Do:** open http://127.0.0.1:8000: it may already be running. Or start this
  copy on another port: `PORT=8001 ./run.sh`.

### Python too old or missing

- **You see:** `Python 3.10 or newer is needed.`
- **Do:** install Python from https://www.python.org/downloads/ and run
  `./run.sh` again. If the app ran before with an older Python, delete the
  `.venv` folder in the app's folder so it sets up again.

### `./run.sh: Permission denied`

- **Why:** the file lost its "may run" mark (this happens when a folder is copied
  or unzipped).
- **Do:** run `sh run.sh`, or once: `chmod +x run.sh`.

### On Windows

- Double-click `run.bat`, or type `run.bat` in the folder's Command Prompt.
- "Python was not found": install Python and tick **Add python.exe to PATH**
  in the installer. Then run `run.bat` again.
- Windows may ask whether Python may use the network: **Allow** for private
  networks.
- The app uses port 8000 on Windows. If that is taken, close the other program
  or the other Wimm window.
- Stop the app with Ctrl+C in its window, or close the window.

### "Installing what the app needs failed"

- **Why:** the first start (and an update) downloads the parts the app needs.
  That needs an internet connection.
- **Do:** check the connection and start it again. Still failing? Delete the
  `.venv` folder and start again.

## Prices

### Prices are missing or old

- **You see:** at the top, *"No live prices right now"* or *"Prices from …
  (could not refresh them yet)"*. The check-up may say *"Prices are not coming in"*.
- **Why:** prices come from Yahoo Finance. When Yahoo does not answer, the app
  shows the last prices it had. Exchange rates come from Yahoo too; without them
  the app uses built-in rates.
- **Do:** wait an hour; it usually passes. Check that the computer is online.
  If it lasts a day, update the app ([Updating](#updating)): when Yahoo changes
  how it answers, an update follows.

### A holding has no price

- **You see:** *"1 holding has no live price and is counted at what you paid."*
- **Why:** the app could not find the share on Yahoo. Delisted shares and some
  funds have no price there.
- **Do:** search for it with the search box at the top of the dashboard. If
  Yahoo shows it under another name or ticker, [report it](#reporting-a-bug)
  with its ISIN (the code on your statement, such as US0378331005), so the app
  can learn it. If it was delisted, there is nothing to do.

## Accounts and statements

### A broker key is refused

- **You see:** in Settings, an account marked *Error*, or the check-up says
  *"Trading 212 key not working"*.
- **Why:** the key was deleted, expired, typed wrong, or lacks a permission.
- **Do:** make a new read-only key at the broker (the steps in Settings → Add an
  account say which boxes to tick). Then in Settings → Connected accounts remove
  the old account and add it again with the new key. A key you set in `.env`
  (the `T212_…` lines) is changed there instead; restart afterwards.

A bank link (Enable Banking) ends after the time the bank allows, often 90 or
180 days. The check-up warns two weeks before. Press **Renew** in Connected
accounts and approve it in your bank's app.

### A statement is refused

The message says what is wrong. The usual ones:

| You see | Do |
|---|---|
| *"That is DEGIRO's Transactions export"* | Upload the **Account statement** instead: DEGIRO → Inbox → Account statement |
| *"in a language the app does not read yet"* | Switch DEGIRO to English (or another listed language), export again; or use the CSV export |
| *"larger than 12 MB"* | Export a shorter period, or the CSV instead of the PDF |
| *"No trades or positions found"* | Check it is the right report: Trading 212 *Activity statement* (Invest account), Trade Republic *Account statement* or *Transaction export* |
| *"could not be read just now"* on a statement you uploaded before | The file is damaged: upload it again, or restore a backup |

On a free host a very long PDF can take a minute or two. Keep the page open.

### Your statement is old

The check-up says *"Your DEGIRO statement ends on …"* when the newest entry is
over two months old and the account still holds shares. Statements do not update
themselves. Download a new one (from your first trade to today) and upload it.
If nothing happened in the account since, there is nothing to do.

## Numbers look wrong

1. **Open the check-up.** Most wrong-looking numbers have a reason it lists.
2. **"Sales without a recorded purchase"**: you sold shares the app never saw
   you buy, because the statement starts after the purchase. Upload a statement
   that starts on the day you opened the account. Shares moved in from another
   broker stay a gap.
3. **"Live exchange rates are missing"** or **"No exchange rate for …"**:
   amounts in that currency are off until the rate returns. Wait an hour.
4. **Counted twice?** Don't upload a Trading 212 statement for an account that
   also has a key.
5. **Tax figures** are estimates. The UK tax card uses today's exchange rate;
   HMRC uses the rate on each trade date. When a new tax year starts before the
   app is updated, the tax cards say so.

Still wrong? [Report it](#reporting-a-bug).

## Passwords and keys

### I forgot the dashboard password

The password is not stored in the app's data, so it cannot be reset there.

- **On your own computer:** open the `.env` file in the app's folder with a text
  editor. Change `DASHBOARD_PASSWORD=` to a new password, save, and start the app
  again. (No `.env`? Then there is no password.)
- **On Render:** open the service → **Environment**, change
  `DASHBOARD_PASSWORD`, save. Render restarts the app with it.

After five wrong tries the login waits 15 minutes.

### I lost DASHBOARD_SECRET

`DASHBOARD_SECRET` keeps you logged in, and on a host it also encrypts the copy
of your data saved to GitHub (unless you set `DATA_KEY`, which then does that).

- **On Render** it is still in the service's **Environment** page: reveal and copy it.
- **Changed it, and now the check-up says "Your saved data could not be
  opened"?** Put the old value back. That copy cannot be read without it.
- **Truly lost?** The saved copy cannot be opened by anyone. Set a new, long
  secret, then restore your last downloaded backup (Settings → Back up and
  restore), which saves your data again with the new secret. No backup? Add your
  accounts and statements again.
- **On your own computer** the data folder is not encrypted: a new secret only
  logs you out.

To change the secret on purpose: download a backup first, change the secret,
restart, log in, restore the backup.

## A copy on Render

### It shows "Set a password first"

- **Why:** a copy on the internet shows nothing until it has a password.
- **Do:** in Render → your service → **Environment**, add `DASHBOARD_USER`,
  `DASHBOARD_PASSWORD` and `DASHBOARD_SECRET` (a long random text, 40+
  characters; keep a copy). Save and wait for the restart. See [SETUP.md](SETUP.md).

### The hosted copy lost data after a restart

- **Why:** Render wipes its disk on every restart. The app saves your data,
  encrypted, to a private GitHub repository, but only with `GITHUB_TOKEN` and
  `GITHUB_REPO` set, and only while the token works. GitHub tokens expire on the
  date you chose when you made them.
- **Do:** open Settings → **Where your data is saved** and the check-up. Then:
  - not set: follow [SETUP.md](SETUP.md), "Anywhere, always on", steps 2 and 3;
  - "rejected the token" or "HTTP 401": make a new token (same settings) and
    replace `GITHUB_TOKEN` on Render;
  - "HTTP 403/404": the token needs **Contents: Read and write** on that repository.
  Then restore your last downloaded backup, or upload again what is missing.
  Download a backup now and then: it is your copy if all else fails.

### It is slow, or takes a minute to open

The free plan sleeps after 15 minutes without visits and needs about a minute to
wake; the first ~10 minutes after a start are slow. The app keeps itself awake
while it runs. See [DEPLOY_RENDER.md](DEPLOY_RENDER.md).

## Your data

### Moving to a new computer

1. On the old computer: Settings → **Download a backup**.
2. On the new one: install the app ([SETUP.md](SETUP.md), step 1).
3. If you had a `.env` file (password, keys set there), copy it into the new
   app folder.
4. Open Settings → **Restore from a backup**, choose the file, check what it
   holds, press **Restore this backup**.

The same works from your computer to a copy on Render, and back.

### Restoring a backup

Settings → Back up and restore → choose the `.zip` → **Check the file**. The
app shows what the backup holds and what it replaces, and changes nothing until
you press **Restore this backup**. Your current files are kept in
`data/before-restore-<date>`. To undo a restore, stop the app, copy the files
from that folder back into `data`, and start it again.

A backup holds your keys and statements, not encrypted. Keep it private.

### Back to the example data, or starting over

1. Download a backup first, in case.
2. Stop the app.
3. Move everything in the `data` folder **except** the files with `.sample.` in
   their name to another folder (or delete them).
4. Start the app. You see the example data; add your accounts again.

To remove one account only, use Settings → Connected accounts → ✕.

## Updating

- **Your own computer:** stop the app, run `./run.sh update` (Windows:
  `run.bat update`), start it again. Your data and `.env` are never touched.
- **A fork on Render:** on GitHub, open your fork and press **Sync fork**.
  Render installs it within a few minutes.
- **Docker:** `git pull`, then `docker compose up -d --build`.
- **Downloaded as a zip (no git):** download the new version, copy your `data`
  folder and `.env` into it.

Settings → Version and updates shows your version. Switch on the daily check
there to hear about new versions on the check-up.

When `./run.sh update` cannot update, it says why:

| It says | Do |
|---|---|
| *"files of the app were changed on this computer"* | If you did not change them on purpose: `git stash`, then `./run.sh update` |
| *"Could not update"* with *"Could not resolve host"* | No internet: connect and try again |
| *"no tracking information"* | Run `git checkout main`, then update again |
| *"Not possible to fast-forward"* | This copy has its own changes: ask whoever made them |
| *"the git program is not installed"* | Install Git from https://git-scm.com/downloads |

## Reporting a bug

Open an issue at https://github.com/GidonPeeper/Where-is-my-money/issues. Include:

- what you did, and the exact message you saw;
- your version (Settings → Version and updates) and your system (Mac, Windows,
  Render; which browser);
- for a holding without a price: its ISIN and the broker.

**Never include** your statements, the `data` folder, a backup file, `.env`,
keys, tokens or passwords. Cover amounts and account numbers in screenshots.
Lines from the app's window are fine once you have checked they hold none of these.
