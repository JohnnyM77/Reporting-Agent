"""
agents/sws_drip/sws_downloader.py
───────────────────────────────────
Direct HTTP API approach: authenticate with Bearer JWT from storage_state,
resolve ticker via Algolia, download CSV via SWS REST API.

No Playwright. No browser. No Cloudflare.
"""

from __future__ import annotations

import json
import time
import random
from pathlib import Path
from typing import Union

import httpx


class SWSDownloadError(Exception):
    """Raised when a download step fails."""


# Algolia credentials (public — baked into SWS's own frontend JS)
_ALGOLIA_APP_ID = "17IQHZWXZW"
_ALGOLIA_API_KEY = "be7c37718f927d0137a88a11b69ae4198"
_ALGOLIA_URL = f"https://{_ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/companies/query"

_SWS_BASE = "https://simplywall.st"
_CSV_URL_TEMPLATE = (
    "/api/company/download/csv//stocks/{country}/{sector}/asx-{ticker}/{slug}"
)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _extract_bearer(storage_state: Union[str, dict]) -> str:
    """
    Pull the Bearer JWT from storage_state.

    Checks localStorage first (SWS stores auth_token there), then cookies.
    Raises SWSDownloadError if not found.
    """
    if isinstance(storage_state, str):
        if storage_state.strip().startswith("{"):
            state = json.loads(storage_state)
        else:
            state = json.loads(Path(storage_state).read_text(encoding="utf-8"))
    else:
        state = storage_state

    # 1. Check localStorage origins
    for origin in state.get("origins", []):
        for entry in origin.get("localStorage", []):
            key = entry.get("name", "")
            if "token" in key.lower() or "auth" in key.lower() or "jwt" in key.lower():
                val = entry.get("value", "")
                if val and len(val) > 20:
                    return val

    # 2. Check cookies named 'token', 'auth_token', 'jwt', etc.
    for cookie in state.get("cookies", []):
        name = cookie.get("name", "").lower()
        if any(k in name for k in ("token", "auth", "jwt", "session")):
            val = cookie.get("value", "")
            if val and len(val) > 20:
                return val

    raise SWSDownloadError(
        "Could not find Bearer token in storage_state. "
        "Session may have expired — re-run --setup locally and update SWS_STORAGE_STATE secret."
    )


def _build_cookie_header(storage_state: Union[str, dict]) -> str:
    """Build a Cookie header string from all SWS cookies in storage_state."""
    if isinstance(storage_state, str):
        if storage_state.strip().startswith("{"):
            state = json.loads(storage_state)
        else:
            state = json.loads(Path(storage_state).read_text(encoding="utf-8"))
    else:
        state = storage_state

    parts = []
    for cookie in state.get("cookies", []):
        if "simplywall" in cookie.get("domain", ""):
            parts.append(f"{cookie['name']}={cookie['value']}")
    return "; ".join(parts)


def _resolve_ticker_via_algolia(ticker: str, exchange: str) -> dict:
    """
    Query Algolia to resolve ticker → {slug, sector, country, company_id}.
    Returns the first matching hit or raises SWSDownloadError.
    """
    query = f"{exchange}:{ticker}"
    payload = {
        "query": query,
        "hitsPerPage": 5,
        "filters": f"exchange_symbol:{exchange}",
    }
    headers = {
        "X-Algolia-Application-Id": _ALGOLIA_APP_ID,
        "X-Algolia-API-Key": _ALGOLIA_API_KEY,
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }

    with httpx.Client(timeout=20) as client:
        resp = client.post(_ALGOLIA_URL, json=payload, headers=headers)

    if resp.status_code != 200:
        raise SWSDownloadError(
            f"Algolia search failed for {query}: HTTP {resp.status_code} — {resp.text[:200]}"
        )

    data = resp.json()
    hits = data.get("hits", [])
    if not hits:
        # Retry without exchange filter (some tickers have unusual exchange codes)
        payload_retry = {"query": ticker, "hitsPerPage": 5}
        with httpx.Client(timeout=20) as client:
            resp2 = client.post(_ALGOLIA_URL, json=payload_retry, headers=headers)
        hits = resp2.json().get("hits", []) if resp2.status_code == 200 else []

    # Find best match: ticker symbol must match exactly
    for hit in hits:
        hit_ticker = (hit.get("ticker_symbol") or hit.get("symbol") or "").upper()
        if hit_ticker == ticker.upper():
            slug = hit.get("slug") or hit.get("unique_symbol_slug") or ""
            sector = hit.get("industry_name") or hit.get("sector") or ""
            country = hit.get("country_iso") or hit.get("country") or "au"
            # Normalise: slug may already include exchange prefix
            if slug.startswith("asx-"):
                slug = slug[4:]
            return {
                "slug": slug.lower(),
                "sector": _slugify(sector),
                "country": country.lower(),
                "hit": hit,
            }

    raise SWSDownloadError(
        f"Algolia: no exact match for ticker '{ticker}' on exchange '{exchange}'. "
        f"Hits returned: {[h.get('ticker_symbol') for h in hits]}"
    )


