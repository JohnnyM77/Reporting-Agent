#!/usr/bin/env python3
# results_pack_agent/main.py
# Standalone ASX HY/FY Results Pack Agent.
#
# Usage:
#   python -m results_pack_agent.main --ticker NHC --market ASX --report-type HY \
#       --upload --build-valuation --include-strawman
#
# See --help for all options.

import argparse
import datetime as dt
import os
import sys
from pathlib import Path
from typing import List, Optional

from .asx_fetcher import fetch_announcements
from .claude_runner import run_prompts
from .config import GDRIVE_FOLDER_ID, OUTPUT_ROOT
from .gdrive_uploader import upload_results_pack
from .models import RunSummary
from .pack_detector import detect_result_pack, find_nearest_result_dates
from .pdf_downloader import download_pack_pdfs, save_pack_metadata
from .utils import iso_to_asx_date, log, make_output_folder
from .valuation_runner import build_valuation


# ── Company name lookup ────────────────────────────────────────────────────────

def _resolve_company_name(ticker: str) -> str:
    """Look up the company name for *ticker* from tickers.yaml.

    Falls back to the ticker code itself if no entry is found.
    """
    try:
        import yaml
        _repo_root = Path(__file__).parent.parent
        tickers_file = _repo_root / "tickers.yaml"
        if tickers_file.exists():
            data = yaml.safe_load(tickers_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Search all market sections (asx, lse, etc.)
                for _market, entries in data.items():
                    if isinstance(entries, dict) and ticker in entries:
                        name = entries[ticker]
                        if isinstance(name, str) and name:
                            return name
    except Exception:
        pass
    return ticker


# ── CLI ────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="results_pack_agent",
        description=(
            "ASX Results Pack Agent — download the full HY/FY result-day PDF "
            "pack for a ticker, run deep Claude analysis, build a valuation "
            "workbook, and upload everything to Google Drive."
        ),
    )
    # Required
    p.add_argument("--ticker", required=True, help="ASX ticker code (e.g. NHC)")

    # Optional / mode
    p.add_argument(
        "--market",
        default="ASX",
        choices=["ASX"],
        help="Exchange market (currently ASX only; default: ASX)",
    )
    p.add_argument(
        "--report-type",
        dest="report_type",
        default=None,
        choices=["HY", "FY"],
        help=(
            "Result type to look for: HY (half-year) or FY (full-year). "
            "If omitted, the agent detects the latest available result."
        ),
    )
    p.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help=(
            "Target a specific result date (treated as a preference, not a "
            "hard filter). If no exact match, nearest valid dates are suggested. "
            "Useful for replay/testing."
        ),
    )

    # Feature flags — all default ON; use the --no-* / --skip-* flags to disable
    p.add_argument(
        "--no-upload",
        dest="no_upload",
        action="store_true",
        help="Suppress the Google Drive upload (upload is ON by default).",
    )
    p.add_argument(
        "--skip-valuation",
        dest="skip_valuation",
        action="store_true",
        help="Skip the valuation workbook build step (valuation is ON by default).",
    )
    p.add_argument(
        "--skip-strawman",
        dest="skip_strawman",
        action="store_true",
        help="Skip the Strawman post draft (Strawman is ON by default).",
    )

    # Legacy opt-in flags kept for backwards compatibility — they are now no-ops
    # because the features are enabled by default.
    p.add_argument(
        "--upload",
        action="store_true",
        help=argparse.SUPPRESS,  # hidden; upload is now the default
    )
    p.add_argument(
        "--build-valuation",
        dest="build_valuation",
        action="store_true",
        help=argparse.SUPPRESS,  # hidden; valuation is now the default
    )
    p.add_argument(
        "--include-strawman",
        dest="include_strawman",
        action="store_true",
        help=argparse.SUPPRESS,  # hidden; Strawman is now the default
    )

    # Debug / testing
    p.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "Log what would happen without making any HTTP requests or "
            "writing any files (implies --no-upload and --skip-valuation)."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-download and reprocess even if outputs already exist.",
    )
    p.add_argument(
        "--list-recent-dates",
        dest="list_recent_dates",
        action="store_true",
        help=(
            "Print recent result-day candidate dates for the ticker and exit. "
            "Use with --report-type to filter by HY or FY. "
            "Example: python -m results_pack_agent.main --ticker NHC --list-recent-dates"
        ),
    )

    return p


# ── Helper: _make_failure ──────────────────────────────────────────────────────

def _make_failure(
    ticker: str,
    reason: str,
    message: str,
    nearest_dates: Optional[List[str]] = None,
    report_type: str = "AUTO",
) -> RunSummary:
    """Return a RunSummary that represents a structured failure."""
    summary = RunSummary(
        ticker=ticker,
        result_date="N/A",
        result_type=report_type,
        pdfs_downloaded=0,
        prompts_run=[],
        local_folder="N/A",
        drive_folder_url=None,
        valuation_path=None,
        failure_reason=reason,
        failure_message=message,
        nearest_dates=nearest_dates or [],
    )
    return summary


# ── List-recent-dates mode ─────────────────────────────────────────────────────

