# shared/asx.py
#
# Low-level ASX plumbing shared by every ASX fetcher: the endpoint URLs, the
# browser-like HTTP session ASX needs, relative-link resolution, and the
# idsId -> PDF URL rule.
#
# The fetchers themselves stay separate because they do different jobs:
#
#   asx_fetch.py                   Bob / Wally: recent announcements, HTML +
#                                  JSON parsing, Playwright fallback when
#                                  runner IPs are blocked
#   shared/asx_simple_fetcher.py   Results Pack: 6-month history, plain HTTP
#   sunday-sally/src/document_fetcher.py
#                                  Sally: evidence for a holding, ASX JSON API
#
# Keep this module free of parsing and selection logic. "Which
# announcements matter" is the agent's decision, not the transport's.

from __future__ import annotations

import re
from typing import Optional

ASX_BASE = "https://www.asx.com.au"

# Six months of announcements for one code, HTML table. The endpoint Bob and
# Results Pack both use.
ANNOUNCEMENTS_URL = (
    ASX_BASE + "/asx/v2/statistics/announcements.do"
    "?asxCode={ticker}&by=asxCode&period=M6&timeframe=D"
)

DISPLAY_PDF_URL = ASX_BASE + "/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId={ids_id}"

# ASX serves an access gate to obvious bots. Both Chrome strings are in live
# use (Bob/Results Pack on 122, the Playwright path on 124); keep each caller
# on the one it had.
CHROME_122_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
CHROME_124_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_IDS_ID_RE = re.compile(r"[?&]idsid=([^&]+)", re.IGNORECASE)


def browser_session(
    accept: str = "*/*",
    user_agent: str = CHROME_122_USER_AGENT,
):
    """A ``requests.Session`` with the browser-like headers ASX expects."""
    import requests

    s = requests.Session()
    s.headers.update({
        "User-Agent": user_agent,
        "Accept": accept,
        "Referer": ASX_BASE + "/",
    })
    return s


def absolute_url(href: str) -> str:
    """Resolve a site-relative ASX link (``/asxpdf/...``) to a full URL."""
    if href.startswith("/"):
        return ASX_BASE + href
    return href


def extract_ids_id(url: str) -> Optional[str]:
    """The ``idsId`` document key from an announcement URL, or None."""
    m = _IDS_ID_RE.search(url or "")
    return m.group(1) if m else None


def pdf_url_for_ids_id(ids_id: Optional[str]) -> Optional[str]:
    """The direct PDF display URL for an ``idsId``, or None."""
    if not ids_id:
        return None
    return DISPLAY_PDF_URL.format(ids_id=ids_id)
