# asx_fetch.py
#
# Shared ASX announcement fetching module.
#
# 3-stage fallback:
#   1. ASX v2 statistics endpoint — JSON parse first, HTML parse fallback
#   2. Requests scrape of live company page (cheap, usually zero for JS-rendered)
#   3. Playwright scrape of live company page (reliable JS render)
#
# Public API
# ----------
# fetch_asx_announcements_html(session, ticker, from_date, to_date) -> List[Dict]
# parse_asx_html_announcements(html, ticker, from_date, to_date) -> List[Dict]
# parse_company_page_html(html, ticker, from_date, to_date) -> List[Dict]

from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

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
_PARENT_TRAVERSE_DEPTH = 5


def _normalise_href(href: str) -> str:
    if href.startswith("/"):
        return "https://www.asx.com.au" + href
    return href


def _parse_json_rows(
    rows: list,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Dict]:
    """Parse the JSON rows returned by the ASX v2 endpoint.

    The endpoint switched from returning an HTML table to returning a JSON
    payload with a top-level 'data' array. Each row has keys like:
      header / headline  — announcement title
      documentKey        — relative path to the PDF
      releasedDate       — epoch milliseconds (UTC)
    """
    items: List[Dict] = []
    seen: set = set()

    for row in rows:
        if not isinstance(row, dict):
            continue

        title = (row.get("header") or row.get("headline") or "").strip()
        if not title:
            continue

        # Build absolute URL from documentKey or url field
        doc_url = (row.get("url") or "").strip()
        if not doc_url:
            doc_key = (row.get("documentKey") or "").strip()
            if doc_key:
                doc_url = "https://www.asx.com.au/" + doc_key.lstrip("/")
        if not doc_url or doc_url in seen:
            continue
        seen.add(doc_url)
        if doc_url.startswith("/"):
            doc_url = "https://www.asx.com.au" + doc_url

        # Parse releasedDate — usually epoch ms (UTC), convert to SGT (UTC+8)
        released = row.get("releasedDate") or row.get("issueDate") or row.get("date")
        item_date = None
        time_str = ""
        if released is not None:
            try:
                if isinstance(released, (int, float)) or (
                    isinstance(released, str) and str(released).isdigit()
                ):
                    ts = dt.datetime.utcfromtimestamp(int(released) / 1000.0)
                    ts_sgt = ts + dt.timedelta(hours=8)
                    item_date = ts_sgt.date()
                    time_str = ts_sgt.strftime("%I:%M %p")
                else:
                    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
                        try:
                            item_date = dt.datetime.strptime(
                                str(released)[:10], fmt
                            ).date()
                            break
                        except Exception:
                            continue
            except Exception:
                pass

        if item_date is None:
            item_date = dt.date.today()

        if from_date is not None and item_date < from_date:
            continue
        if to_date is not None and item_date > to_date:
            continue

        items.append({
            "exchange": "ASX",
            "ticker": ticker,
            "date": item_date.strftime("%d/%m/%Y"),
            "time": time_str,
            "title": title,
            "url": doc_url,
        })

    return items


def parse_asx_html_announcements(
    html: str,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Dict]:
    """Parse the ASX announcements HTML table and return a list of dicts."""
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
    """Parse rendered company page HTML for announcement links."""
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
    url = f"https://www.asx.com.au/markets/company/{ticker.upper()}"
    r = session.get(url, timeout=HTTP_TIMEOUT_SECS)
    r.raise_for_status()
    return parse_company_page_html(r.text, ticker, from_date=from_date, to_date=to_date)


def _scrape_company_page_with_playwright(
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Dict]:
    """Reliable fallback: render the live company page with Playwright."""
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

            # Click "SEE ALL ANNOUNCEMENTS" if present
            try:
                btn = page.get_by_text("SEE ALL ANNOUNCEMENTS")
                if await btn.count() > 0:
                    await btn.first.click()
                    await page.wait_for_load_state("networkidle")
            except Exception:
                pass

            # Handle consent gate if present
            try:
                content = await page.content()
                if "agree and proceed" in content.lower():
                    await page.get_by_role("button", name="Agree and proceed").click(
                        timeout=5_000
                    )
                    await page.wait_for_timeout(2_000)
            except Exception:
                pass

            html = await page.content()
            await context.close()
            await browser.close()
            return parse_company_page_html(
                html, ticker, from_date=from_date, to_date=to_date
            )

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

    Stage 1 — ASX v2 statistics endpoint.
               Tries JSON parse first (endpoint now returns JSON),
               falls back to HTML table parse of same response.
    Stage 2 — Requests scrape of the live company page (cheap, usually
               zero for JS-rendered pages).
    Stage 3 — Playwright render of the live company page (reliable, slower,
               handles consent gates).
    """
    ticker = ticker.upper().strip()

    # ------------------------------------------------------------------ #
    # Stage 1: v2 statistics endpoint — JSON first, HTML fallback         #
    # ------------------------------------------------------------------ #
    try:
        url = ASX_V2_URL.format(ticker=ticker)
        r = session.get(url, timeout=HTTP_TIMEOUT_SECS)
        r.raise_for_status()

        # 1a: JSON parse
        try:
            payload = r.json()
            rows = (
                payload.get("data", [])
                if isinstance(payload, dict)
                else (payload if isinstance(payload, list) else [])
            )
            if rows:
                items = _parse_json_rows(
                    rows, ticker, from_date=from_date, to_date=to_date
                )
                if items:
                    print(
                        f"[asx_fetch] v2 JSON returned {len(items)} items for {ticker}"
                    )
                    return items
        except Exception:
            pass

        # 1b: HTML table parse (legacy fallback)
        items = parse_asx_html_announcements(
            r.text, ticker, from_date=from_date, to_date=to_date
        )
        if items:
            print(
                f"[asx_fetch] v2 HTML parse returned {len(items)} items for {ticker}"
            )
            return items

        print(f"[asx_fetch] v2 endpoint returned zero for {ticker}")

    except Exception as exc:
        print(f"[asx_fetch] v2 endpoint failed for {ticker}: {exc}")

    # ------------------------------------------------------------------ #
    # Stage 2: Cheap requests scrape of company page                      #
    # ------------------------------------------------------------------ #
    try:
        items = _scrape_company_page_with_requests(
            session, ticker, from_date=from_date, to_date=to_date
        )
        if items:
            print(
                f"[asx_fetch] requests company-page returned {len(items)} items for {ticker}"
            )
            return items
        print(f"[asx_fetch] requests company-page returned zero for {ticker}")
    except Exception as exc:
        print(f"[asx_fetch] requests company-page failed for {ticker}: {exc}")

    # ------------------------------------------------------------------ #
    # Stage 3: Playwright render of company page                          #
    # ------------------------------------------------------------------ #
    items = _scrape_company_page_with_playwright(
        ticker, from_date=from_date, to_date=to_date
    )
    if items:
        print(
            f"[asx_fetch] Playwright returned {len(items)} items for {ticker}"
        )
        return items

    print(f"[asx_fetch] all fetch paths returned zero for {ticker}")
    return []
