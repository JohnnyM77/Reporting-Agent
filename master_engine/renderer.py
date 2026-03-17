# master_engine/renderer.py
#
# Builds HTML email digest and Markdown digest file from a ranked list of
# InvestorEvent objects.

from __future__ import annotations

import datetime as dt
import html as htmlmod
import json
import logging
from pathlib import Path
from typing import Optional

from .schemas import InvestorEvent, PRIORITY_ORDER

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Colour palette (matches existing Bob/Ned styling)
# ---------------------------------------------------------------------------
_COLOR_BG = "#0B1220"
_COLOR_PANEL = "#111B2E"
_COLOR_TEXT = "#E5E7EB"
_COLOR_MUTED = "#9CA3AF"
_COLOR_CRITICAL = "#EF4444"   # red
_COLOR_HIGH = "#F59E0B"       # amber/gold
_COLOR_MEDIUM = "#3B82F6"     # blue
_COLOR_LOW = "#10B981"        # green
_COLOR_FYI = "#6B7280"        # grey

_PRIORITY_COLORS: dict[str, str] = {
    "CRITICAL": _COLOR_CRITICAL,
    "HIGH": _COLOR_HIGH,
    "MEDIUM": _COLOR_MEDIUM,
    "LOW": _COLOR_LOW,
    "FYI": _COLOR_FYI,
}


def _priority_color(priority: str) -> str:
    return _PRIORITY_COLORS.get(priority, _COLOR_FYI)


def _group_by_priority(
    events: list[InvestorEvent],
) -> dict[str, list[InvestorEvent]]:
    grouped: dict[str, list[InvestorEvent]] = {p: [] for p in PRIORITY_ORDER}
    for event in events:
        bucket = event.priority if event.priority in grouped else "FYI"
        grouped[bucket].append(event)
    return grouped


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------

def _html_links(links: dict[str, str]) -> str:
    """Return a run of HTML link chips."""
    if not links:
        return ""
    parts = []
    label_map = {
        "quote_page": "Yahoo Finance",
        "market_index": "Market Index",
        "asx_announcement": "ASX Announcement",
        "company_ir": "Company IR",
        "google_drive_report": "Google Drive Report",
        "internal_report": "Internal Report",
    }
    for key, url in links.items():
        label = label_map.get(key, key.replace("_", " ").title())
        parts.append(
            f'<a href="{htmlmod.escape(url)}" style="'
            f"color:{_COLOR_HIGH};text-decoration:none;margin-right:12px;"
            f'">{htmlmod.escape(label)}</a>'
        )
    return "".join(parts)


def _html_event_card(event: InvestorEvent, priority_color: str) -> str:
    """Render a single event as an HTML card."""
    ticker_display = htmlmod.escape(event.ticker)
    company_display = htmlmod.escape(event.company_name)
    headline_display = htmlmod.escape(event.headline)
    summary_display = htmlmod.escape(event.summary) if event.summary else ""
    action_display = htmlmod.escape(event.action) if event.action else ""
    thesis_display = htmlmod.escape(event.thesis_impact) if event.thesis_impact else ""
    links_html = _html_links(event.source_links)

    rows = [
        f'<tr><td style="padding:12px 16px;border-bottom:1px solid #1E2D45;">',
        f'  <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;">',
        f'    <span style="background:{priority_color};color:#fff;font-size:12px;'
        f'font-weight:700;padding:2px 8px;border-radius:4px;">'
        f'{htmlmod.escape(event.priority)}</span>',
        f'    <span style="color:{_COLOR_TEXT};font-weight:700;font-size:16px;">'
        f'{ticker_display}</span>',
        f'    <span style="color:{_COLOR_MUTED};font-size:14px;">{company_display}</span>',
        f'    <span style="color:{_COLOR_MUTED};font-size:12px;margin-left:auto;">'
        f'Score: {event.score}</span>',
        f'  </div>',
        f'  <div style="color:{_COLOR_TEXT};font-size:16px;font-weight:600;margin-bottom:4px;">'
        f'{headline_display}</div>',
    ]
    if summary_display:
        rows.append(
            f'  <div style="color:{_COLOR_MUTED};font-size:14px;margin-bottom:4px;">'
            f'{summary_display}</div>'
        )
    if thesis_display:
        rows.append(
            f'  <div style="color:{_COLOR_MUTED};font-size:13px;font-style:italic;'
            f'margin-bottom:4px;">Thesis impact: {thesis_display}</div>'
        )
    if action_display:
        rows.append(
            f'  <div style="color:{_COLOR_HIGH};font-size:14px;font-weight:600;'
            f'margin-bottom:4px;">Action: {action_display}</div>'
        )
    if links_html:
        rows.append(
            f'  <div style="font-size:13px;margin-top:4px;">{links_html}</div>'
        )
    rows.append(f'</td></tr>')
    return "\n".join(rows)


