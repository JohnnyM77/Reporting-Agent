"""
agents/sws_drip/sws_downloader.py
───────────────────────────────────
Direct HTTP API approach: authenticate with Bearer JWT from storage_state,
resolve ticker slug by following the SWS short-URL redirect, then download
CSV via the SWS REST API.

No Playwright. No browser. No Cloudflare. No rotating Algolia keys.
"""

from __future__ import annotations

import json
import re
import time
import random
from pathlib import Path
from typing import Union

import httpx


class SWSDownloadError(Exception):
    """Raised when a download step fails."""


_SWS_BASE = "https://simplywall.st"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _parse_state(storage_state: Union[str, dict]) -> dict:
    if isinstance(storage_state, str):
        if storage_state.strip().startswith("{"):
            return json.loads(storage_state)
        return json.loads(Path(storage_state).read_text(encoding="utf-8"))
    return storage_state


def _extract_bearer(storage_state: Union[str, dict]) -> str:
    """
    Pull the Bearer JWT from storage_state (localStorage → cookies).
    Raises SWSDownloadError if not found.
    """
    state = _parse_state(storage_state)

    # 1. Check localStorage
    for origin in state.get("origins", []):
        for entry in origin.get("localStorage", []):
            key = entry.get("name", "")
            if "token" in key.lower() or "auth" in key.lower() or "jwt" in key.lower():
                val = entry.get("value", "")
                if val and len(val) > 20:
                    return val

    # 2. Check cookies
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
    state = _parse_state(storage_state)
    parts = []
    for cookie in state.get("cookies", []):
        if "simplywall" in cookie.get("domain", ""):
            parts.append(f"{cookie['name']}={cookie['value']}")
    return "; ".join(parts)


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _api_headers(bearer: str, cookie_header: str, referer: str = "") -> dict:
    """Headers for authenticated SWS API requests."""
    h = {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/vnd.simplywallst.v2",
        "User-Agent": _USER_AGENT,
        "Origin": "https://simplywall.st",
    }
    if referer:
        h["Referer"] = referer
    if cookie_header:
        h["Cookie"] = cookie_header
    return h


def _resolve_via_redirect(ticker: str, bearer: str, cookie_header: str) -> dict:
    """
    Hit the SWS short company URL and follow the redirect to get
    country/sector/slug from the final URL path.

    SWS maps: /stocks/asx-{ticker}  →  /stocks/{country}/{sector}/asx-{ticker}/{slug}

    Returns {slug, sector, country} or raises SWSDownloadError.
    """
    short_url = f"{_SWS_BASE}/stocks/asx-{ticker.lower()}"
    headers = _api_headers(bearer, cookie_header, referer="https://simplywall.st/dashboard")

    print(f"[sws_downloader] Trying redirect resolution: {short_url}", flush=True)
    with httpx.Client(timeout=15, follow_redirects=True) as client:
        resp = client.get(short_url, headers=headers)

    final_url = str(resp.url)
    print(f"[sws_downloader] Redirect → {final_url}  (status {resp.status_code})", flush=True)

    # Parse /stocks/{country}/{sector}/asx-{ticker}/{slug}
    m = re.search(r"/stocks/([^/]+)/([^/]+)/asx-[^/]+/([^/?#]+)", final_url)
    if m:
        country, sector, slug = m.group(1), m.group(2), m.group(3)
        print(f"[sws_downloader] Parsed from URL: country={country!r} sector={sector!r} slug={slug!r}", flush=True)
        return {"country": country, "sector": sector, "slug": slug}

    # Didn't redirect — but maybe the API returned JSON with the URL
    if resp.status_code == 200:
        content_type = resp.headers.get("content-type", "")
        if "json" in content_type:
            try:
                data = resp.json()
                print(f"[sws_downloader] JSON response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}", flush=True)
            except Exception:
                pass
        # Log first 300 chars of body for debugging
        print(f"[sws_downloader] Response body[:300]: {resp.text[:300]!r}", flush=True)

    raise SWSDownloadError(
        f"Could not resolve URL components for '{ticker}' from redirect. "
        f"Final URL was: {final_url!r}  (status {resp.status_code}). "
        f"Check debug output above."
    )


