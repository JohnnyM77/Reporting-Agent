# results_pack_agent/asx_fetcher.py
# ASX announcement fetcher for the Results Pack Agent.
#
# Delegates entirely to the shared asx_fetch module — the same code path used
# by Bob (agent.py).  Both agents call fetch_asx_announcements_html() from
# asx_fetch so they share exactly the same endpoint and parse logic.
#
#   - Single endpoint: ASX v2 statistics HTML endpoint
#   - HTML-only parsing (BeautifulSoup table rows)
#   - No company-page fallback (/companies/ URL — was broken/404)
#   - No v1 JSON API fallback

from __future__ import annotations

import datetime as dt
from typing import List, Optional

import requests

from asx_fetch import fetch_asx_announcements_html, parse_asx_html_announcements
from .models import Announcement
from .utils import http_session, log


def fetch_announcements(
    ticker: str,
    session: Optional[requests.Session] = None,
) -> List[Announcement]:
    """Fetch ASX announcements for *ticker* using Bob's shared code path.

    Delegates to the shared ``asx_fetch`` module so both agents hit the exact
    same endpoint and parse logic.  Never raises — network errors are logged
    and an empty list is returned.

    Args:
        ticker: ASX ticker code (e.g. ``"NHC"``).
        session: Optional requests session.  A new browser-like session is
            created if not supplied.

    Returns:
        List of ``Announcement`` objects (may be empty on error or no data).
    """
    s = session or http_session()
    ticker = ticker.upper().strip()

    try:
        raw = fetch_asx_announcements_html(s, ticker)
    except Exception as exc:
        log(f"[asx_fetcher] Fetch failed for {ticker}: {exc}")
        return []

    if not raw:
        print(f"[asx_fetcher] No announcements returned for {ticker} using shared Bob path")
    else:
        print(f"[asx_fetcher] Retrieved {len(raw)} announcements for {ticker}")
        print(f"[asx_fetcher] Sample titles: {[a['title'] for a in raw[:3]]}")

    return [
        Announcement(
            ticker=i["ticker"],
            title=i["title"],
            date=i["date"],
            time=i["time"],
            url=i["url"],
        )
        for i in raw
    ]


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
    return [
        Announcement(
            ticker=i["ticker"],
            title=i["title"],
            date=i["date"],
            time=i["time"],
            url=i["url"],
        )
        for i in parse_asx_html_announcements(html, ticker, from_date=from_date, to_date=to_date)
    ]
