#!/usr/bin/env python3
"""
investing/import_sws_csv.py
───────────────────────────
Import Simply Wall St CSV exports into the JM Investing SQLite database.

CSV format (one file per ticker, e.g. ASX_BHP.csv):
  - Column 0: metric_name
  - Columns 1+: values (may be a single scalar or multi-value for timeseries)
  - Rows ending in _date pair with the matching non-_date row

Usage:
  python import_sws_csv.py <csv_or_folder> [<csv_or_folder> ...] [options]

Options:
  --db PATH       Path to SQLite file (default: jm_investing_sws.sqlite)
  --exchange STR  Exchange code override, e.g. ASX (default: inferred from filename)
  --dry-run       Parse and report without writing to the database
  --verbose       Print per-row detail
  --force         Re-import even if file hash already exists

Exit codes:
  0  all files imported successfully
  1  one or more files failed or were skipped
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import io
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Schema SQL (kept in sync with schema.sql)
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS companies (
    ticker              TEXT PRIMARY KEY,
    exchange            TEXT NOT NULL DEFAULT 'ASX',
    source              TEXT NOT NULL DEFAULT 'Simply Wall St',
    first_imported_at   TEXT,
    last_imported_at    TEXT
);

CREATE TABLE IF NOT EXISTS import_log (
    import_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT    NOT NULL,
    source_file         TEXT    NOT NULL,
    file_hash           TEXT    NOT NULL,
    imported_at         TEXT    NOT NULL,
    raw_rows            INTEGER NOT NULL DEFAULT 0,
    scalar_rows         INTEGER NOT NULL DEFAULT 0,
    timeseries_rows     INTEGER NOT NULL DEFAULT 0,
    status              TEXT    NOT NULL DEFAULT 'ok',
    warning             TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_import_log_hash ON import_log (file_hash);

CREATE TABLE IF NOT EXISTS sws_raw_rows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    metric_name     TEXT    NOT NULL,
    value_index     INTEGER NOT NULL,
    raw_value       TEXT,
    import_id       INTEGER NOT NULL REFERENCES import_log(import_id),
    source_file     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_raw_ticker_metric ON sws_raw_rows (ticker, metric_name);

CREATE TABLE IF NOT EXISTS sws_scalar_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    metric_name     TEXT    NOT NULL,
    value           REAL,
    raw_value       TEXT,
    import_id       INTEGER NOT NULL REFERENCES import_log(import_id),
    source_file     TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_scalar_ticker_metric
    ON sws_scalar_metrics (ticker, metric_name);
CREATE INDEX IF NOT EXISTS ix_scalar_ticker ON sws_scalar_metrics (ticker);

CREATE TABLE IF NOT EXISTS sws_timeseries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    metric_name     TEXT    NOT NULL,
    date_unix       INTEGER NOT NULL,
    date_utc        TEXT    NOT NULL,
    value           REAL,
    raw_value       TEXT,
    value_index     INTEGER,
    import_id       INTEGER NOT NULL REFERENCES import_log(import_id),
    source_file     TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ts_ticker_metric_date
    ON sws_timeseries (ticker, metric_name, date_unix);
CREATE INDEX IF NOT EXISTS ix_ts_ticker_metric ON sws_timeseries (ticker, metric_name);

CREATE VIEW IF NOT EXISTS v_company_import_summary AS
SELECT c.ticker, c.exchange, c.last_imported_at, l.source_file, l.raw_rows,
       l.scalar_rows, l.timeseries_rows, l.status
FROM companies c
JOIN import_log l ON l.ticker = c.ticker
WHERE l.import_id IN (SELECT MAX(import_id) FROM import_log GROUP BY ticker);

CREATE VIEW IF NOT EXISTS v_key_scalar_metrics AS
SELECT ticker, metric_name, value
FROM sws_scalar_metrics
WHERE metric_name IN (
    'value_pe','value_pb','value_price_to_sales','value_ev_to_ebitda',
    'value_intrinsic_discount','value_npv_per_share','value_last_share_price',
    'value_market_cap','value_price_target','value_price_target_low','value_price_target_high',
    'past_earnings_per_share','past_earnings_per_share_1y','past_earnings_per_share_3y',
    'past_earnings_per_share_5y','past_earnings_per_share_growth_1y',
    'past_revenue','past_revenue_growth_1y','past_revenue_growth_3y',
    'past_return_on_equity','past_return_on_assets','past_net_income_margin',
    'past_ebit_margin','past_gross_profit_margin',
    'future_earnings_per_share_1y','future_earnings_per_share_2y','future_earnings_per_share_3y',
    'future_earnings_per_share_growth_1y','future_earnings_per_share_growth_3y',
    'future_revenue_1y','future_revenue_growth_1y','future_revenue_growth_3y',
    'future_return_on_equity_1y','future_return_on_equity_3y',
    'dividend_dividend_yield','dividend_dividend_yield_future','dividend_payout_ratio',
    'dividend_buyback_yield','dividend_total_shareholder_yield',
    'health_debt_to_equity_ratio','health_net_debt_to_equity','health_current_solvency_ratio',
    'health_net_interest_cover','health_total_debt','health_total_equity',
    'health_total_assets','health_cash_st_investments','health_levered_free_cash_flow'
);

CREATE VIEW IF NOT EXISTS v_eps_forecasts AS
SELECT ticker, metric_name, date_utc, value AS eps
FROM sws_timeseries
WHERE metric_name IN (
    'future_merged_future_earnings_per_share',
    'future_merged_future_earnings_per_share_high',
    'future_merged_future_earnings_per_share_low'
)
ORDER BY ticker, metric_name, date_utc;

CREATE VIEW IF NOT EXISTS v_dividend_history AS
SELECT ticker, date_utc, value AS dividend_per_share
FROM sws_timeseries
WHERE metric_name = 'dividend_historical_dividend_payments'
ORDER BY ticker, date_utc;

CREATE VIEW IF NOT EXISTS v_revenue_history AS
SELECT ticker,
       CAST(substr(date_utc, 1, 4) AS INTEGER) AS report_year,
       value AS revenue
FROM sws_timeseries
WHERE metric_name = 'past_revenue_ltm_history'
  AND value IS NOT NULL
ORDER BY ticker, report_year;

CREATE VIEW IF NOT EXISTS v_net_income_history AS
SELECT ticker,
       CAST(substr(date_utc, 1, 4) AS INTEGER) AS report_year,
       value AS net_income
FROM sws_timeseries
WHERE metric_name = 'past_net_income_ltm_history'
  AND value IS NOT NULL
ORDER BY ticker, report_year;

CREATE VIEW IF NOT EXISTS v_eps_history AS
SELECT ticker,
       CAST(substr(date_utc, 1, 4) AS INTEGER) AS report_year,
       value AS eps
FROM sws_timeseries
WHERE metric_name = 'past_earnings_per_share_ltm_history'
  AND value IS NOT NULL
ORDER BY ticker, report_year;

CREATE VIEW IF NOT EXISTS v_company_fundamentals_10y AS
SELECT
    r.ticker,
    r.report_year,
    r.revenue,
    ni.net_income,
    e.eps
FROM v_revenue_history r
LEFT JOIN v_net_income_history ni ON ni.ticker = r.ticker AND ni.report_year = r.report_year
LEFT JOIN v_eps_history e ON e.ticker = r.ticker AND e.report_year = r.report_year
ORDER BY r.ticker, r.report_year;

CREATE VIEW IF NOT EXISTS v_latest_company_snapshot AS
SELECT
    e.ticker,
    r.revenue   AS latest_revenue,
    ni.net_income AS latest_net_income,
    e.eps       AS latest_eps,
    (SELECT SUM(value)
     FROM sws_timeseries dh
     WHERE dh.ticker = e.ticker
       AND dh.metric_name = 'dividend_historical_dividend_payments'
       AND substr(dh.date_utc, 1, 4) = CAST(e.report_year AS TEXT)
    ) AS latest_dividend,
    (SELECT value FROM sws_scalar_metrics sm
     WHERE sm.ticker = e.ticker AND sm.metric_name = 'past_return_on_equity') AS latest_roe,
    (SELECT value FROM sws_scalar_metrics sm
     WHERE sm.ticker = e.ticker AND sm.metric_name = 'health_debt_to_equity_ratio') AS latest_debt_equity
FROM (
    SELECT ticker, MAX(report_year) AS report_year
    FROM v_eps_history
    GROUP BY ticker
) latest
JOIN v_eps_history e ON e.ticker = latest.ticker AND e.report_year = latest.report_year
LEFT JOIN v_revenue_history r ON r.ticker = latest.ticker AND r.report_year = latest.report_year
LEFT JOIN v_net_income_history ni ON ni.ticker = latest.ticker AND ni.report_year = latest.report_year;
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _to_float(s: str) -> Optional[float]:
    """Parse a string to float; return None on failure."""
    if s is None or s.strip() == "":
        return None
    try:
        return float(s.strip())
    except (ValueError, TypeError):
        return None


def _unix_to_utc(ts: int) -> str:
    """Convert Unix timestamp (seconds or milliseconds) to YYYY-MM-DD."""
    if ts > 1e10:  # milliseconds
        ts = ts // 1000
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")


def _infer_ticker_exchange(filename: str) -> tuple[str, str]:
    """
    Infer ticker and exchange from filename.
    ASX_BHP.csv → ('BHP', 'ASX')
    NYSE_AAPL.csv → ('AAPL', 'NYSE')
    BHP.csv → ('BHP', 'ASX')   # default exchange
    """
    stem = Path(filename).stem.upper()
    # Expect EXCHANGE_TICKER or just TICKER
    parts = stem.split("_", 1)
    known_exchanges = {"ASX", "NYSE", "NASDAQ", "LSE", "TSE", "HKEX", "NZX"}
    if len(parts) == 2 and parts[0] in known_exchanges:
        return parts[1], parts[0]
    return stem, "ASX"


# ---------------------------------------------------------------------------
# CSV parsing
# ---------------------------------------------------------------------------

def parse_sws_csv(path: Path) -> dict:
    """
    Parse a Simply Wall St CSV export.

    Returns:
      {
        "ticker": str,
        "exchange": str,
        "raw_rows": [(metric_name, value_index, raw_value), ...],
        "scalar_metrics": [(metric_name, value, raw_value), ...],
        "timeseries": [(metric_name, date_unix, date_utc, value, raw_value, value_index), ...],
        "warnings": [str, ...]
      }
    """
    ticker, exchange = _infer_ticker_exchange(path.name)
    warnings: list[str] = []

    # Read all rows; handle BOM and variable column counts
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows_by_metric: dict[str, list[str]] = {}
    raw_rows: list[tuple[str, int, str]] = []

    for raw_row in reader:
        if not raw_row:
            continue
        metric_name = raw_row[0].strip()
        if not metric_name:
            continue
        values = raw_row[1:]  # may be empty, single, or multi-value
        rows_by_metric[metric_name] = values
        for idx, val in enumerate(values):
            raw_rows.append((metric_name, idx, val if val else None))

    # Extract base fiscal year for offset-based metrics (offset 0 = this year)
    _base_year_raw = rows_by_metric.get("past_latest_fiscal_year", [])
    _base_year_str = next((v.strip() for v in _base_year_raw if v.strip()), None)
    try:
        base_fiscal_year = int(_base_year_str) if _base_year_str else None
    except (ValueError, TypeError):
        base_fiscal_year = None

    # Identify _date rows and pair them with value rows
    date_metrics: dict[str, list[str]] = {}
    for metric_name, values in rows_by_metric.items():
        if metric_name.endswith("_date"):
            base = metric_name[: -len("_date")]
            date_metrics[base] = values

    # Identify _offset rows (e.g. past_revenue_ltm_history_offset) and pair with value rows
    offset_metrics: dict[str, list[str]] = {}
    for metric_name, values in rows_by_metric.items():
        if metric_name.endswith("_offset"):
            base = metric_name[: -len("_offset")]
            offset_metrics[base] = values

    scalar_metrics: list[tuple[str, Optional[float], str]] = []
    timeseries: list[tuple[str, int, str, Optional[float], str, int]] = []
    paired_bases: set[str] = set()

    for base_metric, date_values in date_metrics.items():
        value_row = rows_by_metric.get(base_metric)
        if value_row is None:
            # No paired value row — store the date itself as a scalar ISO date string
            non_empty_dates = [d.strip() for d in date_values if d.strip()]
            if len(non_empty_dates) == 1:
                try:
                    ts = int(float(non_empty_dates[0]))
                    scalar_metrics.append((f"{base_metric}_date", None, _unix_to_utc(ts)))
                    paired_bases.add(f"{base_metric}_date")
                except (ValueError, TypeError):
                    pass
            continue

        paired_bases.add(base_metric)
        paired_bases.add(f"{base_metric}_date")

        for idx, (d_str, v_str) in enumerate(zip(date_values, value_row)):
            d_str = d_str.strip()
            if not d_str:
                continue
            try:
                ts = int(float(d_str))
            except (ValueError, TypeError):
                warnings.append(f"Non-numeric date '{d_str}' in {base_metric}_date[{idx}] — skipped")
                continue
            date_utc = _unix_to_utc(ts)
            value = _to_float(v_str)
            timeseries.append((base_metric, ts, date_utc, value, v_str.strip() if v_str else None, idx))

    # Process _offset paired rows → timeseries using fiscal year anchor
    for base_metric, offset_values in offset_metrics.items():
        value_row = rows_by_metric.get(base_metric)
        if value_row is None or base_fiscal_year is None:
            continue

        paired_bases.add(base_metric)
        paired_bases.add(f"{base_metric}_offset")

        for idx, (o_str, v_str) in enumerate(zip(offset_values, value_row)):
            o_str = o_str.strip()
            if not o_str:
                continue
            try:
                offset = int(float(o_str))
            except (ValueError, TypeError):
                continue
            report_year = base_fiscal_year + offset
            # Use June 30 as the synthetic fiscal year-end date; encode as unix timestamp
            date_utc = f"{report_year}-06-30"
            import calendar
            ts = int(calendar.timegm((report_year, 6, 30, 0, 0, 0, 0, 0, 0)))
            value = _to_float(v_str)
            timeseries.append((base_metric, ts, date_utc, value, v_str.strip() if v_str else None, idx))

    # Everything else → scalar or multi-value scalar
    for metric_name, values in rows_by_metric.items():
        if metric_name in paired_bases:
            continue
        non_empty = [v.strip() for v in values if v.strip() != ""]
        if len(non_empty) == 0:
            scalar_metrics.append((metric_name, None, None))
        elif len(non_empty) == 1:
            scalar_metrics.append((metric_name, _to_float(non_empty[0]), non_empty[0]))
        else:
            # Multi-value non-timeseries (e.g. quoted strings, offset arrays)
            # Store as scalar with joined raw value and no numeric
            joined = ",".join(non_empty)
            scalar_metrics.append((metric_name, None, joined))

    return {
        "ticker": ticker,
        "exchange": exchange,
        "raw_rows": raw_rows,
        "scalar_metrics": scalar_metrics,
        "timeseries": timeseries,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def open_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    # Drop and recreate views so schema changes take effect on re-runs
    _views = [
        "v_company_import_summary", "v_key_scalar_metrics",
        "v_eps_forecasts", "v_dividend_history",
        "v_revenue_history", "v_net_income_history", "v_eps_history",
        "v_company_fundamentals_10y", "v_latest_company_snapshot",
    ]
    for v in _views:
        con.execute(f"DROP VIEW IF EXISTS {v}")
    for stmt in _SCHEMA_SQL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                pass  # table already exists etc.
    con.commit()
    return con


def import_file(
    con: sqlite3.Connection,
    path: Path,
    exchange_override: Optional[str] = None,
    force: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    """
    Import a single SWS CSV file into *con*.

    Returns a result dict with keys: ticker, status, warning, scalar_rows, timeseries_rows.
    """
    file_hash = _sha256(path)
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Duplicate check
    existing = con.execute(
        "SELECT import_id, imported_at FROM import_log WHERE file_hash = ?", (file_hash,)
    ).fetchone()
    if existing and not force:
        return {
            "ticker": path.stem,
            "status": "skipped",
            "warning": f"Already imported on {existing[1]} (import_id={existing[0]}). Use --force to re-import.",
            "scalar_rows": 0,
            "timeseries_rows": 0,
        }

    parsed = parse_sws_csv(path)
    ticker = parsed["ticker"]
    exchange = exchange_override or parsed["exchange"]

    if verbose:
        print(f"  [{ticker}] raw={len(parsed['raw_rows'])} scalars={len(parsed['scalar_metrics'])} ts={len(parsed['timeseries'])}")
        for w in parsed["warnings"]:
            print(f"  [{ticker}] WARN: {w}")

    if dry_run:
        return {
            "ticker": ticker,
            "status": "dry-run",
            "warning": None,
            "scalar_rows": len(parsed["scalar_metrics"]),
            "timeseries_rows": len(parsed["timeseries"]),
        }

    warning_text = "; ".join(parsed["warnings"]) if parsed["warnings"] else None

    try:
        with con:
            # Remove old data for this ticker (UPSERT-style full refresh)
            if force and existing:
                old_id = existing[0]
                con.execute("DELETE FROM sws_raw_rows WHERE import_id = ?", (old_id,))
                con.execute("DELETE FROM sws_scalar_metrics WHERE ticker = ?", (ticker,))
                con.execute("DELETE FROM sws_timeseries WHERE ticker = ?", (ticker,))
                con.execute("DELETE FROM import_log WHERE import_id = ?", (old_id,))

            # Insert import_log row
            cur = con.execute(
                """INSERT INTO import_log
                   (ticker, source_file, file_hash, imported_at, raw_rows,
                    scalar_rows, timeseries_rows, status, warning)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    ticker, path.name, file_hash, now_utc,
                    len(parsed["raw_rows"]),
                    len(parsed["scalar_metrics"]),
                    len(parsed["timeseries"]),
                    "warning" if parsed["warnings"] else "ok",
                    warning_text,
                ),
            )
            import_id = cur.lastrowid

            # Raw rows
            con.executemany(
                "INSERT INTO sws_raw_rows (ticker, metric_name, value_index, raw_value, import_id, source_file) VALUES (?,?,?,?,?,?)",
                [(ticker, m, i, v, import_id, path.name) for m, i, v in parsed["raw_rows"]],
            )

            # Scalar metrics (INSERT OR REPLACE for re-imports)
            con.executemany(
                """INSERT OR REPLACE INTO sws_scalar_metrics
                   (ticker, metric_name, value, raw_value, import_id, source_file)
                   VALUES (?,?,?,?,?,?)""",
                [(ticker, m, v, rv, import_id, path.name) for m, v, rv in parsed["scalar_metrics"]],
            )

            # Timeseries (INSERT OR REPLACE for re-imports)
            con.executemany(
                """INSERT OR REPLACE INTO sws_timeseries
                   (ticker, metric_name, date_unix, date_utc, value, raw_value, value_index, import_id, source_file)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [(ticker, m, du, dt, v, rv, vi, import_id, path.name)
                 for m, du, dt, v, rv, vi in parsed["timeseries"]],
            )

            # Upsert company
            con.execute(
                """INSERT INTO companies (ticker, exchange, source, first_imported_at, last_imported_at)
                   VALUES (?,?,'Simply Wall St',?,?)
                   ON CONFLICT(ticker) DO UPDATE SET
                       exchange = excluded.exchange,
                       last_imported_at = excluded.last_imported_at""",
                (ticker, exchange, now_utc, now_utc),
            )

    except Exception as exc:
        return {
            "ticker": ticker,
            "status": "error",
            "warning": str(exc),
            "scalar_rows": 0,
            "timeseries_rows": 0,
        }

    return {
        "ticker": ticker,
        "status": "warning" if parsed["warnings"] else "ok",
        "warning": warning_text,
        "scalar_rows": len(parsed["scalar_metrics"]),
        "timeseries_rows": len(parsed["timeseries"]),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def collect_csv_paths(inputs: list[str]) -> list[Path]:
    """Expand files and directories into a sorted list of CSV paths."""
    paths: list[Path] = []
    for inp in inputs:
        p = Path(inp)
        if p.is_dir():
            paths.extend(sorted(p.glob("*.csv")))
        elif p.is_file() and p.suffix.lower() == ".csv":
            paths.append(p)
        else:
            print(f"WARNING: '{inp}' is not a CSV file or directory — skipped", file=sys.stderr)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import Simply Wall St CSV exports into the JM Investing SQLite database."
    )
    parser.add_argument("inputs", nargs="+", help="CSV files or directories")
    parser.add_argument("--db", default="jm_investing_sws.sqlite", help="SQLite database path")
    parser.add_argument("--exchange", help="Exchange override (e.g. ASX)")
    parser.add_argument("--dry-run", action="store_true", help="Parse without writing")
    parser.add_argument("--verbose", action="store_true", help="Verbose per-row output")
    parser.add_argument("--force", action="store_true", help="Re-import even if hash exists")
    args = parser.parse_args(argv)

    csv_paths = collect_csv_paths(args.inputs)
    if not csv_paths:
        print("No CSV files found.", file=sys.stderr)
        return 1

    db_path = Path(args.db)
    if not args.dry_run:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"Database: {db_path.resolve()}")
    else:
        print("[DRY RUN] No writes will occur.")

    con = open_db(db_path)

    results: list[dict] = []
    for csv_path in csv_paths:
        print(f"Importing {csv_path.name} ...", end=" ", flush=True)
        result = import_file(
            con, csv_path,
            exchange_override=args.exchange,
            force=args.force,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        results.append(result)
        status = result["status"]
        ticker = result["ticker"]
        print(f"{ticker}: {status} (scalars={result['scalar_rows']}, ts={result['timeseries_rows']})")
        if result.get("warning"):
            print(f"  ! {result['warning']}")

    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()

    # Summary
    ok = sum(1 for r in results if r["status"] in ("ok", "warning"))
    skipped = sum(1 for r in results if r["status"] == "skipped")
    errors = sum(1 for r in results if r["status"] == "error")
    total_scalars = sum(r["scalar_rows"] for r in results)
    total_ts = sum(r["timeseries_rows"] for r in results)

    print()
    print("=" * 50)
    print(f"Import complete: {ok} ok, {skipped} skipped, {errors} errors")
    print(f"Total scalar metrics: {total_scalars}")
    print(f"Total timeseries rows: {total_ts}")
    print("=" * 50)

    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
