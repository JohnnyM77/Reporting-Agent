import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

async def fetch_pdf_with_playwright(url: str, output_path: Path) -> bool:
    """
    Opens the ASX announcement link,
    clicks 'Agree and proceed' if present,
    downloads the real PDF.

    Returns True if PDF saved.
    """

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        await page.goto(url, wait_until="networkidle")

        # If ASX gate page appears, click consent
        if await page.locator("text=Agree and proceed").count() > 0:
            await page.click("text=Agree and proceed")
            await page.wait_for_load_state("networkidle")

        # Try to trigger download
        async with page.expect_download() as download_info:
            await page.click("a[href*='displayAnnouncement']")
        download = await download_info.value

        await download.save_as(str(output_path))
        await browser.close()

    return output_path.exists()