def list_recent_dates(ticker: str, report_type: Optional[str] = None, n: int = 10) -> None:
    """Fetch and print recent result-day candidate dates for *ticker*."""
    ticker = ticker.upper().strip()
    log(f"[main] Fetching announcement history for {ticker} …")
    announcements = fetch_announcements(ticker=ticker)
    if not announcements:
        log(f"[main] No announcements found for {ticker}. The ticker may be invalid.")
        print(f"\nNo announcements found for {ticker}.")
        return

    dates = find_nearest_result_dates(announcements, report_type=report_type, n=n)
    type_label = f" ({report_type})" if report_type else ""
    print(f"\nRecent result-day candidates for {ticker}{type_label}:")
    if dates:
        for d in dates:
            print(f"  - {d}")
    else:
        print("  (none found in the last 6 months)")
    print()


# ── Main workflow ──────────────────────────────────────────────────────────────

def run(
    ticker: str,
    market: str = "ASX",
    report_type: Optional[str] = None,
    target_date: Optional[str] = None,
    upload: bool = True,
    build_valuation_flag: bool = True,
    include_strawman: bool = True,
    dry_run: bool = False,
    no_upload: bool = False,
    skip_valuation: bool = False,
    skip_strawman: bool = False,
    force: bool = False,
) -> RunSummary:
    """Run the full results-pack workflow and return a ``RunSummary``.

    This function is the programmatic entry point — it is also called by
    ``main()`` after parsing CLI arguments.

    The function always returns a ``RunSummary`` (never calls sys.exit).
    Check ``summary.failure_reason`` to determine whether the run succeeded.
    """
    ticker = ticker.upper().strip()
    do_upload = upload and not no_upload and not dry_run
    do_valuation = build_valuation_flag and not skip_valuation
    do_strawman = include_strawman and not skip_strawman

    log("=" * 60)
    log(f"  Results Pack Agent — {ticker} ({market}) {report_type or 'AUTO'}")
    log("=" * 60)
    if target_date:
        log(f"[main] Target date: {target_date} (preference, not hard filter)")

    # ── 1. Parse target date ────────────────────────────────────────────────────
    target_dt: Optional[dt.date] = None
    if target_date:
        try:
            target_dt = dt.datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            msg = f"Invalid --date format '{target_date}' — expected YYYY-MM-DD."
            log(f"[main] {msg}")
            summary = _make_failure(ticker, "INVALID_DATE_FORMAT", msg, report_type=report_type or "AUTO")
            summary.print_summary()
            return summary

    if dry_run:
        log("[main] [DRY-RUN] Would fetch ASX announcements — skipping.")
        summary = RunSummary(
            ticker=ticker,
            result_date=target_date or "N/A",
            result_type=report_type or "AUTO",
            pdfs_downloaded=0,
            prompts_run=[],
            local_folder="(dry-run)",
            drive_folder_url=None,
            valuation_path=None,
        )
        summary.print_summary()
        return summary

    # ── 2. Fetch ASX announcements (full 6-month history, no date pre-filter) ──
    # We intentionally do NOT pass from_date/to_date here so the full history
    # is available for nearest-date matching and latest-pack selection.
    log(f"[main] Fetching announcement history for {ticker} (last 6 months) …")
    announcements = fetch_announcements(ticker=ticker)

    print(f"[results_pack] announcements_found={len(announcements)} for {ticker}")

    if not announcements:
        msg = (
            f"ASX fetch failed — zero announcements returned from shared fetch for {ticker}. "
            "This likely indicates a fetch/parsing issue or unexpected ASX response format. "
            "Check the [asx_fetcher] log lines above to diagnose the root cause."
        )
        log(f"[main] {msg}")
        summary = _make_failure(ticker, "NO_ANNOUNCEMENTS_FOUND", msg, report_type=report_type or "AUTO")
        summary.print_summary()
        return summary

    log(f"[main] Found {len(announcements)} announcement(s) for {ticker}.")

    # ── 3. Detect result day pack ───────────────────────────────────────────────
    log("[main] Detecting result day pack …")

    # First attempt: exact date match (if target_dt given)
    pack = detect_result_pack(
        announcements=announcements,
        report_type=report_type,
        target_date=target_dt,
    )

    if pack is None and target_dt is not None:
        # Exact date match failed — try without date constraint to find nearest
        log(
            f"[main] No exact match on {target_date}. "
            "Searching for nearest result-day candidates …"
        )
        nearest = find_nearest_result_dates(
            announcements, report_type=report_type, n=5
        )

        if nearest:
            dates_str = ", ".join(nearest)
            msg = (
                f"No {report_type or 'HY/FY'} result pack found for {ticker} "
                f"on {target_date}.\n"
                f"Nearest candidate date(s): {dates_str}\n"
                "Re-run with one of these dates or omit --date to use the latest."
            )
            log(f"[main] {msg}")
            summary = _make_failure(
                ticker,
                "TICKER_VALID_BUT_NO_MATCHING_DATE",
                msg,
                nearest_dates=nearest,
                report_type=report_type or "AUTO",
            )
            summary.print_summary()
            return summary
        else:
            msg = (
                f"No {report_type or 'HY/FY'} result pack found for {ticker}. "
                "No result-day triggers found in the last 6 months of announcements."
            )
            log(f"[main] {msg}")
            summary = _make_failure(ticker, "NO_RESULT_PACK_FOUND", msg, report_type=report_type or "AUTO")
            summary.print_summary()
            return summary

    if pack is None:
        # No date constraint but still no pack found
        nearest = find_nearest_result_dates(announcements, report_type=report_type, n=3)
        msg = (
            f"No {report_type or 'HY/FY'} result pack found for {ticker} "
            "in the last 6 months of announcements."
        )
        if nearest:
            msg += f" Nearest candidate date(s): {', '.join(nearest)}"
        log(f"[main] {msg}")
        summary = _make_failure(ticker, "NO_RESULT_PACK_FOUND", msg, nearest_dates=nearest, report_type=report_type or "AUTO")
        summary.print_summary()
        return summary

    # Resolve real company name (prefer tickers.yaml over raw ticker)
    company_name = _resolve_company_name(ticker)
    pack.company_name = company_name

    log(
        f"[main] Pack detected: {pack.result_type} results on {pack.result_date} "
        f"for {pack.company_name} — {len(pack.announcements)} document(s)."
    )

    # ── 4. Create output folder ─────────────────────────────────────────────────
    output_folder = make_output_folder(OUTPUT_ROOT, pack.folder_name)
    log(f"[main] Output folder: {output_folder}")

    # ── 5. Download PDFs ────────────────────────────────────────────────────────
    log("[main] Downloading PDFs …")
    pdfs_downloaded = download_pack_pdfs(
        pack=pack,
        output_folder=output_folder,
        dry_run=dry_run,
    )

    if pdfs_downloaded == 0:
        log("[main] WARNING: No PDFs downloaded — Claude analysis will be limited.")

    # Save metadata JSON
    meta_path = save_pack_metadata(pack, output_folder)
    artifacts: dict[str, str] = {"metadata": str(meta_path)}

    # ── 6. Run Claude prompts ───────────────────────────────────────────────────
    prompts_to_run = ["management_report", "equity_report"]
    if do_strawman:
        prompts_to_run.append("strawman_post")

    log(f"[main] Running Claude prompts: {prompts_to_run} …")
    prompt_artifacts = run_prompts(
        pack=pack,
        output_folder=output_folder,
        prompts_to_run=prompts_to_run,
        include_strawman=do_strawman,
        dry_run=dry_run,
    )
    artifacts.update(prompt_artifacts)

    # ── 7. Build valuation workbook ─────────────────────────────────────────────
    valuation_path: Optional[str] = None
    if do_valuation:
        log("[main] Building valuation workbook …")
        wally_ticker = f"{ticker}.AX" if market == "ASX" and "." not in ticker else ticker
        valuation_path = build_valuation(
            ticker=wally_ticker,
            output_folder=output_folder,
            file_prefix=pack.file_prefix,
            dry_run=dry_run,
        )
        if valuation_path:
            artifacts["valuation"] = valuation_path

    # ── 8. Upload to Google Drive ───────────────────────────────────────────────
    drive_url: Optional[str] = None
    if do_upload:
        log("[main] Uploading to Google Drive …")
        drive_url = upload_results_pack(
            local_folder=output_folder,
            ticker=ticker,
            folder_name=pack.folder_name,
            root_folder_id=GDRIVE_FOLDER_ID,
            dry_run=dry_run,
        )
        if drive_url:
            log(f"[main] Drive upload complete: {drive_url}")
        else:
            log("[main] Drive upload failed or not configured.")
    elif no_upload:
        log("[main] --no-upload set — skipping Drive upload.")
    else:
        log("[main] Drive upload skipped (no credentials or disabled).")

    # ── 9. Return summary ───────────────────────────────────────────────────────
    summary = RunSummary(
        ticker=ticker,
        result_date=pack.result_date,
        result_type=pack.result_type,
        pdfs_downloaded=pdfs_downloaded,
        prompts_run=prompts_to_run,
        local_folder=str(output_folder),
        drive_folder_url=drive_url,
        valuation_path=valuation_path,
        artifacts=artifacts,
    )
    summary.print_summary()
    return summary


def main() -> None:
    """CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    # List-recent-dates mode: print candidates and exit
    if args.list_recent_dates:
        list_recent_dates(
            ticker=args.ticker,
            report_type=args.report_type,
            n=10,
        )
        return

    summary = run(
        ticker=args.ticker,
        market=args.market,
        report_type=args.report_type,
        target_date=args.date,
        upload=True,  # upload is ON by default; --no-upload disables it
        build_valuation_flag=True,  # valuation is ON by default; --skip-valuation disables it
        include_strawman=True,  # Strawman is ON by default; --skip-strawman disables it
        dry_run=args.dry_run,
        no_upload=args.no_upload,
        skip_valuation=args.skip_valuation,
        skip_strawman=args.skip_strawman,
        force=args.force,
    )

    # Exit with non-zero code on failure so CI/workflows detect problems
    if not summary.success:
        sys.exit(1)


if __name__ == "__main__":
    main()
