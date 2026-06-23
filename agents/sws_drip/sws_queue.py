"""
agents/sws_drip/sws_queue.py
─────────────────────────────
Queue management: rebuild from watchlist YAMLs, get next tickers, mark status.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .sws_db_init import init_db

# Exchange codes that are ASX
_ASX_EXCHANGES = {"ASX"}
# Exchange suffix → exchange name mapping for plain-ticker watchlists
_SUFFIX_TO_EXCHANGE: dict[str, str] = {
    ".AX": "ASX",
}
# Exchanges to skip (non-ASX)
_SKIP_EXCHANGES = {"NASDAQ", "NYSE", "LSE", "TSX", "NZX", "TSE", "EURONEXT"}


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_asx_tickers(watchlist_path: Path) -> tuple[list[str], str]:
    """
    Parse one watchlist YAML. Returns (list_of_bare_ASX_tickers, watchlist_name).
    Supports both {symbol, exchange} dicts and plain strings.
    """
    data = yaml.safe_load(watchlist_path.read_text(encoding="utf-8")) or {}
    if isinstance(data, dict):
        name = str(data.get("name") or watchlist_path.stem)
        items = data.get("tickers", [])
    elif isinstance(data, list):
        name = watchlist_path.stem
        items = data
    else:
        return [], watchlist_path.stem

    tickers: list[str] = []
    for item in items:
        if isinstance(item, dict):
            sym = str(item.get("symbol") or item.get("ticker") or "").strip().upper()
            exch = str(item.get("exchange") or "").strip().upper()
            if not sym:
                continue
            if exch in _ASX_EXCHANGES:
                tickers.append(sym)
            elif exch in _SKIP_EXCHANGES:
                continue
            elif sym.endswith(".AX"):
                tickers.append(sym.replace(".AX", ""))
        elif isinstance(item, str):
            sym = item.strip().upper()
            if sym.endswith(".AX"):
                tickers.append(sym.replace(".AX", ""))
            # skip plain tickers without .AX — unknown exchange
    return tickers, name


def rebuild_queue(db_path: Path, watchlist_dir: Path | None = None) -> int:
    """
    Scan all watchlist YAML files, extract ASX tickers, upsert into sws_download_queue.

    Searches in:
      - .github/Watchlist/**/*.yaml  (relative to repo root)
      - watchlists/*.yaml            (relative to repo root)
      - watchlist_dir if explicitly provided

    For each ticker:
      - If new: INSERT with status='missing', priority=0
      - If existing: UPDATE the watchlists field only, DO NOT clobber status

    Returns count of tickers in queue.
    """
    # Resolve repo root (2 levels above this file: agents/sws_drip/ → repo root)
    repo_root = Path(__file__).resolve().parents[2]

    search_dirs: list[Path] = [
        repo_root / ".github" / "Watchlist",
        repo_root / "watchlists",
    ]
    if watchlist_dir:
        search_dirs.append(watchlist_dir)

    # Collect all YAML files, deduped by resolved path
    yaml_files: list[Path] = []
    seen_paths: set[Path] = set()
    for d in search_dirs:
        if d.is_dir():
            for p in sorted(d.rglob("*.yaml")):
                rp = p.resolve()
                if rp not in seen_paths:
                    seen_paths.add(rp)
                    yaml_files.append(p)

    # ticker → list of watchlist names
    ticker_to_watchlists: dict[str, list[str]] = {}
    for yaml_file in yaml_files:
        try:
            tickers, wl_name = _extract_asx_tickers(yaml_file)
            for t in tickers:
                ticker_to_watchlists.setdefault(t, []).append(wl_name)
        except Exception as e:
            print(f"[sws_queue] Warning: could not parse {yaml_file}: {e}")

    if not ticker_to_watchlists:
        print("[sws_queue] No ASX tickers found in watchlists.")
        return 0

    con = init_db(db_path)
    now = _now_utc()
    upserted = 0

    for ticker, wl_names in sorted(ticker_to_watchlists.items()):
        watchlists_str = ",".join(sorted(set(wl_names)))
        existing = con.execute(
            "SELECT ticker FROM sws_download_queue WHERE ticker = ?", (ticker,)
        ).fetchone()
        if existing:
            con.execute(
                "UPDATE sws_download_queue SET watchlists = ? WHERE ticker = ?",
                (watchlists_str, ticker),
            )
        else:
            con.execute(
                """INSERT INTO sws_download_queue
                   (ticker, exchange, status, priority, watchlists)
                   VALUES (?, 'ASX', 'missing', 0, ?)""",
                (ticker, watchlists_str),
            )
            upserted += 1

    con.commit()
    total = con.execute("SELECT COUNT(*) FROM sws_download_queue").fetchone()[0]
    con.close()
    print(f"[sws_queue] Rebuilt queue: {upserted} new tickers added, {total} total.")
    return total


def get_next_tickers(db_path: Path, limit: int = 2) -> list[dict]:
    """
    Returns up to `limit` tickers to download next.

    Priority order:
      1. priority DESC
      2. status: 'missing' before 'stale'
      3. failed only after 24h cooldown
      4. last_attempt_at ASC (oldest first)
    """
    con = init_db(db_path)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    rows = con.execute(
        """
        SELECT ticker, exchange, status, priority, last_attempt_at, watchlists
        FROM sws_download_queue
        WHERE
            status IN ('missing', 'stale')
            OR (status = 'failed' AND (last_attempt_at IS NULL OR last_attempt_at < ?))
        ORDER BY
            priority DESC,
            CASE status WHEN 'missing' THEN 0 WHEN 'stale' THEN 1 ELSE 2 END,
            COALESCE(last_attempt_at, '1970-01-01') ASC
        LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()
    con.close()
    return [
        {
            "ticker": r[0],
            "exchange": r[1],
            "status": r[2],
            "priority": r[3],
            "last_attempt_at": r[4],
            "watchlists": r[5],
        }
        for r in rows
    ]


def mark_attempt(
    db_path: Path,
    ticker: str,
    success: bool,
    error: str | None = None,
    csv_path: str | None = None,
) -> None:
    """Update queue row after a download attempt."""
    con = init_db(db_path)
    now = _now_utc()
    if success:
        con.execute(
            """UPDATE sws_download_queue
               SET status='downloaded', last_downloaded_at=?, last_attempt_at=?,
                   error_message=NULL, csv_path=?
               WHERE ticker=?""",
            (now, now, csv_path, ticker),
        )
    else:
        con.execute(
            """UPDATE sws_download_queue
               SET status='failed', last_attempt_at=?, error_message=?
               WHERE ticker=?""",
            (now, error, ticker),
        )
    con.commit()
    con.close()


def mark_stale(db_path: Path, days_threshold: int = 90) -> int:
    """
    Bulk-mark 'downloaded' rows as 'stale' if last_downloaded_at is older than
    days_threshold days. Returns the number of rows updated.
    """
    con = init_db(db_path)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_threshold)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    cur = con.execute(
        """UPDATE sws_download_queue
           SET status='stale'
           WHERE status='downloaded'
             AND last_downloaded_at < ?""",
        (cutoff,),
    )
    count = cur.rowcount
    con.commit()
    con.close()
    print(f"[sws_queue] Marked {count} tickers as stale (threshold: {days_threshold}d).")
    return count


def bump_priority(db_path: Path, ticker: str, priority: int = 10) -> None:
    """
    Push a ticker to the front of the queue (e.g. after earnings results arrive).
    Also resets status to 'stale' so it will be re-downloaded.
    """
    con = init_db(db_path)
    con.execute(
        """UPDATE sws_download_queue
           SET priority=?, status='stale'
           WHERE ticker=?""",
        (priority, ticker),
    )
    con.commit()
    con.close()
    print(f"[sws_queue] Bumped {ticker} to priority={priority}, status=stale.")


def queue_summary(db_path: Path) -> dict:
    """Return a dict of status counts for reporting."""
    con = init_db(db_path)
    rows = con.execute(
        "SELECT status, COUNT(*) FROM sws_download_queue GROUP BY status"
    ).fetchall()
    con.close()
    return {r[0]: r[1] for r in rows}
