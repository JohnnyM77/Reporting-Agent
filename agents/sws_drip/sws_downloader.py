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

from . import sws_url_map


class SWSDownloadError(Exception):
    """Raised when a download step fails."""


_SWS_BASE = "https://simplywall.st"

# Cached {ticker -> country/sector/slug} map built from SWS sitemaps.
_URL_MAP_CACHE = Path(__file__).resolve().parents[2] / "data" / "sws_url_map.json"

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


def _looks_like_jwt(val: str) -> bool:
    """A JWT is three base64url segments joined by dots, header starts with 'ey'."""
    return val.startswith("ey") and val.count(".") == 2


def _jwt_exp_info(token: str) -> str:
    """
    Decode a JWT payload (no verification) and report its expiry status.
    Used purely for diagnostics so we can see if a 401 is just an expired token.
    """
    if not _looks_like_jwt(token):
        return "not-a-jwt"
    try:
        import base64
        import json as _json
        from datetime import datetime, timezone

        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)  # pad to multiple of 4
        payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if exp is None:
            return "jwt (no exp claim)"
        exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        delta_h = (exp_dt - now).total_seconds() / 3600
        state = "EXPIRED" if delta_h < 0 else "valid"
        return f"jwt {state}, exp={exp_dt.isoformat()} ({delta_h:+.1f}h from now)"
    except Exception as e:
        return f"jwt (decode failed: {type(e).__name__})"


def _extract_bearer(storage_state: Union[str, dict]) -> str:
    """
    Pull the Bearer token from localStorage -> cookies.

    Prefers values that look like a JWT over arbitrary token-named entries,
    and logs which key was chosen plus the token's expiry so a 401 is easy to
    diagnose. Raises if nothing usable is found.
    """
    state = _parse_state(storage_state)

    candidates: list[tuple[str, str]] = []  # (source_label, value)
    for origin in state.get("origins", []):
        for entry in origin.get("localStorage", []):
            key = entry.get("name", "")
            if any(k in key.lower() for k in ("token", "auth", "jwt")):
                val = entry.get("value", "")
                if val and len(val) > 20:
                    candidates.append((f"localStorage:{key}", val))
    for cookie in state.get("cookies", []):
        name = cookie.get("name", "")
        if any(k in name.lower() for k in ("token", "auth", "jwt", "session")):
            val = cookie.get("value", "")
            if val and len(val) > 20:
                candidates.append((f"cookie:{name}", val))

    if not candidates:
        raise SWSDownloadError(
            "Could not find Bearer token in storage_state. "
            "Session may have expired — re-run --setup locally and update SWS_STORAGE_STATE secret."
        )

    # Prefer a real JWT; fall back to the first candidate otherwise.
    jwts = [(src, v) for src, v in candidates if _looks_like_jwt(v)]
    chosen_src, chosen = (jwts[0] if jwts else candidates[0])
    print(
        f"[sws_downloader] Bearer source: {chosen_src} | {_jwt_exp_info(chosen)} "
        f"| {len(candidates)} candidate(s)",
        flush=True,
    )
    return chosen


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


def _fetch_xrw_header(session: cffi_requests.Session) -> str:
    """
    Extract the current X-Requested-With service identifier from SWS's homepage.
    SWS embeds it as a string like 'sws-services/mono-<build-hash>' in the HTML.
    Falls back to a known-good value if extraction fails.
    """
    fallback = "sws-services/mono-manual-9eaf70d9-hotfix"
    try:
        resp = session.get(_SWS_BASE, timeout=20)
        if resp.status_code == 200:
            m = re.search(r'(sws-services/[a-z0-9/\-]+)', resp.text)
            if m:
                val = m.group(1).rstrip("-")
                print(f"[sws_downloader] X-Requested-With from homepage: {val}", flush=True)
                return val
    except Exception as e:
        print(f"[sws_downloader] X-Requested-With extraction failed: {e}", flush=True)
    print(f"[sws_downloader] X-Requested-With fallback: {fallback}", flush=True)
    return fallback


def _make_session(bearer: str, cookies: dict) -> cffi_requests.Session:
    """Create a curl-cffi session that looks like Chrome to Cloudflare."""
    session = cffi_requests.Session(impersonate=_CHROME_IMPERSONATE)
    session.headers.update({
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/vnd.simplywallst.v2",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": _USER_AGENT,
        "Origin": "https://simplywall.st",
    })
    session.cookies.update(cookies)
    return session