def _slugify(s: str) -> str:
    """Convert 'Real Estate' → 'real-estate' for URL segments."""
    import re
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def download_csv(
    ticker: str,
    exchange: str,
    *,
    storage_state: Union[str, dict],
    output_dir: Path,
    headless: bool = True,  # kept for API compatibility, unused
    debug_dir: Path,
) -> Path:
    """
    Download one SWS CSV for the given ticker using direct HTTP API calls.

    Steps:
      1. Extract Bearer JWT from storage_state
      2. Resolve ticker → slug/sector/country via Algolia
      3. GET the CSV download endpoint with auth headers

    Returns the path to the saved CSV file.
    Raises SWSDownloadError on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{exchange}_{ticker}.csv"

    # 1. Extract auth
    bearer = _extract_bearer(storage_state)
    cookie_header = _build_cookie_header(storage_state)

    print(f"[sws_downloader] Resolving {exchange}:{ticker} via Algolia...", flush=True)

    # 2. Resolve ticker metadata
    info = _resolve_ticker_via_algolia(ticker, exchange)
    slug = info["slug"]
    sector = info["sector"]
    country = info["country"]

    print(
        f"[sws_downloader] Resolved: slug={slug!r} sector={sector!r} country={country!r}",
        flush=True,
    )

    # 3. Download CSV
    csv_path_segment = _CSV_URL_TEMPLATE.format(
        country=country,
        sector=sector,
        ticker=ticker.lower(),
        slug=slug,
    )
    url = _SWS_BASE + csv_path_segment

    headers = {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/vnd.simplywallst.v2",
        "User-Agent": _USER_AGENT,
        "Referer": f"https://simplywall.st/stocks/au/asx-{ticker.lower()}/{slug}",
        "Origin": "https://simplywall.st",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header

    print(f"[sws_downloader] Downloading CSV from: {url}", flush=True)

    # Small polite delay
    time.sleep(random.uniform(1.5, 3.0))

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)

    if resp.status_code == 401 or resp.status_code == 403:
        raise SWSDownloadError(
            f"Auth failed (HTTP {resp.status_code}) for {ticker}. "
            "Session may have expired — re-run --setup and update SWS_STORAGE_STATE secret."
        )
    if resp.status_code != 200:
        # Save debug info
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"fail_{ticker}_csv_download.txt").write_text(
            f"URL: {url}\nStatus: {resp.status_code}\nBody: {resp.text[:2000]}",
            encoding="utf-8",
        )
        raise SWSDownloadError(
            f"CSV download failed for {ticker}: HTTP {resp.status_code}"
        )

    content = resp.text
    if not content.strip() or "<html" in content[:100].lower():
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"fail_{ticker}_csv_content.txt").write_text(
            content[:2000], encoding="utf-8"
        )
        raise SWSDownloadError(
            f"CSV download for {ticker} returned HTML instead of CSV data. "
            "Check session/URL."
        )

    output_path.write_text(content, encoding="utf-8")
    print(f"[sws_downloader] Downloaded {ticker} → {output_path}", flush=True)
    return output_path


async def setup_session(output_path: Path) -> None:
    """
    One-time manual login flow. Opens a headed browser, navigates to SWS login,
    waits for the user to log in, then saves the browser context to output_path.
    """
    from playwright.async_api import async_playwright

    print("[sws_drip] Opening browser for manual login...")
    print("[sws_drip] Log into Simply Wall St, then close the browser window.")
    print(f"[sws_drip] Storage state will be saved to: {output_path}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,
            slow_mo=200,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        await page.goto("https://simplywall.st/login", wait_until="domcontentloaded")

        print("[sws_drip] Waiting for login... (watching for dashboard URL)")
        try:
            await page.wait_for_url("**/dashboard**", timeout=300000)
        except Exception:
            pass

        output_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(output_path))
        print(f"[sws_drip] Session saved to {output_path}")
        await browser.close()
