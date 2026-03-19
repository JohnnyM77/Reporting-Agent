# results_pack_agent/asx_fetcher.py
# ASX announcement fetcher for the Results Pack Agent.
#
# Thin wrapper around the shared/asx_announcements module — the single source
# of truth for ASX announcement retrieval shared with Bob (agent.py).
# All fetch logic (including the 3-stage fallback) lives in shared/asx_announcements.py.

from __future__ import annotations

import datetime as dt
from typing import List, Optional

import requests

# Import from the canonical shared module.  The shared module in turn wraps
# asx_fetch.py so both Bob and the Results Pack Agent share exactly one
# retrieval and parsing code path.
from shared.asx_announcements import (
    fetch_ticker_announcements as _fetch_shared,
    parse_asx_html_announcements,   # re-exported for backward-compatible imports
)
from .models import Announcement
from .utils import http_session, log


def fetch_announcements(
    ticker: str,
    session: Optional[requests.Session] = None,
) -> List[Announcement]:
    """Fetch ASX announcements for *ticker* via the shared asx_announcements module.

    Delegates to ``shared.asx_announcements.fetch_ticker_announcements`` which
    applies the proven 3-stage fallback (legacy v2 endpoint → requests scrape →
    Playwright render).  Never raises — all errors are handled internally and
    an empty list is returned on total failure.

    Args:
        ticker:  ASX ticker code (e.g. ``"NHC"``).
        session: Optional requests session.  A new browser-like session is
                 created if not supplied.

    Returns:
        List of ``Announcement`` objects (may be empty on error or no data).
    """
    shared_anns = _fetch_shared(ticker, session=session)

    log(f"[asx_fetcher] shared_module=shared.asx_announcements announcements_found={len(shared_anns)} for {ticker}")

    return [
        Announcement(
            ticker=a.ticker,
            title=a.title,
            date=a.date,
            time=a.time,
            url=a.url,
            pdf_url=a.pdf_url,
        )
        for a in shared_anns
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
    """Parse ASX HTML using the shared parse path.

    Returns ``Announcement`` objects.  Thin wrapper around
    ``shared.asx_announcements.parse_asx_html_announcements`` kept for
    backwards-compatible imports.
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
