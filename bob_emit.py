# bob_emit.py
#
# Emits normalized InvestorEvent objects from Bob's (agent.py) ASX
# announcement data.
#
# This module reads Bob's dashboard JSON output (docs/data/bob.json) to
# convert already-processed announcements into InvestorEvent objects.
# This avoids re-running the full Bob pipeline (which involves LLM calls,
# PDF downloads, etc.) and instead consumes Bob's latest output.
#
# For a full live run, call bob_collect_live() which invokes Bob's ASX
# announcement fetcher directly.

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT))

from master_engine.schemas import (
    InvestorEvent,
    AGENT_BOB,
    EVENT_TYPE_EARNINGS_RELEASE,
    EVENT_TYPE_GUIDANCE_CHANGE,
    EVENT_TYPE_CAPITAL_RAISE,
    EVENT_TYPE_TAKEOVER,
    EVENT_TYPE_MAJOR_CONTRACT,
    EVENT_TYPE_GENERIC_NEWS,
    EVENT_TYPE_APPENDIX_4D,
    EVENT_TYPE_APPENDIX_4E,
    EVENT_TYPE_ACQUISITION,
    EVENT_TYPE_PROFIT_WARNING,
    normalise_ticker,
)

_BOB_JSON = _REPO_ROOT / "docs" / "data" / "bob.json"

# ---------------------------------------------------------------------------
# Map Bob's internal announcement type strings to canonical event_type values
# ---------------------------------------------------------------------------
_BOB_TYPE_MAP: dict[str, str] = {
    "results": EVENT_TYPE_EARNINGS_RELEASE,
    "acquisition": EVENT_TYPE_ACQUISITION,
    "capital": EVENT_TYPE_CAPITAL_RAISE,
}

# Keyword-based fallback mapping for FYI / MATERIAL items that lack a type tag
_KEYWORD_MAP: list[tuple[list[str], str]] = [
    (["appendix 4e"], EVENT_TYPE_APPENDIX_4E),
    (["appendix 4d"], EVENT_TYPE_APPENDIX_4D),
    (["half year", "full year", "results", "earnings", "financial report",
      "annual report", "interim"], EVENT_TYPE_EARNINGS_RELEASE),
    (["guidance", "outlook", "forecast"], EVENT_TYPE_GUIDANCE_CHANGE),
    (["profit warning", "downgrade"], EVENT_TYPE_PROFIT_WARNING),
    (["capital raise", "placement", "rights issue", "entitlement",
      "share purchase plan", "capital raising", "debt facility",
      "refinance", "bond", "convertible"], EVENT_TYPE_CAPITAL_RAISE),
    (["takeover", "scheme", "merger", "bid"], EVENT_TYPE_TAKEOVER),
    (["acquisition", "acquire", "acquires", "transaction"], EVENT_TYPE_ACQUISITION),
    (["contract", "award", "agreement", "partnership"], EVENT_TYPE_MAJOR_CONTRACT),
]


def _infer_event_type(title: str, item_type: Optional[str] = None) -> str:
    if item_type and item_type in _BOB_TYPE_MAP:
        return _BOB_TYPE_MAP[item_type]
    lower = title.lower()
    for keywords, event_type in _KEYWORD_MAP:
        if any(kw in lower for kw in keywords):
            return event_type
    return EVENT_TYPE_GENERIC_NEWS


def _item_to_event(
    item: dict,
    tier: str,
    timestamp: str,
) -> Optional[InvestorEvent]:
    """Convert a single Bob dashboard item to an InvestorEvent."""
    ticker = item.get("ticker", "")
    title = item.get("title", "")
    url = item.get("url", "")
    item_type = item.get("type")

    if not ticker or not title:
        return None

    # Normalise ticker to .AX
    normalised = normalise_ticker(ticker)

    event_type = _infer_event_type(title, item_type)

    source_links: dict[str, str] = {}
    if url:
        source_links["asx_announcement"] = url

    # Priority hint based on Bob's tier classification
    if tier == "high_impact":
        action = "Read full Bob analysis — see email/Drive report"
    elif tier == "material":
        action = "Review price-sensitive announcement"
    else:
        action = "FYI — open announcement if relevant"

    return InvestorEvent(
        ticker=normalised,
        company_name=ticker,  # Bob dashboard doesn't include company name; will be enriched
        agent=AGENT_BOB,
        event_type=event_type,
        headline=title[:200],
        timestamp=timestamp,
        action=action,
        asx_url=url if url else None,
        source_links=source_links,
    )


