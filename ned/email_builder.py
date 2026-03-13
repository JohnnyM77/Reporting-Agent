# ned/email_builder.py
#
# Builds the plain-text and HTML email digest for Ned the News Agent.
# Mirrors Bob's dark-navy colour scheme.

from __future__ import annotations

import html as htmlmod
import datetime as dt

COLOR_BG = "#0B1220"
COLOR_TEXT = "#E5E7EB"
COLOR_PANEL = "#1E293B"
COLOR_YOUTUBE = "#EF4444"   # red
COLOR_NEWS = "#3B82F6"      # blue
COLOR_SILENCE = "#6B7280"   # grey


def _ticker_badges(tickers: list[str]) -> str:
    badges = ""
    for t in tickers:
        badges += (
            f'<span style="display:inline-block;background:#334155;color:#94A3B8;'
            f'font-size:11px;font-weight:700;padding:2px 7px;border-radius:6px;'
            f'margin-right:4px;">{htmlmod.escape(t)}</span>'
        )
    return badges


def _hit_html(hit: dict, llm_summary: str | None) -> str:
    tickers_str = ", ".join(hit["tickers"])
    title = htmlmod.escape(hit["title"])
    url = htmlmod.escape(hit["url"])
    source = htmlmod.escape(hit["source"])
    summary_block = ""
    if llm_summary:
        summary_block = (
            f'<div style="margin-top:8px;font-size:13px;color:#CBD5E1;">'
            f'{htmlmod.escape(llm_summary)}</div>'
        )
    return (
        f'<div style="margin-bottom:14px;padding:12px;background:{COLOR_PANEL};border-radius:10px;">'
        f'{_ticker_badges(hit["tickers"])}'
        f'<div style="margin-top:6px;font-size:14px;font-weight:700;">'
        f'<a href="{url}" style="color:#60A5FA;text-decoration:none;">{title}</a></div>'
        f'<div style="font-size:12px;color:#64748B;margin-top:3px;">{source}</div>'
        f'{summary_block}'
        f'</div>'
    )


def _section_html(title: str, color: str, hits_html: str) -> str:
    if not hits_html:
        return ""
    return (
        f'<div style="margin:18px 0;">'
        f'<div style="padding:10px 12px;background:{color};color:#0B1220;font-weight:800;'
        f'border-radius:10px;letter-spacing:0.6px;">{title}</div>'
        f'<div style="margin-top:10px;">{hits_html}</div>'
        f'</div>'
    )


def build_email(
    youtube_hits: list[dict],
    news_hits: list[dict],
    summaries: dict[str, str],   # seen_key → LLM summary
    lookback_hours: int,
    run_date: str,
) -> tuple[str, str]:
    """Returns (plain_text, html)."""

    # --- Plain text ---
    lines = ["Ned the News Agent", "=" * 18,
             f"Media Digest — last {lookback_hours}h — {run_date} (SGT)", ""]

    if youtube_hits:
        lines += ["YOUTUBE MENTIONS", "-" * 60]
        for h in youtube_hits:
            tickers = ", ".join(h["tickers"])
            lines.append(f"[{tickers}] {h['title']}")
            lines.append(f"  Source: {h['source']}  {h['url']}")
            if h["seen_key"] in summaries:
                lines.append(f"  {summaries[h['seen_key']]}")
            lines.append("")

    if news_hits:
        lines += ["NEWS & PRESS RELEASES", "-" * 60]
        for h in news_hits:
            tickers = ", ".join(h["tickers"])
            lines.append(f"[{tickers}] {h['title']}")
            lines.append(f"  Source: {h['source']}  {h['url']}")
            if h["seen_key"] in summaries:
                lines.append(f"  {summaries[h['seen_key']]}")
            lines.append("")

    if not youtube_hits and not news_hits:
        lines.append("No mentions found in the monitored channels and feeds this run.")

    plain = "\n".join(lines)

    # --- HTML ---
    yt_html = "".join(
        _hit_html(h, summaries.get(h["seen_key"])) for h in youtube_hits
    )
    news_html = "".join(
        _hit_html(h, summaries.get(h["seen_key"])) for h in news_hits
    )

    body_html = (
        f'<div style="padding:18px;background:{COLOR_BG};color:{COLOR_TEXT};'
        f'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;">'
        f'<div style="font-size:22px;font-weight:900;margin-bottom:6px;">Ned the News Agent</div>'
        f'<div style="opacity:0.9;font-size:14px;margin-bottom:18px;">'
        f'Media Digest — last {lookback_hours}h — {htmlmod.escape(run_date)} (SGT)</div>'
        + _section_html("YOUTUBE MENTIONS", COLOR_YOUTUBE, yt_html)
        + _section_html("NEWS &amp; PRESS RELEASES", COLOR_NEWS, news_html)
    )

    if not youtube_hits and not news_hits:
        body_html += (
            f'<div style="margin:18px 0;padding:12px;background:{COLOR_SILENCE};'
            f'color:#0B1220;font-weight:800;border-radius:10px;">'
            f'No mentions found this run.</div>'
        )

    body_html += "</div>"
    return plain, body_html
