# shared/asx_announcements.py
# Single source of truth for ASX announcement retrieval.
#
# This module is the canonical shared fetch layer used by:
#   - Bob (agent.py)
#   - Results Pack Agent (results_pack_agent/)
#
# Both agents import from here (or from asx_fetch which this wraps) so they
# share exactly the same retrieval and parsing logic.
#
# Public API
# ----------
# fetch_ticker_announcements(ticker, months, session)           -> list[Announcement]
#   Fetch the announcement history for a ticker (default: last 6 months).
#
# fetch_ticker_announcements_by_date(ticker, target_date, tolerance_days, session)
#                                                                -> list[Announcement]
#   Return announcements within *tolerance_days* of *target_date*.
#
# fetch_ticker_announcements_replay(ticker, from_date, to_date, session)
#                                                                -> list[Announcement]
#   Return announcements in a specific date window (replay/back-test mode).
#
# build_result_pack(ticker, report_type, target_date, session)  -> Optional[ResultPack]
#   High-level convenience: fetch announcements then detect the latest HY/FY pack.
#   Delegates detection to results_pack_agent.pack_detector (lazy import to
#   avoid circular dependencies).
#
# Re-exported from asx_fetch for backward-compatibility:
#   fetch_asx_announcements_html, parse_asx_html_announcements,
#   parse_company_page_html
#
# Announcement fields
# -------------------
# ticker       – ASX ticker code (uppercase)
# title        – announcement headline
# date         – date string in DD/MM/YYYY (ASX) format
# time         – time string (may be empty)
# url          – full absolute announcement URL
# company_name – company display name if available (None otherwise)
# pdf_url      – direct PDF URL if derivable from the announcement URL
# asx_ids_id   – ASX IDs document ID extracted from the URL (None otherwise)
# file_size    – file size in bytes if reported by the endpoint (None otherwise)

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import List, Optional, TYPE_CHECKING

import requests

# The repo-root shared fetch module is the underlying engine for all
# ASX announcement retrieval.  Do NOT duplicate its logic here.
from asx_fetch import (  # noqa: F401  (re-exported for callers)
    fetch_asx_announcements_html,
    parse_asx_html_announcements,
    parse_company_page_html,
)

if TYPE_CHECKING:
    # Avoid a hard circular import; pack_detector is only needed at runtime
    # inside build_result_pack().
    pass


# ---------------------------------------------------------------------------
# Announcement model
# ---------------------------------------------------------------------------

@dataclass
class Announcement:
    """A single ASX announcement item from the shared retrieval layer.

    This is the canonical announcement type shared by Bob and the Results Pack
    Agent.  Fields map directly to the data available from the ASX v2 HTML
    endpoint (and the company-page scrape fallbacks).

    Note: pdf_bytes and pdf_path are intentionally absent here — those are
    download-specific fields added by the Results Pack Agent's pdf_downloader.
    """

    ticker: str
    title: str
    date: str                        # DD/MM/YYYY (ASX format)
    time: str                        # e.g. "10:15 am" (may be empty)
    url: str                         # full absolute URL
    company_name: Optional[str] = None
    pdf_url: Optional[str] = None    # direct PDF URL if derivable
    asx_ids_id: Optional[str] = None # ASX IDs document key extracted from URL
    file_size: Optional[int] = None  # bytes (if reported by endpoint)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _http_session() -> requests.Session:
    """Return a requests Session with browser-like headers for ASX."""
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": "https://www.asx.com.au/",
        }
    )
    return s


def _extract_asx_ids_id(url: str) -> Optional[str]:
    """Extract the ASX IDs document key from an announcement URL.

    Example::

        https://www.asx.com.au/asx/statistics/displayAnnouncement.do
            ?display=pdf&idsId=02934567

        → "02934567"
    """
    m = re.search(r"[?&]idsId=([^&]+)", url, re.I)
    if m:
        return m.group(1)
    # Also handle documentKey query param
    m = re.search(r"[?&]documentKey=([^&]+)", url, re.I)
    if m:
        return m.group(1)
    return None


def _raw_to_announcement(raw: dict) -> Announcement:
    """Convert a raw dict from asx_fetch into a shared Announcement."""
    url = raw.get("url", "")
    return Announcement(
        ticker=raw.get("ticker", ""),
        title=raw.get("title", ""),
        date=raw.get("date", ""),
        time=raw.get("time", ""),
        url=url,
        company_name=None,
        pdf_url=raw.get("pdf_url"),         # asx_fetch may populate this
        asx_ids_id=_extract_asx_ids_id(url),
        file_size=raw.get("file_size"),
    )


# ---------------------------------------------------------------------------
# Public fetch functions
# ---------------------------------------------------------------------------