def collect_events_from_dashboard() -> list[InvestorEvent]:
    """
    Read Bob's dashboard JSON and convert items to InvestorEvent objects.

    Returns an empty list if bob.json does not exist or is unreadable.
    """
    if not _BOB_JSON.exists():
        logger.info(
            "[bob_emit] %s not found — no Bob events available", _BOB_JSON
        )
        return []

    try:
        data = json.loads(_BOB_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("[bob_emit] Could not parse bob.json: %s", exc)
        return []

    last_run = data.get("last_run", dt.datetime.utcnow().date().isoformat())
    # Use a rough timestamp for recency scoring (noon SGT on last_run date)
    timestamp = f"{last_run}T04:00:00Z"  # 04:00 UTC ≈ 12:00 SGT

    events: list[InvestorEvent] = []

    # High impact items (HY/FY results, acquisitions, capital raises)
    for item in data.get("high_impact", []):
        ev = _item_to_event(item, "high_impact", timestamp)
        if ev:
            events.append(ev)

    # Material items (price-sensitive)
    for item in data.get("material", []):
        ev = _item_to_event(item, "material", timestamp)
        if ev:
            events.append(ev)

    # FYI items (all announcements)
    for item in data.get("fyi", []):
        ev = _item_to_event(item, "fyi", timestamp)
        if ev:
            events.append(ev)

    logger.info(
        "[bob_emit] %d InvestorEvent(s) from dashboard (last_run: %s)",
        len(events), last_run,
    )
    return events


def collect_events_live(hours_back: int = 24) -> list[InvestorEvent]:
    """
    Collect live Bob events by running a lightweight version of the ASX
    announcement fetcher (no LLM, no PDF download, no email).

    Parameters
    ----------
    hours_back : int
        How many hours back to look for announcements.

    Returns
    -------
    list[InvestorEvent]
    """
    import yaml

    try:
        from agent import (
            http_session,
            fetch_asx_announcements,
            is_price_sensitive_title,
            classify_from_title_only,
            looks_like_results_title,
            now_sgt,
        )
    except Exception as exc:
        logger.error("[bob_emit] Could not import from agent.py: %s", exc)
        return []

    try:
        with open(_REPO_ROOT / "tickers.yaml") as fh:
            ticker_data = yaml.safe_load(fh) or {}
    except Exception as exc:
        logger.error("[bob_emit] Could not load tickers.yaml: %s", exc)
        return []

    asx = ticker_data.get("asx", {})
    asx_tickers = list(asx.keys()) if isinstance(asx, dict) else list(asx)

    session = http_session()
    timestamp = dt.datetime.utcnow().isoformat() + "Z"
    events: list[InvestorEvent] = []

    for ticker in asx_tickers:
        try:
            items = fetch_asx_announcements(session, ticker, hours_back=hours_back)
        except Exception as exc:
            logger.warning("[bob_emit] Fetch failed for %s: %s", ticker, exc)
            continue

        for item in items:
            title = item.get("title", "")
            url = item.get("url", "")
            if not title:
                continue

            normalised = normalise_ticker(ticker)
            event_type = _infer_event_type(title)

            source_links: dict[str, str] = {}
            if url:
                source_links["asx_announcement"] = url

            action = (
                "Review results — run Bob for full analysis"
                if looks_like_results_title(title)
                else "Review announcement"
            )

            events.append(
                InvestorEvent(
                    ticker=normalised,
                    company_name=asx.get(ticker, ticker) if isinstance(asx, dict) else ticker,
                    agent=AGENT_BOB,
                    event_type=event_type,
                    headline=title[:200],
                    timestamp=timestamp,
                    action=action,
                    asx_url=url if url else None,
                    source_links=source_links,
                )
            )

    logger.info(
        "[bob_emit] %d InvestorEvent(s) collected live (%d tickers, %dh lookback)",
        len(events), len(asx_tickers), hours_back,
    )
    return events


# Default collector — reads from dashboard JSON (fast, no API calls)
def collect_events() -> list[InvestorEvent]:
    """
    Collect Bob events.  Reads from the dashboard JSON first; falls back to
    live fetching if the JSON is absent.
    """
    events = collect_events_from_dashboard()
    if not events:
        logger.info("[bob_emit] Dashboard empty — falling back to live fetch")
        events = collect_events_live()
    return events
