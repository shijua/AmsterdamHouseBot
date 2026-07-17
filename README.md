# Amsterdam House Bot

Telegram bot that scans Amsterdam rental listings and sends a message when a new listing matches a user's filters.

Supported sources:

- Pararius
- Funda
- Kamernet
- Huurwoningen
- VVA
- Roofz

The bot stores user filters and already-seen listings in SQLite, so duplicate listings are not sent twice.

## What it does

- Runs every enabled platform on the same fast scan cadence
- Checks both the Amsterdam Pararius search page and Pararius' public newest-rentals feed
- Lets each Telegram user save rental preferences, rent, bedroom/room, and surface-area filters
- Applies Apartment to Kamernet, Huurwoningen, Pararius, and Funda; applies Furnished to all of those except Funda
- Sends new listings directly in Telegram
- Can automatically reply to new matching Kamernet listings when explicitly enabled
- Supports an on-demand scan with `/test`

## Prerequisites

- Python 3.13.7, managed by `uv`
- `uv` 0.8.15 for local development
- A Telegram bot token from BotFather

## Setup From Zero

### 1. Open the project

If you already have the folder locally, just open it in VS Code or your terminal.

### 2. Install dependencies

Use [uv](https://docs.astral.sh/uv/) from the project root. The lockfile is part of the supply-chain protection for this bot, so install with `--locked`:

```bash
uv sync --locked
```

Activate the virtual environment if you want to run commands manually:

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
source .venv/bin/activate
```

### 3. Install browser drivers

Roofz requires browser automation. Run this once after installing dependencies:

```bash
# For Roofz (Playwright)
playwright install chromium
```

### 4. Create the environment file

Create a `.env` file in the project root with the following content:

```env
TELEGRAM_TOKEN=123456789:replace-with-your-real-token
FAST_POLL_INTERVAL_SECONDS=900
SCAN_JITTER_SECONDS=30
FORBIDDEN_FAILURE_THRESHOLD=3
FORBIDDEN_BACKOFF_SECONDS=21600,43200,86400
SCRAPER_TIMEOUT_SECONDS=45
KAMERNET_AUTOREPLY_TIMEOUT_SECONDS=45
KAMERNET_AUTOREPLY_MAX_PER_SCAN=2
KAMERNET_MAX_PAGES_PER_SCAN=3
KAMERNET_AUTOREPLY_STORAGE_STATE_PATH=
KAMERNET_AUTOREPLY_EMAIL=
KAMERNET_AUTOREPLY_PASSWORD=
KAMERNET_AUTOREPLY_HEADLESS=true
KAMERNET_AUTOREPLY_DRY_RUN=false
PARARIUS_SCRAPER_TIMEOUT_SECONDS=20
FUNDA_SCRAPER_TIMEOUT_SECONDS=25
FUNDA_PYFUNDA_TIMEOUT_SECONDS=12
FUNDA_PYFUNDA_MAX_RETRIES=2
FUNDA_PYFUNDA_RETRY_BACKOFF_SECONDS=0.1
FUNDA_MAX_BACKGROUND_THREADS=1
ROOFZ_SCRAPER_TIMEOUT_SECONDS=90
ROOFZ_ENABLED=true
VVA_MAX_PAGES_PER_SCAN=1
MAX_CONCURRENT_USERS_PER_JOB=3
DB_PATH=listings.db
SQLITE_BUSY_TIMEOUT_MS=5000
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

Environment variables:

- `TELEGRAM_TOKEN`: required, Telegram bot token from BotFather
- `FAST_POLL_INTERVAL_SECONDS`: optional, scan interval for every enabled platform, defaults to `PARARIUS_POLL_INTERVAL_SECONDS` when set, otherwise `POLL_INTERVAL_SECONDS` (`900` by default)
- `SCAN_JITTER_SECONDS`: optional, adds a random delay from zero up to this many seconds to each scheduled scan, defaults to `30`
- `FORBIDDEN_FAILURE_THRESHOLD`: optional, consecutive HTTP 403 responses before pausing a source, defaults to `3`
- `FORBIDDEN_BACKOFF_SECONDS`: optional, comma-separated source cooldowns after repeated HTTP 403 responses, defaults to `21600,43200,86400` (6, 12, and 24 hours)
- `POLL_INTERVAL_SECONDS`: optional, legacy fallback used only to derive `FAST_POLL_INTERVAL_SECONDS`, defaults to `900`
- `PARARIUS_POLL_INTERVAL_SECONDS`: optional, legacy fallback for `FAST_POLL_INTERVAL_SECONDS`
- `ROOFZ_POLL_INTERVAL_SECONDS`: deprecated; Roofz now uses the same fast interval as every other enabled platform
- `SCRAPER_TIMEOUT_SECONDS`: optional, timeout for general scrapers, defaults to `45`
- `KAMERNET_AUTOREPLY_TIMEOUT_SECONDS`: optional, timeout for Kamernet reply browser actions, defaults to `45`
- `KAMERNET_AUTOREPLY_MAX_PER_SCAN`: optional, max automatic Kamernet replies per scan per user, defaults to `2`
- `KAMERNET_MAX_PAGES_PER_SCAN`: optional, max Kamernet search pages fetched per scan, defaults to `3`
- `KAMERNET_AUTOREPLY_STORAGE_STATE_PATH`: optional, Playwright login-session file for Kamernet. If unset, it defaults to `kamernet_storage_state.json` locally, or next to `DB_PATH` when `DB_PATH` includes a directory.
- `KAMERNET_AUTOREPLY_EMAIL`: optional, Kamernet email used to log in if no valid storage state is available
- `KAMERNET_AUTOREPLY_PASSWORD`: optional, Kamernet password used with `KAMERNET_AUTOREPLY_EMAIL`
- `KAMERNET_AUTOREPLY_HEADLESS`: optional, set to `false` to show the browser while debugging, defaults to `true`
- `KAMERNET_AUTOREPLY_DRY_RUN`: optional, records auto-reply attempts without submitting them, defaults to `false`
- `PARARIUS_SCRAPER_TIMEOUT_SECONDS`: optional, timeout for Pararius, defaults to `20`
- `FUNDA_SCRAPER_TIMEOUT_SECONDS`: optional, outer timeout for Funda scans, defaults to `25`
- `FUNDA_PYFUNDA_TIMEOUT_SECONDS`: optional, timeout passed to `pyfunda`, defaults to `12`
- `FUNDA_PYFUNDA_MAX_RETRIES`: optional, retry count passed to `pyfunda`, defaults to `2`
- `FUNDA_PYFUNDA_RETRY_BACKOFF_SECONDS`: optional, retry backoff passed to `pyfunda`, defaults to `0.1`
- `FUNDA_MAX_BACKGROUND_THREADS`: optional, max active `pyfunda` worker threads, defaults to `1`
- `ROOFZ_SCRAPER_TIMEOUT_SECONDS`: optional, timeout for Roofz browser automation, defaults to `90`
- `ROOFZ_ENABLED`: optional, set to `false` to disable Roofz, defaults to `true`
- `VVA_MAX_PAGES_PER_SCAN`: optional, max VVA result pages fetched per scan, defaults to `1`
- `MAX_CONCURRENT_USERS_PER_JOB`: optional, active users scanned in parallel per scheduled job, defaults to `3`
- `DB_PATH`: optional, SQLite database path, defaults to `listings.db`
- `SQLITE_BUSY_TIMEOUT_MS`: optional, SQLite lock wait timeout, defaults to `5000`
- `TELEGRAM_ALLOWED_CHAT_IDS`: optional, comma-separated Telegram chat IDs allowed to use the bot. Leave empty for local unrestricted use.

### 5. Start the bot

```bash
python main.py
```

Expected startup message:

```text
Bot started. Press Ctrl+C to stop.
```

On first boot the bot automatically creates the SQLite database and its tables.

## First Use In Telegram

1. Open your bot in Telegram.
2. Send `/start`.
3. Send `/search` to configure:
   - Rental preferences. Tap one or more options, then tap `Done`.
   - max monthly rent
   - minimum bedrooms/rooms
   - minimum surface area in square meters
4. Send `/test` to trigger an immediate scan.

After that, the scheduled scanner will keep running in the background while the process stays alive.

## Available Commands

- `/start` - initialize the bot and show help
- `/help` - show available commands and options
- `/commands` - same as `/help`
- `/search` - save or update filters
- `/filters` - show current filters
- `/test` - run a scan immediately
- `/autoreply_on` - automatically reply to new matching Kamernet listings
- `/autoreply_off` - disable Kamernet auto-reply
- `/autoreply_status` - show Kamernet auto-reply state and attempt counts
- `/autoreply_template` - show or set the Kamernet reply text
- `/pause` - pause notifications
- `/resume` - resume notifications
- `/clear` - clear the seen listings database
- `/cancel` - cancel the filter setup flow

`/search` options:

- Rental preferences: `Any property type`, `Room`, `Apartment`, `Studio`, `Anti-squat`, `Student Housing`, `Furnished`, `Short Term`, `Long Term`
- `Apartment` applies to Kamernet, Huurwoningen, Pararius, and Funda.
- `Furnished` applies to Kamernet, Huurwoningen, and Pararius; Funda does not expose a reliable furnishing filter.
- `Short Term` and `Long Term` remain Kamernet-only.
- Max rent: number in EUR, or `0` for no limit
- Minimum bedrooms/rooms: number, or `0` for no minimum
- Minimum size: number in m2, or `0` for no minimum

## Kamernet Auto-Reply

Kamernet auto-reply is disabled by default and only runs after `/autoreply_on`.

The reply loop only fires for listings that already passed your saved filters and were deduplicated as new for your Telegram user. It also keeps a separate `kamernet_auto_replies` table so the same listing is not auto-replied twice.

Set your message with:

```text
/autoreply_template Hello, I am interested in this place and would like to schedule a viewing. Kind regards
```

Optional placeholders are available in the template: `{title}`, `{price}`, `{address}`, `{url}`, `{city}`, `{rooms}`, and `{size}`.

The auto-reply browser needs a normal logged-in Kamernet session. You can either provide `KAMERNET_AUTOREPLY_EMAIL` and `KAMERNET_AUTOREPLY_PASSWORD` in `.env`, or point `KAMERNET_AUTOREPLY_STORAGE_STATE_PATH` at an existing Playwright storage-state file. On the VPS, leave `KAMERNET_AUTOREPLY_STORAGE_STATE_PATH` unset unless you need a custom path, so the saved session lives beside the production database and survives deploys. If Kamernet asks for human verification, login, or payment/premium access, the bot stops that reply attempt and sends a Telegram failure notice instead of trying to bypass it.

## How the bot works

1. `main.py` starts the Telegram application.
2. `bot.py` registers commands and schedules the recurring scan job.
3. `scanner.py` runs selected scrapers for each active user, with all enabled source groups scheduled on the same fast cadence.
4. `db.py` stores filters and deduplicates listings in SQLite.

When a source returns HTTP 403 three times in a row, the scanner stops contacting that source for 6 hours. A failed recovery probe increases the cooldown to 12 hours and then 24 hours; a successful response clears the cooldown immediately.

## Project Structure

```text
.
|-- bot.py
|-- config.py
|-- db.py
|-- kamernet_autoreply.py
|-- main.py
|-- pyproject.toml
|-- scanner.py
|-- scrapers/
|   |-- base.py
|   |-- funda.py
|   |-- http_clients.py
|   |-- huurwoningen.py
|   |-- kamernet.py
|   |-- pararius.py
|   |-- roofz.py
|   `-- vva.py
`-- uv.lock
```

## Troubleshooting

### `TELEGRAM_TOKEN not found`

Your `.env` file is missing or the token is empty.

### No listings are being sent

- Make sure you ran `/start` and `/search`
- Run `/test` to check whether listings are available right now
- Verify that your rental preferences, rent, bedroom/room, and size filters are not too restrictive

### I want to start fresh

Delete `listings.db`, or use `/clear` to clear previously seen listings.

## Run Without VS Code

The bot does not depend on VS Code. Any terminal is fine as long as the virtual environment is active and `.env` is configured.

## Run On A DigitalOcean VPS

The bot uses Telegram polling, so the VPS does not need a public web port for the bot. Keep SSH open for deployment and make sure the droplet can make outbound HTTPS requests.

This setup assumes:

- Ubuntu droplet
- SSH access as `root`
- Your local `.env` contains a valid `TELEGRAM_TOKEN`
- Your local `.env` contains `TELEGRAM_ALLOWED_CHAT_IDS` if the VPS bot should be private
- The VPS should start with a fresh SQLite database

### Deploy From Windows PowerShell

From the project root:

```powershell
.\scripts\deploy.ps1 -Host YOUR_DROPLET_IP
```

If your SSH key is not the default key:

```powershell
.\scripts\deploy.ps1 -Host YOUR_DROPLET_IP -IdentityFile C:\path\to\key
```

The deploy script uploads the project to `/opt/amsterdam-house-bot`, uploads `.env` to `/etc/amsterdam-house-bot/bot.env`, creates `/var/lib/amsterdam-house-bot/listings.db` on first boot, installs dependencies, and starts a `systemd` service.

The bootstrap pins `uv` to version `0.8.15`, verifies the downloaded binary checksum, installs Python `3.13.7`, and runs `uv sync --locked` so Python dependencies come from `uv.lock`. The local `.env`, `.venv`, `.git`, `__pycache__`, local database files, and the default Kamernet storage-state file are excluded from the code archive. The `.env` file is uploaded separately as the service environment file. During VPS setup, any `DB_PATH=` value from `.env` is removed so production always uses `/var/lib/amsterdam-house-bot/listings.db` and deployments do not reset sent-listing history. If an older deploy created `/opt/amsterdam-house-bot/listings.db`, the bootstrap script migrates it before replacing app files.

### Manage The VPS Service

SSH into the droplet:

```bash
ssh root@YOUR_DROPLET_IP
```

Check status:

```bash
systemctl status amsterdam-house-bot
```

Follow logs:

```bash
journalctl -u amsterdam-house-bot -f
```

Restart the bot:

```bash
systemctl restart amsterdam-house-bot
```

Stop the bot:

```bash
systemctl stop amsterdam-house-bot
```

Show recent logs:

```bash
journalctl -u amsterdam-house-bot -n 100 --no-pager
```

### After Deployment

Open Telegram and send:

```text
/start
/search
/test
```

The bot will continue running after you close your terminal because `systemd` owns the process.