def _resolve_via_api(ticker: str, exchange: str, bearer: str, cookie_header: str) -> dict:
    """
    Try SWS REST API endpoints to get company metadata.
    Logs responses to help diagnose which endpoint pattern is correct.
    """
    headers = _api_headers(bearer, cookie_header)
    endpoints = [
        f"{_SWS_BASE}/api/company?unique_symbol={exchange}:{ticker}",
        f"{_SWS_BASE}/api/company?query={exchange}:{ticker}&limit=5",
        f"{_SWS_BASE}/api/company?query={ticker}&exchange_symbol={exchange}&limit=5",
        f"{_SWS_BASE}/api/companies?query={exchange}:{ticker}&limit=5",
        f"{_SWS_BASE}/api/companies/search?q={exchange}:{ticker}",
    ]

    with httpx.Client(timeout=15, follow_redirects=True) as client:
        for url in endpoints:
            try:
                resp = client.get(url, headers=headers)
                print(
                    f"[sws_downloader] {url!r} → {resp.status_code} "
                    f"body[:200]={resp.text[:200]!r}",
                    flush=True,
                )
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        items = data.get("data", []) if isinstance(data, dict) else data
                        if isinstance(items, list) and items:
                            item = items[0]
                            return _extract_meta_from_item(item, ticker)
                    except Exception as e:
                        print(f"[sws_downloader] JSON parse error: {e}", flush=True)
            except Exception as e:
                print(f"[sws_downloader] Request error for {url}: {e}", flush=True)

    raise SWSDownloadError(
        f"All API endpoint guesses failed for '{ticker}'. "
        "See diagnostic output above to identify the correct endpoint."
    )


def _extract_meta_from_item(item: dict, ticker: str) -> dict:
    slug = (
        item.get("slug") or item.get("unique_symbol_slug") or item.get("company_name_slug") or ""
    ).lower()
    if slug.startswith("asx-"):
        slug = slug[4:]
    if not slug:
        slug = _slugify(item.get("name") or item.get("company_name") or ticker)

    sector = (
        item.get("industry_name") or item.get("sector") or item.get("gics_sector") or "unknown"
    )
    country = (item.get("country_iso") or item.get("country") or "au").lower()
    return {"slug": slug, "sector": _slugify(sector), "country": country}


def _resolve_ticker(ticker: str, exchange: str, bearer: str, cookie_header: str) -> dict:
    """
    Resolve ticker → {slug, sector, country}.

    Strategy 1: follow /stocks/asx-{ticker} redirect (cleanest)
    Strategy 2: probe known API endpoint patterns (diagnostic fallback)
    """
    try:
        return _resolve_via_redirect(ticker, bearer, cookie_header)
    except SWSDownloadError as e:
        print(f"[sws_downloader] Redirect strategy failed: {e}", flush=True)

    return _resolve_via_api(ticker, exchange, bearer, cookie_header)


def download_csv(
    ticker: str,
    exchange: str,
    *,
    storage_state: Union[str, dict],
    output_dir: Path,
    headless: bool = True,
    debug_dir: Path,
) -> Path:
    """
    Download one SWS CSV for the given ticker using direct HTTP API calls.

    Steps:
      1. Extract Bearer JWT from storage_state
      2. Resolve ticker → slug/sector/country (redirect or API probe)
      3. GET the CSV download endpoint with auth headers

    Returns the path to the saved CSV file.
    Raises SWSDownloadError on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{exchange}_{ticker}.csv"

    bearer = _extract_bearer(storage_state)
    cookie_header = _build_cookie_header(storage_state)

    print(f"[sws_downloader] Resolving {exchange}:{ticker}...", flush=True)

    info = _resolve_ticker(ticker, exchange, bearer, cookie_header)
    slug = info["slug"]
    sector = info["sector"]
    country = info["country"]

    # Build CSV URL — note the double-slash before 'stocks' is intentional (matches SWS pattern)
    url = f"{_SWS_BASE}/api/company/download/csv//stocks/{country}/{sector}/asx-{ticker.lower()}/{slug}"

    headers = _api_headers(
        bearer,
        cookie_header,
        referer=f"https://simplywall.st/stocks/{country}/{sector}/asx-{ticker.lower()}/{slug}",
    )

    print(f"[sws_downloader] Downloading CSV: {url}", flush=True)
    time.sleep(random.uniform(1.5, 3.0))

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)

    print(f"[sws_downloader] CSV response: {resp.status_code}  content-type={resp.headers.get('content-type', '')!r}", flush=True)

    if resp.status_code in (401, 403):
        raise SWSDownloadError(
            f"Auth failed (HTTP {resp.status_code}) for {ticker}. "
            "Session may have expired — re-run --setup and update SWS_STORAGE_STATE secret."
        )
    if resp.status_code != 200:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"fail_{ticker}_csv_download.txt").write_text(
            f"URL: {url}\nStatus: {resp.status_code}\nBody: {resp.text[:2000]}",
            encoding="utf-8",
        )
        raise SWSDownloadError(f"CSV download failed for {ticker}: HTTP {resp.status_code}")

    content = resp.text
    if not content.strip() or "<html" in content[:100].lower():
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"fail_{ticker}_csv_content.txt").write_text(content[:2000], encoding="utf-8")
        raise SWSDownloadError(
            f"CSV download for {ticker} returned HTML instead of CSV. Check session/URL."
        )

    output_path.write_text(content, encoding="utf-8")
    print(f"[sws_downloader] Saved → {output_path}", flush=True)
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
