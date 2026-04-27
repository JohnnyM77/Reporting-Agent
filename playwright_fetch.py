# playwright_fetch.py
#
# Robust PDF fetcher for ASX "consent gate" pages.
# - Intercepts all network responses to capture PDF bytes directly
# - Handles the consent gate by clicking "Agree and proceed" then waiting
#   for the resulting PDF response (server sets cookie → redirect → CDN PDF)
# - Also captures browser download events as a secondary signal
# - Handles popups / new tabs opened by the consent page
# - Hard timeouts so Bob doesn't hang forever

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_WAIT_MS = 500


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

            # Accumulate PDF bytes from any network response and downloads.
            pdf_bytes: list = []
            downloads: list = []

            async def _on_response(response):
                if pdf_bytes:
                    return
                ct = (response.headers.get("content-type") or "").lower()
                rurl = response.url.lower()
                if (
                    "application/pdf" in ct
                    or rurl.endswith(".pdf")
                    or rurl.endswith(".ashx")
                ):
                    try:
                        body = await response.body()
                        if body[:4] == b"%PDF":
                            pdf_bytes.append(body)
                    except Exception:
                        pass

            page = await context.new_page()
            page.on("response", _on_response)
            page.on("download", lambda d: downloads.append(d))

            # Wire up listeners on any popup / new-tab pages too.
            def _wire_page(pg):
                pg.on("response", _on_response)
                pg.on("download", lambda d: downloads.append(d))

            context.on("page", _wire_page)

            async def _save_from_downloads() -> bool:
                for dl in list(downloads):
                    try:
                        tmp = await dl.path()
                        if tmp:
                            data = Path(tmp).read_bytes()
                            if data[:4] == b"%PDF":
                                out_path.write_bytes(data)
                                return True
                    except Exception:
                        pass
                return False

            try:
                # ── Stage 1: navigate and wait for immediate PDF response ──
                await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                await page.wait_for_timeout(_WAIT_MS)

                if pdf_bytes:
                    out_path.write_bytes(pdf_bytes[0])
                    return True
                if await _save_from_downloads():
                    return True

                # ── Stage 2: handle consent gate ──────────────────────────
                content = await page.content()
                if _looks_like_gate_html(content):
                    clicked = False
                    for _attempt in [
                        lambda: page.get_by_role("button", name="Agree and proceed"),
                        lambda: page.get_by_role("button", name="Agree"),
                        lambda: page.locator("text=Agree and proceed"),
                        lambda: page.locator("input[value='Agree and proceed']"),
                        lambda: page.locator("input[type=submit]"),
                    ]:
                        try:
                            loc = _attempt()
                            if await loc.count() > 0:
                                await loc.first.click(timeout=5_000)
                                clicked = True
                                break
                        except Exception:
                            pass

                    if clicked:
                        # Wait up to 6 s for the PDF response / download to arrive.
                        for _ in range(12):
                            await page.wait_for_timeout(_WAIT_MS)
                            if pdf_bytes or downloads:
                                break

                        if pdf_bytes:
                            out_path.write_bytes(pdf_bytes[-1])
                            return True
                        if await _save_from_downloads():
                            return True

                    # Last resort: re-navigate now that the consent cookie
                    # should be set, and wait again.
                    await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
                    for _ in range(8):
                        await page.wait_for_timeout(_WAIT_MS)
                        if pdf_bytes or downloads:
                            break

                    if pdf_bytes:
                        out_path.write_bytes(pdf_bytes[-1])
                        return True
                    if await _save_from_downloads():
                        return True

                # ── Stage 3: iframe fallback ──────────────────────────────
                try:
                    for iframe_el in await page.query_selector_all("iframe"):
                        src = await iframe_el.get_attribute("src") or ""
                        if "pdf" in src.lower() or "announcement" in src.lower():
                            await page.goto(
                                src, wait_until="domcontentloaded", timeout=nav_timeout_ms
                            )
                            for _ in range(6):
                                await page.wait_for_timeout(_WAIT_MS)
                                if pdf_bytes or downloads:
                                    break
                            if pdf_bytes:
                                out_path.write_bytes(pdf_bytes[-1])
                                return True
                            if await _save_from_downloads():
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
