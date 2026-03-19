# asx_fetch.py
# Shared ASX announcement fetching module.
#
# This module contains the SINGLE source of truth for fetching ASX announcements.
# Both agent.py and results_pack_agent use this module so they share exactly the
# same retrieval and parsing logic.
#
# Public API
# ----------
# fetch_asx_announcements_html(session, ticker, from_date, to_date)  -> List[Dict]
#   Full fetch with 3-stage fallback:
#     1. Legacy v2 statistics HTML endpoint
#     2. Requests scrape of live company page (cheap, usually zero for JS-rendered)
#     3. Playwright scrape of live company page (reliable JS render)
#
# parse_asx_html_announcements(html, ticker, from_date, to_date)     -> List[Dict]
#   Pure parse of v2 endpoint HTML.  Useful for unit tests.
#
# parse_company_page_html(html, ticker, from_date, to_date)          -> List[Dict]
#   Pure parse of rendered company page HTML.
#
# Each Dict has keys: exchange, ticker, date (DD/MM/YYYY), time, title, url.

from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

# The legacy ASX endpoint — 6 months of history, all announcement types.
ASX_V2_URL = (
    "https://www.asx.com.au/asx/v2/statistics/announcements.do"
    "?asxCode={ticker}&by=asxCode&period=M6&timeframe=D"
)

HTTP_TIMEOUT_SECS = 30
PLAYWRIGHT_TIMEOUT_MS = 45_000
PLAYWRIGHT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
# How many DOM levels to walk up from an announcement link when looking for
# the adjacent date/time text.  5 levels is enough to reach a typical card
# or table-row container while avoiding false positives from distant ancestors.
_PARENT_TRAVERSE_DEPTH = 5


def _normalise_href(href: str) -> str:
    if href.startswith("/"):
        return "https://www.asx.com.au" + href
    return href


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

        link = row.select_one("a[href]")
        if not link:
            continue

        title = link.get_text(" ", strip=True)
        href = _normalise_href(str(link["href"]))
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


def parse_company_page_html(
    html: str,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Dict]:
    """Parse rendered company page HTML.

    Looks for links to displayAnnouncement.do and extracts nearby date/time text.
    Used by both the requests and Playwright company-page scrape paths.
    """
    soup = BeautifulSoup(html, "html.parser")
    items: List[Dict] = []
    seen: set = set()

    for a in soup.select("a[href*='displayAnnouncement']"):
        href = _normalise_href(str(a.get("href", "")))
        title = a.get_text(" ", strip=True)
        if not href or not title:
            continue
        if href in seen:
            continue

        container = a
        for _ in range(_PARENT_TRAVERSE_DEPTH):
            if container.parent is None:
                break
            container = container.parent

        blob = container.get_text(" ", strip=True)
        m = re.search(r"(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2}\s*[ap]m)", blob, re.I)
        if not m:
            continue

        date_text = m.group(1)
        time_text = m.group(2)

        try:
            item_date = dt.datetime.strptime(date_text, "%d/%m/%Y").date()
        except Exception:
            continue

        if from_date is not None and item_date < from_date:
            continue
        if to_date is not None and item_date > to_date:
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


def _scrape_company_page_with_requests(
    session: requests.Session,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Dict]:
    """Lightweight non-JS fallback.

    Usually returns zero because the page is JS-rendered, but keep it as a
    cheap attempt before invoking the heavier Playwright path.
    """
    url = f"https://www.asx.com.au/markets/company/{ticker.upper()}"
    r = session.get(url, timeout=HTTP_TIMEOUT_SECS)
    r.raise_for_status()
    return parse_company_page_html(r.text, ticker, from_date=from_date, to_date=to_date)


def _scrape_company_page_with_playwright(
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Dict]:
    """Reliable fallback: render the live company page and scrape announcement links."""
    try:
        import asyncio
        from playwright.async_api import async_playwright
    except Exception:
        return []

    async def _run() -> List[Dict]:
        url = f"https://www.asx.com.au/markets/company/{ticker.upper()}"
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=PLAYWRIGHT_USER_AGENT)
            page = await context.new_page()
            await page.goto(url, wait_until="networkidle", timeout=PLAYWRIGHT_TIMEOUT_MS)

            try:
                btn = page.get_by_text("SEE ALL ANNOUNCEMENTS")
                if await btn.count() > 0:
                    await btn.first.click()
                    await page.wait_for_load_state("networkidle")
            except Exception:
                pass

            html = await page.content()
            await context.close()
            await browser.close()
            return parse_company_page_html(html, ticker, from_date=from_date, to_date=to_date)

    try:
        return asyncio.run(_run())
    except Exception:
        return []


def fetch_asx_announcements_html(
    session: requests.Session,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Dict]:
    """Fetch ASX announcements for *ticker* with a 3-stage fallback.

    Shared by both Bob (agent.py) and Results Pack Agent so they benefit from
    the same retrieval logic.

    Stage 1 — Legacy v2 statistics HTML endpoint (fast, proven).
    Stage 2 — Requests scrape of the live company page (cheap, usually zero for
               JS-rendered pages, but worth trying before Playwright).
    Stage 3 — Playwright render of the live company page (reliable, slower).

    Returns a list of dicts with keys:
        exchange  – always "ASX"
        ticker    – uppercased ticker code
        date      – announcement date in DD/MM/YYYY format
        time      – time string (may be empty)
        title     – announcement headline
        url       – full absolute URL
    """
    ticker = ticker.upper().strip()

    # Stage 1: Legacy endpoint
    try:
        url = ASX_V2_URL.format(ticker=ticker)
        r = session.get(url, timeout=HTTP_TIMEOUT_SECS)
        r.raise_for_status()
        items = parse_asx_html_announcements(r.text, ticker, from_date=from_date, to_date=to_date)
        if items:
            return items
        print(f"[asx_fetch] legacy endpoint returned zero for {ticker}")
    except Exception as exc:
        print(f"[asx_fetch] legacy endpoint failed for {ticker}: {exc}")

    # Stage 2: Cheap requests scrape of company page
    try:
        items = _scrape_company_page_with_requests(session, ticker, from_date=from_date, to_date=to_date)
        if items:
            print(f"[asx_fetch] requests company-page scrape returned {len(items)} items for {ticker}")
            return items
        print(f"[asx_fetch] requests company-page scrape returned zero for {ticker}")
    except Exception as exc:
        print(f"[asx_fetch] requests company-page scrape failed for {ticker}: {exc}")

    # Stage 3: Playwright render
    items = _scrape_company_page_with_playwright(ticker, from_date=from_date, to_date=to_date)
    if items:
        print(f"[asx_fetch] Playwright company-page scrape returned {len(items)} items for {ticker}")
        return items

    print(f"[asx_fetch] all fetch paths returned zero for {ticker}")
    return []
