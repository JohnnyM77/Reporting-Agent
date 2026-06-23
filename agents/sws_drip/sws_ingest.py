"""
agents/sws_drip/sws_ingest.py
──────────────────────────────
Parse SWS CSV exports and upsert into sws_snapshots.

SWS CSVs are NOT standard tabular data — each row is (metric_name, value...)
where value may be a comma-separated timeseries array. We store each row as
a single metric with all value columns joined back into one string.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date
from pathlib import Path

from .sws_db_init import init_db


def ingest_csv(csv_path: Path, db_path: Path) -> dict:
    """
    Read an SWS CSV file and upsert all rows into sws_snapshots.

    Returns:
        {ticker, snapshot_date, rows_imported, success: bool}
    """
    # Infer ticker from filename: "ASX_BHP.csv" → "BHP"
    stem = csv_path.stem.upper()  # e.g. "ASX_BHP"
    parts = stem.split("_", 1)
    ticker = parts[1] if len(parts) == 2 else stem

    snapshot_date = date.today().isoformat()
    rows_imported = 0
    success = False
    error_msg: str | None = None

    try:
        text = csv_path.read_text(encoding="utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))

        rows: list[tuple[str, str, str, str | None]] = []
        for raw_row in reader:
            if not raw_row:
                continue
            metric = raw_row[0].strip()
            if not metric:
                continue
            # Join all value columns back; preserve the original multi-value structure
            value_cols = raw_row[1:]
            non_empty = [v for v in value_cols if v.strip()]
            value = ",".join(non_empty) if non_empty else None
            rows.append((ticker, snapshot_date, metric, value))

        con = init_db(db_path)
        con.executemany(
            """INSERT OR REPLACE INTO sws_snapshots
               (ticker, snapshot_date, metric, value)
               VALUES (?, ?, ?, ?)""",
            rows,
        )
        rows_imported = len(rows)

        # Write import log
        con.execute(
            """INSERT INTO sws_import_log (ticker, csv_path, imported_at, rows_imported, success)
               VALUES (?, ?, datetime('now'), ?, 1)""",
            (ticker, str(csv_path), rows_imported),
        )
        con.commit()
        con.close()
        success = True
        print(
            f"[sws_ingest] {ticker}: {rows_imported} metrics ingested from {csv_path.name}",
            flush=True,
        )

    except Exception as e:
        error_msg = str(e)
        print(f"[sws_ingest] ERROR ingesting {csv_path}: {e}", flush=True)
        try:
            con = init_db(db_path)
            con.execute(
                """INSERT INTO sws_import_log (ticker, csv_path, imported_at, rows_imported, success)
                   VALUES (?, ?, datetime('now'), 0, 0)""",
                (ticker, str(csv_path)),
            )
            con.commit()
            con.close()
        except Exception:
            pass

    return {
        "ticker": ticker,
        "snapshot_date": snapshot_date,
        "rows_imported": rows_imported,
        "success": success,
        "error": error_msg,
    }


def ingest_all_csvs(csv_dir: Path, db_path: Path) -> list[dict]:
    """Ingest all CSVs in csv_dir. Used for bulk re-build of the database."""
    results = []
    for csv_path in sorted(csv_dir.glob("*.csv")):
        results.append(ingest_csv(csv_path, db_path))
    return results
