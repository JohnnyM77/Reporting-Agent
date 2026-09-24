"""Transcript digests (single-episode YouTube / podcast) card.

Pure rendering: takes the agent's docs/data JSON (already loaded) and
returns HTML. Reading and writing docs/data stays in scripts/build_dashboard.py."""

from __future__ import annotations

import re

from dashboard.common import _esc, _fmt_date


# ---------------------------------------------------------------------------
# Transcripts (single-episode YouTube / podcast digests)
# ---------------------------------------------------------------------------

def _transcript_digest_summary(md: str, max_chars: int = 400) -> str:
    """Extract the TL;DR (or the first paragraph) from a transcript digest
    for the dashboard card, trimming to `max_chars`.

    The digest markdown Ned emits starts with a "**TL;DR** — …" section by
    convention; grab that when it's there and fall back to the first
    non-blank line otherwise. HTML-escape the result before returning.
    """
    if not md:
        return ""
    # Prefer the TL;DR section.
    tldr_re = re.compile(
        r"(?is)\*\*TL[;\s]*DR\*\*[^\n]*?[—\-:]\s*(.+?)(?:\n\s*\n|\n\s*\d\.|\Z)"
    )
    m = tldr_re.search(md)
    if m:
        text = m.group(1).strip()
    else:
        # Skip a leading numbered header line if any, then take the first
        # non-empty text line.
        text = ""
        for line in md.splitlines():
            line = line.strip()
            if not line:
                continue
            if re.match(r"^\d+\.\s*\*\*", line):
                continue
            text = line
            break
    # Strip markdown bold/italics tokens for the summary.
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"[*_`]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return _esc(text)


def _transcript_card(item: dict) -> str:
    kind = str(item.get("kind", ""))
    url = _esc(str(item.get("source_url", "")))
    title = _esc(str(item.get("title", "") or "(untitled)"))
    ts = _fmt_date(item.get("timestamp"))
    chars = item.get("chars", 0) or 0
    kind_label = "YouTube" if kind == "youtube" else "Podcast"
    kind_colour = "#ef4444" if kind == "youtube" else "#8b5cf6"

    # Provenance line: for podcasts we know whether Whisper was used;
    # for YouTube we know whether the captions were manual or auto.
    if kind == "podcast":
        source_note = (
            "Whisper transcription"
            if item.get("used_whisper", True)
            else "Show-published transcript (free)"
        )
    else:
        source_note = f"{item.get('caption_kind', 'captions')}"

    summary = _transcript_digest_summary(str(item.get("digest_markdown", "")))
    summary_html = (
        f"<div style='color:#94a3b8;font-size:12px;margin-top:6px;line-height:1.45'>{summary}</div>"
        if summary else ""
    )
    title_html = (
        f"<a href='{url}' target='_blank' style='color:#e2e8f0;text-decoration:none'>{title}</a>"
        if url else f"<span style='color:#e2e8f0'>{title}</span>"
    )
    stats_html = f"{chars:,} chars &middot; {source_note}" if chars else source_note

    # PDF link when we generated one. The path is stored relative to docs/
    # so a static site at /Reporting-Agent/ resolves it via a plain relative
    # href — the card sits inside index.html at the site root. Prune
    # retention means the link 404s after ~30 days, which is the deliberate
    # "for a short time" behaviour.
    pdf_path = str(item.get("pdf_path") or "").strip()
    pdf_link_html = ""
    if pdf_path:
        pdf_href = _esc(pdf_path)
        pdf_link_html = (
            "<a href='" + pdf_href + "' target='_blank' rel='noopener' "
            "style='display:inline-block;margin-top:8px;font-size:11px;font-weight:600;"
            "color:#0f172a;background:#10b981;padding:3px 9px;border-radius:5px;"
            "text-decoration:none;letter-spacing:0.3px;'>📄 Download PDF</a>"
        )

    return (
        "<div style='background:#0f172a;border:1px solid #334155;border-left:3px solid "
        f"{kind_colour};border-radius:8px;padding:12px 14px;display:flex;flex-direction:column'>"
        "<div style='display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px'>"
        f"<span style='background:{kind_colour};color:#0b1220;font-size:9px;font-weight:800;"
        f"padding:2px 7px;border-radius:5px;letter-spacing:0.5px'>{kind_label}</span>"
        f"<span style='color:#64748b;font-size:11px'>{ts}</span>"
        "</div>"
        f"<div style='font-size:13px;font-weight:600;line-height:1.4'>{title_html}</div>"
        f"{summary_html}"
        f"<div style='color:#64748b;font-size:11px;margin-top:8px'>{stats_html}</div>"
        f"{pdf_link_html}"
        "</div>"
    )


def _transcripts_section(items: list[dict]) -> str:
    """Render Ned's single-episode transcripts (YouTube + podcast) as a
    dashboard section. Omits the section entirely when there are no items,
    to avoid a permanently-empty card on a fresh install."""
    if not items:
        return ""
    n = min(len(items), 12)  # cap what shows in the section
    cards = "".join(_transcript_card(it) for it in items[:n])
    last = _fmt_date(items[0].get("timestamp"))
    return f"""
    <div class="agent-card">
      <div class="card-header">
        <div>
          <span class="agent-name">Transcripts</span>
          <span class="agent-role">Single-episode digests &middot; YouTube + podcasts (on demand)</span>
        </div>
        <div style="text-align:right">
          <div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#22c55e;margin-right:6px"></span><span style="font-size:13px;color:#e2e8f0">{n} recent</span></div>
          <div style="font-size:12px;color:#64748b;margin-top:4px">Latest: {last}</div>
        </div>
      </div>
      <div class='ned-grid'>{cards}</div>
    </div>"""
