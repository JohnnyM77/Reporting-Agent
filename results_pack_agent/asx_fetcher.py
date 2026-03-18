# results_pack_agent/asx_fetcher.py
# Standalone ASX announcement fetcher.
# No dependency on agent.py / Bob's logic.
#
# Fetch strategy (in order):
#   1. ASX v2 statistics endpoint — tries JSON parse first, then HTML parse.
#   2. ASX v1 JSON API            — newer endpoint, different JSON schema.
#   3. Company page HTML          — public HTML page as last resort.
#
# All three paths are logged so failures can be diagnosed.

from __future__ import annotations

import datetime as dt
import json as _json
import re
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from .config import (
    ASX_ANNOUNCEMENTS_URL,
    ASX_ANNOUNCEMENTS_URL_V1,
    ASX_COMPANY_PAGE_URL,
    HTTP_TIMEOUT_SECS,
)
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
    is returned.  It tries three endpoints in order and stops at the first
    that returns at least one announcement.
    """
    from .utils import http_session as _http_session

    s = session or _http_session()

    # ── 1. ASX v2 endpoint ────────────────────────────────────────────────────
    items = _fetch_v2(s, ticker, from_date=from_date, to_date=to_date)
    if items:
        return items

    # ── 2. ASX v1 JSON API ────────────────────────────────────────────────────
    log(f"[asx_fetcher] v2 returned zero items — trying v1 JSON API for {ticker}")
    items = _fetch_v1(s, ticker, from_date=from_date, to_date=to_date)
    if items:
        return items

    # ── 3. Company page HTML (last resort) ────────────────────────────────────
    log(f"[asx_fetcher] v1 returned zero items — falling back to company page for {ticker}")
    items = _fetch_company_page(s, ticker, from_date=from_date, to_date=to_date)
    if not items:
        log(
            f"[asx_fetcher] All fetch paths returned zero announcements for {ticker}. "
            "This likely indicates a fetch/parsing issue or unexpected ASX response format."
        )
    return items


# ── v2 endpoint ───────────────────────────────────────────────────────────────

def _fetch_v2(
    session: requests.Session,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Announcement]:
    """Fetch from the legacy v2 statistics endpoint."""
    url = ASX_ANNOUNCEMENTS_URL.format(ticker=ticker)
    log(f"[asx_fetcher] v2 URL={url}")

    try:
        r = session.get(url, timeout=HTTP_TIMEOUT_SECS)
        content_type = r.headers.get("Content-Type", "")
        log(
            f"[asx_fetcher] v2 status={r.status_code} "
            f"content_type={content_type!r}"
        )
        if r.status_code != 200:
            log(f"[asx_fetcher] v2 non-200 response — body preview: {r.text[:500]!r}")
            return []
    except Exception as exc:
        log(f"[asx_fetcher] v2 request failed for {ticker}: {exc}")
        return []

    # Try JSON first
    items = _parse_announcements_json(r.text, ticker, from_date=from_date, to_date=to_date)
    if items:
        log(
            f"[asx_fetcher] v2 JSON parsed — {len(items)} item(s). "
            f"Samples: {[a.title for a in items[:3]]}"
        )
        return items

    # Try HTML table
    items = _parse_announcements_html(r.text, ticker, from_date=from_date, to_date=to_date)
    if items:
        log(
            f"[asx_fetcher] v2 HTML parsed — {len(items)} item(s). "
            f"Samples: {[a.title for a in items[:3]]}"
        )
        return items

    # Neither parser succeeded — log a body snippet to help diagnose why
    log(
        f"[asx_fetcher] v2 JSON and HTML parsing both yielded zero items. "
        f"Body preview: {r.text[:500]!r}"
    )
    return []


# ── v1 JSON API endpoint ──────────────────────────────────────────────────────

def _fetch_v1(
    session: requests.Session,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Announcement]:
    """Fetch from the newer ASX v1 JSON API.

    The v1 API returns a JSON payload with the shape::

        {"data": [{"id": "...", "header": "...", "document_date": "...",
                   "url": "...", ...}, ...]}

    where ``document_date`` is an ISO 8601 timestamp.
    """
    url = ASX_ANNOUNCEMENTS_URL_V1.format(ticker=ticker)
    log(f"[asx_fetcher] v1 URL={url}")

    try:
        r = session.get(url, timeout=HTTP_TIMEOUT_SECS)
        content_type = r.headers.get("Content-Type", "")
        log(
            f"[asx_fetcher] v1 status={r.status_code} "
            f"content_type={content_type!r}"
        )
        if r.status_code != 200:
            log(f"[asx_fetcher] v1 non-200 response — body preview: {r.text[:500]!r}")
            return []
    except Exception as exc:
        log(f"[asx_fetcher] v1 request failed for {ticker}: {exc}")
        return []

    items = _parse_announcements_json_v1(r.text, ticker, from_date=from_date, to_date=to_date)
    if items:
        log(
            f"[asx_fetcher] v1 JSON parsed — {len(items)} item(s). "
            f"Samples: {[a.title for a in items[:3]]}"
        )
    else:
        log(
            f"[asx_fetcher] v1 JSON parsing yielded zero items. "
            f"Body preview: {r.text[:500]!r}"
        )
    return items


# ── Company page HTML fallback ────────────────────────────────────────────────

def _fetch_company_page(
    session: requests.Session,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Announcement]:
    """Scrape the public ASX company announcements page as a last resort."""
    url = ASX_COMPANY_PAGE_URL.format(ticker=ticker)
    log(f"[asx_fetcher] company page URL={url}")

    try:
        r = session.get(url, timeout=HTTP_TIMEOUT_SECS)
        content_type = r.headers.get("Content-Type", "")
        log(
            f"[asx_fetcher] company page status={r.status_code} "
            f"content_type={content_type!r}"
        )
        if r.status_code != 200:
            log(f"[asx_fetcher] company page non-200 — body preview: {r.text[:500]!r}")
            return []
    except Exception as exc:
        log(f"[asx_fetcher] company page request failed for {ticker}: {exc}")
        return []

    items = _parse_announcements_html(r.text, ticker, from_date=from_date, to_date=to_date)
    if items:
        log(
            f"[asx_fetcher] company page HTML parsed — {len(items)} item(s). "
            f"Samples: {[a.title for a in items[:3]]}"
        )
    else:
        log(
            f"[asx_fetcher] company page HTML parsing yielded zero items. "
            f"Body preview: {r.text[:500]!r}"
        )
    return items


# ── Date parsing ──────────────────────────────────────────────────────────────

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


# ── JSON parsers ──────────────────────────────────────────────────────────────

def _parse_announcements_json(
    text: str,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Announcement]:
    """Parse ASX announcements from a v2 JSON response body.

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


def _parse_announcements_json_v1(
    text: str,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Announcement]:
    """Parse ASX announcements from a v1 JSON API response body.

    The ASX v1 company announcements endpoint returns a JSON payload with the
    shape::

        {"data": [{"id": "...", "header": "...",
                   "document_date": "2026-03-17T10:00:00+11:00",
                   "url": "https://...", ...}, ...]}

    where ``document_date`` is an ISO 8601 timestamp.  The ``id`` field is used
    to construct a URL when the ``url`` field is absent or empty.

    Returns an empty list if *text* is not valid JSON or lacks the expected
    structure.
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

        # v1 uses "document_date" (ISO 8601) instead of "releasedDate"
        date_raw = str(row.get("document_date") or row.get("releasedDate") or "").strip()
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
            # Build URL from announcement id
            ann_id = str(row.get("id") or "").strip()
            if ann_id:
                href = (
                    f"https://www.asx.com.au/announcements/{ticker.upper()}/{ann_id}"
                )
        if not href:
            continue
        if href.startswith("/"):
            href = "https://www.asx.com.au" + href

        # Deduplicate by URL
        if href in seen_urls:
            continue
        seen_urls.add(href)

        asx_date = item_date.strftime("%d/%m/%Y")

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
