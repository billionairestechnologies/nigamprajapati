# Intraday Equity Scanner & Auto-Trading Strategy

A self-hosted dashboard for scanning NSE stocks intraday, generating trade
signals from a multi-indicator confirmation stack, and executing them
automatically (or manually) across one or more broker accounts (Angel One,
Alice Blue). Runs entirely on your own machine — no data leaves it except to
your broker(s) and, optionally, a webhook URL you configure.

## Requirements

- Python 3.10+
- macOS, Linux, or Windows
- An Angel One SmartAPI account and/or an Alice Blue trading account, if you
  intend to connect real brokers

## First run

Pick whichever matches your machine — all three do the same thing (create a
Python virtual environment, install dependencies from `requirements.txt`,
generate an encryption key into `backend/.env` if one doesn't exist yet,
then start the server):

- **macOS**: double-click `run.command` in Finder.
- **Windows**: double-click `run.bat` in Explorer.
- **Linux / a remote server over SSH**: `./run.sh`

The dashboard starts at `http://127.0.0.1:8787` and opens automatically in
your default browser. Every subsequent run is the same double-click /
`./run.sh` — dependencies only get (re-)installed if `backend/.venv`
doesn't already exist.

### Configuration

`backend/.env` holds local configuration (created automatically on first
run from `backend/.env.example`):

| Variable | Purpose |
|---|---|
| `APP_SECRET_KEY` | Encrypts broker credentials and the login session at rest. Auto-generated on first run. **Back this up** — if it's lost, stored broker credentials can't be decrypted and accounts will need to be re-added. |
| `DASHBOARD_USERNAME` / `DASHBOARD_PASSWORD` | Login for the dashboard itself. Change these from the defaults before exposing the dashboard beyond your own machine. |
| `HOST` | Defaults to `127.0.0.1` (this machine only). Set to `0.0.0.0` only if running behind a firewall/VPN you trust — there is no TLS here, and it holds broker credentials. |
| `PORT` | Defaults to `8787`. |

## Adding broker accounts

Open the **Accounts** tab → **+ Add Account**.

- **Angel One** needs: Client ID, SmartAPI API key, login password/PIN, and
  the TOTP **secret** — the base32 seed shown once when TOTP is enabled on
  the SmartAPI developer portal, not a 6-digit code.
- **Alice Blue** needs: Client ID (user ID) and API key from the Alice Blue
  developer console. After saving, a dialog shows the Alice Blue login URL —
  open it (or copy it to another device/browser) to complete the OAuth
  login; it redirects back here automatically once signed in.

Credentials are encrypted at rest and never leave this machine except to
the broker's own API.

### Per-account risk settings

Each account has its own:
- **Sizing mode** — either a % of that account's own available funds, or a
  fixed ₹ amount per trade.
- **Max trades per day** — capped independently per account.

Edit these any time via **Edit Risk** on the Accounts tab. The table also
shows a live-computed "Capital / Trade" estimate and each account's
connection status (with the actual error message if one exists).

## Trading modes

- **Mode** (header): **Manual** — signals appear under *Signals* as
  `pending` for you to Approve or Reject. **Auto** — signals execute
  immediately, subject to the risk checks below.
- **Trading** (header): **Paper** — every signal simulates a fill using
  real live market data, without ever sending an order to the broker.
  **Live** — real orders. This is one global switch, not per-account.
- **Kill Switch** (header): stops the engine and immediately squares off
  every open position. Click again to resume.

## How a signal fires

1. **Scan** (once daily, configurable time): index bias, then sector and
   stock moves against configurable thresholds, builds a watchlist.
2. **Confirmation** (every 5 minutes on the watchlist): EMA, VWAP,
   Supertrend, RSI, ADX, and Volume are evaluated on the 5-minute chart.
   Any of these can be individually disabled in Settings — a signal only
   needs the *enabled* directional indicators (EMA/VWAP/Supertrend/RSI) to
   agree, with ADX/Volume acting as optional confirmation gates.
3. **Entry / Target / Stop-loss price**, computed per the modes configured
   in Settings:
   - **Stop loss**: breakout candle low/high, a fixed %, or a fixed ₹ points
     distance from entry.
   - **Target**: a fixed % from entry, or a multiple of the stop distance
     (R-multiple).
4. **Execution**: in Auto mode, immediately; in Manual mode, on Approve.
   Position size and the daily trade cap are per-account (see above).
5. **Exit**: monitored every minute against the stop-loss/target; a
   portfolio-wide daily profit target (% of total capital across all
   accounts) halts new entries for the day once reached. Everything still
   open is squared off automatically at the configured square-off time.

## Settings tab

- **Daily Risk Limit** — portfolio-wide profit target that halts new
  entries for the day (not a per-trade number).
- **Signal Detection** — sector/stock move thresholds, and the indicator
  on/off toggles described above.
- **Per-Trade Target & Stop Loss** — the exit-price modes described above.
- **Schedule** — scan time, square-off time, and informational-only
  pre-/post-market snapshot times (these refresh the display only and never
  touch the trading watchlist).
- **Webhook / API Integration** — an optional URL that receives a JSON POST
  whenever a signal fires or an order executes, so this strategy's output
  can feed any other algo platform or bot. Use **Send Test Webhook** to
  verify it before relying on it.

## Tabs

- **Dashboard** — market bias, gainers/losers, sector ranking, watchlist
  with live indicator reads, and today's signals.
- **Accounts** — broker connections and per-account risk settings.
- **Orders** — full order log across all accounts and days.
- **History** — one row per trading day (today included, updated live);
  click a row to see that day's actual trades and signals in full detail.
- **Logs** — the running strategy/error log, filterable to warnings/errors
  only.
- **Settings** — described above.

## Alerts

Sound alerts are on by default (audio unlocks silently on the first click
anywhere on the page, due to browser autoplay restrictions) — a chime on
new signals, a spoken alert on stop-loss hits, and an alarm plus a visible
banner on any error (broker disconnects, failed orders, rate limits, etc.),
each with the underlying reason. Click **Sound Alerts** in the header to
mute/unmute.

## Logs

`backend/data/logs/app.log` rotates daily and keeps 30 days of history —
the same file the Logs tab reads from, useful for investigating anything
after the fact even if the dashboard was closed at the time.

## Data & backups

Everything is stored locally in `backend/data/`:
- `scanner.db` — SQLite database (accounts, signals, orders, settings, daily history).
- `scrip_master.json` — cached symbol → instrument-token mapping, refreshed daily.
- `logs/` — daily rotating log files.

Back up `backend/data/scanner.db` and `backend/.env` together if you want
to preserve trading history and encrypted credentials.

## Before trusting this with real capital

- `backend/app/strategy/universe.json` is a curated sector/stock list, not
  a live official index-constituent feed — review and edit it for your
  needs.
- Run in **Paper** mode first end-to-end (a full trading day, ideally) to
  confirm signals, sizing, and exits all behave as expected before
  switching to **Live**.
- Angel One's SmartAPI requires the app's registered IP to match the
  machine's actual public IP for order placement to succeed — check this
  in the Angel One SmartAPI developer portal if live orders are rejected
  with an IP-related error.
