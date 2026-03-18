# results_pack_agent/pack_detector.py
# Detect the latest HY/FY result day and group the same-day announcement pack.

from __future__ import annotations

import datetime as dt
import re
from typing import List, Optional

from .config import (
    HARD_NO_KEYWORDS,
    PACK_INCLUDE_KEYWORDS,
    RESULT_DAY_TRIGGER_KEYWORDS,
)
from .models import Announcement, ResultPack
from .utils import log, parse_asx_date


# ── Primary trigger detection ──────────────────────────────────────────────────

def is_result_day_trigger(title: str) -> bool:
    """Return True when *title* indicates the primary HY/FY results announcement.

    A trigger announcement kicks off same-day pack collection.  Supplementary
    documents (investor presentations, dividend notices) are included in the
    pack but do not qualify as triggers on their own.
    """
    t = title.lower()
    if any(x in t for x in HARD_NO_KEYWORDS):
        return False
    return any(x in t for x in RESULT_DAY_TRIGGER_KEYWORDS)


def is_pack_document(title: str) -> bool:
    """Return True when *title* looks like a result-day supporting document.

    This is deliberately broad — the important gate is the primary trigger.
    Once a trigger date is found, all same-day documents matching this test
    are added to the pack.
    """
    t = title.lower()
    if any(x in t for x in HARD_NO_KEYWORDS):
        return False
    if any(x in t for x in PACK_INCLUDE_KEYWORDS):
        return True
    # Also catch fyNN / fy20NN patterns like fy26, fy2026
    if re.search(r"\bfy\d{2,4}\b", t):
        return True
    return False


def infer_result_type(announcements: List[Announcement]) -> str:
    """Guess whether the pack is HY (half-year) or FY (full-year).

    Checks all announcement titles in priority order:
    1. Explicit HY / interim keywords  → "HY"
    2. Explicit FY / annual keywords   → "FY"
    3. Appendix 4D (half-year)         → "HY"
    4. Appendix 4E (full-year)         → "FY"
    5. Falls back to "FY" if ambiguous.
    """
    combined = " ".join(a.title.lower() for a in announcements)

    hy_signals = [
        "half year", "half-year", "h1 ", "1h ", "1hfy", "interim",
        "appendix 4d",
    ]
    fy_signals = [
        "full year", "full-year", "annual report", "fy results",
        "appendix 4e",
    ]

    hy_score = sum(1 for kw in hy_signals if kw in combined)
    fy_score = sum(1 for kw in fy_signals if kw in combined)

    if hy_score > fy_score:
        return "HY"
    if fy_score > hy_score:
        return "FY"

    # Tie-break: if requested type known, use that — otherwise default FY
    return "FY"


# ── Pack detection ─────────────────────────────────────────────────────────────

def detect_result_pack(
    announcements: List[Announcement],
    report_type: Optional[str] = None,
    target_date: Optional[dt.date] = None,
) -> Optional[ResultPack]:
    """Find the latest result-day pack from a list of announcements.

    Algorithm:
    1. Walk announcements newest-first.
    2. Identify the first announcement that is a primary trigger (HY/FY
       results, Appendix 4D/4E, etc.).
    3. If *report_type* is specified (HY or FY), only accept a trigger that
       matches the requested type.
    4. If *target_date* is specified, only accept triggers on that exact date.
    5. Once the trigger date is found, collect ALL same-date announcements
       that are result-pack documents.
    6. Return a ``ResultPack`` with the full set.

    Returns ``None`` if no matching trigger is found.
    """
    # Sort newest-first (ASX dates are DD/MM/YYYY)
    def _sort_key(a: Announcement) -> dt.date:
        try:
            return parse_asx_date(a.date)
        except Exception:
            return dt.date.min

    sorted_anns = sorted(announcements, key=_sort_key, reverse=True)

    trigger_date: Optional[str] = None  # DD/MM/YYYY

    for ann in sorted_anns:
        if not is_result_day_trigger(ann.title):
            continue

        # Optional date filter
        if target_date is not None:
            try:
                if parse_asx_date(ann.date) != target_date:
                    continue
            except Exception:
                continue

        # Optional report-type filter
        if report_type is not None:
            detected_type = _quick_type_from_title(ann.title)
            if detected_type and detected_type != report_type.upper():
                continue

        trigger_date = ann.date
        break

    if trigger_date is None:
        log("[pack_detector] No result-day trigger found in announcement list.")
        return None

    # Collect all same-day pack documents
    pack_anns = [
        a for a in announcements
        if a.date == trigger_date and is_pack_document(a.title)
    ]

    # Ensure the trigger itself is included even if is_pack_document is False
    trigger_urls = {a.url for a in pack_anns}
    for ann in announcements:
        if ann.date == trigger_date and ann.url not in trigger_urls and is_result_day_trigger(ann.title):
            pack_anns.append(ann)
            trigger_urls.add(ann.url)

    # Sort pack announcements by title for deterministic ordering
    pack_anns.sort(key=lambda a: a.title)

    # Determine result type
    if report_type:
        result_type = report_type.upper()
    else:
        result_type = infer_result_type(pack_anns)

    ticker = announcements[0].ticker if announcements else "UNKNOWN"

    pack = ResultPack(
        ticker=ticker,
        company_name=ticker,    # caller can override if company name known
        result_date=trigger_date,
        result_type=result_type,
        announcements=pack_anns,
    )

    log(
        f"[pack_detector] Detected {result_type} result day "
        f"on {trigger_date} for {ticker} — "
        f"{len(pack_anns)} pack document(s)."
    )
    return pack


def find_nearest_result_dates(
    announcements: List[Announcement],
    report_type: Optional[str] = None,
    n: int = 5,
) -> List[str]:
    """Return up to *n* unique dates that have a result-day trigger.

    If *report_type* is specified (HY or FY) only matching triggers are
    considered.  Dates are returned in descending order (newest first) as
    ISO YYYY-MM-DD strings.
    """
    seen: set = set()
    result_dates: List[str] = []

    # Sort newest-first so the most recent dates come first in results
    def _sort_key(a: Announcement) -> dt.date:
        try:
            return parse_asx_date(a.date)
        except Exception:
            return dt.date.min

    sorted_anns = sorted(announcements, key=_sort_key, reverse=True)

    for ann in sorted_anns:
        if not is_result_day_trigger(ann.title):
            continue
        if report_type is not None:
            detected_type = _quick_type_from_title(ann.title)
            if detected_type and detected_type != report_type.upper():
                continue
        date_key = ann.date
        if date_key not in seen:
            seen.add(date_key)
            try:
                iso = dt.datetime.strptime(date_key, "%d/%m/%Y").strftime("%Y-%m-%d")
            except Exception:
                iso = date_key
            result_dates.append(iso)
        if len(result_dates) >= n:
            break

    return result_dates


def _quick_type_from_title(title: str) -> Optional[str]:
    """Fast HY/FY inference from a single announcement title."""
    t = title.lower()
    hy = ["half year", "half-year", "h1 ", "1h ", "1hfy", "interim", "appendix 4d"]
    fy = ["full year", "full-year", "annual", "fy results", "appendix 4e"]
    if any(k in t for k in hy):
        return "HY"
    if any(k in t for k in fy):
        return "FY"
    return None