def _extract_algolia_key(session: cffi_requests.Session) -> tuple[str, str] | None:
    """
    Extract the current Algolia app_id + api_key from SWS's homepage JS bundle.
    SWS embeds these as public credentials in their frontend code.
    Returns (app_id, api_key) or None if not found.
    """
    try:
        resp = session.get(_SWS_BASE, timeout=20)
        if resp.status_code != 200:
            print(f"[sws_downloader] homepage -> HTTP {resp.status_code}", flush=True)
            return None
        # Find the JS bundle URL(s)
        js_urls = re.findall(r'src="(/_next/static/[^"]+\.js)"', resp.text)
        if not js_urls:
            js_urls = re.findall(r'src="(/[^"]+chunk[^"]+\.js)"', resp.text)
        print(f"[sws_downloader] Found {len(js_urls)} JS chunks to scan for Algolia key", flush=True)
        for js_url in js_urls[:10]:
            full_url = _SWS_BASE + js_url if js_url.startswith("/") else js_url
            r = session.get(full_url, timeout=15)
            if r.status_code != 200:
                continue
            # Look for Algolia credentials pattern
            m = re.search(r'"([A-Z0-9]{8,12})","([a-f0-9]{32,})"', r.text)
            if m:
                app_id, api_key = m.group(1), m.group(2)
                print(f"[sws_downloader] Found Algolia creds: app_id={app_id}", flush=True)
                return app_id, api_key
    except Exception as e:
        print(f"[sws_downloader] algolia-key extraction error: {e}", flush=True)
    return None


def _resolve_via_algolia(ticker: str, exchange: str, app_id: str, api_key: str) -> dict | None:
    """Query Algolia search to get slug/sector/country for a ticker."""
    algolia_url = f"https://{app_id.lower()}-dsn.algolia.net/1/indexes/companies/query"
    headers = {
        "X-Algolia-Application-Id": app_id,
        "X-Algolia-API-Key": api_key,
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }
    payload = {"query": f"{exchange}:{ticker}", "hitsPerPage": 5}
    try:
        import httpx
        with httpx.Client(timeout=15) as client:
            resp = client.post(algolia_url, json=payload, headers=headers)
        print(
            f"[sws_downloader] algolia query -> HTTP {resp.status_code} "
            f"body[:100]={resp.text[:100]!r}",
            flush=True,
        )
        if resp.status_code == 200:
            for hit in resp.json().get("hits", []):
                sym = (hit.get("ticker_symbol") or hit.get("symbol") or "").upper()
                if sym == ticker.upper():
                    return _extract_meta(hit, ticker)
    except Exception as e:
        print(f"[sws_downloader] algolia query error: {e}", flush=True)
    return None


