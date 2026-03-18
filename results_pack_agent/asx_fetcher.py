# results_pack_agent/asx_fetcher.py
# Standalone ASX announcement fetcher.
# No dependency on agent.py / Bob's logic.

from __future__ import annotations

import datetime as dt
import json as _json
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

    # The ASX v2 endpoint may return JSON or HTML depending on server version.
    # Try JSON first; fall back to HTML if the response is not valid JSON or
    # the JSON doesn't contain the expected announcement structure.
    items = _parse_announcements_json(r.text, ticker, from_date=from_date, to_date=to_date)
    if not items:
        items = _parse_announcements_html(r.text, ticker, from_date=from_date, to_date=to_date)
    return items


def _parse_asx_release_date(date_str: str) -> Optional[dt.date]:
    """Parse an ASX release-date string in any known format.

    Handles:
    - ``DD/MM/YYYY`` — original ASX HTML table column format
    - ``DD/MM/YYYY H:MM am/pm`` — HTML format with time appended
    - ``YYYY-MM-DDThh:mm:ss...`` — ISO 8601 (JSON API)
    - ``YYYY-MM-DD`` — ISO date only
    """
    if not date_str:
        return None
    # ISO format first: "2026-03-17" or "2026-03-17T10:30:00.000+11:00"
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})", date_str.strip())
    if iso_match:
        try:
            return dt.datetime.strptime(iso_match.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    # ASX DMY format: "18/03/2026" or "18/03/2026 10:00 am"
    dmy_match = re.match(r"^(\d{2}/\d{2}/\d{4})", date_str.strip())
    if dmy_match:
        try:
            return dt.datetime.strptime(dmy_match.group(1), "%d/%m/%Y").date()
        except ValueError:
            pass
    return None


def _parse_announcements_json(
    text: str,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Announcement]:
    """Parse ASX announcements from a JSON response body.

    The ASX v2 announcements endpoint returns a JSON payload with the shape::

        {"data": [{"header": "...", "releasedDate": "...", "url": "...",
                   "documentKey": "..."}, ...]}

    Returns an empty list if *text* is not valid JSON or lacks the expected
    structure (so the caller can fall back to HTML parsing).
    """
    try:
        payload = _json.loads(text)
    except Exception:
        return []

    if not isinstance(payload, dict):
        return []

    rows = payload.get("data", [])
    if not isinstance(rows, list) or not rows:
        return []

    items: List[Announcement] = []
    seen_urls: set[str] = set()

    for row in rows:
        if not isinstance(row, dict):
            continue

        title = str(row.get("header") or "").strip()
        if not title:
            continue

        date_raw = str(row.get("releasedDate") or "").strip()
        item_date = _parse_asx_release_date(date_raw)
        if item_date is None:
            continue

        # Apply optional date window filter
        if from_date is not None and item_date < from_date:
            continue
        if to_date is not None and item_date > to_date:
            continue

        href = str(row.get("url") or "").strip()
        if not href:
            # Build URL from documentKey when a direct URL is absent
            doc_key = str(row.get("documentKey") or "").strip()
            if doc_key:
                href = (
                    "https://www.asx.com.au/asx/v2/statistics/"
                    f"displayAnnouncement.do?idsId={doc_key}"
                )
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.asx.com.au" + href

        # Deduplicate by URL
        if href in seen_urls:
            continue
        seen_urls.add(href)

        # Convert date to DD/MM/YYYY for the Announcement model
        asx_date = item_date.strftime("%d/%m/%Y")

        # Extract time component from the raw date string when present
        time_text = ""
        t_match = re.search(r"\d{1,2}:\d{2}(?:\s*[ap]m)?", date_raw, re.IGNORECASE)
        if t_match:
            time_text = t_match.group(0)

        items.append(
            Announcement(
                ticker=ticker,
                title=title,
                date=asx_date,
                time=time_text,
                url=href,
            )
        )

    return items


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
