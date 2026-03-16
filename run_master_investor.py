#!/usr/bin/env python3
# run_master_investor.py
#
# Master Investor Alert — top-level runner script.
#
# This script:
#   1. Collects events from Ned (news), Wally (watchlist screening), and Bob (ASX filings)
#   2. Normalizes and aggregates them via the Master Engine
#   3. Scores and ranks them via the Super Investor Agent
#   4. Generates an HTML + Markdown + JSON digest
#   5. Sends the digest by email
#   6. Logs all actions
#
# Usage:
#   python run_master_investor.py [options]
#
# Options:
#   --no-email          Skip email sending (generate digest files only)
#   --no-ned            Skip Ned news collection
#   --no-wally          Skip Wally watchlist screening
#   --no-bob            Skip Bob ASX announcement collection
#   --wally-tii75       Include TII75 watchlist in Wally run
#   --bob-live          Fetch Bob events live (instead of reading bob.json)
#   --output-dir PATH   Output directory for digest files
#   --dry-run           Collect and score events but do not write or send

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging setup — must come before any module imports that use logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("run_master_investor")

# ---------------------------------------------------------------------------
# Repo root on sys.path
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT))


def _build_ned_collector(enabled: bool):
    """Return a Ned events collector function, or None if disabled."""
    if not enabled:
        logger.info("[runner] Ned collector disabled")
        return None

    def ned_collector():
        logger.info("[runner] Collecting events from Ned…")
        try:
            from ned.emit import collect_events as ned_collect
            events = ned_collect()
            logger.info("[runner] Ned → %d event(s)", len(events))
            return events
        except Exception as exc:
            logger.error("[runner] Ned collector error: %s", exc)
            return []

    return ned_collector


def _build_wally_collector(enabled: bool, include_tii75: bool):
    """Return a Wally events collector function, or None if disabled."""
    if not enabled:
        logger.info("[runner] Wally collector disabled")
        return None

    def wally_collector():
        logger.info("[runner] Collecting events from Wally…")
        try:
            from wally.emit import collect_events as wally_collect
            events = wally_collect(include_tii75=include_tii75)
            logger.info("[runner] Wally → %d event(s)", len(events))
            return events
        except Exception as exc:
            logger.error("[runner] Wally collector error: %s", exc)
            return []

    return wally_collector


def _build_bob_collector(enabled: bool, live: bool):
    """Return a Bob events collector function, or None if disabled."""
    if not enabled:
        logger.info("[runner] Bob collector disabled")
        return None

    def bob_collector():
        logger.info("[runner] Collecting events from Bob…")
        try:
            from bob_emit import collect_events, collect_events_live
            events = collect_events_live() if live else collect_events()
            logger.info("[runner] Bob → %d event(s)", len(events))
            return events
        except Exception as exc:
            logger.error("[runner] Bob collector error: %s", exc)
            return []

    return bob_collector


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Master Investor Alert — ranked cross-agent investor briefing"
    )
    parser.add_argument(
        "--no-email",
        action="store_true",
        help="Skip email sending",
    )
    parser.add_argument(
        "--no-ned",
        action="store_true",
        help="Skip Ned news collection",
    )
    parser.add_argument(
        "--no-wally",
        action="store_true",
        help="Skip Wally watchlist screening",
    )
    parser.add_argument(
        "--no-bob",
        action="store_true",
        help="Skip Bob ASX announcement collection",
    )
    parser.add_argument(
        "--wally-tii75",
        action="store_true",
        help="Include TII75 watchlist in Wally run",
    )
    parser.add_argument(
        "--bob-live",
        action="store_true",
        help="Fetch Bob events live (rather than reading bob.json)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for digest files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect and score events but do not write files or send email",
    )

    args = parser.parse_args(argv)

    run_date = dt.datetime.utcnow().date().isoformat()
    output_dir = args.output_dir or (_REPO_ROOT / "outputs" / run_date)

    logger.info("=" * 60)
    logger.info("JOHNNY MASTER INVESTOR ALERT")
    logger.info("Run date: %s", run_date)
    logger.info("Output dir: %s", output_dir)
    logger.info("=" * 60)

    # Build collectors
    ned_collector = _build_ned_collector(not args.no_ned)
    wally_collector = _build_wally_collector(not args.no_wally, args.wally_tii75)
    bob_collector = _build_bob_collector(not args.no_bob, args.bob_live)

    if args.dry_run:
        logger.info("[runner] DRY RUN mode — no files or emails will be generated")

    # Run Super Investor Agent
    try:
        from agents.super_investor.agent import run as super_investor_run

        result = super_investor_run(
            ned_collector=ned_collector,
            wally_collector=wally_collector,
            bob_collector=bob_collector,
            output_dir=None if args.dry_run else output_dir,
            run_date=run_date,
            send_email=(not args.no_email and not args.dry_run),
        )
    except Exception as exc:
        logger.exception("[runner] Super Investor Agent failed: %s", exc)
        return 1

    total = result.get("total", 0)
    email_sent = result.get("email_sent", False)

    logger.info("=" * 60)
    logger.info("Run complete")
    logger.info("  Total events scored: %d", total)
    logger.info("  Email sent:          %s", email_sent)
    if not args.dry_run:
        digest = result.get("digest", {})
        if digest:
            logger.info("  HTML digest:         %s", digest.get("html_path", "—"))
            logger.info("  Markdown digest:     %s", digest.get("markdown_path", "—"))
            logger.info("  JSON archive:        %s", digest.get("json_path", "—"))
    logger.info("=" * 60)

    # Print priority summary
    events = result.get("events", [])
    if events:
        from master_engine.schemas import PRIORITY_ORDER
        counts: dict[str, int] = {p: 0 for p in PRIORITY_ORDER}
        for ev in events:
            counts[ev.priority] = counts.get(ev.priority, 0) + 1
        logger.info("Priority breakdown:")
        for priority in PRIORITY_ORDER:
            if counts[priority]:
                logger.info("  %-10s %d", priority, counts[priority])

    return 0


if __name__ == "__main__":
    sys.exit(main())
