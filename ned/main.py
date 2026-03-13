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
def write_dashboard_json(youtube_hits: list[dict], news_hits: list[dict]) -> None:
    out_path = REPO_ROOT / "docs" / "data" / "ned.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = len(youtube_hits) + len(news_hits)
    out_path.write_text(json.dumps({
        "agent": "Ned",
        "last_run": dt.datetime.utcnow().isoformat() + "Z",
        "youtube_hits": len(youtube_hits),
        "news_hits": len(news_hits),
        "total_hits": total,
        "status": "ok" if total else "silence",
    }, indent=2))


# ----------------------------
# Main
# ----------------------------
def main():
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
    write_dashboard_json(youtube_hits, news_hits)


if __name__ == "__main__":
    main()
