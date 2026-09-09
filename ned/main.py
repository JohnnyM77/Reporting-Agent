#!/usr/bin/env python3
# ned/main.py
#
# Ned the News Agent — media scanner for portfolio companies.
#
# Sources:
#   - YouTube channels (via YouTube Data API v3 + youtube-transcript-api)
#   - Google News RSS (one feed per company)
#   - Static RSS feeds (Livewire, etc.)
#   - Yahoo Finance news
#
# Config:
#   ../tickers.yaml        — portfolio companies
#   ../media_sources.yaml  — which YouTube channels + RSS feeds to scan
#
# Required secrets:
#   YOUTUBE_API_KEY        — YouTube Data API v3 key (Google Cloud Console)
#   ANTHROPIC_API_KEY      — for LLM summarisation
#   EMAIL_FROM / EMAIL_TO / EMAIL_APP_PASSWORD — Gmail SMTP

from __future__ import annotations

import argparse
import json
import os
import smtplib
import ssl
import sys
import datetime as dt
from email.message import EmailMessage
from pathlib import Path

import yaml
import anthropic

# Allow imports from repo root (news_context_fetcher etc.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ned.youtube_scanner import scan_youtube_channels
from ned.news_scanner import scan_rss_feeds, scan_yahoo_finance
from ned.email_builder import build_email
from ned.importance_scorer import sort_by_importance
from ned.youtube_transcript_fetcher import (
    TranscriptError,
    TranscriptResult,
    extract_video_id,
    fetch_transcript,
)

# ----------------------------
# Config / paths
# ----------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
TICKERS_PATH = REPO_ROOT / "tickers.yaml"
MEDIA_SOURCES_PATH = REPO_ROOT / "media_sources.yaml"
SEEN_STATE_PATH = Path(os.environ.get("NED_SEEN_STATE_PATH", "ned_seen.json"))
SEEN_STATE_RETENTION_HOURS = 96

MODEL = os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001")
MAX_LLM_CALLS = 30

# Where single-episode transcripts are written. Kept out of the repo (the CI
# workflow uploads this directory as a downloadable artifact instead).
TRANSCRIPTS_DIR = Path(os.environ.get("NED_TRANSCRIPTS_DIR", str(REPO_ROOT / "transcripts")))
# Cap the transcript text handed to the LLM. An hour of speech is ~9k words;
# 60k characters comfortably covers a long episode while bounding token spend.
TRANSCRIPT_LLM_MAX_CHARS = int(os.environ.get("NED_TRANSCRIPT_LLM_MAX_CHARS", "60000"))


# ----------------------------
# Seen-state helpers
# ----------------------------
def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        cutoff = (dt.datetime.utcnow() - dt.timedelta(hours=SEEN_STATE_RETENTION_HOURS)).isoformat()
        return {k for k, ts in data.items() if ts > cutoff}
    except Exception:
        return set()


def save_seen(path: Path, seen: set[str]) -> None:
    now = dt.datetime.utcnow().isoformat()
    data = {k: now for k in seen}
    path.write_text(json.dumps(data, indent=2))


