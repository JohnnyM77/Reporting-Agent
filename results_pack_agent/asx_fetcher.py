# results_pack_agent/asx_fetcher.py
# Standalone ASX announcement fetcher.
# No dependency on agent.py / Bob's logic.

from __future__ import annotations

import datetime as dt
import re
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from .config import ASX_ANNOUNCEMENTS_URL, HTTP_TIMEOUT_SECS
from .models import Announcement
from .utils import log


def fetch_announcements(
    ticker: str,
    session: Optional[requests.Session] = None,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Announcement]:
    """Fetch ASX announcements for *ticker* within the optional date window.

    If *from_date* / *to_date* are omitted the last 6 months of history are
    returned without any date filter.

    The function never raises — network errors are logged and an empty list
    is returned.
    """
    from .utils import http_session as _http_session

    s = session or _http_session()
    url = ASX_ANNOUNCEMENTS_URL.format(ticker=ticker)

    try:
        r = s.get(url, timeout=HTTP_TIMEOUT_SECS)
        r.raise_for_status()
    except Exception as exc:
        log(f"[asx_fetcher] Failed to fetch announcements for {ticker}: {exc}")
        return []

    return _parse_announcements_html(r.text, ticker, from_date=from_date, to_date=to_date)


def _parse_announcements_html(
    html: str,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Announcement]:
    """Parse the ASX announcements HTML table and return Announcement objects."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table tr")

    items: List[Announcement] = []
    seen_urls: set[str] = set()

    for row in rows:
        cols = [c.get_text(" ", strip=True) for c in row.select("td")]
        if len(cols) < 2:
            continue

        link = row.select_one("a")
        if not link or not link.get("href"):
            continue

        title = link.get_text(" ", strip=True)
        href = str(link["href"])
        if href.startswith("/"):
            href = "https://www.asx.com.au" + href

        date_text = cols[0]
        time_text = cols[1] if len(cols) > 1 else ""

        # Only process rows with a parseable date
        try:
            item_date = dt.datetime.strptime(date_text, "%d/%m/%Y").date()
        except Exception:
            continue

        # Apply optional date window filter
        if from_date is not None and item_date < from_date:
            continue
        if to_date is not None and item_date > to_date:
            continue

        # Deduplicate by URL
        if href in seen_urls:
            continue
        seen_urls.add(href)

        items.append(
            Announcement(
                ticker=ticker,
                title=title,
                date=date_text,   # keep in DD/MM/YYYY (ASX format)
                time=time_text,
                url=href,
            )
        )

    return items
