# playwright_fetch.py
#
# Robust PDF fetcher for ASX "consent gate" pages.
# - Uses Playwright Chromium (headless)
# - Clicks "Agree and proceed" if present
# - Downloads the PDF (or captures direct response body if possible)
# - Hard timeouts so Bob doesn't hang forever

import asyncio
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright


ASX_GATE_PHRASES = [
    "Access to this site",
    "Agree and proceed",
    "General Conditions",
]


def _looks_like_gate_html(html: str) -> bool:
    if not html:
        return False
    h = html.lower()
    return ("access to this site" in h and "agree and proceed" in h) or (
        "general conditions" in h and "agree and proceed" in h
    )


async def fetch_pdf_with_playwright(
    url: str,
    out_path: Path,
    *,
    overall_timeout_s: int = 45,
    nav_timeout_ms: int = 25_000,
) -> bool:
    """
    Returns True if a real PDF was downloaded (starts with %PDF).
    Saves to out_path.
    """

    async def _run() -> bool:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                accept_downloads=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
            )
            context.set_default_timeout(nav_timeout_ms)

            page = await context.new_page()

            try:
                # 1) Navigate
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)

                # 2) If we landed on the ASX consent page, click Agree
                #    (Button text varies; we match by visible name)
                content = await page.content()
                if _looks_like_gate_html(content):
                    # Try common selectors
                    # Button text: "Agree and proceed"
                    try:
                        await page.get_by_role("button", name="Agree and proceed").click(timeout=5_000)
                    except Exception:
                        # fallback: find any element containing the phrase
                        try:
                            await page.locator("text=Agree and proceed").first.click(timeout=5_000)
                        except Exception:
                            pass

                    # Give it a moment to redirect
                    await page.wait_for_timeout(1_000)

                # 3) Attempt to download.
                # Some ASX links trigger a direct PDF response, others trigger a download.
                # We listen for downloads and also inspect network response.

                # If it’s already a PDF response, try to read body
                if resp is not None:
                    try:
                        ct = (resp.headers.get("content-type") or "").lower()
                        if "application/pdf" in ct:
                            body = await resp.body()
                            if body[:4] == b"%PDF":
                                out_path.write_bytes(body)
                                await context.close()
                                await browser.close()
                                return True
                    except Exception:
                        pass

                # Otherwise, click/trigger a download by reloading and waiting for download event
                try:
                    async with page.expect_download(timeout=nav_timeout_ms) as dl_info:
                        # reload can trigger the download flow
                        await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                    download = await dl_info.value
                    tmp_file = await download.path()
                    if tmp_file:
                        data = Path(tmp_file).read_bytes()
                        if data[:4] == b"%PDF":
                            out_path.write_bytes(data)
                            await context.close()
                            await browser.close()
                            return True
                except Exception:
                    # No download event occurred
                    pass

                # Final attempt: sometimes PDF is embedded as <iframe src="...pdf...">
                try:
                    frame = page.frame_locator("iframe").first
                    src = await frame.locator("xpath=..").get_attribute("src")
                    if src:
                        # try to navigate to iframe src
                        r2 = await page.goto(src, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                        if r2 is not None:
                            ct = (r2.headers.get("content-type") or "").lower()
                            if "application/pdf" in ct:
                                body = await r2.body()
                                if body[:4] == b"%PDF":
                                    out_path.write_bytes(body)
                                    await context.close()
                                    await browser.close()
                                    return True
                except Exception:
                    pass

                await context.close()
                await browser.close()
                return False

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
        return await asyncio.wait_for(_run(), timeout=overall_timeout_s)
    except asyncio.TimeoutError:
        return False
    except Exception:
        return False
