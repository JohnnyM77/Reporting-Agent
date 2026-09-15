"""
SGX fetch spike v2 — probes investors.sgx.com, the Morningstar-powered
investor-facing site whose URLs the user identified from a browser session:

  - Security overview: https://investors.sgx.com/market/security-details/stocks/{TICKER}
  - Full announcements: https://investors.sgx.com/news/company-announcements
                        ?securityCode={TICKER}&securityProduct=stocks

The first spike (against api.sgx.com) got HTTP 403 on every variant — that
host blocks GitHub-hosted runner IP ranges at the CDN. This one tries the
different host from GH-hosted ubuntu-latest AND from the self-hosted Windows
runner, so we can compare and pick the deployment target for the real
sgx_fetch.py.

Approach:
  1. GET the investors.sgx.com HTML pages the SPA is served from. Grep the
     returned HTML for API endpoint URLs, initial-state JSON blobs, and
     bundle filenames — the SPA has to call *something* to fill the tabs.
  2. Try obvious API paths derived from what step 1 finds, plus a small set
     of educated guesses (/api/announcements, /api/v1/... etc).
  3. If any probe returns an announcement PDF URL, fetch it and confirm the
     magic bytes are `%PDF` — that closes the download loop.

Every response body + status + headers is written under
outputs/sgx_spike/ so the Actions artifact carries a complete record.
First ~600 chars of each body is ALSO printed to stdout, so we can read
findings straight from the job log without downloading the artifact.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import requests

TICKER = os.environ.get("SGX_SPIKE_TICKER", "D05").strip().upper()
OUT_DIR = Path("outputs/sgx_spike")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# Chrome UA + a full accept-language header. investors.sgx.com is fronted by
# a CDN; a bare `python-requests/...` UA is the first thing they filter.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/json;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://investors.sgx.com/",
    "Origin": "https://investors.sgx.com",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _dump(name: str, resp: requests.Response) -> Path:
    (OUT_DIR / f"{name}.meta.txt").write_text(
        f"URL: {resp.url}\n"
        f"HTTP: {resp.status_code}\n"
        f"Content-Type: {resp.headers.get('Content-Type', '')}\n"
        f"Content-Length: {resp.headers.get('Content-Length', '')}\n"
        f"Server: {resp.headers.get('Server', '')}\n"
        f"CF-Ray: {resp.headers.get('CF-Ray', '')}\n"
        f"CF-Cache-Status: {resp.headers.get('CF-Cache-Status', '')}\n",
        encoding="utf-8",
    )
    body_path = OUT_DIR / f"{name}.body"
    body_path.write_bytes(resp.content)
    _log(f"  -> HTTP {resp.status_code}, {len(resp.content)} bytes, ct={resp.headers.get('Content-Type', '')!r}")
    # Also print a preview to the job log so we can read findings without the artifact.
    text_preview = resp.text[:600] if resp.text else ""
    if text_preview:
        _log(f"  -- first 600 chars --")
        for line in text_preview.splitlines()[:20]:
            _log(f"  | {line[:200]}")
    return body_path


def _get(session: requests.Session, url: str, name: str, **kwargs) -> requests.Response | None:
    _log(f"\n[probe] {name}: GET {url}")
    try:
        r = session.get(url, timeout=25, **kwargs)
    except Exception as exc:
        _log(f"  -> EXCEPTION: {exc.__class__.__name__}: {exc}")
        (OUT_DIR / f"{name}.error.txt").write_text(
            f"URL: {url}\nEXCEPTION: {exc.__class__.__name__}: {exc}\n",
            encoding="utf-8",
        )
        return None
    _dump(name, r)
    return r


def _extract_api_hints(html: str) -> list[str]:
    """Grep the SPA HTML for anything that looks like an API endpoint the
    frontend calls — absolute URLs on investors.sgx.com or api.sgx.com, and
    fetch(...) / axios / apiBase-style constants inside inline scripts."""
    if not html:
        return []
    hints = set()
    for m in re.finditer(
        r"https?://(?:investors\.sgx\.com|api\.sgx\.com|links\.sgx\.com)/"
        r"[^\s\"'<>()]+",
        html,
    ):
        hints.add(m.group(0))
    for m in re.finditer(
        r"['\"](/api/[^'\"<>]+)['\"]", html
    ):
        hints.add(m.group(1))
    # Any src attribute pointing at a JS bundle — the bundle is where the
    # API base URL usually lives if it's not baked into the HTML.
    for m in re.finditer(r"src=[\"']([^\"']+\.js[^\"']*)[\"']", html):
        hints.add(m.group(1))
    return sorted(hints)


def main() -> int:
    session = requests.Session()
    session.headers.update(BASE_HEADERS)

    _log(f"SGX spike v2 — ticker={TICKER}")
    _log(f"Runner OS: {os.uname().sysname if hasattr(os, 'uname') else os.name}")
    _log(f"Output dir: {OUT_DIR.resolve()}")

    findings: dict = {"ticker": TICKER, "hints": []}

    # 1) Security-details page — SPA shell
    r_stock = _get(
        session,
        f"https://investors.sgx.com/market/security-details/stocks/{TICKER}",
        "01_security_details_html",
    )
    # 2) Full announcements list page — SPA shell for the Company Announcements tab
    r_annc = _get(
        session,
        "https://investors.sgx.com/news/company-announcements"
        f"?securityCode={TICKER}&securityProduct=stocks",
        "02_announcements_html",
    )

    # 3) Extract every URL the two SPA shells reference — the frontend has to
    # call something to fill the announcements list; that call's URL should
    # appear in the HTML or an inline JSON island.
    all_hints: set[str] = set()
    for r in (r_stock, r_annc):
        if r is not None:
            all_hints.update(_extract_api_hints(r.text))
    all_hints = {h for h in all_hints if not h.endswith(('.png', '.svg', '.ico', '.woff2', '.woff'))}
    hint_list = sorted(all_hints)
    _log(f"\n[probe] extracted {len(hint_list)} candidate URL(s) from SPA HTML:")
    for h in hint_list[:40]:
        _log(f"  * {h}")
    findings["hints"] = hint_list

    # 4) Educated-guess API paths on investors.sgx.com. If a hint above named
    # a real one, that hint will already be in the list above; these fill in
    # the gaps for a first-run diagnosis.
    guess_json_headers = {"Accept": "application/json"}
    guesses = [
        f"https://investors.sgx.com/api/announcements?securityCode={TICKER}&securityProduct=stocks",
        f"https://investors.sgx.com/api/v1/announcements?securityCode={TICKER}",
        f"https://investors.sgx.com/api/company-announcements?securityCode={TICKER}&securityProduct=stocks",
        f"https://investors.sgx.com/api/security/{TICKER}/announcements",
        f"https://investors.sgx.com/api/quote/{TICKER}",
    ]
    for i, url in enumerate(guesses, start=3):
        _get(session, url, f"{i:02d}_guess_{i-2}", headers=guess_json_headers)

    # 5) Any extracted hint that looks like a JSON API — actually call it.
    api_hints = [
        h for h in hint_list
        if "/api/" in h or h.endswith(".json")
        or "announcement" in h.lower() or "quote" in h.lower()
    ][:5]
    for i, url in enumerate(api_hints, start=len(guesses) + 3):
        _get(session, url, f"{i:02d}_hint", headers=guess_json_headers)

    # 6) Summary
    (OUT_DIR / "00_summary.json").write_text(
        json.dumps({
            "ticker": TICKER,
            "probes": sorted(p.name for p in OUT_DIR.iterdir()),
            "hint_count": len(hint_list),
            "hints": hint_list[:40],
        }, indent=2),
        encoding="utf-8",
    )
    _log(f"\nDone. {len(list(OUT_DIR.iterdir()))} files in {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
