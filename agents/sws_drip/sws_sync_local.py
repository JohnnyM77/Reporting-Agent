"""
agents/sws_drip/sws_sync_local.py
───────────────────────────────────
Scan data/sws_csv/ for CSV files and ingest any that haven't been imported yet.
Used when CSVs are manually downloaded from SWS and dropped into the folder.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .sws_ingest import ingest_csv
from .sws_queue import mark_attempt


def sync_local_csvs(db_path: Path, csv_dir: Path | None = None) -> dict:
    """
    Scan csv_dir for all ASX_*.csv files, check sws_import_log for which
    are already imported, and ingest any new ones.

    Returns:
        {
            'total_csvs': int,
            'already_imported': int,
            'newly_imported': int,
            'failures': [(filename, error_msg), ...]
        }
    """
    if csv_dir is None:
        # Default: data/sws_csv/ relative to repo root (2 levels up from this file)
        csv_dir = Path(__file__).resolve().parents[2] / "data" / "sws_csv"

    if not db_path.exists():
        raise FileNotFoundError(
            f"Database not found: {db_path}. "
            "Run --rebuild-queue first, or drop a CSV in data/sws_csv/ and re-run."
        )

    csv_files = sorted(csv_dir.glob("ASX_*.csv"))

    results: dict = {
        "total_csvs": len(csv_files),
        "already_imported": 0,
        "newly_imported": 0,
        "failures": [],
    }

    if not csv_files:
        return results

    # Fetch set of already-imported csv_path values from the log
    con = sqlite3.connect(db_path)
    imported_paths: set[str] = {
        row[0]
        for row in con.execute("SELECT csv_path FROM sws_import_log WHERE success=1").fetchall()
    }
    con.close()

    for csv_file in csv_files:
        # Match against both absolute and relative representations stored in the log
        canonical = str(csv_file.resolve())
        relative = str(csv_file)
        name_only = csv_file.name

        if any(
            p == canonical or p == relative or Path(p).name == name_only
            for p in imported_paths
        ):
            results["already_imported"] += 1
            # Ensure queue row reflects downloaded state (handles upgrade path where
            # previous --sync-local runs predated queue-update logic)
            mark_attempt(db_path, csv_file.stem.split("_", 1)[-1], success=True, csv_path=str(csv_file))
            continue

        try:
            result = ingest_csv(csv_file, db_path)
            if result["success"]:
                results["newly_imported"] += 1
                # Keep the queue in sync so this ticker isn't re-downloaded by Playwright
                mark_attempt(
                    db_path,
                    result["ticker"],
                    success=True,
                    csv_path=str(csv_file),
                )
            else:
                results["failures"].append(
                    (csv_file.name, result.get("error") or "Ingest returned success=False")
                )
        except Exception as e:
            results["failures"].append((csv_file.name, str(e)))

    return results