# ----------------------------
# LLM summarisation
# ----------------------------
def llm_summarise(hit: dict, llm_calls: list[int]) -> str | None:
    """One-line summary of a hit. Returns None if cap reached or call fails."""
    if llm_calls[0] >= MAX_LLM_CALLS:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    llm_calls[0] += 1
    tickers = ", ".join(hit["tickers"])
    source_text = hit.get("transcript_snippet") or hit.get("description") or hit["title"]
    prompt = (
        f"Portfolio tickers: {tickers}\n"
        f"Source: {hit['source']}\n"
        f"Title: {hit['title']}\n"
        f"Content snippet: {source_text[:3000]}\n\n"
        "Write one punchy sentence: what happened and why it matters to a shareholder. "
        "If the content is vague or unrelated to these companies, write: [not material]"
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        return (resp.content[0].text or "").strip()
    except Exception as exc:
        print(f"[ned/llm] LLM failed: {exc}")
        return None


def llm_transcript_digest(source_url: str, transcript: str, llm_calls: list[int]) -> str | None:
    """Summarise a full episode transcript into Ned's digest format.

    Uses the same Anthropic client, model and call-cap plumbing as
    `llm_summarise`, but with a prompt tuned for a long single-source
    transcript (an interview or podcast episode) rather than a one-line news
    hit. Returns the digest markdown, or None if the cap is reached, no API key
    is set, or the call fails.
    """
    if llm_calls[0] >= MAX_LLM_CALLS:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    llm_calls[0] += 1
    clipped = transcript[:TRANSCRIPT_LLM_MAX_CHARS]
    truncated_note = ""
    if len(transcript) > TRANSCRIPT_LLM_MAX_CHARS:
        truncated_note = (
            "\n\n[Note: the transcript was truncated for length; summarise what "
            "is present.]"
        )
    prompt = (
        "You are Ned, a markets news analyst. Below is the full transcript of a "
        "single YouTube episode (an interview, podcast or briefing). Produce a "
        "concise digest for a busy portfolio investor.\n\n"
        f"Source: {source_url}\n\n"
        "Structure your answer as:\n"
        "1. **TL;DR** — 2-3 sentences on what this episode is about.\n"
        "2. **Key points** — 5-8 bullets of the most important claims, "
        "numbers, or arguments made.\n"
        "3. **Companies / tickers mentioned** — any listed companies discussed, "
        "with a few words on the context (or 'none' if not applicable).\n"
        "4. **So what** — 1-2 sentences on why a shareholder should care.\n\n"
        "Be factual and do not invent figures that are not in the transcript.\n\n"
        f"--- TRANSCRIPT ---\n{clipped}{truncated_note}"
    )
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        return (resp.content[0].text or "").strip()
    except Exception as exc:
        print(f"[ned/llm] Transcript digest LLM failed: {exc}")
        return None


# ----------------------------
# Single-episode transcript pipeline
# ----------------------------
def _save_transcripts(result: TranscriptResult) -> tuple[Path, Path]:
    """Write the plain and timestamped transcripts to disk. Returns their paths."""
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    base = f"{result.video_id}_{stamp}"
    plain_path = TRANSCRIPTS_DIR / f"{base}.txt"
    ts_path = TRANSCRIPTS_DIR / f"{base}.timestamped.txt"
    plain_path.write_text(result.plain_text, encoding="utf-8")
    ts_path.write_text(result.timestamped_text, encoding="utf-8")
    return plain_path, ts_path


def _transcript_email(source_url: str, result: TranscriptResult, digest: str | None) -> tuple[str, str]:
    """Build (plain, html) for a single-episode transcript digest, in Ned's style."""
    import html as htmlmod

    watch_url = f"https://www.youtube.com/watch?v={result.video_id}"
    header = f"Transcript digest — {result.video_id} ({result.caption_kind} captions)"

    # Plain text
    lines = [
        "Ned the News Agent",
        "=" * 18,
        header,
        f"Source: {source_url}",
        "",
    ]
    if digest:
        lines.append(digest)
    else:
        lines.append(
            "(LLM digest unavailable — transcript saved to file. See attached / artifact.)"
        )
    plain = "\n".join(lines)

    # HTML — reuse Ned's dark-navy scheme; digest markdown rendered lightly.
    digest_html = _digest_markdown_to_html(digest) if digest else (
        '<div style="font-size:13px;color:#CBD5E1;">LLM digest unavailable — '
        'the transcript was saved to file.</div>'
    )
    body_html = (
        '<div style="padding:18px;background:#0B1220;color:#E5E7EB;'
        'font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Arial,sans-serif;">'
        '<div style="font-size:22px;font-weight:900;margin-bottom:6px;">Ned the News Agent</div>'
        f'<div style="opacity:0.9;font-size:14px;margin-bottom:4px;">{htmlmod.escape(header)}</div>'
        f'<div style="font-size:12px;margin-bottom:18px;">'
        f'<a href="{htmlmod.escape(watch_url)}" style="color:#60A5FA;text-decoration:none;">'
        f'{htmlmod.escape(watch_url)}</a></div>'
        '<div style="padding:14px;background:#1E293B;border-radius:10px;">'
        f'{digest_html}</div>'
        '</div>'
    )
    return plain, body_html


def _digest_markdown_to_html(md: str) -> str:
    """Very small markdown renderer for the digest: bold, bullets, paragraphs."""
    import html as htmlmod
    import re as _re

    def inline(text: str) -> str:
        text = htmlmod.escape(text)
        return _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)

    html_parts: list[str] = []
    in_list = False
    for raw in md.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        is_bullet = stripped.startswith(("- ", "* ", "• "))
        if is_bullet:
            if not in_list:
                html_parts.append('<ul style="margin:6px 0 10px 18px;padding:0;">')
                in_list = True
            item = stripped[2:].strip()
            html_parts.append(
                f'<li style="font-size:13px;color:#E5E7EB;margin:3px 0;">{inline(item)}</li>'
            )
        else:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            if stripped:
                html_parts.append(
                    f'<div style="font-size:13px;color:#E5E7EB;margin:6px 0;">{inline(stripped)}</div>'
                )
    if in_list:
        html_parts.append("</ul>")
    return "".join(html_parts)


