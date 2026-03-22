# shared/asx_simple_fetcher.py
# Single-stage ASX announcement fetcher.
#
# This is the ONLY ASX retrieval logic used by the Results Pack Agent.
# No fallbacks, no multi-stage pipelines — just one clean HTTP call and
# a BeautifulSoup HTML parse.
#
# Endpoint:
#   https://www.asx.com.au/asx/v2/statistics/announcements.do
#       ?asxCode={TICKER}&by=asxCode&period=M6&timeframe=D
#
# Returns:
#   List of dicts:  {title, date (DD/MM/YYYY), time, url, pdf_url}

from __future__ import annotations

import re
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

ASX_V2_URL = (
    "https://www.asx.com.au/asx/v2/statistics/announcements.do"
    "?asxCode={ticker}&by=asxCode&period=M6&timeframe=D"
)

HTTP_TIMEOUT_SECS = 30

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*",
    "Referer": "https://www.asx.com.au/",
}


def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _normalise_href(href: str) -> str:
    if href.startswith("/"):
        return "https://www.asx.com.au" + href
    return href


def _extract_ids_id(url: str) -> Optional[str]:
    """Extract the idsId query parameter from an ASX announcement URL."""
    m = re.search(r"[?&]idsId=([^&]+)", url)
    return m.group(1) if m else None


def _build_pdf_url(ids_id: Optional[str]) -> Optional[str]:
    """Build the direct PDF display URL from an idsId value."""
    if not ids_id:
        return None
    return (
        f"https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do"
        f"?display=pdf&idsId={ids_id}"
    )


_DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}(?:\s*[ap]m)?)\b", re.IGNORECASE)


def _extract_date_time(cell_text: str) -> Optional[tuple]:
    """Extract (date_str, time_str) from raw cell text.

    Handles formats where date and time appear in the same cell, e.g.:
        "17/03/2026"
        "17/03/2026 05:21 AM"
        "17/03/2026\n05:21 AM"
    Returns ``None`` if no DD/MM/YYYY date is found.
    """
    m_date = _DATE_RE.search(cell_text)
    if not m_date:
        return None
    date_str = m_date.group(1)
    m_time = _TIME_RE.search(cell_text)
    time_str = m_time.group(1) if m_time else ""
    return date_str, time_str


def parse_announcements_html(html: str, ticker: str) -> List[Dict]:
    """Parse the ASX v2 statistics HTML page and return a list of announcement dicts.

    Each dict has:
        ticker   – uppercased ASX ticker code
        title    – announcement headline
        date     – announcement date in DD/MM/YYYY format
        time     – time string (e.g. "10:21 am", may be empty)
        url      – full absolute URL to the announcement page
        pdf_url  – direct PDF URL derived from the idsId parameter (may be None)

    Rows without a parseable DD/MM/YYYY date are skipped.
    Duplicate URLs are deduplicated.
    The date may appear in a single cell combined with the time
    (e.g. "17/03/2026 05:21 AM") or in separate cells.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table tr")

    items: List[Dict] = []
    seen: set = set()

    for row in rows:
        cols = row.select("td")
        if len(cols) < 2:
            continue

        link = row.select_one("a[href]")
        if not link:
            continue

        title = link.get_text(" ", strip=True)
        href = _normalise_href(str(link["href"]))

        # Extract date (and optionally time) from the first column.
        # The real ASX page often places date and time in the same cell.
        first_col_text = cols[0].get_text(" ", strip=True)
        dt_pair = _extract_date_time(first_col_text)
        if not dt_pair:
            continue
        date_text, time_text = dt_pair

        # If time was not found in col[0], try col[1] (separate-cell layout).
        if not time_text and len(cols) > 1:
            second_col_text = cols[1].get_text(" ", strip=True)
            m_time = _TIME_RE.search(second_col_text)
            if m_time:
                time_text = m_time.group(1)

        if href in seen:
            continue
        seen.add(href)

        ids_id = _extract_ids_id(href)
        pdf_url = _build_pdf_url(ids_id)

        items.append({
            "ticker": ticker.upper(),
            "title": title,
            "date": date_text,
            "time": time_text,
            "url": href,
            "pdf_url": pdf_url,
        })

    return items


def fetch_announcements(
    ticker: str,
    session: Optional[requests.Session] = None,
) -> List[Dict]:
    """Fetch ASX announcements for *ticker* from the v2 statistics endpoint.

    Makes a single HTTP GET request.  Returns an empty list on any error
    — never raises.

    Args:
        ticker:  ASX ticker code (e.g. ``"NHC"``).
        session: Optional ``requests.Session``.  A new session is created if
                 not provided.

    Returns:
        List of announcement dicts (may be empty on error or no data).
    """
    ticker = ticker.upper().strip()
    if session is None:
        session = _make_session()

    url = ASX_V2_URL.format(ticker=ticker)
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT_SECS)
        r.raise_for_status()
        return parse_announcements_html(r.text, ticker)
    except Exception as exc:
        print(f"[asx_simple_fetcher] fetch failed for {ticker}: {exc}")
        return []
