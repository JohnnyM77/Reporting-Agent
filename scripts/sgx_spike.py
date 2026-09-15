"""
SGX fetch spike v5 — real Chrome on the self-hosted Windows runner.

v4 confirmed the 401 blocker: api.sgx.com's announcements list endpoint
rejects our headless-Chromium request. The captured request headers
showed `sec-ch-ua: "HeadlessChrome"` and an empty `authorizationtoken`.
v5 tests the "just anti-headless filtering" hypothesis by:

  1. Running on the self-hosted Windows runner (residential IP, matches
     what a real user session looks like — same box the user browses SGX
     from).
  2. Using Playwright's `channel='chrome'` so it launches the installed
     Google Chrome, not the bundled Chromium. That changes sec-ch-ua from
     '"HeadlessChrome"' to '"Google Chrome"' — the exact difference
     between a working manual session and our failing spike.
  3. Retrying the /announcements/v1.1/securitycode call after the SPA
     settles, from within the browser context so cookies carry.

If v5 gets 200 with real JSON: the block was pure UA fingerprinting and
the SGX build proceeds against api.sgx.com from the Windows runner. If
it still 401s: the token is genuinely computed and we go DevTools cURL
or Option 3 (skip the announcements list entirely, use corp-actions +
financials only).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
from pathlib import Path

TICKER = os.environ.get("SGX_SPIKE_TICKER", "D05").strip().upper()
OUT_DIR = Path("outputs/sgx_spike")
OUT_DIR.mkdir(parents=True, exist_ok=True)

ANNOUNCEMENTS_HOST = "https://api.sgx.com"
ANNOUNCEMENTS_PATH = "/announcements/v1.1/securitycode"

# Chrome 153 UA, WITHOUT "HeadlessChrome". Playwright with channel='chrome'
# in headless mode still stamps HeadlessChrome into the UA; overriding it
# here makes both the UA and the sec-ch-ua header consistent with real
# Chrome. If SGX filters on the UA, this closes the last gap.
REAL_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _fresh_periodstart() -> str:
    sgt_now = dt.datetime.utcnow() + dt.timedelta(hours=8)
    return sgt_now.strftime("%Y%m%d_%H%M%S")


def _list_url(ticker: str, sub: str = "ANNC17") -> str:
    return (
        f"{ANNOUNCEMENTS_HOST}{ANNOUNCEMENTS_PATH}"
        f"?value={ticker}&cat=ANNC&securityCodeParams=securitycode"
        f"&sub={sub}&pagestart=0&pagesize=250&periodstart={_fresh_periodstart()}"
    )


async def run() -> int:
    from playwright.async_api import async_playwright

    _log(f"SGX spike v5 (real Chrome) — ticker={TICKER}")
    _log(f"Platform: {sys.platform}")
    _log(f"Output dir: {OUT_DIR.resolve()}")

    captured_401_headers: dict = {}

    async with async_playwright() as p:
        # channel='chrome' launches the installed Google Chrome, not the
        # bundled Chromium — this is what fixes the sec-ch-ua fingerprint.
        try:
            browser = await p.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            _log("[browser] launched channel='chrome' (installed Google Chrome)")
        except Exception as exc:
            _log(f"[browser] channel='chrome' failed ({exc}); falling back to bundled Chromium")
            browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=REAL_CHROME_UA,
        )
        page = await context.new_page()

        async def _on_request(req) -> None:
            if "api.sgx.com" in req.url and "securitycode" in req.url:
                try:
                    captured_401_headers.clear()
                    captured_401_headers.update(await req.all_headers())
                    captured_401_headers["_url"] = req.url
                except Exception:
                    pass

        page.on("request", lambda r: asyncio.create_task(_on_request(r)))

        # Nav + settle
        nav_url = (
            f"https://investors.sgx.com/news/company-announcements"
            f"?securityCode={TICKER}&securityProduct=stocks"
        )
        _log(f"\n[nav] {nav_url}")
        try:
            await page.goto(nav_url, wait_until="networkidle", timeout=60_000)
        except Exception as exc:
            _log(f"    nav exception: {exc.__class__.__name__}: {exc}")
        await page.wait_for_timeout(8_000)

        # Report captured headers — this is where sec-ch-ua tells us if
        # real Chrome vs HeadlessChrome fingerprinting was the block.
        _log(f"\n[headers] on the securitycode request Flutter fired:")
        if captured_401_headers:
            for k, v in sorted(captured_401_headers.items()):
                _log(f"    {k}: {v[:160]}")
        else:
            _log("    (no securitycode request captured — Flutter never fired it)")

        # Retry from context.request -- carries the same cookies + real Chrome UA.
        _log(f"\n[retry] context.request -> {ANNOUNCEMENTS_PATH}")
        for sub in ("ANNC17", "ANNC"):
            try:
                resp = await context.request.get(
                    _list_url(TICKER, sub=sub),
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Origin": "https://investors.sgx.com",
                        "Referer": "https://investors.sgx.com/",
                    },
                )
                body = await resp.text()
                _log(f"    sub={sub}: HTTP {resp.status}, {len(body)} bytes")
                _log(f"    first 500 chars: {body[:500]!r}")
                (OUT_DIR / f"retry_{sub}.body").write_text(body, encoding="utf-8")
            except Exception as exc:
                _log(f"    sub={sub}: exception {exc}")

        # Cookie dump
        cookies = await context.cookies()
        _log(f"\n[cookies] {len(cookies)} cookies on sgx.com hosts:")
        for c in cookies:
            _log(f"    {c.get('name')} @ {c.get('domain')}")

        (OUT_DIR / "captured_headers.json").write_text(
            json.dumps(captured_401_headers, indent=2), encoding="utf-8"
        )
        (OUT_DIR / "cookies.json").write_text(
            json.dumps(cookies, indent=2), encoding="utf-8"
        )

        await context.close()
        await browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