def run_transcript_digest(url: str) -> int:
    """Fetch a single YouTube episode transcript, save it, summarise it, and
    email a digest in Ned's format.

    Returns a process exit code (0 on success, 1 on a handled failure).
    """
    print(f"[ned/transcript] Requested URL: {url}")
    try:
        video_id = extract_video_id(url)
    except TranscriptError as exc:
        print(f"[ned/transcript] {exc}")
        return 1
    print(f"[ned/transcript] Video ID: {video_id}")

    try:
        result = fetch_transcript(video_id)
    except TranscriptError as exc:
        # Captions disabled / none found / network — a clear message, no crash.
        print(f"[ned/transcript] {exc}")
        _maybe_email(
            subject=f"Ned — transcript fetch failed — {video_id}",
            plain=f"Could not fetch transcript for {url}\n\n{exc}",
            html=(
                '<div style="padding:18px;background:#0B1220;color:#E5E7EB;">'
                f'<b>Transcript fetch failed for</b> {video_id}<br>{exc}</div>'
            ),
        )
        return 1

    print(
        f"[ned/transcript] Got {len(result.segments)} segments "
        f"({result.caption_kind} captions, {result.language_code or '?'}); "
        f"{len(result.plain_text)} chars of plain text"
    )

    plain_path, ts_path = _save_transcripts(result)
    print(f"[ned/transcript] Saved plain transcript      -> {plain_path}")
    print(f"[ned/transcript] Saved timestamped transcript -> {ts_path}")

    # Hand the clean text to the LLM digest step.
    llm_calls = [0]
    digest = llm_transcript_digest(url, result.plain_text, llm_calls)
    if digest:
        print("[ned/transcript] LLM digest produced.")
    else:
        print("[ned/transcript] LLM digest unavailable (no key / cap / error).")

    plain, html = _transcript_email(url, result, digest)
    _maybe_email(
        subject=f"Ned — Transcript digest — {video_id}",
        plain=plain,
        html=html,
    )
    return 0


def _maybe_email(subject: str, plain: str, html: str) -> None:
    """Send via Ned's SMTP path when email env is configured; else print."""
    if all(os.environ.get(k) for k in ("EMAIL_FROM", "EMAIL_TO", "EMAIL_APP_PASSWORD")):
        try:
            send_email(subject, plain, html)
            print("[ned/transcript] Email sent.")
            return
        except Exception as exc:
            print(f"[ned/transcript] Email send failed: {exc}")
    print("[ned/transcript] Email not configured — digest below:\n")
    print(plain)


