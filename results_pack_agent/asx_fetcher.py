# results_pack_agent/asx_fetcher.py
# ASX announcement fetcher for the Results Pack Agent.
#
# Uses the SAME code path as Bob (agent.py) via the shared asx_fetch module:
#   - Single endpoint: ASX v2 statistics HTML endpoint
#   - HTML-only parsing (BeautifulSoup table rows)
#   - No company-page fallback (/companies/ URL — was broken/404)
#   - No v1 JSON API fallback

from __future__ import annotations

import datetime as dt
from typing import List, Optional

import requests

from asx_fetch import (
    fetch_asx_announcements_html,
    parse_asx_html_announcements,
)
from .models import Announcement
from .utils import log


def fetch_announcements(
    ticker: str,
    session: Optional[requests.Session] = None,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Announcement]:
    """Fetch ASX announcements for *ticker* using Bob's proven v2 HTML code path.

    Delegates to the shared ``asx_fetch`` module so both agents hit the exact
    same endpoint and parse logic.

    If *from_date* / *to_date* are omitted the full 6-month history is returned
    without any calendar pre-filter.

    Never raises — network errors are logged and an empty list is returned.
    """
    from .utils import http_session as _http_session

    s = session or _http_session()
    ticker = ticker.upper().strip()

    log(f"[asx_fetcher] Fetching announcements for {ticker} via shared asx_fetch …")

    try:
        raw = fetch_asx_announcements_html(
            s, ticker, from_date=from_date, to_date=to_date
        )
    except Exception as exc:
        log(f"[asx_fetcher] Fetch failed for {ticker}: {exc}")
        return []

    if not raw:
        log(
            f"[asx_fetcher] No items returned for {ticker}. "
            "Check the HTTP status and body preview in asx_fetch logs."
        )
        return []

    log(
        f"[asx_fetcher] {len(raw)} item(s) for {ticker}. "
        f"Samples: {[i['title'] for i in raw[:3]]}"
    )
    return _dicts_to_announcements(raw)


# ── HTML parse shim ───────────────────────────────────────────────────────────
# Kept so callers and tests that import _parse_announcements_html directly
# from this module continue to work without modification.

def _parse_announcements_html(
    html: str,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Announcement]:
    """Parse ASX HTML using the shared asx_fetch parse path.

    Returns ``Announcement`` objects.  This is a thin wrapper around
    ``asx_fetch.parse_asx_html_announcements`` kept for backwards-compatible
    imports.
    """
    return _dicts_to_announcements(
        parse_asx_html_announcements(html, ticker, from_date=from_date, to_date=to_date)
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _dicts_to_announcements(items: list) -> List[Announcement]:
    """Convert raw dicts (from asx_fetch) to Announcement objects."""
    return [
        Announcement(
            ticker=i["ticker"],
            title=i["title"],
            date=i["date"],
            time=i["time"],
            url=i["url"],
        )
        for i in items
    ]
