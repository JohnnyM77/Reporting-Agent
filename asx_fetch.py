# asx_fetch.py
# Shared ASX announcement fetching module.
#
# This module contains the SINGLE source of truth for fetching ASX announcements
# from the v2 statistics HTML endpoint — the proven code path used by Bob (agent.py).
#
# Both agent.py and results_pack_agent use this module so they share exactly the
# same retrieval and parsing logic.
#
# Public API
# ----------
# fetch_asx_announcements_html(session, ticker, from_date, to_date)  -> List[Dict]
#   Full fetch: HTTP GET + HTML parse.  Raises on network error.
#
# parse_asx_html_announcements(html, ticker, from_date, to_date)     -> List[Dict]
#   Pure parse: no network.  Useful for unit tests.
#
# Each Dict has keys: exchange, ticker, date (DD/MM/YYYY), time, title, url.

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

# The single proven ASX endpoint — 6 months of history, all announcement types.
ASX_V2_URL = (
    "https://www.asx.com.au/asx/v2/statistics/announcements.do"
    "?asxCode={ticker}&by=asxCode&period=M6&timeframe=D"
)

HTTP_TIMEOUT_SECS = 30


def fetch_asx_announcements_html(
    session: requests.Session,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Dict]:
    """Fetch ASX announcements for *ticker* from the v2 statistics HTML endpoint.

    This is Bob's proven retrieval path.  Both ``agent.py`` and
    ``results_pack_agent`` call this function so they share the exact same
    endpoint and parse logic.

    * If *from_date* / *to_date* are omitted every row returned by the endpoint
      (typically ~6 months of history) is included without any calendar filter.
    * Network errors propagate as exceptions — the caller decides how to handle
      them.

    Returns a list of dicts with keys:
        exchange  – always "ASX"
        ticker    – uppercased ticker code
        date      – announcement date in DD/MM/YYYY format
        time      – time string (may be empty)
        title     – announcement headline
        url       – full absolute URL
    """
    url = ASX_V2_URL.format(ticker=ticker.upper())
    r = session.get(url, timeout=HTTP_TIMEOUT_SECS)
    r.raise_for_status()
    return parse_asx_html_announcements(r.text, ticker, from_date=from_date, to_date=to_date)


def parse_asx_html_announcements(
    html: str,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Dict]:
    """Parse the ASX announcements HTML table and return a list of dicts.

    This is the same HTML parse logic used by Bob (agent.py).  It is exposed
    separately so tests can inject mock HTML without making network requests.

    Rows without a parseable DD/MM/YYYY date in the first column are silently
    skipped.  Duplicates (same URL) are deduplicated.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table tr")

    items: List[Dict] = []
    seen: set = set()

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

        try:
            item_date = dt.datetime.strptime(date_text, "%d/%m/%Y").date()
        except Exception:
            continue

        if from_date is not None and item_date < from_date:
            continue
        if to_date is not None and item_date > to_date:
            continue

        if href in seen:
            continue
        seen.add(href)

        items.append({
            "exchange": "ASX",
            "ticker": ticker.upper(),
            "date": date_text,
            "time": time_text,
            "title": title,
            "url": href,
        })

    return items
