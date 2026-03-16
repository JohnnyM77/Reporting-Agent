# ned/emit.py
#
# Emits normalized InvestorEvent objects from Ned's collected news hits.
#
# This module is designed to be called by the Master Engine runner to collect
# events from Ned without running the full Ned pipeline (email, seen-state,
# etc.). It re-uses Ned's existing scanners and scoring logic.

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sys
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Allow imports from repo root
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from master_engine.schemas import (
    InvestorEvent,
    AGENT_NED,
    EVENT_TYPE_EARNINGS_RELEASE,
    EVENT_TYPE_GUIDANCE_CHANGE,
    EVENT_TYPE_CAPITAL_RAISE,
    EVENT_TYPE_TAKEOVER,
    EVENT_TYPE_REGULATOR_ACTION,
    EVENT_TYPE_MAJOR_CONTRACT,
    EVENT_TYPE_CEO_CHANGE,
    EVENT_TYPE_LITIGATION,
    EVENT_TYPE_PROFIT_WARNING,
    EVENT_TYPE_GENERIC_NEWS,
    EVENT_TYPE_APPENDIX_4D,
    EVENT_TYPE_APPENDIX_4E,
    EVENT_TYPE_ACQUISITION,
    normalise_ticker,
)

# ---------------------------------------------------------------------------
# Keyword → event_type mapping (ordered: first match wins)
# ---------------------------------------------------------------------------
_KEYWORD_EVENT_MAP: list[tuple[list[str], str]] = [
    (["appendix 4e"], EVENT_TYPE_APPENDIX_4E),
    (["appendix 4d"], EVENT_TYPE_APPENDIX_4D),
    (["half year results", "full year results", "hy results", "fy results",
      "half-year results", "full-year results", "earnings", "results"],
     EVENT_TYPE_EARNINGS_RELEASE),
    (["guidance", "outlook", "forecast update"], EVENT_TYPE_GUIDANCE_CHANGE),
    (["profit warning", "earnings downgrade", "downgrade"], EVENT_TYPE_PROFIT_WARNING),
    (["capital raise", "placement", "rights issue", "entitlement offer",
      "share purchase plan", "capital raising"],
     EVENT_TYPE_CAPITAL_RAISE),
    (["takeover", "scheme of arrangement", "merger", "bid", "acquisition offer"],
     EVENT_TYPE_TAKEOVER),
    (["acquisition", "acquires", "acquire"], EVENT_TYPE_ACQUISITION),
    (["regulator", "asic", "asx query", "investigation", "fine"],
     EVENT_TYPE_REGULATOR_ACTION),
    (["contract", "partnership", "agreement", "memorandum of understanding", "mou"],
     EVENT_TYPE_MAJOR_CONTRACT),
    (["ceo", "managing director", "chief executive", "leadership change"],
     EVENT_TYPE_CEO_CHANGE),
    (["lawsuit", "litigation", "legal proceedings", "court", "class action"],
     EVENT_TYPE_LITIGATION),
]


def _map_event_type(headline: str) -> str:
    """Map a news headline to a canonical event_type."""
    lower = headline.lower()
    for keywords, event_type in _KEYWORD_EVENT_MAP:
        for kw in keywords:
            if re.search(r"\b" + re.escape(kw) + r"\b", lower):
                return event_type
    return EVENT_TYPE_GENERIC_NEWS


