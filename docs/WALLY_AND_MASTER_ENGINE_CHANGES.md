# Wally and Master Engine Alert Changes

## Overview

This document describes the recent enhancements to the Wally agent and the addition of the Master Engine Alert workflow to address user feedback.

## Changes Made

### 1. Enhanced Logging for Wally Data Fetching

**Problem:** Wally was not picking up tii75 stocks, and it wasn't clear where it was getting prices from or if it was "hallucinating."

**Solution:** Added comprehensive logging to `wally/data_fetch.py`:
- Logs successful price fetches with actual values
- Logs network errors when Yahoo Finance cannot be reached
- Logs when data is empty or invalid
- All logs are prefixed with `[wally/data_fetch]` for easy identification

**What Wally Uses:**
- **Primary Source:** Yahoo Finance via the `yfinance` Python library
- **Data Fetched:** 1-year daily close prices to calculate:
  - Current price (most recent close)
  - 52-week low (minimum close price over 1 year)
  - 52-week high (maximum close price over 1 year)
- **Requirements:** Internet access to `finance.yahoo.com` and `query1.finance.yahoo.com`

**Note:** If Wally runs in an environment without internet access or where Yahoo Finance is blocked, it will fail to fetch prices and log appropriate error messages. This is **not hallucination** - it's a connectivity issue.

### 2. Combined Email Support for Wally

**Problem:** Multiple watchlists generated separate emails, making it harder to review all alerts at once.

**Solution:** Added combined email functionality that consolidates all watchlist results into a single email:

#### New Features:
- **Combined email mode**: Process multiple watchlists and send one email
- **New CLI flags**:
  - `--combined-email`: Use with `--all-standard-watchlists` to combine standard watchlists
  - `--all-combined`: Process all watchlists (standard + TII75) and send one combined email

#### Updated Workflow:
The `wally_watchlists.yml` workflow now uses combined email mode:
- **Tuesday runs**: Standard watchlists (TII, JM, Aussie Tech) in one email
- **Friday runs**: All watchlists including TII75 (if gate allows) in one email
- **Manual triggers**: All watchlists including forced TII75 in one email

#### Email Format:
The combined email includes:
- Overall summary (total checked/flagged across all watchlists)
- Separate sections for each watchlist
- All charts and attachments from flagged stocks
- Clear visual separation between watchlists with horizontal rules

### 3. Master Engine Alert Workflow

**Problem:** The Master Engine Alert and Super Investor Agent were not visible in the GitHub Actions list and couldn't be run.

**Solution:** Created `.github/workflows/master_engine_alert.yml`:

#### Features:
- **Manual Trigger (workflow_dispatch)** with configurable options:
  - Include/exclude TII75 watchlist
  - Skip individual agents (Ned, Wally, Bob)
  - Skip email sending (generate digest only)
- **Scheduled Run**: Daily at 00:00 UTC (after all other agents complete)
- **Automatic Commit**: Saves digest files to the repository

#### How to Run:
1. Go to GitHub Actions tab
2. Select "Master Engine Alert (Super Investor)" workflow
3. Click "Run workflow"
4. Configure options as needed
5. Click "Run workflow" button

#### Where to Find Output:
- **Email**: Sent to configured `EMAIL_TO` address with subject "Johnny Master Investor Alert — {date} ({N} alert(s))"
- **Files**: Saved in `outputs/YYYY-MM-DD/`:
  - `master_investor_digest.html` - Full HTML digest
  - `master_investor_digest.md` - Markdown summary
  - `master_investor_events.json` - JSON archive
- **Logs**: Available in the GitHub Actions run details

## Usage Examples

### Running Wally with Combined Email (Local)

```bash
# Run standard watchlists with combined email
python -m wally.main --all-standard-watchlists --combined-email

# Run all watchlists (standard + TII75) with combined email
python -m wally.main --all-combined

# Force TII75 inclusion even if gated
python -m wally.main --all-combined --force
```

### Running Wally with Separate Emails (Legacy Mode)

```bash
# Run standard watchlists with separate emails
python -m wally.main --all-standard-watchlists

# Run TII75 separately
python -m wally.main --tii75
```

### Running Master Engine Alert (Local)

```bash
# Run with all agents
python run_master_investor.py

# Skip specific agents
python run_master_investor.py --no-ned --no-bob

# Include TII75 watchlist in Wally data
python run_master_investor.py --wally-tii75

# Generate digest without sending email
python run_master_investor.py --no-email

# Dry run (no files or emails)
python run_master_investor.py --dry-run
```

## Environment Variables Required

### For Wally:
- `EMAIL_FROM` or `EMAIL_USER` - Sender email address
- `EMAIL_TO` - Recipient email address(es)
- `EMAIL_APP_PASSWORD` or `SMTP_PASS` - Email password/app password
- `SMTP_HOST` (optional, defaults to smtp.gmail.com)
- `SMTP_PORT` (optional, defaults to 465)
- `SMTP_USER` (optional, defaults to EMAIL_FROM)

### For Master Engine Alert:
All of the above plus:
- `ANTHROPIC_API_KEY` - For Ned (Claude API)
- `OPENAI_API_KEY` - For Bob (OpenAI API)
- `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN`, `GDRIVE_FOLDER_ID` (optional, for Google Drive uploads)

## Troubleshooting

### Wally Not Finding Stocks

**Symptoms:**
- No stocks flagged when you expect some
- Error messages about network connectivity

**Possible Causes:**
1. **Network Issues**: Cannot reach Yahoo Finance
   - Check logs for "Failed to perform" or "Could not resolve host" errors
   - Verify internet connectivity
   - Check if Yahoo Finance is blocked

2. **Data Issues**: Yahoo Finance has no data for the ticker
   - Check logs for "Empty history" or "No Close prices" messages
   - Verify ticker symbols are correct (e.g., `WOW.AX` for ASX stocks)

3. **Stocks Not Within Threshold**: Stocks may not be within 5% of 52-week low
   - Check the actual prices in the logs
   - Current threshold is 5% (configurable via `WALLY_LOW_THRESHOLD_PCT`)

### Master Engine Alert Not Sending Email

**Possible Causes:**
1. **Email Credentials Missing**: Check that all required environment variables are set
2. **No Events to Report**: Check individual agent outputs (Ned, Wally, Bob)
3. **--no-email Flag Used**: Check if email sending was explicitly disabled

### Workflow Not Visible

If you don't see the workflows in the Actions tab:
1. Ensure the workflow files are in `.github/workflows/` directory
2. Check that the YAML is valid (use a YAML validator)
3. Push the changes to the default branch (usually `main` or `master`)
4. Refresh the GitHub Actions page

## Testing

To test the changes locally:

```bash
# Install dependencies
pip install -r requirements-wally.txt

# Test Wally with combined email (dry run - will fail at email send without credentials)
python -m wally.main --all-combined --force

# Check logs for price fetching details
# Should see "[wally/data_fetch] Successfully fetched..." or error messages
```

## Additional Notes

- The combined email functionality is **backward compatible** - the old separate email mode still works if you don't use the new flags
- Dashboard data (`docs/data/wally.json`) is still written the same way regardless of email mode
- The Master Engine Alert workflow is **independent** of individual agent workflows - it reads their output files
- TII75 fortnightly gating is still respected unless `--force` is used
