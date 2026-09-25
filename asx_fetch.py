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

from shared.asx import ANNOUNCEMENTS_URL, CHROME_124_USER_AGENT, absolute_url

ASX_V2_URL = ANNOUNCEMENTS_URL

HTTP_TIMEOUT_SECS = 30
PLAYWRIGHT_TIMEOUT_MS = 45_000
PLAYWRIGHT_USER_AGENT = CHROME_124_USER_AGENT

_normalise_href = absolute_url


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
                doc_url = absolute_url("/" + doc_key.lstrip("/"))
        if not doc_url or doc_url in seen:
            continue
        seen.add(doc_url)
        doc_url = absolute_url(doc_url)

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
        # ASX combines date and time in the first column, e.g. "25/03/2026 08:30 AM"
        # Use a regex to extract the date rather than a strict strptime.
        first_col = cols[0]
        m_date = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", first_col)
        if not m_date:
            continue
        date_text = m_date.group(1)
        m_time = re.search(r"\b(\d{1,2}:\d{2}(?:\s*[ap]m)?)\b", first_col, re.IGNORECASE)
        time_text = m_time.group(1) if m_time else (cols[1] if len(cols) > 1 else "")

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

        # Detect the ASX price-sensitive flag.
        # The v2 HTML table has a dedicated column that contains "Y" (or an image
        # with alt="Y") when ASX has classified the announcement as price sensitive.
        # Scan every cell in the row rather than relying on a fixed column index.
        td_elements = row.select("td")
        price_sensitive = False
        for td in td_elements:
            cell_text = td.get_text(" ", strip=True).strip().upper()
            if cell_text == "Y":
                price_sensitive = True
                break
            for img in td.select("img"):
                alt = (img.get("alt") or "").strip().upper()
                if alt in ("Y", "YES") or "PRICE" in alt or "SENSITIVE" in alt:
                    price_sensitive = True
                    break
            if price_sensitive:
                break

        items.append({
            "exchange": "ASX",
            "ticker": ticker.upper(),
            "date": date_text,
            "time": time_text,
            "title": title,
            "url": href,
            "price_sensitive": price_sensitive,
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


def _looks_like_gate(body: str) -> bool:
    """ASX's terms-of-use interstitial rather than the announcements list."""
    b = (body or "").lower()
    return "agree and proceed" in b or "access to this site" in b[:5000]


async def _click_agree(page) -> bool:
    """Click the "Agree and proceed" control, whichever markup ASX uses."""
    for make in (
        lambda: page.get_by_role("button", name=re.compile(r"agree and proceed", re.I)),
        lambda: page.get_by_role("link", name=re.compile(r"agree and proceed", re.I)),
        lambda: page.locator("input[type=submit][value*='gree' i]"),
        lambda: page.get_by_text(re.compile(r"agree and proceed", re.I)),
    ):
        try:
            loc = make()
            if await loc.count() > 0:
                await loc.first.click(timeout=5_000)
                return True
        except Exception:
            continue
    return False


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
                body = None
                if resp is not None:
                    try:
                        body = (await resp.body()).decode("utf-8", errors="replace")
                    except Exception:
                        body = None
                if not body:
                    body = await page.content()

                # ASX consent gate ("Access to this site" / "Agree and
                # proceed"). Click through, then read the page the click
                # leads to. The first response is the gate itself; the old
                # code only reached the list because reading that response's
                # body happens to fail once the click has navigated away.
                if _looks_like_gate(body):
                    print(f"[asx_fetch] consent gate for {ticker} -- clicking Agree and proceed")
                    if await _click_agree(page):
                        try:
                            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                        except Exception:
                            pass
                        body = await page.content()
                        if _looks_like_gate(body):
                            # Consent is now stored in a cookie; ask again.
                            resp = await page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_TIMEOUT_MS)
                            body = await page.content()
                            if resp is not None:
                                try:
                                    raw = (await resp.body()).decode("utf-8", errors="replace")
                                    if raw and not _looks_like_gate(raw):
                                        body = raw
                                except Exception:
                                    pass
                    if _looks_like_gate(body):
                        print(f"[asx_fetch] still on the consent gate for {ticker} after clicking")

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
        if _looks_like_gate(r.text):
            print(f"[asx_fetch] ASX consent gate on direct HTTP for {ticker} — trying Playwright")
        elif parse_asx_html_announcements(r.text, ticker):
            print(f"[asx_fetch] no announcements in the window for {ticker} (list page OK) — checking with Playwright")
        else:
            print(f"[asx_fetch] Unexpected response for {ticker} — first 200 chars: {r.text[:200]!r}")
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