def fetch_ticker_announcements(
    ticker: str,
    months: int = 6,  # noqa: ARG001  (future: pass to endpoint)
    session: Optional[requests.Session] = None,
) -> List[Announcement]:
    """Fetch the last *months* of announcements for *ticker*.

    Delegates to ``asx_fetch.fetch_asx_announcements_html`` which implements
    the proven 3-stage fallback (legacy v2 endpoint → requests scrape →
    Playwright render).  Never raises; returns [] on total failure.

    Args:
        ticker:  ASX ticker code (case-insensitive).
        months:  How many months of history to retrieve.  Currently the
                 underlying ASX v2 endpoint always returns ~6 months; this
                 parameter is accepted for forward-compatibility.
        session: Optional requests.Session.  A new session is created if
                 not supplied.

    Returns:
        List of :class:`Announcement` objects, newest-first.
    """
    s = session or _http_session()
    raw = fetch_asx_announcements_html(s, ticker.upper().strip())
    return [_raw_to_announcement(r) for r in raw]


def fetch_ticker_announcements_by_date(
    ticker: str,
    target_date: dt.date,
    tolerance_days: int = 3,
    session: Optional[requests.Session] = None,
) -> List[Announcement]:
    """Return announcements within *tolerance_days* of *target_date*.

    Fetches the full history first (no pre-filter), then filters client-side.
    This avoids the early date-filtering bug that caused false failures in
    earlier versions of the Results Pack Agent.

    Args:
        ticker:         ASX ticker code.
        target_date:    Centre of the date window.
        tolerance_days: Inclusive window on each side of *target_date*.
        session:        Optional requests.Session.

    Returns:
        Announcements whose date is within [target_date - tolerance_days,
        target_date + tolerance_days].
    """
    all_anns = fetch_ticker_announcements(ticker, session=session)
    lo = target_date - dt.timedelta(days=tolerance_days)
    hi = target_date + dt.timedelta(days=tolerance_days)

    filtered: List[Announcement] = []
    for ann in all_anns:
        try:
            d = dt.datetime.strptime(ann.date, "%d/%m/%Y").date()
        except Exception:
            continue
        if lo <= d <= hi:
            filtered.append(ann)
    return filtered


def fetch_ticker_announcements_replay(
    ticker: str,
    from_date: dt.date,
    to_date: dt.date,
    session: Optional[requests.Session] = None,
) -> List[Announcement]:
    """Return announcements in the [from_date, to_date] window.

    Useful for replay/back-test mode where a specific historical date range
    is required.

    Args:
        ticker:    ASX ticker code.
        from_date: Start of the date window (inclusive).
        to_date:   End of the date window (inclusive).
        session:   Optional requests.Session.

    Returns:
        Announcements whose date falls within [from_date, to_date].
    """
    s = session or _http_session()
    raw = fetch_asx_announcements_html(
        s,
        ticker.upper().strip(),
        from_date=from_date,
        to_date=to_date,
    )
    return [_raw_to_announcement(r) for r in raw]


def build_result_pack(
    ticker: str,
    report_type: Optional[str] = None,
    target_date: Optional[dt.date] = None,
    session: Optional[requests.Session] = None,
) -> Optional[object]:
    """High-level convenience: fetch announcements then detect the HY/FY pack.

    This is the recommended single-call entry point for callers that want
    both fetching and pack-detection in one step.

    Detection delegates to ``results_pack_agent.pack_detector.detect_result_pack``
    (lazy import to avoid a hard circular dependency).  The function returns
    a ``results_pack_agent.models.ResultPack`` on success, or ``None`` if no
    matching pack is found.

    Args:
        ticker:      ASX ticker code.
        report_type: "HY", "FY", or None to auto-detect.
        target_date: Prefer packs on this date (treated as a preference, not
                     a hard pre-fetch filter).
        session:     Optional requests.Session.

    Returns:
        A ``ResultPack`` instance or ``None``.
    """
    # Lazy imports to avoid circular dependency at module load time.
    from results_pack_agent.models import Announcement as RpaAnnouncement  # noqa: PLC0415
    from results_pack_agent.pack_detector import detect_result_pack  # noqa: PLC0415

    shared_anns = fetch_ticker_announcements(ticker, session=session)

    # Convert shared Announcement → results_pack_agent Announcement
    rpa_anns = [
        RpaAnnouncement(
            ticker=a.ticker,
            title=a.title,
            date=a.date,
            time=a.time,
            url=a.url,
            pdf_url=a.pdf_url,
        )
        for a in shared_anns
    ]

    return detect_result_pack(
        announcements=rpa_anns,
        report_type=report_type,
        target_date=target_date,
    )
