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
from .pack_detector import detect_result_pack
from .pdf_downloader import download_pack_pdfs, save_pack_metadata
from .utils import iso_to_asx_date, log, make_output_folder
from .valuation_runner import build_valuation


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
            "Target a specific result date instead of detecting the latest. "
            "Useful for replay/testing."
        ),
    )

    # Feature flags
    p.add_argument(
        "--upload",
        action="store_true",
        help="Upload all artifacts to Google Drive after the run.",
    )
    p.add_argument(
        "--build-valuation",
        dest="build_valuation",
        action="store_true",
        help="Build the Wally value-chart spreadsheet for the ticker.",
    )
    p.add_argument(
        "--include-strawman",
        dest="include_strawman",
        action="store_true",
        help="Include a Strawman.com post draft in the outputs.",
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
        "--no-upload",
        dest="no_upload",
        action="store_true",
        help="Suppress the Drive upload even when --upload is set.",
    )
    p.add_argument(
        "--skip-valuation",
        dest="skip_valuation",
        action="store_true",
        help="Skip the valuation workbook build step.",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Re-download and reprocess even if outputs already exist.",
    )

    return p


# ── Main workflow ──────────────────────────────────────────────────────────────

def run(
    ticker: str,
    market: str = "ASX",
    report_type: Optional[str] = None,
    target_date: Optional[str] = None,
    upload: bool = False,
    build_valuation_flag: bool = False,
    include_strawman: bool = False,
    dry_run: bool = False,
    no_upload: bool = False,
    skip_valuation: bool = False,
    force: bool = False,
) -> RunSummary:
    """Run the full results-pack workflow and return a ``RunSummary``.

    This function is the programmatic entry point — it is also called by
    ``main()`` after parsing CLI arguments.
    """
    ticker = ticker.upper().strip()
    do_upload = upload and not no_upload and not dry_run
    do_valuation = build_valuation_flag and not skip_valuation

    log("=" * 60)
    log(f"  Results Pack Agent — {ticker} ({market}) {report_type or 'AUTO'}")
    log("=" * 60)

    # ── 1. Fetch ASX announcements ──────────────────────────────────────────────
    log(f"[main] Fetching announcements for {ticker} …")

    target_dt: Optional[dt.date] = None
    if target_date:
        try:
            target_dt = dt.datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            log(f"[main] Invalid --date format '{target_date}' — expected YYYY-MM-DD. Aborting.")
            sys.exit(1)

    if dry_run:
        log("[main] [DRY-RUN] Would fetch ASX announcements — skipping.")
        # Return a minimal summary for dry-run
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

    announcements = fetch_announcements(
        ticker=ticker,
        from_date=target_dt,
        to_date=target_dt,
    )

    if not announcements:
        log(f"[main] No announcements found for {ticker}. Check the ticker and try again.")
        sys.exit(1)

    log(f"[main] Found {len(announcements)} announcement(s) for {ticker}.")

    # ── 2. Detect result day pack ───────────────────────────────────────────────
    log("[main] Detecting result day …")
    pack = detect_result_pack(
        announcements=announcements,
        report_type=report_type,
        target_date=target_dt,
    )

    if pack is None:
        log(
            f"[main] No {report_type or 'HY/FY'} result day found for {ticker}. "
            "Try --date YYYY-MM-DD to target a specific date, or check the ticker."
        )
        sys.exit(1)

    log(
        f"[main] Pack detected: {pack.result_type} results on {pack.result_date} "
        f"— {len(pack.announcements)} document(s)."
    )

    # ── 3. Create output folder ─────────────────────────────────────────────────
    output_folder = make_output_folder(OUTPUT_ROOT, pack.folder_name)
    log(f"[main] Output folder: {output_folder}")

    # ── 4. Download PDFs ────────────────────────────────────────────────────────
    log("[main] Downloading PDFs …")
    pdfs_downloaded = download_pack_pdfs(
        pack=pack,
        output_folder=output_folder,
        dry_run=dry_run,
    )

    # Save metadata JSON
    meta_path = save_pack_metadata(pack, output_folder)
    artifacts: dict[str, str] = {"metadata": str(meta_path)}

    # ── 5. Run Claude prompts ───────────────────────────────────────────────────
    if pdfs_downloaded == 0:
        log("[main] WARNING: No PDFs downloaded — Claude analysis will be limited.")

    prompts_to_run = ["management_report", "equity_report"]
    if include_strawman:
        prompts_to_run.append("strawman_post")

    log(f"[main] Running Claude prompts: {prompts_to_run} …")
    prompt_artifacts = run_prompts(
        pack=pack,
        output_folder=output_folder,
        prompts_to_run=prompts_to_run,
        include_strawman=include_strawman,
        dry_run=dry_run,
    )
    artifacts.update(prompt_artifacts)

    # ── 6. Build valuation workbook ─────────────────────────────────────────────
    valuation_path: Optional[str] = None
    if do_valuation:
        log("[main] Building valuation workbook …")
        # Valuation uses ticker without exchange suffix for Wally config lookup
        wally_ticker = f"{ticker}.AX" if market == "ASX" and "." not in ticker else ticker
        valuation_path = build_valuation(
            ticker=wally_ticker,
            output_folder=output_folder,
            file_prefix=pack.file_prefix,
            dry_run=dry_run,
        )
        if valuation_path:
            artifacts["valuation"] = valuation_path

    # ── 7. Upload to Google Drive ───────────────────────────────────────────────
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
    elif upload and no_upload:
        log("[main] --no-upload set — skipping Drive upload.")
    elif not upload:
        log("[main] --upload not set — skipping Drive upload.")

    # ── 8. Return summary ───────────────────────────────────────────────────────
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

    run(
        ticker=args.ticker,
        market=args.market,
        report_type=args.report_type,
        target_date=args.date,
        upload=args.upload,
        build_valuation_flag=args.build_valuation,
        include_strawman=args.include_strawman,
        dry_run=args.dry_run,
        no_upload=args.no_upload,
        skip_valuation=args.skip_valuation,
        force=args.force,
    )


if __name__ == "__main__":
    main()
