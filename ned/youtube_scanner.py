# ned/youtube_scanner.py
#
# Scans YouTube channels for videos that mention portfolio companies.
# Uses YouTube Data API v3 (channels.list + playlistItems.list) to fetch
# recent uploads, then youtube-transcript-api to get transcripts.
# Avoids costly search.list (100 units each) by using the uploads playlist.

from __future__ import annotations

import datetime as dt
import os
import re
import requests
from typing import Optional

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


def _yt_get(endpoint: str, params: dict) -> dict:
    params["key"] = os.environ["YOUTUBE_API_KEY"]
    r = requests.get(f"{YOUTUBE_API_BASE}/{endpoint}", params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _resolve_channel(handle: str) -> Optional[str]:
    """Return the uploads-playlist ID for a @handle, or None on failure."""
    try:
        data = _yt_get("channels", {
            "part": "contentDetails",
            "forHandle": handle,
            "maxResults": 1,
        })
        items = data.get("items", [])
        if not items:
            return None
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except Exception as exc:
        print(f"[ned/youtube] Could not resolve @{handle}: {exc}")
        return None


def _recent_videos(playlist_id: str, published_after: dt.datetime, max_results: int = 15) -> list[dict]:
    """Return videos from the uploads playlist published after the given UTC datetime."""
    try:
        data = _yt_get("playlistItems", {
            "part": "snippet",
            "playlistId": playlist_id,
            "maxResults": max_results,
        })
    except Exception as exc:
        print(f"[ned/youtube] Could not list playlist {playlist_id}: {exc}")
        return []

    out = []
    for item in data.get("items", []):
        snip = item.get("snippet", {})
        published_str = snip.get("publishedAt", "")
        try:
            published = dt.datetime.fromisoformat(published_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        if published < published_after.replace(tzinfo=dt.timezone.utc):
            continue
        vid_id = snip.get("resourceId", {}).get("videoId")
        if not vid_id:
            continue
        out.append({
            "video_id": vid_id,
            "title": snip.get("title", ""),
            "description": snip.get("description", "")[:500],
            "published": published.isoformat(),
            "url": f"https://www.youtube.com/watch?v={vid_id}",
        })
    return out


def _get_transcript(video_id: str) -> str:
    """Return transcript text (first 8000 chars), or empty string on failure."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
        entries = YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "en-AU", "en-GB"])
        text = " ".join(e["text"] for e in entries)
        return text[:8_000]
    except Exception:
        return ""


def _mentions_company(text: str, ticker: str, company_name: str) -> bool:
    """Quick heuristic check before spending an LLM call."""
    haystack = text.lower()
    # Match ticker (e.g. DRO, BHP) as a whole word
    if re.search(rf"\b{re.escape(ticker.lower())}\b", haystack):
        return True
    # Match first word of company name (e.g. "DroneShield", "Brambles")
    first_word = company_name.split()[0].lower()
    if len(first_word) > 3 and first_word in haystack:
        return True
    return False


def scan_youtube_channels(
    channels: list[dict],
    companies: dict[str, str],   # {TICKER: "Company Name"}
    lookback_hours: int,
    seen_keys: set[str],
) -> list[dict]:
    """
    Scan each channel for recent videos mentioning portfolio companies.
    Returns list of hit dicts ready for email/LLM summarisation.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY", "")
    if not api_key:
        print("[ned/youtube] YOUTUBE_API_KEY not set — skipping YouTube scan")
        return []

    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=lookback_hours)
    hits: list[dict] = []

    for ch in channels:
        handle = ch["handle"]
        label = ch.get("label", handle)
        print(f"[ned/youtube] Scanning @{handle} ({label})…")

        playlist_id = _resolve_channel(handle)
        if not playlist_id:
            continue

        videos = _recent_videos(playlist_id, cutoff)
        print(f"[ned/youtube]   {len(videos)} recent video(s)")

        for vid in videos:
            combined_text = f"{vid['title']} {vid['description']}"
            matched_tickers = [
                ticker for ticker, name in companies.items()
                if _mentions_company(combined_text, ticker, name)
            ]
            if not matched_tickers:
                continue

            key = f"yt|{vid['video_id']}"
            if key in seen_keys:
                continue

            # Fetch transcript for confirmed hits
            transcript = _get_transcript(vid["video_id"])

            hits.append({
                "source": label,
                "source_type": "youtube",
                "tickers": matched_tickers,
                "title": vid["title"],
                "url": vid["url"],
                "published": vid["published"],
                "transcript_snippet": transcript[:3_000] if transcript else "",
                "seen_key": key,
            })

    return hits