# ----------------------------
# Email
# ----------------------------
def send_email(subject: str, plain: str, html: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(os.environ["EMAIL_FROM"], os.environ["EMAIL_APP_PASSWORD"])
        s.send_message(msg)


def today_sgt() -> str:
    sgt = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.now(sgt).date().isoformat()


# ----------------------------
# Dashboard JSON
# ----------------------------
# Only these importance levels are surfaced on the public dashboard.
_DASHBOARD_LEVELS = ("CRITICAL", "HIGH")
# At most this many cards per company — the dashboard shows each company's
# highest-priority items, not a flat global top-N. A company with lots of news
# still only takes two cards, so twenty companies is forty cards, not two
# hundred.
_DASHBOARD_MAX_PER_COMPANY = 2


def _dashboard_items(all_hits: list[dict], summaries: dict[str, str]) -> list[dict]:
    """Pick the Critical + High hits for the dashboard: each company's two
    highest-priority items, companies ordered by their most important item.

    A hit is attributed to its first ticker (news hits carry exactly one), and
    the same headline never appears twice.
    """
    picked = [h for h in all_hits if h.get("importance_level") in _DASHBOARD_LEVELS]
    # Most important first; title breaks ties for a stable order.
    picked.sort(key=lambda h: (-h.get("importance_score", 0), h.get("title", "")))

    per_company: dict[str, list[dict]] = {}
    seen_keys: set[str] = set()
    for h in picked:
        key = h.get("seen_key", "")
        if key and key in seen_keys:
            continue
        tickers = h.get("tickers") or []
        company = tickers[0] if tickers else "—"
        bucket = per_company.setdefault(company, [])
        if len(bucket) >= _DASHBOARD_MAX_PER_COMPANY:
            continue
        if key:
            seen_keys.add(key)
        bucket.append({
            "tickers": tickers,
            "title": h.get("title", ""),
            "url": h.get("url", ""),
            "source": h.get("source", ""),
            "level": h.get("importance_level", ""),
            "summary": summaries.get(key, ""),
            "published": h.get("published", ""),
            "_score": h.get("importance_score", 0),
        })

    # Order companies by their single most-important item, keep each company's
    # cards together, then drop the private sort key.
    ordered_companies = sorted(
        per_company.values(),
        key=lambda cards: -max(c["_score"] for c in cards),
    )
    items: list[dict] = []
    for cards in ordered_companies:
        for c in cards:
            c.pop("_score", None)
            items.append(c)
    return items


def write_dashboard_json(
    youtube_hits: list[dict],
    news_hits: list[dict],
    summaries: dict[str, str] | None = None,
) -> None:
    out_path = REPO_ROOT / "docs" / "data" / "ned.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = len(youtube_hits) + len(news_hits)
    all_hits = youtube_hits + news_hits
    items = _dashboard_items(all_hits, summaries or {})
    critical_high_total = sum(
        1 for h in all_hits if h.get("importance_level") in _DASHBOARD_LEVELS
    )
    out_path.write_text(json.dumps({
        "agent": "Ned",
        "last_run": dt.datetime.utcnow().isoformat() + "Z",
        "youtube_hits": len(youtube_hits),
        "news_hits": len(news_hits),
        "total_hits": total,
        "critical_high_count": critical_high_total,
        "status": "ok" if total else "silence",
        "items": items,
    }, indent=2))


# ----------------------------
# Main
# ----------------------------
def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        prog="ned",
        description="Ned the News Agent — media scanner and transcript fetcher.",
    )
    parser.add_argument(
        "--transcript",
        metavar="YOUTUBE_URL",
        default=None,
        help=(
            "Fetch a single YouTube episode's transcript, save it, summarise it, "
            "and email the digest. If omitted, Ned runs its normal channel/feed "
            "scan. Falls back to the NED_TRANSCRIPT_URL environment variable."
        ),
    )
    args = parser.parse_args(argv)

    # A pasted link (CLI flag or workflow input via env) switches Ned into
    # single-episode transcript mode instead of the daily scan.
    transcript_url = args.transcript or os.environ.get("NED_TRANSCRIPT_URL", "").strip()
    if transcript_url:
        return run_transcript_digest(transcript_url)

    return run_scan()


