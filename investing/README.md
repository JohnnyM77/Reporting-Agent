# JM Investing — Simply Wall St Data Warehouse

Central SQLite data warehouse for Bob, Wally and Sally.
Source: Simply Wall St CSV exports (one file per ticker).

## Database location

The live database lives in **Google Drive → Investing/** as `jm_investing_sws.sqlite`.
It is **not** stored in this repo (`.gitignore` excludes `*.sqlite` except test fixtures).

A small test fixture database (`tests/fixtures/test_sws.sqlite`) is committed
for CI/integration tests.

## Quick start

```bash
# Install dependencies
pip install -r requirements-investing.txt

# Import a folder of SWS CSV exports
python investing/import_sws_csv.py /path/to/sws_exports/ --db /path/to/jm_investing_sws.sqlite

# Import a single file
python investing/import_sws_csv.py ASX_BHP.csv --db jm_investing_sws.sqlite

# Dry run (no writes)
python investing/import_sws_csv.py /path/to/sws_exports/ --dry-run

# Force re-import (even if file hash already exists)
python investing/import_sws_csv.py ASX_BHP.csv --force
```

## CSV format

Each SWS export is named like `ASX_BHP.csv` (exchange underscore ticker).

- Column 0: `metric_name`
- Columns 1+: values

Rows ending in `_date` pair with the matching non-`_date` row to form timeseries.

Example:
```
dividend_historical_dividend_payments_date,1455667200,1471392000,...
dividend_historical_dividend_payments,1.24,0.32,...
```

Timestamps are Unix seconds (or milliseconds if > 1e10) and are converted to UTC dates.

## Database schema

| Table | Purpose |
|---|---|
| `companies` | One row per ticker — exchange, first/last import time |
| `import_log` | Audit trail of every import, SHA-256 deduplication |
| `sws_raw_rows` | Verbatim preservation of every CSV row |
| `sws_scalar_metrics` | Single-value metrics (numeric or text) |
| `sws_timeseries` | Time-indexed series — one row per (ticker, metric, date) |

### Views

| View | Purpose |
|---|---|
| `v_company_import_summary` | Latest import status per ticker |
| `v_key_scalar_metrics` | Curated subset of the most useful metrics |
| `v_eps_history` | Historical EPS per ticker |
| `v_eps_forecasts` | Analyst consensus EPS forecasts (consensus / high / low) |
| `v_dividend_history` | Dividend payment history |

## Files

| File | Purpose |
|---|---|
| `schema.sql` | Full DDL — tables, indexes, views |
| `import_sws_csv.py` | CLI importer (accepts files or folder) |
| `example_queries.sql` | Ready-to-run analysis queries |
| `tests/test_import.py` | Integration tests |
| `tests/fixtures/` | Sample CSVs and reference database |

## Google Drive sync

After importing, upload the database to Drive:

```python
from google_drive_uploader import upload_file
upload_file("jm_investing_sws.sqlite", folder_name="Investing")
```

Or run:
```bash
python scripts/sync_investing_db.py
```

## Integration with Wally and Sally

Wally's `fundamentals_store.py` reads from the SWS database via:
```python
from investing.sws_reader import get_eps_history, get_eps_forecasts
```

This replaces unreliable live API calls with the pre-imported, curated SWS data.
`sws_reader.py` is the thin adapter layer — see that file for the full API.

## Metric naming convention

Metrics are prefixed by category:

| Prefix | Category |
|---|---|
| `value_` | Valuation (P/E, P/B, intrinsic value) |
| `past_` | Historical performance (EPS, revenue, margins) |
| `future_` | Analyst forecasts |
| `dividend_` | Dividend metrics |
| `health_` | Balance sheet / financial health |
| `management_` | Management quality |
| `misc_` | Price, volatility, analyst coverage |
| `industry_averages_` | Peer comparison benchmarks |

## Deduplication

Imports are deduplicated by SHA-256 hash of the raw CSV bytes.
Re-importing the same file is a no-op unless `--force` is passed.
