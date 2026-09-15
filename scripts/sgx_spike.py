"""
SGX fetch spike v4 — solve the 401 on the announcements list endpoint.

v3 found the endpoint:
  GET https://api.sgx.com/announcements/v1.1/securitycode
      ?value=D05&cat=ANNC&securityCodeParams=securitycode&sub=ANNC17
      &pagestart=0&pagesize=250&periodstart=YYYYMMDD_HHMMSS

but it returned 401 despite firing from a real Chromium context where 20
other api.sgx.com calls returned 200. That means the endpoint requires an
extra ticket the browser sets up later in the flow — a session cookie,
bearer token, or wait-for-auth-init step.

This spike tries five things, in order, and reports which (if any) work:

  A. Capture the exact request headers + cookie jar at the moment of the
     401, so we know what was missing. Diff against the working 200 calls
     (e.g. /companylist) to see the delta.
  B. After the SPA settles, RETRY the same URL from Playwright's own
     request context — same cookie jar, but a fresh request that gives
     us headers control.
  C. Try the URL with a fresh timestamped periodstart (Flutter regenerates
     it every call — a stale one might be rejected).
  D. Wait longer (12s) after nav to let any deferred auth handshake fire.
  E. Try the announcement PDF host links.sgx.com directly with a
     known-good URL structure, to confirm PDFs are downloadable at all.

Prints one clear finding per step to stdout, so the answer is readable
from the Actions log without pulling the artifact.
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

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

ANNOUNCEMENTS_HOST = "https://api.sgx.com"
ANNOUNCEMENTS_PATH = "/announcements/v1.1/securitycode"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _fresh_periodstart() -> str:
    """SGX's periodstart is a compact SGT timestamp — YYYYMMDD_HHMMSS."""
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

    _log(f"SGX spike v4 (solve the 401) — ticker={TICKER}")
    _log(f"Output dir: {OUT_DIR.resolve()}")

    # Capture EVERY api.sgx.com request/response so we can compare working
    # and failing calls after the fact.
    captured: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()

        # Instrument requests + responses; we want the ACTUAL headers the
        # browser sent (including Origin, Referer, cookies) and any
        # response headers that mention auth/cookies.
        async def _on_request(req) -> None:
            if "api.sgx.com" in req.url:
                try:
                    headers = await req.all_headers()
                except Exception:
                    headers = {}
                captured.append({
                    "phase": "request",
                    "method": req.method,
                    "url": req.url,
                    "headers": headers,
                })

        async def _on_response(resp) -> None:
            if "api.sgx.com" in resp.url:
                try:
                    headers = await resp.all_headers()
                except Exception:
                    headers = {}
                captured.append({
                    "phase": "response",
                    "url": resp.url,
                    "status": resp.status,
                    "headers": headers,
                })

        page.on("request", lambda r: asyncio.create_task(_on_request(r)))
        page.on("response", lambda r: asyncio.create_task(_on_response(r)))

        # ------------------------------------------------------------------
        # STEP D (upfront): navigate + longer wait, so any deferred auth
        # handshake fires before we act.
        # ------------------------------------------------------------------
        nav_url = (
            f"https://investors.sgx.com/news/company-announcements"
            f"?securityCode={TICKER}&securityProduct=stocks"
        )
        _log(f"\n[D] nav {nav_url} + 12s settle")
        try:
            await page.goto(nav_url, wait_until="networkidle", timeout=60_000)
        except Exception as exc:
            _log(f"    nav exception: {exc.__class__.__name__}: {exc}")
        await page.wait_for_timeout(12_000)

        # ------------------------------------------------------------------
        # STEP A: dump the cookie jar + captured request headers for the
        # first securitycode call, alongside a working /companylist call
        # for comparison.
        # ------------------------------------------------------------------
        _log("\n[A] cookie + header capture")
        cookies = await context.cookies()
        (OUT_DIR / "cookies.json").write_text(
            json.dumps(cookies, indent=2), encoding="utf-8"
        )
        _log(f"    context has {len(cookies)} cookie(s) across sgx.com hosts")
        for c in cookies[:10]:
            _log(f"      cookie: {c.get('name')} @ {c.get('domain')} (path={c.get('path')})")

        req_401 = next(
            (
                x for x in captured
                if x["phase"] == "request" and "securitycode" in x["url"]
            ),
            None,
        )
        req_200 = next(
            (
                x for x in captured
                if x["phase"] == "request" and "companylist" in x["url"]
            ),
            None,
        )
        if req_401:
            _log("    request headers on the 401 securitycode call:")
            for k, v in sorted((req_401.get("headers") or {}).items()):
                _log(f"      {k}: {v[:120]}")
        else:
            _log("    (no securitycode request captured — Flutter never fired it)")
        if req_200 and req_401:
            missing = set((req_200.get("headers") or {}).keys()) - set(
                (req_401.get("headers") or {}).keys()
            )
            extra = set((req_401.get("headers") or {}).keys()) - set(
                (req_200.get("headers") or {}).keys()
            )
            _log(f"    header keys in the 200 (companylist) not in the 401: {sorted(missing)}")
            _log(f"    header keys in the 401 not in the 200: {sorted(extra)}")

        # ------------------------------------------------------------------
        # STEP B: use Playwright's context.request to hit the same URL. It
        # inherits the browser's cookies AND we can set headers ourselves.
        # ------------------------------------------------------------------
        _log("\n[B] retry via context.request (same cookie jar)")
        b_url = _list_url(TICKER)
        try:
            resp = await context.request.get(
                b_url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Origin": "https://investors.sgx.com",
                    "Referer": "https://investors.sgx.com/",
                    "User-Agent": USER_AGENT,
                },
            )
            body = await resp.text()
            _log(f"    HTTP {resp.status}, body_len={len(body)}")
            _log(f"    first 400 chars: {body[:400]!r}")
            (OUT_DIR / "B_retry.body").write_text(body, encoding="utf-8")
        except Exception as exc:
            _log(f"    exception: {exc.__class__.__name__}: {exc}")

        # ------------------------------------------------------------------
        # STEP C: try a fresh timestamp AND a couple of alternate `sub`
        # codes in case ANNC17 is the specific category and one of the
        # others opens without auth.
        # ------------------------------------------------------------------
        _log("\n[C] try alternate sub= codes (fresh timestamp each)")
        for sub in ("ANNC", "ANNC17", "ANNC12", "ANNC02"):
            c_url = _list_url(TICKER, sub=sub)
            try:
                resp = await context.request.get(
                    c_url,
                    headers={
                        "Accept": "application/json",
                        "Origin": "https://investors.sgx.com",
                        "Referer": "https://investors.sgx.com/",
                        "User-Agent": USER_AGENT,
                    },
                )
                body = await resp.text()
                _log(f"    sub={sub:>7}: HTTP {resp.status}, {len(body)} bytes, "
                     f"first 80: {body[:80]!r}")
                (OUT_DIR / f"C_sub_{sub}.body").write_text(body, encoding="utf-8")
            except Exception as exc:
                _log(f"    sub={sub}: exception {exc}")

        # ------------------------------------------------------------------
        # STEP E: prove PDFs are directly downloadable. We don't yet have a
        # real announcement PDF URL from the API, so this uses the URL
        # pattern from the user's screenshot (links.sgx.com/1.0.0/...).
        # If it 200s and starts with %PDF, PDF downloads are free. If 404,
        # we need an announcement ID from the (still-blocked) list call.
        # ------------------------------------------------------------------
        _log("\n[E] links.sgx.com direct download shape check")
        # No real ID to try — just probe the base + a known announcement ref
        # pattern (SG260806OTHRU39K from your screenshot).
        for path in (
            "/1.0.0/corporate-announcements/",
            "/1.0.0/corporate-announcements/SG260806OTHRU39K/2Q26_performance_summary.pdf",
        ):
            e_url = f"https://links.sgx.com{path}"
            try:
                resp = await context.request.get(
                    e_url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Referer": "https://investors.sgx.com/",
                    },
                )
                body = await resp.body()
                head = body[:8]
                _log(f"    {path}: HTTP {resp.status}, {len(body)} bytes, "
                     f"first 8 bytes: {head!r}")
            except Exception as exc:
                _log(f"    {path}: exception {exc}")

        # Save the full captured trace for post-hoc analysis.
        (OUT_DIR / "trace.json").write_text(
            json.dumps(captured, indent=2, default=str), encoding="utf-8"
        )
        _log(f"\n(saved trace.json: {len(captured)} request/response events)")

        await context.close()
        await browser.close()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