def _resolve_ticker(ticker: str, exchange: str, session: cffi_requests.Session) -> dict:
    """
    Resolve ticker -> {slug, sector, country}.

    Strategy 0 (primary): look the ticker up in the sitemap-derived URL map.
        This is the reliable, dynamic path — SWS publishes every stock URL in
        its sitemaps, so once the map is built any ASX ticker resolves instantly.
        If the ticker is missing from a cached map, the map is rebuilt once.
    Strategy 1: SWS site search page (/search?q=TICKER) — parse stock links from HTML.
    Strategy 2: Extract live Algolia key from SWS homepage JS, then query Algolia.
    Strategy 3: Probe known SWS REST search endpoints.
    """
    key = ticker.upper()

    # Strategy 0: sitemap-derived URL map (cached, rebuilt when stale/missing).
    try:
        url_map = sws_url_map.load_or_build(session, _URL_MAP_CACHE)
        if key not in url_map:
            # Ticker absent — force one rebuild in case the cache predates a listing.
            print(f"[sws_downloader] {key} not in cached map; rebuilding from sitemaps...", flush=True)
            url_map = sws_url_map.load_or_build(session, _URL_MAP_CACHE, force=True)
        if key in url_map:
            result = url_map[key]
            print(f"[sws_downloader] Resolved via sitemap map: {result}", flush=True)
            return {"country": result["country"], "sector": result["sector"], "slug": result["slug"]}
        print(f"[sws_downloader] {key} not found in sitemap map; trying fallbacks...", flush=True)
    except Exception as e:
        print(f"[sws_downloader] sitemap map error: {type(e).__name__}: {e}", flush=True)

    _STOCK_URL_RE = re.compile(
        r"/stocks/([a-z]{2})/([^/]+)/asx-" + re.escape(ticker.lower()) + r"/([^/?#\"' ]+)"
    )

    # Strategy 1: SWS site search page
    for search_url in (
        f"{_SWS_BASE}/search?q={ticker}",
        f"{_SWS_BASE}/search?q=ASX:{ticker}",
        f"{_SWS_BASE}/discover/investing-ideas?search={ticker}",
    ):
        try:
            resp = session.get(search_url, timeout=20, allow_redirects=True)
            final_url = str(resp.url)
            print(
                f"[sws_downloader] {search_url} -> HTTP {resp.status_code} final={final_url}",
                flush=True,
            )
            # Check redirect URL first
            m = _STOCK_URL_RE.search(final_url)
            if m:
                result = {"country": m.group(1), "sector": m.group(2), "slug": m.group(3)}
                print(f"[sws_downloader] Resolved via redirect: {result}", flush=True)
                return result
            if resp.status_code == 200:
                m = _STOCK_URL_RE.search(resp.text)
                if m:
                    result = {"country": m.group(1), "sector": m.group(2), "slug": m.group(3)}
                    print(f"[sws_downloader] Resolved via search page HTML: {result}", flush=True)
                    return result
                print(
                    f"[sws_downloader] 200 OK but no stock URL found in HTML (first 300): "
                    f"{resp.text[:300]!r}",
                    flush=True,
                )
        except Exception as e:
            print(f"[sws_downloader] search error {search_url}: {type(e).__name__}: {e}", flush=True)

    # Strategy 2: extract live Algolia key from SWS JS and query Algolia
    print("[sws_downloader] Trying Algolia key extraction from SWS homepage...", flush=True)
    algolia_creds = _extract_algolia_key(session)
    if algolia_creds:
        result = _resolve_via_algolia(ticker, exchange, *algolia_creds)
        if result:
            return result

    # Strategy 3: probe known SWS REST endpoints
    for endpoint, params in (
        (f"{_SWS_BASE}/api/companies", {"query": ticker, "exchange": exchange}),
        (f"{_SWS_BASE}/api/company/search", {"q": ticker}),
        (f"{_SWS_BASE}/api/search", {"q": ticker, "type": "company"}),
    ):
        try:
            resp = session.get(endpoint, params=params, timeout=15)
            print(
                f"[sws_downloader] {endpoint} -> HTTP {resp.status_code} "
                f"body[:150]={resp.text[:150]!r}",
                flush=True,
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("data") or data if isinstance(data, (dict, list)) else []
                if isinstance(items, dict):
                    items = list(items.values())[:1]
                for item in (items if isinstance(items, list) else []):
                    sym = (
                        item.get("ticker_symbol") or item.get("unique_symbol") or item.get("symbol") or ""
                    ).upper().replace(f"{exchange}:", "")
                    if sym == ticker.upper():
                        return _extract_meta(item, ticker)
        except Exception as e:
            print(f"[sws_downloader] endpoint error {endpoint}: {type(e).__name__}: {e}", flush=True)

    raise SWSDownloadError(
        f"Could not resolve '{ticker}'. All strategies failed — see logs above. "
        "The SWS session may have expired, or the ticker may not be listed on SWS."
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

    # Fetch the live X-Requested-With value from SWS homepage so we match
    # whatever build hash their API gateway currently requires.
    xrw = _fetch_xrw_header(session)
    session.headers["X-Requested-With"] = xrw

    print(f"[sws_downloader] Resolving {exchange}:{ticker}...", flush=True)
    info = _resolve_ticker(ticker, exchange, session)
    slug = info["slug"]
    sector = info["sector"]
    country = info["country"]
    print(f"[sws_downloader] Resolved: country={country!r} sector={sector!r} slug={slug!r}", flush=True)

    # Double-slash before 'stocks' is intentional — matches the SWS API pattern
    url = f"{_SWS_BASE}/api/company/download/csv//stocks/{country}/{sector}/asx-{ticker.lower()}/{slug}"
    referer = f"https://simplywall.st/stocks/{country}/{sector}/asx-{ticker.lower()}/{slug}"

    print(f"[sws_downloader] Downloading CSV: {url}", flush=True)
    time.sleep(random.uniform(1.5, 3.0))

    # Use Accept: */* for the CSV download — the browser sends this for file downloads,
    # not application/vnd.simplywallst.v2 which is for JSON API responses.
    resp = session.get(url, timeout=30, headers={
        "Referer": referer,
        "Accept": "*/*",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    })
    resp_headers_str = "\n".join(f"  {k}: {v}" for k, v in resp.headers.items())
    print(f"[sws_downloader] CSV response: {resp.status_code}  ct={resp.headers.get('content-type','')!r}", flush=True)

    if resp.status_code in (401, 403):
        print(f"[sws_downloader] auth-fail body[:300]={resp.text[:300]!r}", flush=True)
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"fail_{ticker}_auth.txt").write_text(
            f"URL: {url}\nReferer: {referer}\nX-Requested-With: {xrw}\n"
            f"Status: {resp.status_code}\nResponse headers:\n{resp_headers_str}\n\nBody: {resp.text[:2000]}",
            encoding="utf-8",
        )
        raise SWSDownloadError(
            f"Auth failed (HTTP {resp.status_code}) for {ticker}. "
            "Session may have expired — re-run --setup and update SWS_STORAGE_STATE secret. "
            "See the body logged above for the exact reason."
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
