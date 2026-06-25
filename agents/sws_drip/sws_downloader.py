"""
agents/sws_drip/sws_downloader.py
───────────────────────────────────
Direct HTTP API: authenticate with Bearer JWT + cf_clearance from storage_state.
Uses curl-cffi to impersonate Chrome's TLS fingerprint, bypassing Cloudflare.

No Playwright. No browser headaches. No Cloudflare blocks.
"""

from __future__ import annotations

import json
import re
import time
import random
from pathlib import Path
from typing import Union

# curl_cffi impersonates Chrome's TLS fingerprint — required to pass Cloudflare
from curl_cffi import requests as cffi_requests


class SWSDownloadError(Exception):
    """Raised when a download step fails."""


_SWS_BASE = "https://simplywall.st"

# Chrome version to impersonate — must match what was used when capturing storage_state
_CHROME_IMPERSONATE = "chrome124"

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
    """Pull Bearer JWT from localStorage → cookies. Raises if not found."""
    state = _parse_state(storage_state)

    for origin in state.get("origins", []):
        for entry in origin.get("localStorage", []):
            key = entry.get("name", "")
            if "token" in key.lower() or "auth" in key.lower() or "jwt" in key.lower():
                val = entry.get("value", "")
                if val and len(val) > 20:
                    return val

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


def _build_cookie_jar(storage_state: Union[str, dict]) -> dict:
    """Build a cookie dict from all SWS cookies (includes cf_clearance)."""
    state = _parse_state(storage_state)
    jar = {}
    for cookie in state.get("cookies", []):
        if "simplywall" in cookie.get("domain", ""):
            jar[cookie["name"]] = cookie["value"]
    return jar


def _slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def _make_session(bearer: str, cookies: dict) -> cffi_requests.Session:
    """Create a curl-cffi session that looks like Chrome to Cloudflare."""
    session = cffi_requests.Session(impersonate=_CHROME_IMPERSONATE)
    session.headers.update({
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/vnd.simplywallst.v2",
        "User-Agent": _USER_AGENT,
        "Origin": "https://simplywall.st",
    })
    session.cookies.update(cookies)
    return session


def _resolve_ticker(ticker: str, exchange: str, session: cffi_requests.Session) -> dict:
    """
    Resolve ticker → {slug, sector, country}.

    Strategy 1 (canonical): GET the short company URL /stocks/asx-{ticker} and
    follow the redirect. SWS redirects it to the full canonical path
    /stocks/{country}/{sector}/asx-{ticker}/{slug}, which we parse directly.
    No guessing, no API key — the exact path SWS itself uses.

    Strategy 2 (fallback): probe the company search API and log raw responses.

    All log messages use ASCII only so they never crash on Windows cp1252.
    """
    # ---- Strategy 1: follow the short-URL redirect ----------------------------
    short_url = f"{_SWS_BASE}/stocks/asx-{ticker.lower()}"
    try:
        resp = session.get(short_url, timeout=15, allow_redirects=True)
        final_url = str(resp.url)
        print(
            f"[sws_downloader] short-url {short_url} -> HTTP {resp.status_code}, "
            f"final={final_url}",
            flush=True,
        )
        m = re.search(r"/stocks/([^/]+)/([^/]+)/asx-[^/]+/([^/?#]+)", final_url)
        if m:
            return {"country": m.group(1), "sector": m.group(2), "slug": m.group(3)}
    except Exception as e:
        print(f"[sws_downloader] short-url error: {type(e).__name__}: {e}", flush=True)

    # ---- Strategy 2: company search API (diagnostic fallback) ------------------
    for endpoint in (f"{_SWS_BASE}/api/company", f"{_SWS_BASE}/api/companies"):
        for query in (f"{exchange}:{ticker}", ticker):
            try:
                resp = session.get(endpoint, params={"query": query, "limit": 5}, timeout=15)
                print(
                    f"[sws_downloader] {endpoint}?query={query} -> HTTP {resp.status_code} "
                    f"({resp.headers.get('content-type','')[:40]}) "
                    f"body[:150]={resp.text[:150]!r}",
                    flush=True,
                )
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        items = data.get("data", []) if isinstance(data, dict) else data
                        if isinstance(items, list):
                            for item in items:
                                sym = (
                                    item.get("ticker_symbol")
                                    or item.get("unique_symbol")
                                    or item.get("symbol")
                                    or ""
                                ).upper().replace("ASX:", "")
                                if sym == ticker.upper():
                                    return _extract_meta(item, ticker)
                    except Exception as e:
                        print(f"[sws_downloader] JSON parse error: {type(e).__name__}: {e}", flush=True)
            except Exception as e:
                print(f"[sws_downloader] request error: {type(e).__name__}: {e}", flush=True)

    raise SWSDownloadError(
        f"Could not resolve '{ticker}'. See diagnostic output above for the "
        "actual HTTP responses from SWS."
    )


def _extract_meta(item: dict, ticker: str) -> dict:
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
    Download one SWS CSV using curl-cffi (Chrome TLS impersonation).

    1. Extract Bearer JWT + cf_clearance cookie from storage_state
    2. Create a curl-cffi session that Cloudflare accepts as Chrome
    3. Resolve ticker → slug/sector/country via SWS company API
    4. Download CSV

    Returns path to saved CSV. Raises SWSDownloadError on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{exchange}_{ticker}.csv"

    bearer = _extract_bearer(storage_state)
    cookies = _build_cookie_jar(storage_state)

    cf_present = "cf_clearance" in cookies
    print(
        f"[sws_downloader] Auth: bearer={'yes' if bearer else 'NO'} "
        f"cf_clearance={'yes' if cf_present else 'NO (may still work if session is fresh)'} "
        f"total_cookies={len(cookies)}",
        flush=True,
    )

    session = _make_session(bearer, cookies)

    print(f"[sws_downloader] Resolving {exchange}:{ticker}...", flush=True)
    info = _resolve_ticker(ticker, exchange, session)
    slug = info["slug"]
    sector = info["sector"]
    country = info["country"]
    print(f"[sws_downloader] Resolved: country={country!r} sector={sector!r} slug={slug!r}", flush=True)

    # Double-slash before 'stocks' is intentional — matches the SWS API pattern
    url = f"{_SWS_BASE}/api/company/download/csv//stocks/{country}/{sector}/asx-{ticker.lower()}/{slug}"
    session.headers["Referer"] = f"https://simplywall.st/stocks/{country}/{sector}/asx-{ticker.lower()}/{slug}"

    print(f"[sws_downloader] Downloading CSV: {url}", flush=True)
    time.sleep(random.uniform(1.5, 3.0))

    resp = session.get(url, timeout=30)
    print(f"[sws_downloader] CSV response: {resp.status_code}  ct={resp.headers.get('content-type','')!r}", flush=True)

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
    One-time manual login flow. Opens a headed browser, waits for login,
    saves browser context (including cf_clearance cookie) to output_path.
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
            user_agent=_USER_AGENT,
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
