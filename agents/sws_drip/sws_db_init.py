"""
agents/sws_drip/sws_db_init.py
───────────────────────────────
Idempotent SQLite schema setup for the SWS drip bot.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sws_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    snapshot_date   TEXT    NOT NULL,
    metric          TEXT    NOT NULL,
    value           TEXT,
    UNIQUE(ticker, snapshot_date, metric)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_ticker
    ON sws_snapshots(ticker);
CREATE INDEX IF NOT EXISTS idx_snapshot_metric
    ON sws_snapshots(metric);
CREATE INDEX IF NOT EXISTS idx_snapshot_ticker_metric
    ON sws_snapshots(ticker, metric);

CREATE TABLE IF NOT EXISTS sws_download_queue (
    ticker              TEXT    PRIMARY KEY,
    exchange            TEXT    NOT NULL DEFAULT 'ASX',
    status              TEXT    NOT NULL DEFAULT 'missing',
    priority            INTEGER NOT NULL DEFAULT 0,
    last_downloaded_at  TEXT,
    last_attempt_at     TEXT,
    error_message       TEXT,
    csv_path            TEXT,
    watchlists          TEXT
);

CREATE TABLE IF NOT EXISTS sws_import_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    csv_path        TEXT    NOT NULL,
    imported_at     TEXT    NOT NULL,
    rows_imported   INTEGER,
    success         INTEGER NOT NULL
);
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the database and apply the schema. Returns the connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    for stmt in _SCHEMA_SQL.strip().split(";"):
        stmt = stmt.strip()
        if stmt:
            con.execute(stmt)
    con.commit()
    return con
