"""
agents/sws_drip/run_drip.py
─────────────────────────────
Main orchestrator for the SWS CSV drip bot.

Usage:
  # One-time local setup (captures SWS session)
  python -m agents.sws_drip.run_drip --setup

  # Populate queue from watchlist YAMLs
  python -m agents.sws_drip.run_drip --rebuild-queue

  # Show what would be downloaded (no Playwright)
  python -m agents.sws_drip.run_drip --dry-run

  # Normal CI run: download 2 CSVs, ingest, done
  python -m agents.sws_drip.run_drip

  # Override limit
  python -m agents.sws_drip.run_drip --limit 4

  # Local mode (reads storage_state.json from disk instead of env var)
  python -m agents.sws_drip.run_drip --local --limit 1

  # Sync manually-downloaded CSVs from data/sws_csv/ into SQLite
  python -m agents.sws_drip.run_drip --sync-local
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

from .sws_db_init import init_db
from .sws_ingest import ingest_csv
from .sws_queue import (
    bump_priority,
    get_next_tickers,
    mark_attempt,
    mark_stale,
    queue_summary,
    rebuild_queue,
)
from .sws_telegram import notify
from .sws_downloader import SWSDownloadError, download_csv, setup_session
from .sws_sync_local import sync_local_csvs

# ── Paths ────────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DB_PATH = _REPO_ROOT / "data" / "sws.db"
_CSV_DIR = _REPO_ROOT / "data" / "sws_csv"
_DEBUG_DIR = _REPO_ROOT / "data" / "sws_debug"
_STORAGE_STATE_PATH = _REPO_ROOT / "storage_state.json"


def _load_storage_state(local: bool) -> dict | str:
    """
    Load SWS session state.

    CI:    reads SWS_STORAGE_STATE env var (base64-encoded JSON)
    Local: reads storage_state.json from repo root
    """
    if local or not os.environ.get("SWS_STORAGE_STATE"):
        if not _STORAGE_STATE_PATH.exists():
            print(
                "[sws_drip] storage_state.json not found. "
                "Run --setup first to capture your SWS session.",
                flush=True,
            )
            sys.exit(1)
        print(f"[sws_drip] Using local session: {_STORAGE_STATE_PATH}", flush=True)
        return str(_STORAGE_STATE_PATH)

    raw = os.environ["SWS_STORAGE_STATE"].strip()
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
        return json.loads(decoded)
    except Exception as e:
        print(f"[sws_drip] Failed to decode SWS_STORAGE_STATE: {e}", flush=True)
        sys.exit(1)


def cmd_setup() -> None:
    """Interactive one-time login flow to capture storage_state.json."""
    asyncio.run(setup_session(_STORAGE_STATE_PATH))
    print(
        f"\n[sws_drip] Session saved to {_STORAGE_STATE_PATH}\n"
        f"To encode for GitHub Secrets (macOS):\n"
        f"  base64 -i storage_state.json | pbcopy\n"
        f"Then paste into GitHub Secret named SWS_STORAGE_STATE",
        flush=True,
    )


def cmd_rebuild_queue() -> None:
    """Rebuild the download queue from all watchlist YAMLs."""
    total = rebuild_queue(_DB_PATH)
    summary = queue_summary(_DB_PATH)
    print(f"[sws_drip] Queue rebuilt. Total: {total}")
    for status, count in sorted(summary.items()):
        print(f"  {status}: {count}")


def cmd_dry_run(limit: int) -> None:
    """Print the next N tickers without downloading anything."""
    # Ensure DB/queue exists
    if not _DB_PATH.exists():
        print("[sws_drip] Queue is empty — run --rebuild-queue first.", flush=True)
        return
    tickers = get_next_tickers(_DB_PATH, limit=limit)
    if not tickers:
        print("[sws_drip] Queue is empty or all tickers are downloaded.", flush=True)
        return
    print(f"[sws_drip] Dry run — next {len(tickers)} ticker(s) to download:")
    for t in tickers:
        print(f"  [{t['status']:10s}] {t['exchange']}:{t['ticker']}  (priority={t['priority']})")


async def _run_downloads(tickers: list[dict], storage_state: dict | str) -> tuple[int, int]:
    """Download CSVs for the given tickers. Returns (success_count, fail_count)."""
    success_count = 0
    fail_count = 0

    for i, t in enumerate(tickers):
        ticker = t["ticker"]
        exchange = t["exchange"]
        print(f"\n[sws_drip] Downloading {i+1}/{len(tickers)}: {exchange}:{ticker}", flush=True)

        try:
            csv_path = await download_csv(
                ticker=ticker,
                exchange=exchange,
                storage_state=storage_state,
                output_dir=_CSV_DIR,
                headless=True,
                debug_dir=_DEBUG_DIR,
            )
            # Ingest into local SQLite
            result = ingest_csv(csv_path, _DB_PATH)
            mark_attempt(
                _DB_PATH,
                ticker,
                success=True,
                csv_path=str(csv_path.relative_to(_REPO_ROOT)),
            )
            success_count += 1
            print(
                f"[sws_drip] ✓ {ticker}: {result['rows_imported']} metrics ingested",
                flush=True,
            )

        except SWSDownloadError as e:
            print(f"[sws_drip] ✗ {ticker}: {e}", flush=True)
            mark_attempt(_DB_PATH, ticker, success=False, error=str(e))
            fail_count += 1

        except Exception as e:
            print(f"[sws_drip] ✗ {ticker}: unexpected error: {e}", flush=True)
            mark_attempt(_DB_PATH, ticker, success=False, error=str(e))
            fail_count += 1

        # Anti-detection delay between tickers (skip after last)
        if i < len(tickers) - 1:
            delay = random.uniform(30, 120)
            print(f"[sws_drip] Waiting {delay:.0f}s before next download...", flush=True)
            await asyncio.sleep(delay)

    return success_count, fail_count


def cmd_sync_local() -> None:
    """Scan data/sws_csv/ for new CSVs and ingest them into SQLite."""
    if not _DB_PATH.exists():
        print(
            "[sws_drip] Database not found. Run --rebuild-queue first, or "
            "drop a CSV in data/sws_csv/ and re-run.",
            flush=True,
        )
        return

    print(f"[sws_drip] Syncing local CSVs from {_CSV_DIR} ...", flush=True)
    results = sync_local_csvs(db_path=_DB_PATH, csv_dir=_CSV_DIR)

    print(f"[sws_drip] Total CSVs found:   {results['total_csvs']}", flush=True)
    print(f"[sws_drip] Already imported:   {results['already_imported']}", flush=True)
    print(f"[sws_drip] Newly imported:     {results['newly_imported']}", flush=True)

    if results["failures"]:
        print(f"[sws_drip] Failures: {len(results['failures'])}", flush=True)
        for filename, error in results["failures"]:
            print(f"  - {filename}: {error}", flush=True)
    else:
        print("[sws_drip] No failures.", flush=True)

    notify(
        f"SWS Sync: Imported {results['newly_imported']} new CSVs. "
        f"({results['already_imported']} already in DB, "
        f"{len(results['failures'])} failures)"
    )


def cmd_run(limit: int, local: bool) -> None:
    """Normal run: load session, download N CSVs, ingest, report."""
    storage_state = _load_storage_state(local)

    tickers = get_next_tickers(_DB_PATH, limit=limit)
    if not tickers:
        msg = "Queue is empty or all tickers already downloaded."
        print(f"[sws_drip] {msg}", flush=True)
        notify(msg)
        return

    print(
        f"[sws_drip] Starting drip run: {len(tickers)} ticker(s) to download",
        flush=True,
    )

    success_count, fail_count = asyncio.run(_run_downloads(tickers, storage_state))

    summary = queue_summary(_DB_PATH)
    remaining = summary.get("missing", 0) + summary.get("stale", 0) + summary.get("failed", 0)

    report_lines = [
        f"Downloaded {success_count}/{len(tickers)} successfully.",
        f"Queue: {remaining} remaining",
        f"({summary.get('downloaded', 0)} done, {summary.get('stale', 0)} stale, "
        f"{summary.get('failed', 0)} failed)",
    ]

    if fail_count > 0:
        failed_tickers = [t["ticker"] for t in tickers[success_count:]]
        report_lines.append(f"Failed: {', '.join(failed_tickers)}")

    report = "\n".join(report_lines)
    print(f"\n[sws_drip] {report}", flush=True)
    notify(report, urgent=(fail_count == len(tickers)))  # urgent only if ALL failed


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="SWS Drip Bot — download Simply Wall St CSVs one by one"
    )
    parser.add_argument("--setup", action="store_true", help="One-time login to capture session")
    parser.add_argument("--rebuild-queue", action="store_true", help="Rebuild queue from watchlists")
    parser.add_argument("--dry-run", action="store_true", help="Print next tickers without downloading")
    parser.add_argument("--mark-stale", action="store_true", help="Mark old downloads as stale")
    parser.add_argument("--stale-days", type=int, default=90, help="Days before marking stale (default 90)")
    parser.add_argument("--local", action="store_true", help="Use local storage_state.json (not env var)")
    parser.add_argument("--limit", type=int, default=2, help="Number of CSVs to download (default 2)")
    parser.add_argument(
        "--sync-local",
        action="store_true",
        help=(
            "Scan data/sws_csv/ for new CSVs and import them into SQLite. "
            "Use this after manually downloading files from SWS or when syncing to a new machine."
        ),
    )
    args = parser.parse_args(argv)

    if args.setup:
        cmd_setup()
    elif args.rebuild_queue:
        cmd_rebuild_queue()
    elif args.dry_run:
        cmd_dry_run(args.limit)
    elif args.mark_stale:
        mark_stale(_DB_PATH, days_threshold=args.stale_days)
    elif args.sync_local:
        cmd_sync_local()
    else:
        cmd_run(limit=args.limit, local=args.local)


if __name__ == "__main__":
    main()
