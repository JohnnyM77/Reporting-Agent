# playwright_fetch.py
#
# Robust PDF fetcher for ASX "consent gate" pages.
# - Uses page.expect_response() / page.expect_download() so the listener is
#   registered BEFORE the click that triggers the PDF, avoiding the race
#   condition where the response arrives before the listener is attached.
# - Does NOT re-navigate to the original URL after accepting consent (that
#   re-triggers the gate loop).
# - Hard timeouts so Bob doesn't hang forever.

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def _looks_like_gate_html(html: str) -> bool:
    if not html:
        return False
    h = html.lower()
    return ("access to this site" in h and "agree and proceed" in h) or (
        "general conditions" in h and "agree and proceed" in h
    )


def _is_pdf_response(response) -> bool:
    ct = (response.headers.get("content-type") or "").lower()
    return "application/pdf" in ct


async def fetch_pdf_with_playwright(
    url: str,
    out_path: Path,
    *,
    overall_timeout_s: int = 60,
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
                user_agent=_USER_AGENT,
            )
            context.set_default_timeout(nav_timeout_ms)
            page = await context.new_page()

            try:
                # ── Step 1: navigate ──────────────────────────────────────
                resp = await page.goto(
                    url, wait_until="domcontentloaded", timeout=nav_timeout_ms
                )

                # ── Step 2: check if we already have a PDF response ───────
                if resp is not None and _is_pdf_response(resp):
                    try:
                        body = await resp.body()
                        if body[:4] == b"%PDF":
                            out_path.write_bytes(body)
                            return True
                    except Exception:
                        pass

                # ── Step 3: handle consent gate ───────────────────────────
                content = await page.content()
                if _looks_like_gate_html(content):
                    # Locate the agree button before setting up listeners.
                    agree_loc = None
                    for _attempt in (
                        lambda: page.get_by_role("button", name="Agree and proceed"),
                        lambda: page.get_by_role("button", name="Agree"),
                        lambda: page.locator("text=Agree and proceed"),
                        lambda: page.locator("input[value='Agree and proceed']"),
                        lambda: page.locator("input[type=submit]"),
                    ):
                        try:
                            loc = _attempt()
                            if await loc.count() > 0:
                                agree_loc = loc.first
                                break
                        except Exception:
                            pass

                    if agree_loc is not None:
                        # Register response + download listeners BEFORE clicking
                        # so we don't miss the event that fires immediately after.
                        try:
                            async with page.expect_response(
                                _is_pdf_response, timeout=20_000
                            ) as resp_info:
                                await agree_loc.click(timeout=5_000)
                            pdf_resp = await resp_info.value
                            body = await pdf_resp.body()
                            if body[:4] == b"%PDF":
                                out_path.write_bytes(body)
                                return True
                        except Exception:
                            pass

                        # Secondary: wait for a browser download event.
                        if not out_path.exists():
                            try:
                                async with page.expect_download(timeout=20_000) as dl_info:
                                    # Button was already clicked above; a second
                                    # click may or may not be needed depending on
                                    # page state — try it, ignore errors.
                                    try:
                                        await agree_loc.click(timeout=3_000)
                                    except Exception:
                                        pass
                                download = await dl_info.value
                                tmp = await download.path()
                                if tmp:
                                    data = Path(tmp).read_bytes()
                                    if data[:4] == b"%PDF":
                                        out_path.write_bytes(data)
                                        return True
                            except Exception:
                                pass

                # ── Step 4: no gate detected — try a single reload + download ─
                else:
                    try:
                        async with page.expect_download(timeout=15_000) as dl_info:
                            await page.reload(
                                wait_until="domcontentloaded", timeout=nav_timeout_ms
                            )
                        download = await dl_info.value
                        tmp = await download.path()
                        if tmp:
                            data = Path(tmp).read_bytes()
                            if data[:4] == b"%PDF":
                                out_path.write_bytes(data)
                                return True
                    except Exception:
                        pass

                # ── Step 5: iframe fallback ───────────────────────────────
                try:
                    for iframe_el in await page.query_selector_all("iframe"):
                        src = await iframe_el.get_attribute("src") or ""
                        if ".pdf" in src.lower():
                            r2 = await page.goto(
                                src,
                                wait_until="domcontentloaded",
                                timeout=nav_timeout_ms,
                            )
                            if r2 is not None and _is_pdf_response(r2):
                                body = await r2.body()
                                if body[:4] == b"%PDF":
                                    out_path.write_bytes(body)
                                    return True
                except Exception:
                    pass

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
