"""
agents/sws_drip/sws_downloader.py
───────────────────────────────────
Playwright automation: log into SWS (via storage_state), search for ticker,
click three-dots menu, download CSV.
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from typing import Union


class SWSDownloadError(Exception):
    """Raised when a download step fails. Message includes the failing step name."""


async def _random_delay(min_s: float = 3.0, max_s: float = 8.0) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _save_debug(page, debug_dir: Path, ticker: str, step: str) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(debug_dir / f"fail_{ticker}_{step}.png"), full_page=True)
    except Exception:
        pass
    try:
        html = await page.content()
        (debug_dir / f"fail_{ticker}_{step}.html").write_text(html, encoding="utf-8")
    except Exception:
        pass


async def _try_click(page, selectors: list[str], step: str, timeout: int = 8000):
    """Try each selector in order. Returns the locator that succeeded, or raises."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=timeout)
            await loc.click()
            return loc
        except Exception:
            continue
    raise SWSDownloadError(f"No selector matched at step '{step}': {selectors}")


async def download_csv(
    ticker: str,
    exchange: str,
    *,
    storage_state: Union[str, dict],
    output_dir: Path,
    headless: bool = True,
    debug_dir: Path,
) -> Path:
    """
    Download one SWS CSV for the given ticker.

    Returns the path to the saved CSV file.
    Raises SWSDownloadError on any step failure (screenshot + HTML saved to debug_dir).
    """
    from playwright.async_api import async_playwright

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{exchange}_{ticker}.csv"
    search_query = f"{exchange}:{ticker}"

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            slow_mo=0 if headless else 500,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        ctx_kwargs: dict = {
            "viewport": {"width": 1440, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "accept_downloads": True,
        }
        if isinstance(storage_state, dict):
            ctx_kwargs["storage_state"] = storage_state
        elif isinstance(storage_state, str) and storage_state.strip().startswith("{"):
            ctx_kwargs["storage_state"] = json.loads(storage_state)
        elif isinstance(storage_state, str):
            ctx_kwargs["storage_state"] = storage_state  # file path

        context = await browser.new_context(**ctx_kwargs)
        page = await context.new_page()

        step = "navigate"
        try:
            # 1. Navigate to dashboard (has search box; homepage may be marketing page)
            await page.goto("https://simplywall.st/dashboard", wait_until="domcontentloaded", timeout=30000)
            await _random_delay(2, 4)

            # 1a. Session check — if redirected to login/home, session has expired
            current_url = page.url
            if any(p in current_url for p in ["/login", "/register", "simplywall.st/?", "simplywall.st/#"]):
                await _save_debug(page, debug_dir, ticker, "session_expired")
                raise SWSDownloadError(
                    "SWS session has expired — re-run --setup locally and update SWS_STORAGE_STATE secret"
                )

            # 2. Find and click search box
            step = "search_box"

            # SWS uses a search icon that reveals an input when clicked.
            # Try visible inputs first; if none, click the search icon trigger.
            search_input_selectors = [
                'input[placeholder*="Search" i]',
                'input[type="search"]',
                'input[name*="search" i]',
                '[role="combobox"]',
                '[data-testid*="search" i] input',
            ]
            search_trigger_selectors = [
                'button[aria-label*="Search" i]',
                'button[aria-label*="search" i]',
                '[data-testid*="search" i]',
                'header button:has(svg)',
                'nav button:has(svg)',
                '[class*="Search" i] button',
                '[class*="search" i] button',
                'button:has(svg[class*="search" i])',
                'a[href*="search"]',
            ]

            search_box = None

            # First pass: input already visible
            for sel in search_input_selectors:
                try:
                    loc = page.locator(sel).first
                    await loc.wait_for(state="visible", timeout=3000)
                    search_box = loc
                    break
                except Exception:
                    continue

            # Second pass: click the icon trigger, then wait for input
            if search_box is None:
                for sel in search_trigger_selectors:
                    try:
                        trigger = page.locator(sel).first
                        await trigger.wait_for(state="visible", timeout=3000)
                        await trigger.click()
                        await _random_delay(0.5, 1.0)
                        # Now look for the revealed input
                        for inp_sel in search_input_selectors:
                            try:
                                loc = page.locator(inp_sel).first
                                await loc.wait_for(state="visible", timeout=3000)
                                search_box = loc
                                break
                            except Exception:
                                continue
                        if search_box is not None:
                            break
                    except Exception:
                        continue

            # Third pass: try stocks/search URL fallback
            if search_box is None:
                await page.goto(
                    f"https://simplywall.st/stocks/search?q=ASX%3A{ticker}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                await _random_delay(2, 4)
                for sel in search_input_selectors + search_trigger_selectors:
                    try:
                        loc = page.locator(sel).first
                        await loc.wait_for(state="visible", timeout=4000)
                        search_box = loc
                        break
                    except Exception:
                        continue

            if search_box is None:
                await _save_debug(page, debug_dir, ticker, step)
                raise SWSDownloadError(f"Could not find search box at step '{step}'")

            await search_box.click()
            await _random_delay(0.5, 1.5)
            await search_box.type(search_query, delay=80)
            await _random_delay(1.5, 3.0)

            # 3. Click first matching search result
            step = "search_result"
            result_selectors = [
                f'[href*="asx-{ticker.lower()}"]',
                f'a:has-text("{ticker}")',
                f'[data-testid*="search-result"]:has-text("{ticker}")',
                f'li:has-text("ASX:{ticker}")',
                f'li:has-text("{ticker}")',
            ]
            clicked_result = False
            for sel in result_selectors:
                try:
                    loc = page.locator(sel).first
                    await loc.wait_for(state="visible", timeout=6000)
                    await loc.click()
                    clicked_result = True
                    break
                except Exception:
                    continue
            if not clicked_result:
                await _save_debug(page, debug_dir, ticker, step)
                raise SWSDownloadError(
                    f"Could not click search result for {search_query} at step '{step}'"
                )

            # 4. Wait for company page to load
            step = "page_load"
            try:
                await page.wait_for_url(
                    f"**/asx-{ticker.lower()}/**",
                    timeout=20000,
                    wait_until="domcontentloaded",
                )
            except Exception:
                # URL might not match exactly — wait for network idle instead
                try:
                    await page.wait_for_load_state("networkidle", timeout=20000)
                except Exception:
                    pass
            await _random_delay(2.0, 4.0)

            # 5. Click the three-dots (⋯) menu button
            step = "three_dots"
            dots_selectors = [
                'button[aria-label*="more" i]',
                'button[aria-label*="options" i]',
                'button:has-text("...")',
                'button:has-text("⋯")',
                'button:has-text("•••")',
                '[aria-haspopup="menu"]:near(:text("Watch"))',
                '[data-testid*="more" i]',
                '[data-testid*="menu" i]',
                'button[aria-haspopup="true"]',
            ]
            clicked_dots = False
            for sel in dots_selectors:
                try:
                    loc = page.locator(sel).first
                    await loc.wait_for(state="visible", timeout=5000)
                    await loc.click()
                    clicked_dots = True
                    break
                except Exception:
                    continue
            if not clicked_dots:
                await _save_debug(page, debug_dir, ticker, step)
                raise SWSDownloadError(
                    f"Could not click three-dots menu at step '{step}'"
                )
            await _random_delay(0.8, 2.0)

            # 6. Click "Download to CSV"
            step = "download_csv_click"
            csv_selectors = [
                'text="Download to CSV"',
                'text=/Download.*CSV/i',
                '[role="menuitem"]:has-text("Download")',
                'a:has-text("Download")',
            ]
            async with page.expect_download(timeout=30000) as download_info:
                clicked_download = False
                for sel in csv_selectors:
                    try:
                        loc = page.locator(sel).first
                        await loc.wait_for(state="visible", timeout=5000)
                        await loc.click()
                        clicked_download = True
                        break
                    except Exception:
                        continue
                if not clicked_download:
                    await _save_debug(page, debug_dir, ticker, step)
                    raise SWSDownloadError(
                        f"Could not click 'Download to CSV' at step '{step}'"
                    )

            download = await download_info.value
            await download.save_as(str(output_path))

        except SWSDownloadError:
            raise
        except Exception as e:
            await _save_debug(page, debug_dir, ticker, step)
            raise SWSDownloadError(f"Unexpected error at step '{step}': {e}") from e
        finally:
            await context.close()
            await browser.close()

    print(f"[sws_downloader] Downloaded {ticker} → {output_path}", flush=True)
    return output_path


async def setup_session(output_path: Path) -> None:
    """
    One-time manual login flow. Opens a headed browser, navigates to SWS login,
    waits for the user to log in, then saves the browser context to output_path.
    """
    from playwright.async_api import async_playwright

    print(f"[sws_drip] Opening browser for manual login...")
    print(f"[sws_drip] Log into Simply Wall St, then close the browser window.")
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
            # User may have navigated elsewhere after login
            pass

        output_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(output_path))
        print(f"[sws_drip] Session saved to {output_path}")
        await browser.close()
