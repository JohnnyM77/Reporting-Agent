"""
agents/sws_drip/sws_downloader.py
───────────────────────────────────
Direct HTTP API approach: authenticate with Bearer JWT from storage_state,
resolve ticker via SWS internal company API, download CSV via SWS REST API.

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
_CSV_URL_TEMPLATE = (
    "/api/company/download/csv//stocks/{country}/{sector}/asx-{ticker}/{slug}"
)

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


def _sws_headers(bearer: str, cookie_header: str, referer: str = "") -> dict:
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


def _resolve_ticker_via_sws_api(ticker: str, exchange: str, bearer: str, cookie_header: str) -> dict:
    """
    Use SWS's own authenticated company search API to resolve ticker →
    {slug, sector, country}.

    Tries multiple endpoints in order:
      1. /api/company?query=ASX:BHP  (main search)
      2. /api/company?query=BHP&exchange=ASX
      3. /api/company/asx-{ticker}   (direct lookup by unique_symbol)
    """
    headers = _sws_headers(bearer, cookie_header, referer="https://simplywall.st/dashboard")
    search_query = f"{exchange}:{ticker}"

    with httpx.Client(timeout=20, follow_redirects=True) as client:
        # Attempt 1: search by exchange:ticker
        for params in [
            {"query": search_query, "limit": 5},
            {"query": ticker, "exchange": exchange, "limit": 5},
        ]:
            try:
                resp = client.get(
                    f"{_SWS_BASE}/api/company",
                    params=params,
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # Response may be {"data": [...]} or a list directly
                    items = data.get("data", data) if isinstance(data, dict) else data
                    if isinstance(items, list):
                        for item in items:
                            sym = (
                                item.get("ticker_symbol")
                                or item.get("unique_symbol")
                                or item.get("symbol")
                                or ""
                            ).upper()
                            # Match bare ticker or ASX:TICKER format
                            if sym in (ticker.upper(), f"{exchange}:{ticker}".upper()):
                                return _extract_company_meta(item, ticker)
            except Exception:
                continue

        # Attempt 2: direct unique_symbol lookup
        try:
            resp = client.get(
                f"{_SWS_BASE}/api/company/asx-{ticker.lower()}",
                headers=headers,
            )
            if resp.status_code == 200:
                item = resp.json()
                if isinstance(item, dict) and item.get("data"):
                    item = item["data"]
                return _extract_company_meta(item, ticker)
        except Exception:
            pass

    raise SWSDownloadError(
        f"Could not resolve ticker '{ticker}' on exchange '{exchange}' via SWS company API. "
        f"Check that the ticker exists on SWS, or the session has expired."
    )


def _extract_company_meta(item: dict, ticker: str) -> dict:
    """Pull slug/sector/country out of an SWS company API response item."""
    # Slug can come from several fields
    slug = (
        item.get("slug")
        or item.get("unique_symbol_slug")
        or item.get("company_name_slug")
        or ""
    ).lower()
    # Remove leading exchange prefix if present (e.g. "asx-bhp" → "bhp")
    if slug.startswith("asx-"):
        slug = slug[4:]

    # If still no slug, derive from company name
    if not slug:
        name = item.get("name") or item.get("company_name") or ticker
        slug = _slugify(name)

    sector = (
        item.get("industry_name")
        or item.get("sector")
        or item.get("gics_sector")
        or "unknown"
    )
    country = (
        item.get("country_iso")
        or item.get("country")
        or "au"
    ).lower()

    return {
        "slug": slug,
        "sector": _slugify(sector),
        "country": country,
        "raw": item,
    }


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
      2. Resolve ticker → slug/sector/country via SWS company API (authenticated)
      3. GET the CSV download endpoint with auth headers

    Returns the path to the saved CSV file.
    Raises SWSDownloadError on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{exchange}_{ticker}.csv"

    bearer = _extract_bearer(storage_state)
    cookie_header = _build_cookie_header(storage_state)

    print(f"[sws_downloader] Resolving {exchange}:{ticker} via SWS API...", flush=True)

    info = _resolve_ticker_via_sws_api(ticker, exchange, bearer, cookie_header)
    slug = info["slug"]
    sector = info["sector"]
    country = info["country"]

    print(
        f"[sws_downloader] Resolved: slug={slug!r} sector={sector!r} country={country!r}",
        flush=True,
    )

    csv_path_segment = _CSV_URL_TEMPLATE.format(
        country=country,
        sector=sector,
        ticker=ticker.lower(),
        slug=slug,
    )
    url = _SWS_BASE + csv_path_segment

    headers = _sws_headers(
        bearer,
        cookie_header,
        referer=f"https://simplywall.st/stocks/au/{sector}/asx-{ticker.lower()}/{slug}",
    )

    print(f"[sws_downloader] Downloading CSV from: {url}", flush=True)
    time.sleep(random.uniform(1.5, 3.0))

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)

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