def build_html(events: list[InvestorEvent], run_date: str) -> str:
    """Build a complete HTML email digest."""
    grouped = _group_by_priority(events)

    sections_html: list[str] = []
    for priority in PRIORITY_ORDER:
        bucket = grouped[priority]
        if not bucket:
            continue
        color = _priority_color(priority)
        header = (
            f'<tr><td style="padding:16px 16px 4px;background:{_COLOR_BG};">'
            f'<div style="color:{color};font-size:18px;font-weight:800;'
            f'letter-spacing:1px;border-left:4px solid {color};padding-left:10px;">'
            f'{htmlmod.escape(priority)}</div></td></tr>'
        )
        sections_html.append(header)
        for event in bucket:
            sections_html.append(_html_event_card(event, color))

    total = len(events)
    content = "\n".join(sections_html) if sections_html else (
        '<tr><td style="padding:24px;color:#9CA3AF;text-align:center;">'
        'No alerts to report today.</td></tr>'
    )

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Johnny Master Investor Alert — {htmlmod.escape(run_date)}</title>
</head>
<body style="margin:0;padding:0;background:{_COLOR_BG};font-family:system-ui,-apple-system,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:700px;margin:0 auto;">
  <tr>
    <td style="background:{_COLOR_PANEL};padding:24px 20px;border-bottom:3px solid {_COLOR_HIGH};">
      <div style="color:{_COLOR_HIGH};font-size:22px;font-weight:900;letter-spacing:2px;">
        JOHNNY MASTER INVESTOR ALERT
      </div>
      <div style="color:{_COLOR_MUTED};font-size:12px;margin-top:4px;">
        {htmlmod.escape(run_date)} &nbsp;|&nbsp; {total} alert(s)
      </div>
    </td>
  </tr>
  {content}
  <tr>
    <td style="padding:16px;color:{_COLOR_MUTED};font-size:12px;text-align:center;
    border-top:1px solid #1E2D45;">
      Generated by the Master Engine Alert &amp; Super Investor Agent
    </td>
  </tr>
</table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def _md_links(links: dict[str, str]) -> str:
    if not links:
        return ""
    label_map = {
        "quote_page": "Yahoo Finance",
        "market_index": "Market Index",
        "asx_announcement": "ASX Announcement",
        "company_ir": "Company IR",
        "google_drive_report": "Google Drive Report",
        "internal_report": "Internal Report",
    }
    parts = []
    for key, url in links.items():
        label = label_map.get(key, key.replace("_", " ").title())
        parts.append(f"[{label}]({url})")
    return " | ".join(parts)


def build_markdown(events: list[InvestorEvent], run_date: str) -> str:
    """Build a Markdown digest."""
    grouped = _group_by_priority(events)

    lines: list[str] = [
        "# JOHNNY MASTER INVESTOR ALERT",
        "",
        f"**Date:** {run_date}  |  **Alerts:** {len(events)}",
        "",
        "---",
        "",
    ]

    for priority in PRIORITY_ORDER:
        bucket = grouped[priority]
        if not bucket:
            continue
        lines.append(f"## {priority}")
        lines.append("")
        for event in bucket:
            lines.append(
                f"### {event.ticker} — {event.headline}"
            )
            lines.append(
                f"*Agent: {event.agent}  |  Score: {event.score}  |  "
                f"Event: {event.event_type}*"
            )
            if event.summary:
                lines.append(f"> {event.summary}")
            if event.thesis_impact:
                lines.append(f"> *Thesis impact: {event.thesis_impact}*")
            if event.action:
                lines.append(f"**Action:** {event.action}")
            link_str = _md_links(event.source_links)
            if link_str:
                lines.append(f"**Links:** {link_str}")
            lines.append("")
        lines.append("---")
        lines.append("")

    if not any(grouped[p] for p in PRIORITY_ORDER):
        lines.append("*No alerts to report today.*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON archive
# ---------------------------------------------------------------------------

def build_json_archive(events: list[InvestorEvent], run_date: str) -> str:
    """Return a JSON string suitable for archiving."""
    return json.dumps(
        {
            "run_date": run_date,
            "total_events": len(events),
            "events": [e.to_dict() for e in events],
        },
        indent=2,
        default=str,
    )


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def write_digest(
    events: list[InvestorEvent],
    output_dir: Path,
    run_date: Optional[str] = None,
) -> dict[str, Path]:
    """
    Write HTML, Markdown, and JSON digest files to *output_dir*.

    Returns a dict mapping format name to output path.
    """
    if run_date is None:
        run_date = dt.datetime.utcnow().date().isoformat()

    output_dir.mkdir(parents=True, exist_ok=True)

    paths: dict[str, Path] = {}

    html_path = output_dir / f"master_investor_digest_{run_date}.html"
    html_path.write_text(build_html(events, run_date), encoding="utf-8")
    paths["html"] = html_path
    logger.info("[renderer] HTML digest written → %s", html_path)

    md_path = output_dir / f"master_investor_digest_{run_date}.md"
    md_path.write_text(build_markdown(events, run_date), encoding="utf-8")
    paths["markdown"] = md_path
    logger.info("[renderer] Markdown digest written → %s", md_path)

    json_path = output_dir / f"master_investor_archive_{run_date}.json"
    json_path.write_text(build_json_archive(events, run_date), encoding="utf-8")
    paths["json"] = json_path
    logger.info("[renderer] JSON archive written → %s", json_path)

    return paths