def _hit_to_event(
    hit: dict,
    companies: dict[str, str],
    lookback_hours: int,
) -> list[InvestorEvent]:
    """
    Convert a single Ned news hit to one InvestorEvent per matched ticker.

    Returns an empty list if the hit has no tickers.
    """
    tickers: list[str] = hit.get("tickers", [])
    if not tickers:
        return []

    title: str = hit.get("title", "")
    description: str = hit.get("description", "") or hit.get("transcript_snippet", "")
    url: str = hit.get("link", "") or hit.get("url", "")
    source: str = hit.get("source", "")
    published = hit.get("published")

    # Build timestamp
    if isinstance(published, dt.datetime):
        timestamp = published.isoformat()
    elif isinstance(published, str):
        timestamp = published
    else:
        timestamp = dt.datetime.utcnow().isoformat() + "Z"

    event_type = _map_event_type(title)
    events: list[InvestorEvent] = []

    for ticker in tickers:
        # Normalise to .AX format for ASX tickers
        normalised_ticker = normalise_ticker(ticker)
        company_name = companies.get(ticker, companies.get(normalised_ticker, ticker))

        source_links: dict[str, str] = {}
        if url:
            source_links["source_article"] = url

        events.append(
            InvestorEvent(
                ticker=normalised_ticker,
                company_name=company_name,
                agent=AGENT_NED,
                event_type=event_type,
                headline=title[:200],
                timestamp=timestamp,
                summary=description[:500] if description else "",
                action="Review news item",
                source_links=source_links,
            )
        )

    return events


def collect_events(lookback_hours: int = 48) -> list[InvestorEvent]:
    """
    Collect normalized InvestorEvent objects from Ned's news scanners.

    This function runs the Ned scanners in a lightweight mode (no LLM
    summarisation, no email, no seen-state update) and converts hits to
    InvestorEvent objects.

    Parameters
    ----------
    lookback_hours : int
        How far back to scan for news items.

    Returns
    -------
    list[InvestorEvent]
    """
    tickers_path = _REPO_ROOT / "tickers.yaml"
    media_sources_path = _REPO_ROOT / "media_sources.yaml"

    try:
        with open(tickers_path) as fh:
            ticker_data = yaml.safe_load(fh) or {}
    except Exception as exc:
        logger.error("[ned/emit] Could not load tickers.yaml: %s", exc)
        return []

    asx = ticker_data.get("asx", {})
    lse = ticker_data.get("lse", {})
    skip = set(ticker_data.get("etf_tickers", []))
    companies: dict[str, str] = {}
    if isinstance(asx, dict):
        companies.update({k: v for k, v in asx.items() if k not in skip})
    if isinstance(lse, dict):
        companies.update(lse)

    try:
        with open(media_sources_path) as fh:
            media = yaml.safe_load(fh) or {}
    except Exception as exc:
        logger.warning("[ned/emit] Could not load media_sources.yaml: %s", exc)
        media = {}

    yt_channels: list[dict] = media.get("youtube_channels", [])
    rss_feeds: list[dict] = media.get("rss_feeds", [])
    skip_news = set(media.get("skip_news_tickers", []))
    news_companies = {k: v for k, v in companies.items() if k not in skip_news}

    seen: set[str] = set()  # Fresh seen-set — we don't update Ned's persisted state

    events: list[InvestorEvent] = []
    hit_count = 0

    # Scan YouTube
    try:
        from ned.youtube_scanner import scan_youtube_channels
        yt_hits = scan_youtube_channels(yt_channels, companies, lookback_hours, seen)
        hit_count += len(yt_hits)
        logger.info("[ned/emit] YouTube: %d hit(s)", len(yt_hits))
        for hit in yt_hits:
            events.extend(_hit_to_event(hit, companies, lookback_hours))
    except Exception as exc:
        logger.warning("[ned/emit] YouTube scanner failed: %s", exc)

    # Scan RSS + Yahoo Finance
    try:
        from ned.news_scanner import scan_rss_feeds, scan_yahoo_finance
        rss_hits = scan_rss_feeds(rss_feeds, news_companies, lookback_hours, seen)
        yf_hits = scan_yahoo_finance(news_companies, lookback_hours, seen)
        news_hits = rss_hits + yf_hits
        hit_count += len(news_hits)
        logger.info("[ned/emit] News (RSS + YF): %d hit(s)", len(news_hits))
        for hit in news_hits:
            events.extend(_hit_to_event(hit, companies, lookback_hours))
    except Exception as exc:
        logger.warning("[ned/emit] News scanner failed: %s", exc)

    logger.info(
        "[ned/emit] %d raw hit(s) → %d InvestorEvent(s)", hit_count, len(events)
    )
    return events
