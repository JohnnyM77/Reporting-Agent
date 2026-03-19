# results_pack_agent/asx_fetcher.py
# ASX announcement fetcher for the Results Pack Agent.
#
# Thin wrapper around the shared asx_fetch module — the same code path used
# by Bob (agent.py).  All fetch logic (including fallbacks) lives in asx_fetch.

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
    """Fetch ASX announcements for *ticker* using the shared asx_fetch code path.

    Delegates entirely to ``asx_fetch.fetch_asx_announcements_html`` which
    tries the legacy v2 endpoint first, then falls back to company-page scraping
    (requests then Playwright) if needed.  Never raises — all errors are handled
    internally by asx_fetch and an empty list is returned on total failure.

    Args:
        ticker: ASX ticker code (e.g. ``"NHC"``).
        session: Optional requests session.  A new browser-like session is
            created if not supplied.

    Returns:
        List of ``Announcement`` objects (may be empty on error or no data).
    """
    s = session or http_session()
    raw = fetch_asx_announcements_html(s, ticker)

    log(f"[asx_fetcher] announcements_found={len(raw)} for {ticker}")

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
