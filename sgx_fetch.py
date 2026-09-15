"""
sgx_fetch.py -- Singapore Exchange announcement fetching.

Analog of asx_fetch.py, but different in shape for two SGX-specific
reasons uncovered during the SGX spike (see PR #166 for the recipe):

  1. investors.sgx.com is a Flutter Web app that computes a signed
     `authorizationtoken` header client-side per request. Without
     that header, api.sgx.com returns 401 on the announcements list
     endpoint. The token cannot be reproduced from Python alone --
     it has to be captured from a live browser session.

  2. SGX also rejects non-browser TLS fingerprints, so even a raw
     `requests.get` with correct headers gets 403. Both problems
     go away when Playwright runs with channel='chrome' (installed
     Google Chrome) on the self-hosted Windows runner, with a real
     Chrome User-Agent (not "HeadlessChrome").

The token is NOT ticker-bound, so this module opens Playwright ONCE
per invocation, primes a token from any ticker, then hits the REST
endpoint for every requested ticker reusing that same token.

Public API
----------
    fetch_sgx_announcements(
        tickers: List[str],
        from_date: Optional[dt.date] = None,
        to_date: Optional[dt.date] = None,
        log=print,
    ) -> Dict[str, List[Dict]]

Returns a mapping ticker -> list of announcement dicts. Each dict has
the same core keys as asx_fetch.py's output (exchange, ticker, date,
time, title, url) so downstream classifiers can be shared, plus
SGX-specific extras (ref_id, id, sub, cat, category_name, issuer_name).

CLI
---
    python sgx_fetch.py [--tickers D05,U11] [--hours-back 24]
                        [--out outputs/sgx/announcements.json]

Runtime
-------
MUST run on the self-hosted Windows runner (or another machine with
installed Google Chrome AND a residential IP). GitHub-hosted
ubuntu-latest can reach the SPA but not the announcements list under
SGX's constraints -- see PR #164 for the fingerprint findings.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

import yaml


# --- constants ---------------------------------------------------------------

ANNOUNCEMENTS_HOST = "https://api.sgx.com"
ANNOUNCEMENTS_PATH = "/announcements/v1.1/securitycode"
PRIME_URL_TEMPLATE = (
    "https://investors.sgx.com/news/company-announcements"
    "?securityCode={ticker}&securityProduct=stocks"
)

# Chrome 153 UA WITHOUT "HeadlessChrome". Playwright stamps HeadlessChrome
# into the UA even with channel='chrome' in headless mode -- overriding it
# here matches what a real user session sends. Without this the API returns
# 401 on the securitycode endpoint even though sec-ch-ua is correct.
REAL_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)

# Page-size / period defaults. The Flutter frontend hits pagesize=25 with a
# ~20-year lookback and lets the client paginate; we use the same shape so
# our requests look identical.
DEFAULT_PAGE_SIZE = 25
DEFAULT_LOOKBACK_START = "20060916_160000"  # matches Flutter's own default

# Nav / prime timing.
PRIME_NAV_TIMEOUT_MS = 60_000
PRIME_TAIL_WAIT_MS = 3_000
REST_TIMEOUT_MS = 30_000

SGT = dt.timezone(dt.timedelta(hours=8))


# --- helpers -----------------------------------------------------------------

def _sgt_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(SGT)


def _sgt_stamp(when: dt.datetime) -> str:
    """Compact SGT timestamp SGX uses in periodstart/periodend."""
    return when.strftime("%Y%m%d_%H%M%S")


def _list_url(
    ticker: str,
    period_start: str,
    period_end: str,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> str:
    """Build the announcements list URL in the exact shape Flutter fires
    (verified in PR #166 -- getting these query params right was itself a
    diagnostic round). Both period bounds are compact SGT strings."""
    return (
        f"{ANNOUNCEMENTS_HOST}{ANNOUNCEMENTS_PATH}"
        f"?value={ticker}"
        f"&securityCodeParams={ticker}"
        f"&pagestart=0&pagesize={page_size}"
        f"&periodstart={period_start}"
        f"&periodend={period_end}"
        f"&exactsearch=true"
    )


def _normalize_row(row: Dict) -> Dict:
    """Turn a raw SGX API row into a dict shaped for Bob's downstream
    pipeline. Keeps the ASX-compatible core keys (exchange, ticker, date,
    time, title, url) so shared code doesn't need to branch on exchange,
    and carries the SGX-specific extras (ref_id, id, sub, cat,
    category_name, issuer_name, submission_ts_ms) alongside for the
    SGX-aware classifier."""
    ticker = ""
    issuer = ""
    issuers = row.get("issuers") or []
    if issuers and isinstance(issuers[0], dict):
        ticker = (issuers[0].get("stock_code") or "").strip()
        issuer = (issuers[0].get("issuer_name") or "").strip()

    ts_ms = row.get("submission_date_time") or row.get("broadcast_date_time")
    date_str = ""
    time_str = ""
    if ts_ms:
        try:
            when = (
                dt.datetime.fromtimestamp(int(ts_ms) / 1000, tz=dt.timezone.utc)
                .astimezone(SGT)
            )
            date_str = when.strftime("%d/%m/%Y")
            time_str = when.strftime("%I:%M %p")
        except Exception:
            pass

    return {
        "exchange": "SGX",
        "ticker": ticker,
        "date": date_str,
        "time": time_str,
        "title": (row.get("title") or "").strip(),
        "url": (row.get("url") or "").strip(),
        # SGX-specific -- do not shorten to fit the ASX schema.
        "ref_id": row.get("ref_id"),
        "id": row.get("id"),
        "sub": row.get("sub"),
        "cat": row.get("cat"),
        "category_name": row.get("category_name"),
        "issuer_name": issuer,
        "submission_ts_ms": int(ts_ms) if ts_ms else None,
    }


def _within_window(
    item: Dict,
    from_date: Optional[dt.date],
    to_date: Optional[dt.date],
) -> bool:
    """Post-fetch client-side filter. The API's periodstart/periodend
    already narrows server-side, but users pass from_date/to_date as
    calendar dates in SGT and we want to respect that even when the
    server has rounded to something wider."""
    if from_date is None and to_date is None:
        return True
    ts_ms = item.get("submission_ts_ms")
    if not ts_ms:
        # Undated rows -- keep them; the alternative (silently dropping)
        # is worse than a caller seeing "unknown date" and deciding.
        return True
    try:
        when = (
            dt.datetime.fromtimestamp(ts_ms / 1000, tz=dt.timezone.utc)
            .astimezone(SGT)
            .date()
        )
    except Exception:
        return True
    if from_date is not None and when < from_date:
        return False
    if to_date is not None and when > to_date:
        return False
    return True


# --- Playwright core ---------------------------------------------------------

async def _fetch_all_async(
    tickers: List[str],
    from_date: Optional[dt.date],
    to_date: Optional[dt.date],
    log: Callable[[str], None],
) -> Dict[str, List[Dict]]:
    from playwright.async_api import async_playwright

    period_start_str = DEFAULT_LOOKBACK_START
    if from_date is not None:
        period_start_str = _sgt_stamp(
            dt.datetime.combine(from_date, dt.time.min, tzinfo=SGT)
        )
    period_end_str = _sgt_stamp(
        _sgt_now() if to_date is None
        else dt.datetime.combine(to_date, dt.time.max, tzinfo=SGT)
    )

    results: Dict[str, List[Dict]] = {t: [] for t in tickers}
    if not tickers:
        return results

    async with async_playwright() as p:
        # channel='chrome' is REQUIRED. Headless bundled Chromium sends
        # sec-ch-ua: "HeadlessChrome" which SGX filters. See PR #164.
        try:
            browser = await p.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            log(f"[sgx] chromium launch (channel='chrome') failed: {exc}")
            log("[sgx] this module requires installed Google Chrome and the "
                "self-hosted Windows runner; ubuntu-latest cannot reach the "
                "announcements list endpoint under SGX's constraints.")
            return results

        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=REAL_CHROME_UA,
        )
        page = await context.new_page()

        # Prime: intercept the first api.sgx.com request Flutter fires
        # after nav, capturing its authorizationtoken header. That token
        # is what unlocks REST calls for the whole run. Token is NOT
        # ticker-bound (verified in PR #165) so one prime does all tickers.
        token_holder: List[str] = []

        async def _on_request(req) -> None:
            if "api.sgx.com" not in req.url or token_holder:
                return
            try:
                headers = await req.all_headers()
                tok = (headers.get("authorizationtoken") or "").strip()
                if tok:
                    token_holder.append(tok)
            except Exception:
                pass

        page.on("request", lambda r: asyncio.create_task(_on_request(r)))

        prime_ticker = tickers[0]
        log(f"[sgx] priming token via {prime_ticker}...")
        try:
            await page.goto(
                PRIME_URL_TEMPLATE.format(ticker=prime_ticker),
                wait_until="networkidle",
                timeout=PRIME_NAV_TIMEOUT_MS,
            )
        except Exception as exc:
            log(f"[sgx] prime nav exception: {exc.__class__.__name__}: {exc}")
        # Flutter fires late requests after networkidle briefly settles;
        # tiny tail wait catches them.
        await page.wait_for_timeout(PRIME_TAIL_WAIT_MS)

        if not token_holder:
            log("[sgx] FAILED to capture an authorizationtoken during prime "
                "-- aborting. Investors.sgx.com may have changed shape or "
                "the browser fingerprint is now blocked.")
            await context.close()
            await browser.close()
            return results

        token = token_holder[0]
        log(f"[sgx] token captured (len={len(token)})")

        # REST call per ticker via context.request -- carries cookies
        # from the browser context, and we inject the captured token
        # explicitly since context.request doesn't replay Flutter's
        # inline headers.
        common_headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://investors.sgx.com",
            "Referer": "https://investors.sgx.com/",
            "authorizationtoken": token,
            "content-type": "application/json; charset=UTF-8",
        }
        for ticker in tickers:
            url = _list_url(ticker, period_start_str, period_end_str)
            try:
                resp = await context.request.get(
                    url, headers=common_headers, timeout=REST_TIMEOUT_MS,
                )
                if resp.status != 200:
                    body = await resp.text()
                    log(f"[sgx] {ticker}: HTTP {resp.status} -- "
                        f"{body[:200]!r}")
                    continue
                payload = await resp.json()
            except Exception as exc:
                log(f"[sgx] {ticker}: exception {exc.__class__.__name__}: {exc}")
                continue

            rows = payload.get("data") if isinstance(payload, dict) else None
            rows = rows or []
            items = [_normalize_row(r) for r in rows if isinstance(r, dict)]
            items = [it for it in items if _within_window(it, from_date, to_date)]
            results[ticker] = items
            log(f"[sgx] {ticker}: {len(items)} announcement(s) after date filter")

        await context.close()
        await browser.close()

    return results


# --- public sync entry -------------------------------------------------------

def fetch_sgx_announcements(
    tickers: List[str],
    from_date: Optional[dt.date] = None,
    to_date: Optional[dt.date] = None,
    log: Callable[[str], None] = print,
) -> Dict[str, List[Dict]]:
    """Fetch fresh SGX announcements for `tickers`. Returns a mapping
    ticker -> list of normalized announcement dicts. Empty list means
    the API returned zero rows in the window; missing ticker means the
    fetch itself failed for that ticker."""
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    return asyncio.run(_fetch_all_async(tickers, from_date, to_date, log))


# --- CLI ---------------------------------------------------------------------

def _load_sgx_tickers_from_yaml(path: Path) -> List[str]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sgx = data.get("sgx") or {}
    if not isinstance(sgx, dict):
        return []
    return sorted(k for k in sgx.keys() if isinstance(k, str))


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Fetch SGX announcements.")
    parser.add_argument(
        "--tickers", default=None,
        help="Comma-separated SGX codes. Defaults to tickers.yaml sgx section.",
    )
    parser.add_argument(
        "--hours-back", type=int, default=None,
        help="Filter to announcements from the last N hours (SGT). "
             "Default: no time filter (returns whatever pagesize=25 gives).",
    )
    parser.add_argument(
        "--out", default="outputs/sgx/announcements.json",
        help="Output JSON path.",
    )
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = _load_sgx_tickers_from_yaml(Path("tickers.yaml"))
    if not tickers:
        print("[sgx] no tickers to fetch (--tickers or tickers.yaml sgx: "
              "section required)", file=sys.stderr)
        return 1

    from_date: Optional[dt.date] = None
    if args.hours_back:
        from_date = (_sgt_now() - dt.timedelta(hours=args.hours_back)).date()

    print(f"[sgx] fetching {len(tickers)} ticker(s): {', '.join(tickers)}")
    if from_date:
        print(f"[sgx] from_date (SGT): {from_date}")

    results = fetch_sgx_announcements(tickers, from_date=from_date)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "fetched_at_sgt": _sgt_now().isoformat(),
                "tickers": tickers,
                "from_date": from_date.isoformat() if from_date else None,
                "results": results,
            },
            indent=2, default=str,
        ),
        encoding="utf-8",
    )
    total = sum(len(v) for v in results.values())
    print(f"[sgx] wrote {out_path} ({total} announcement(s) across "
          f"{sum(1 for v in results.values() if v)} ticker(s) with rows)")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
