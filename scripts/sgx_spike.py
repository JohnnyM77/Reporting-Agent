"""
SGX fetch spike v7 — token forwarding.

v6 discovered that Flutter DOES compute a real `authorizationtoken`
(~130-char signed base64) when the browser fingerprint is right:

  - installed Google Chrome via Playwright's channel='chrome'
  - real Chrome User-Agent (no 'HeadlessChrome')
  - running on the Windows self-hosted runner (residential IP)

But the v6 retry via context.request still 401'd -- context.request
inherits cookies but not the header Flutter injects at call-site.

v7 closes the loop:

  1. Nav to the announcements page and record every api.sgx.com
     request that carries a non-empty authorizationtoken. Save the
     token.
  2. Fire the /announcements/v1.1/securitycode LIST endpoint via
     context.request, this time WITH the captured token as an
     authorizationtoken header.
  3. Repeat step 1 + 2 for a second ticker (U11 = UOB) to answer:
     is the token ticker-bound, or is one token good for any ticker?

If step 2 returns 200 with real JSON of DBS announcements, the sgx_fetch.py
architecture is: Playwright once per run to prime a token, replay REST calls
with it. Discovery is done.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

TICKER_A = os.environ.get("SGX_SPIKE_TICKER", "D05").strip().upper()
TICKER_B = "U11"  # UOB - second ticker to test if token is ticker-bound
OUT_DIR = Path("outputs/sgx_spike")
OUT_DIR.mkdir(parents=True, exist_ok=True)

ANNOUNCEMENTS_HOST = "https://api.sgx.com"
ANNOUNCEMENTS_PATH = "/announcements/v1.1/securitycode"

REAL_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _fresh_periodstart() -> str:
    sgt_now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)
    return sgt_now.strftime("%Y%m%d_%H%M%S")


def _list_url(ticker: str, sub: str = "ANNC17") -> str:
    return (
        f"{ANNOUNCEMENTS_HOST}{ANNOUNCEMENTS_PATH}"
        f"?value={ticker}&cat=ANNC&securityCodeParams=securitycode"
        f"&sub={sub}&pagestart=0&pagesize=250&periodstart={_fresh_periodstart()}"
    )


async def _prime_token(page, ticker: str, tokens: list[str]) -> None:
    """Navigate to the announcements page for `ticker`, letting Flutter fire
    its usual XHRs. The response listener attached separately appends every
    non-empty authorizationtoken it sees to `tokens`."""
    url = (
        f"https://investors.sgx.com/news/company-announcements"
        f"?securityCode={ticker}&securityProduct=stocks"
    )
    _log(f"\n[prime] {url}")
    try:
        await page.goto(url, wait_until="networkidle", timeout=60_000)
    except Exception as exc:
        _log(f"    nav exception: {exc.__class__.__name__}: {exc}")
    await page.wait_for_timeout(8_000)


async def run() -> int:
    from playwright.async_api import async_playwright

    _log(f"SGX spike v7 (token forwarding) -- ticker A={TICKER_A}, B={TICKER_B}")

    tokens_a: list[str] = []
    tokens_b: list[str] = []
    current_bucket: list[str] = tokens_a  # switched between primes

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            _log(f"[browser] channel='chrome' failed: {exc}")
            return 1

        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=REAL_CHROME_UA,
        )
        page = await context.new_page()

        async def _on_request(req) -> None:
            if "api.sgx.com" in req.url:
                try:
                    headers = await req.all_headers()
                    token = headers.get("authorizationtoken", "")
                    if token and token.strip():
                        current_bucket.append(token)
                except Exception:
                    pass

        page.on("request", lambda r: asyncio.create_task(_on_request(r)))

        # ---- Ticker A: prime + retry list endpoint ----
        current_bucket = tokens_a
        await _prime_token(page, TICKER_A, tokens_a)
        _log(f"    captured {len(tokens_a)} non-empty token(s) for {TICKER_A}")
        if tokens_a:
            token_a = tokens_a[-1]  # freshest
            _log(f"    token_a[:40] = {token_a[:40]!r}...")
            _log(f"\n[retry] LIST endpoint for {TICKER_A} WITH captured token")
            for sub in ("ANNC17", "ANNC"):
                try:
                    resp = await context.request.get(
                        _list_url(TICKER_A, sub=sub),
                        headers={
                            "Accept": "application/json, text/plain, */*",
                            "Origin": "https://investors.sgx.com",
                            "Referer": "https://investors.sgx.com/",
                            "authorizationtoken": token_a,
                            "content-type": "application/json; charset=UTF-8",
                        },
                    )
                    body = await resp.text()
                    _log(f"    sub={sub}: HTTP {resp.status}, {len(body)} bytes")
                    _log(f"    first 800 chars: {body[:800]!r}")
                    (OUT_DIR / f"list_{TICKER_A}_{sub}.body").write_text(
                        body, encoding="utf-8"
                    )
                except Exception as exc:
                    _log(f"    sub={sub}: exception {exc}")

        # ---- Ticker B: same recipe, tests if token is ticker-bound ----
        current_bucket = tokens_b
        await _prime_token(page, TICKER_B, tokens_b)
        _log(f"    captured {len(tokens_b)} non-empty token(s) for {TICKER_B}")
        if tokens_b:
            token_b = tokens_b[-1]
            _log(f"    token_b[:40] = {token_b[:40]!r}...")

            # Try TICKER_B with TICKER_A's token (does token B work for A's data?)
            if tokens_a:
                _log(f"\n[cross] LIST for {TICKER_B} with {TICKER_A}'s token (ticker-bound check)")
                try:
                    resp = await context.request.get(
                        _list_url(TICKER_B, sub="ANNC17"),
                        headers={
                            "Accept": "application/json",
                            "Origin": "https://investors.sgx.com",
                            "Referer": "https://investors.sgx.com/",
                            "authorizationtoken": tokens_a[-1],
                            "content-type": "application/json; charset=UTF-8",
                        },
                    )
                    body = await resp.text()
                    _log(f"    HTTP {resp.status}, {len(body)} bytes, first 200: {body[:200]!r}")
                except Exception as exc:
                    _log(f"    exception {exc}")

            _log(f"\n[retry] LIST for {TICKER_B} with its own token")
            try:
                resp = await context.request.get(
                    _list_url(TICKER_B, sub="ANNC17"),
                    headers={
                        "Accept": "application/json",
                        "Origin": "https://investors.sgx.com",
                        "Referer": "https://investors.sgx.com/",
                        "authorizationtoken": token_b,
                        "content-type": "application/json; charset=UTF-8",
                    },
                )
                body = await resp.text()
                _log(f"    HTTP {resp.status}, {len(body)} bytes, first 800: {body[:800]!r}")
                (OUT_DIR / f"list_{TICKER_B}_ANNC17.body").write_text(
                    body, encoding="utf-8"
                )
            except Exception as exc:
                _log(f"    exception {exc}")

        await context.close()
        await browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
