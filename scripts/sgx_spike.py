"""
SGX fetch spike v3 — Playwright + network interception.

The v2 spike proved investors.sgx.com is reachable from ubuntu-latest but
the page is a Flutter Web app: every URL returns the same 17KB shell, the
real API calls happen inside a compiled Dart→JS bundle at runtime, and
there is nothing in the HTML to reverse-engineer.

This v3 launches headless Chromium against the DBS announcements page,
lets Flutter run, and records every network request the app fires — URLs,
methods, status codes, response bodies. One run tells us the real endpoint
sgx_fetch.py needs to talk to and what its response shape looks like.

Outputs land in outputs/sgx_spike/:
  - 00_summary.json      — every request/response pair, sortable
  - request_XXX.body     — the response body for the first N interesting
                           XHR/fetch responses (JSON, so we can eyeball them)

Also prints a compact table to stdout so findings are readable from the
Actions log without pulling the artifact zip.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

TICKER = os.environ.get("SGX_SPIKE_TICKER", "D05").strip().upper()
OUT_DIR = Path("outputs/sgx_spike")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# The two pages we care about, from the URLs the user identified.
PAGES = [
    ("security_details", f"https://investors.sgx.com/market/security-details/stocks/{TICKER}"),
    ("company_announcements",
     f"https://investors.sgx.com/news/company-announcements"
     f"?securityCode={TICKER}&securityProduct=stocks"),
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# We tag responses that look like real data (JSON, non-trivial size, not
# a Flutter asset) so the summary table prints them at the top.
INTERESTING_CT_PREFIXES = ("application/json", "text/json")


def _log(msg: str) -> None:
    print(msg, flush=True)


async def _capture(page_name: str, url: str, page) -> list[dict]:
    """Attach request/response listeners to `page`, navigate to `url`, wait
    long enough for the Flutter app to fetch data, return the collected
    request records."""
    records: list[dict] = []

    async def _on_response(resp) -> None:
        req = resp.request
        try:
            ct = resp.headers.get("content-type", "")
        except Exception:
            ct = ""
        record = {
            "page": page_name,
            "method": req.method,
            "url": resp.url,
            "status": resp.status,
            "resource_type": req.resource_type,
            "content_type": ct,
        }
        # Only pull the body for things that look like real API responses,
        # not images/fonts/CSS/JS bundles.
        if req.resource_type in ("xhr", "fetch") and resp.status < 400:
            try:
                body = await resp.body()
                record["body_len"] = len(body)
                record["_body_bytes"] = body
            except Exception as exc:
                record["body_error"] = str(exc)
        records.append(record)

    page.on("response", lambda r: asyncio.create_task(_on_response(r)))

    _log(f"\n[nav] {page_name}: {url}")
    try:
        await page.goto(url, wait_until="networkidle", timeout=60_000)
    except Exception as exc:
        _log(f"  -> nav exception: {exc.__class__.__name__}: {exc}")
    # Flutter can fire XHRs after networkidle briefly settles — give it
    # a small tail window so we catch late-firing data calls too.
    await page.wait_for_timeout(4_000)
    return records


async def run() -> int:
    from playwright.async_api import async_playwright

    _log(f"SGX spike v3 (Playwright) — ticker={TICKER}")
    _log(f"Output dir: {OUT_DIR.resolve()}")

    all_records: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()

        for page_name, url in PAGES:
            recs = await _capture(page_name, url, page)
            all_records.extend(recs)

        await context.close()
        await browser.close()

    # Sort: interesting XHR/fetch JSON responses first, everything else after.
    def _interest_score(r: dict) -> tuple:
        interesting_ct = any(r["content_type"].startswith(p) for p in INTERESTING_CT_PREFIXES)
        is_data = r["resource_type"] in ("xhr", "fetch")
        return (
            0 if (interesting_ct and is_data) else (1 if is_data else 2),
            r["url"],
        )
    all_records.sort(key=_interest_score)

    # Save bodies for interesting responses; also collect a small table.
    saved = 0
    table_rows: list[str] = []
    for i, r in enumerate(all_records):
        row = (
            f"{r['status']:>3}  "
            f"{r['resource_type']:<8}  "
            f"{r.get('body_len', '-'):>7}  "
            f"{r['content_type'][:40]:<40}  "
            f"{r['method']} {r['url']}"
        )
        table_rows.append(row)
        body_bytes = r.pop("_body_bytes", None)
        if body_bytes is not None and saved < 20:
            (OUT_DIR / f"request_{i:03d}.body").write_bytes(body_bytes)
            saved += 1

    _log(f"\n--- captured {len(all_records)} responses across {len(PAGES)} page nav(s) ---")
    _log("status  type      body-B  content-type                              request")
    for row in table_rows[:60]:
        _log(row)
    if len(table_rows) > 60:
        _log(f"... and {len(table_rows) - 60} more (see 00_summary.json)")

    (OUT_DIR / "00_summary.json").write_text(
        json.dumps(all_records, indent=2), encoding="utf-8"
    )
    _log(f"\nSaved {saved} response bodies + 00_summary.json to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
