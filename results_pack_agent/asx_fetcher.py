# results_pack_agent/asx_fetcher.py
# ASX announcement fetcher for the Results Pack Agent.
#
# Thin wrapper around shared/asx_simple_fetcher — the single, no-fallback
# HTML fetch path for the Results Pack Agent.  One HTTP call, one parse,
# done.

from __future__ import annotations

import datetime as dt
from typing import List, Optional

import requests

from shared.asx_simple_fetcher import (
    fetch_announcements as _simple_fetch,
    parse_announcements_html as _parse_html,
)
from .models import Announcement
from .utils import log


def fetch_announcements(
    ticker: str,
    session: Optional[requests.Session] = None,
) -> List[Announcement]:
    """Fetch ASX announcements for *ticker* via the simple HTML fetcher.

    Makes a single HTTP GET to the ASX v2 statistics endpoint and parses the
    HTML response with BeautifulSoup.  No fallback stages, no retries.
    Returns an empty list on any error — never raises.

    Args:
        ticker:  ASX ticker code (e.g. ``"NHC"``).
        session: Optional requests session.  A new browser-like session is
                 created if not supplied.

    Returns:
        List of ``Announcement`` objects (may be empty on error or no data).
    """
    raw = _simple_fetch(ticker, session=session)
    log(f"[fetch] ticker={ticker} announcements_found={len(raw)}")
    return [
        Announcement(
            ticker=d["ticker"],
            title=d["title"],
            date=d["date"],
            time=d["time"],
            url=d["url"],
            pdf_url=d.get("pdf_url"),
        )
        for d in raw
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
    """Parse ASX v2 statistics HTML.

    Returns ``Announcement`` objects.  Kept for backwards-compatible imports.
    Date filtering is applied when *from_date* or *to_date* are provided.
    """
    items = _parse_html(html, ticker)
    result = []
    for i in items:
        if from_date is not None or to_date is not None:
            try:
                item_date = dt.datetime.strptime(i["date"], "%d/%m/%Y").date()
                if from_date is not None and item_date < from_date:
                    continue
                if to_date is not None and item_date > to_date:
                    continue
            except Exception:
                pass
        result.append(Announcement(
            ticker=i["ticker"],
            title=i["title"],
            date=i["date"],
            time=i["time"],
            url=i["url"],
            pdf_url=i.get("pdf_url"),
        ))
    return result
