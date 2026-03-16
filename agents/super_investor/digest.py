# agents/super_investor/digest.py
#
# Generates the final investor briefing (digest) from a ranked, scored list
# of InvestorEvent objects.
#
# This module delegates HTML/Markdown rendering to master_engine/renderer.py
# and adds Super Investor-specific formatting such as section summaries and
# conviction context.

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Optional

from master_engine.schemas import InvestorEvent, PRIORITY_ORDER
from master_engine.renderer import build_html, build_markdown, build_json_archive

logger = logging.getLogger(__name__)


def _plain_text_digest(events: list[InvestorEvent], run_date: str) -> str:
    """Generate a plain-text version of the digest (for email fallback)."""
    lines = [
        "JOHNNY MASTER INVESTOR ALERT",
        "=" * 60,
        f"Date: {run_date}  |  Alerts: {len(events)}",
        "=" * 60,
        "",
    ]

    # Group by priority
    grouped: dict[str, list[InvestorEvent]] = {p: [] for p in PRIORITY_ORDER}
    for event in events:
        bucket = event.priority if event.priority in grouped else "FYI"
        grouped[bucket].append(event)

    for priority in PRIORITY_ORDER:
        bucket = grouped[priority]
        if not bucket:
            continue
        lines.append(f"\n{priority}")
        lines.append("-" * 40)
        for event in bucket:
            lines.append(f"{event.ticker} — {event.headline}")
            if event.summary:
                lines.append(f"  {event.summary}")
            if event.action:
                lines.append(f"  Action: {event.action}")
            link_parts = []
            label_map = {
                "quote_page": "Yahoo Finance",
                "market_index": "Market Index",
                "asx_announcement": "ASX Announcement",
                "company_ir": "Company IR",
                "google_drive_report": "Google Drive Report",
                "internal_report": "Internal Report",
            }
            for key, url in event.source_links.items():
                label = label_map.get(key, key.replace("_", " ").title())
                link_parts.append(f"{label}: {url}")
            if link_parts:
                lines.append("  Links: " + " | ".join(link_parts))
            lines.append("")

    if not events:
        lines.append("No alerts to report today.")

    return "\n".join(lines)


def generate_digest(
    events: list[InvestorEvent],
    output_dir: Path,
    run_date: Optional[str] = None,
) -> dict[str, object]:
    """
    Generate all digest outputs and write them to *output_dir*.

    Parameters
    ----------
    events : list[InvestorEvent]
        Scored and ranked events (descending score).
    output_dir : Path
        Directory to write digest files.
    run_date : str, optional
        ISO date string (defaults to today UTC).

    Returns
    -------
    dict
        ``{html, markdown, json, plain_text}`` content strings plus
        ``{html_path, markdown_path, json_path}`` written paths.
    """
    if run_date is None:
        run_date = dt.datetime.utcnow().date().isoformat()

    output_dir.mkdir(parents=True, exist_ok=True)

    html_content = build_html(events, run_date)
    md_content = build_markdown(events, run_date)
    json_content = build_json_archive(events, run_date)
    plain_content = _plain_text_digest(events, run_date)

    html_path = output_dir / f"master_investor_digest_{run_date}.html"
    md_path = output_dir / f"master_investor_digest_{run_date}.md"
    json_path = output_dir / f"master_investor_archive_{run_date}.json"

    html_path.write_text(html_content, encoding="utf-8")
    md_path.write_text(md_content, encoding="utf-8")
    json_path.write_text(json_content, encoding="utf-8")

    logger.info(
        "[digest] Wrote HTML → %s, Markdown → %s, JSON → %s",
        html_path, md_path, json_path,
    )

    return {
        "html": html_content,
        "markdown": md_content,
        "json": json_content,
        "plain_text": plain_content,
        "html_path": html_path,
        "markdown_path": md_path,
        "json_path": json_path,
        "run_date": run_date,
        "total_events": len(events),
    }
