# asx_fetch.py
#
# Shared ASX announcement fetching module.
#
# 2-stage fetch strategy:
#   1. Direct HTTP request to ASX v2 endpoint — JSON parse first, HTML fallback
#   2. Playwright browser fetch of same v2 endpoint — handles consent gates,
#      JS rendering, and any IP-based blocking of raw HTTP clients
#
# Public API
# ----------
# fetch_asx_announcements_html(session, ticker, from_date, to_date) -> List[Dict]
# parse_asx_html_announcements(html, ticker, from_date, to_date) -> List[Dict]

from __future__ import annotations

import datetime as dt
import json
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
    """Parse the JSON rows returned by the ASX v2 endpoint."""
    items: List[Dict] = []
    seen: set = set()

    for row in rows:
        if not isinstance(row, dict):
            continue

        title = (row.get("header") or row.get("headline") or "").strip()
        if not title:
            continue

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
            "ticker": ticker.upper(),
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
    """Parse the ASX v2 endpoint HTML table."""
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


def _parse_response_body(
    body: str,
    ticker: str,
    from_date: Optional[dt.date],
    to_date: Optional[dt.date],
) -> List[Dict]:
    """Try JSON parse first, fall back to HTML parse."""
    # JSON first
    try:
        payload = json.loads(body)
        rows = (
            payload.get("data", [])
            if isinstance(payload, dict)
            else (payload if isinstance(payload, list) else [])
        )
        if rows:
            items = _parse_json_rows(rows, ticker, from_date=from_date, to_date=to_date)
            if items:
                return items
    except Exception:
        pass

    # HTML fallback
    return parse_asx_html_announcements(body, ticker, from_date=from_date, to_date=to_date)


def _playwright_fetch_v2(
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Dict]:
    """Use Playwright to fetch the ASX v2 endpoint directly.

    This handles consent gates, JS redirects, and any blocking that prevents
    raw HTTP clients from getting a response.
    """
    try:
        import asyncio
        from playwright.async_api import async_playwright
    except Exception:
        return []

    async def _run() -> List[Dict]:
        url = ASX_V2_URL.format(ticker=ticker)
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=PLAYWRIGHT_USER_AGENT)
            page = await context.new_page()

            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)

                # Handle ASX consent gate if present
                try:
                    content = await page.content()
                    if "agree and proceed" in content.lower():
                        try:
                            await page.get_by_role("button", name="Agree and proceed").click(timeout=5_000)
                        except Exception:
                            try:
                                await page.locator("text=Agree and proceed").first.click(timeout=5_000)
                            except Exception:
                                pass
                        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:
                    pass

                # Try to get the raw response body (JSON or HTML)
                body = None
                if resp is not None:
                    try:
                        body_bytes = await resp.body()
                        body = body_bytes.decode("utf-8", errors="replace")
                    except Exception:
                        pass

                # Fall back to page content if body not available
                if not body:
                    body = await page.content()

                return _parse_response_body(body, ticker, from_date, to_date)

            finally:
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass

    try:
        return asyncio.run(_run())
    except Exception as exc:
        print(f"[asx_fetch] Playwright fetch failed for {ticker}: {exc}")
        return []


def fetch_asx_announcements_html(
    session: requests.Session,
    ticker: str,
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
) -> List[Dict]:
    """Fetch ASX announcements for *ticker* with a 2-stage fallback.

    Stage 1 — Direct HTTP request to the ASX v2 endpoint.
               JSON parse first, HTML table parse fallback.
    Stage 2 — Playwright browser fetch of the same v2 endpoint.
               Handles consent gates, JS rendering, and IP-based blocking.
    """
    ticker = ticker.upper().strip()

    # Stage 1: Direct HTTP request
    try:
        url = ASX_V2_URL.format(ticker=ticker)
        r = session.get(url, timeout=HTTP_TIMEOUT_SECS)
        r.raise_for_status()
        items = _parse_response_body(r.text, ticker, from_date=from_date, to_date=to_date)
        if items:
            print(f"[asx_fetch] direct HTTP returned {len(items)} items for {ticker}")
            return items
        print(f"[asx_fetch] direct HTTP returned zero for {ticker} — trying Playwright")
    except Exception as exc:
        print(f"[asx_fetch] direct HTTP failed for {ticker}: {exc} — trying Playwright")

    # Stage 2: Playwright browser fetch of the v2 endpoint
    items = _playwright_fetch_v2(ticker, from_date=from_date, to_date=to_date)
    if items:
        print(f"[asx_fetch] Playwright returned {len(items)} items for {ticker}")
        return items

    print(f"[asx_fetch] all fetch paths returned zero for {ticker}")
    return []
