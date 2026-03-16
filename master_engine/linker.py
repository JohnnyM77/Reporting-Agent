# master_engine/linker.py
#
# Attaches source links to InvestorEvent objects.
#
# For each ticker the linker builds a ``source_links`` dict with keys such as:
#   quote_page         – Yahoo Finance quote URL
#   market_index       – Market Index page (ASX tickers only)
#   asx_announcement   – Direct ASX announcement URL (from Bob/Ned metadata)
#   company_ir         – Company IR page (if derivable)
#   google_drive_report – Drive link from Bob-generated report
#   internal_report    – Internal valuation report placeholder
#
# Links are only added when non-empty; blank / None values are omitted.

from __future__ import annotations

import logging
import re

from .schemas import InvestorEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL templates
# ---------------------------------------------------------------------------
_YAHOO_FINANCE_TEMPLATE = "https://finance.yahoo.com/quote/{ticker}"
_MARKET_INDEX_TEMPLATE = "https://www.marketindex.com.au/asx/{code}"
_ASX_COMPANY_TEMPLATE = "https://www.asx.com.au/markets/company/{code}"


def _yahoo_ticker(ticker: str) -> str:
    """
    Convert a local ticker to Yahoo Finance format.

    Examples:
        "NHC.AX"  → "NHC.AX"   (already correct)
        "NHC"     → "NHC.AX"   (assume ASX when no exchange suffix)
        "RR."     → "RR.L"     (LSE — convert trailing dot to .L)
        "POOL"    → "POOL"     (US ticker — no change)
    """
    if "." not in ticker:
        # Bare ticker — could be US or ASX.  Return as-is and let Yahoo resolve.
        return ticker
    parts = ticker.split(".")
    suffix = parts[-1].upper()
    if suffix == "AX":
        return ticker  # already Yahoo-format
    if suffix == "":
        # Trailing dot (LSE style like "RR.") → Yahoo uses .L
        return parts[0] + ".L"
    return ticker


def _is_asx(ticker: str) -> bool:
    """
    Return True if the ticker is an ASX-listed security.

    Note: ``_yahoo_ticker()`` and ``_is_asx()`` intentionally apply different
    heuristics for bare tickers (no dot):

    * ``_yahoo_ticker`` returns the bare ticker as-is, letting Yahoo Finance
      resolve it.  A US ticker like ``POOL`` should not have ``".AX"`` appended.
    * ``_is_asx`` conservatively assumes short bare tickers (≤5 chars) belong
      to the ASX, so we generate a Market Index / ASX company-IR link for them.

    In practice all ASX tickers in this system are stored with the ``.AX``
    suffix (via ``normalise_ticker``), so the bare-ticker branch is a fallback.
    """
    upper = ticker.upper()
    return upper.endswith(".AX") or (
        "." not in upper and len(upper) <= 5
    )


def _asx_code(ticker: str) -> str:
    """Return the bare ASX code (without exchange suffix)."""
    return ticker.split(".")[0].upper()


def build_links(event: InvestorEvent) -> dict[str, str]:
    """
    Build a ``source_links`` dict for a single event.

    Existing links in ``event.source_links`` are preserved; new ones are
    added only when not already present and only when non-empty.
    """
    links: dict[str, str] = dict(event.source_links)  # start with what agent provided

    ticker = event.ticker

    # Yahoo Finance quote page
    if "quote_page" not in links:
        yf_ticker = _yahoo_ticker(ticker)
        links["quote_page"] = _YAHOO_FINANCE_TEMPLATE.format(ticker=yf_ticker)
        logger.debug("[linker] %s → quote_page attached", ticker)

    # Market Index (ASX only)
    if "market_index" not in links and _is_asx(ticker):
        code = _asx_code(ticker).lower()
        links["market_index"] = _MARKET_INDEX_TEMPLATE.format(code=code)
        logger.debug("[linker] %s → market_index attached", ticker)

    # ASX announcement URL (from event metadata)
    if "asx_announcement" not in links and event.asx_url:
        links["asx_announcement"] = event.asx_url
        logger.debug("[linker] %s → asx_announcement attached", ticker)

    # Google Drive report link (from Bob)
    if "google_drive_report" not in links and event.drive_report_link:
        links["google_drive_report"] = event.drive_report_link
        logger.debug("[linker] %s → google_drive_report attached", ticker)

    # ASX company page as fallback IR link (ASX only)
    if "company_ir" not in links and _is_asx(ticker):
        code = _asx_code(ticker).lower()
        links["company_ir"] = _ASX_COMPANY_TEMPLATE.format(code=code)
        logger.debug("[linker] %s → company_ir attached", ticker)

    return links


def attach_links(events: list[InvestorEvent]) -> list[InvestorEvent]:
    """
    Attach source links to every event in the list in-place.

    Returns the same list for convenience.
    """
    success = 0
    for event in events:
        try:
            event.source_links = build_links(event)
            success += 1
        except Exception as exc:
            logger.warning(
                "[linker] link generation failed for %s: %s", event.ticker, exc
            )
    logger.info(
        "[linker] links attached to %d/%d event(s)", success, len(events)
    )
    return events
