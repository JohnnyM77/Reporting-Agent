# SWS Drip Bot

Downloads up to 2 Simply Wall St CSV exports per day from your SWS Unlimited account, ingests them into a local SQLite database, and commits the CSVs to the repo. Designed to backfill ~60 ASX tickers over ~4–6 weeks, then refresh automatically.

---

## First-time setup (run locally)

### 1. Install dependencies

```bash
pip install playwright pyyaml
playwright install chromium
```

### 2. Capture your SWS session

```bash
python -m agents.sws_drip.run_drip --setup
```

A browser window opens. Log into Simply Wall St normally. Once you see your dashboard, **close the browser**. The script saves `storage_state.json` to the repo root.

> **Keep `storage_state.json` secret.** It gives access to your SWS account. It is gitignored and should never be committed.

### 3. Encode the session for GitHub Secrets

**macOS:**
```bash
base64 -i storage_state.json | pbcopy
```

**Linux:**
```bash
base64 -w 0 storage_state.json
```

Paste the output into a GitHub Secret named **`SWS_STORAGE_STATE`**.

### 4. Set required GitHub Secrets

| Secret | Description |
|--------|-------------|
| `SWS_STORAGE_STATE` | Base64-encoded `storage_state.json` (primary auth) |
| `SWS_EMAIL` | Your SWS login email (fallback if session expires) |
| `SWS_PASSWORD` | Your SWS password (fallback if session expires) |
| `TELEGRAM_BOT_TOKEN` | For run notifications (existing) |
| `TELEGRAM_CHAT_ID` | For run notifications (existing) |

### 5. Populate the download queue

```bash
python -m agents.sws_drip.run_drip --rebuild-queue
```

This scans all watchlist YAMLs in `.github/Watchlist/` and `watchlists/` and populates `data/sws.db` with all ASX tickers.

### 6. Test locally

```bash
# See what would be downloaded (no browser)
python -m agents.sws_drip.run_drip --dry-run

# Download 1 CSV using your local session
python -m agents.sws_drip.run_drip --local --limit 1
```

---

## How it works

1. **Daily cron** runs at 23:30 UTC (09:30 Sydney AEST), weekdays
2. Picks the next 2 tickers from the queue (priority → missing → stale → oldest first)
3. Playwright opens a headless Chromium with your saved session (no login required)
4. Searches SWS for `ASX:<TICKER>`, clicks the three-dots menu, clicks "Download to CSV"
5. Saves CSV to `data/sws_csv/ASX_<TICKER>.csv` and commits to the repo
6. Ingests CSV into `data/sws.db` (SQLite, gitignored, rebuildable from CSVs anytime)
7. Sends Telegram notification with summary

---

## CLI reference

```bash
# One-time setup
python -m agents.sws_drip.run_drip --setup

# Rebuild queue from watchlist YAMLs
python -m agents.sws_drip.run_drip --rebuild-queue

# Dry run (prints next tickers, no download)
python -m agents.sws_drip.run_drip --dry-run [--limit 4]

# Normal run (CI) — uses SWS_STORAGE_STATE env var
python -m agents.sws_drip.run_drip [--limit 4]

# Local run — uses storage_state.json file
python -m agents.sws_drip.run_drip --local [--limit 1]

# Mark old downloads as stale (for periodic refresh)
python -m agents.sws_drip.run_drip --mark-stale [--stale-days 90]
```

---

## Rebuild the database from committed CSVs

The SQLite DB is gitignored but fully rebuildable:

```bash
python -c "
from agents.sws_drip.sws_ingest import ingest_all_csvs
from pathlib import Path
results = ingest_all_csvs(Path('data/sws_csv'), Path('data/sws.db'))
print(f'Ingested {sum(r[\"rows_imported\"] for r in results)} rows from {len(results)} files')
"
```

---

## Troubleshooting failures

1. Check Telegram for the failure message
2. Download the debug artifact from the failed GitHub Actions run
3. View `fail_<TICKER>_<step>.png` to see what the page looked like when it failed
4. View `fail_<TICKER>_<step>.html` to inspect the DOM
5. Update selectors in `sws_downloader.py` if SWS changed their UI

### When `storage_state.json` expires

SWS sessions expire after some time. The bot will send an urgent Telegram:

> 🚨 SWS Drip Bot — session expired

To refresh:
1. Run `--setup` locally
2. Re-encode: `base64 -i storage_state.json | pbcopy`
3. Update `SWS_STORAGE_STATE` in GitHub Secrets

---

## Querying the database

```bash
# PE ratio for BHP
sqlite3 data/sws.db "SELECT metric, value FROM sws_snapshots WHERE ticker='BHP' AND metric='value_pe'"

# Queue status summary
sqlite3 data/sws.db "SELECT status, COUNT(*) FROM sws_download_queue GROUP BY status"

# Latest download dates
sqlite3 data/sws.db "SELECT ticker, last_downloaded_at FROM sws_download_queue WHERE status='downloaded' ORDER BY last_downloaded_at DESC"
```

---

## Phase 2 (future)

- Non-ASX tickers (US, Japan, UK)
- Bob triggers priority bump on earnings results
- Wally consumes SWS snapshots from SQLite
- Automated session refresh