def run_scan():
    # Load portfolio
    with open(TICKERS_PATH) as f:
        ticker_data = yaml.safe_load(f) or {}

    asx = ticker_data.get("asx", {})
    lse = ticker_data.get("lse", {})
    skip = set(ticker_data.get("etf_tickers", []))
    # Build {TICKER: "Company Name"} excluding ETFs
    companies: dict[str, str] = {}
    if isinstance(asx, dict):
        companies.update({k: v for k, v in asx.items() if k not in skip})
    if isinstance(lse, dict):
        companies.update(lse)

    # Load media sources
    with open(MEDIA_SOURCES_PATH) as f:
        media = yaml.safe_load(f) or {}

    lookback_hours: int = media.get("lookback_hours", 48)
    yt_channels: list[dict] = media.get("youtube_channels", [])
    rss_feeds: list[dict] = media.get("rss_feeds", [])
    skip_news = set(media.get("skip_news_tickers", []))
    news_companies = {k: v for k, v in companies.items() if k not in skip_news}

    # Load seen state
    seen = load_seen(SEEN_STATE_PATH)
    print(f"[ned] {len(seen)} items in seen state")

    # Scan YouTube
    print(f"[ned] Scanning {len(yt_channels)} YouTube channel(s)…")
    youtube_hits = scan_youtube_channels(yt_channels, companies, lookback_hours, seen)
    print(f"[ned] {len(youtube_hits)} YouTube hit(s)")

    # Scan RSS + Yahoo Finance
    print(f"[ned] Scanning {len(rss_feeds)} RSS feed(s) + Yahoo Finance…")
    rss_hits = scan_rss_feeds(rss_feeds, news_companies, lookback_hours, seen)
    yf_hits = scan_yahoo_finance(news_companies, lookback_hours, seen)
    news_hits = rss_hits + yf_hits
    print(f"[ned] {len(news_hits)} news hit(s)")

    # LLM summaries
    llm_calls = [0]
    summaries: dict[str, str] = {}
    all_hits = youtube_hits + news_hits
    for hit in all_hits:
        summary = llm_summarise(hit, llm_calls)
        if summary and summary != "[not material]":
            summaries[hit["seen_key"]] = summary

    # Filter out "[not material]" hits where LLM flagged them
    youtube_hits = [h for h in youtube_hits if summaries.get(h["seen_key"]) != "[not material]"]
    news_hits = [h for h in news_hits if summaries.get(h["seen_key"]) != "[not material]"]

    # Sort news by importance (adds importance_score and importance_level to each hit)
    youtube_hits = sort_by_importance(youtube_hits)
    news_hits = sort_by_importance(news_hits)

    # Build + send email
    run_date = today_sgt()
    plain, html = build_email(youtube_hits, news_hits, summaries, lookback_hours, run_date)
    subject = f"Ned the News Agent — Media Digest — {run_date} (SGT)"

    print(f"[ned] Sending email: {len(youtube_hits)} YT + {len(news_hits)} news")
    send_email(subject, plain, html)
    print("[ned] Email sent.")

    # Update seen state
    for hit in all_hits:
        seen.add(hit["seen_key"])
    save_seen(SEEN_STATE_PATH, seen)

    # Write dashboard JSON
    write_dashboard_json(youtube_hits, news_hits, summaries)


if __name__ == "__main__":
    sys.exit(main() or 0)
