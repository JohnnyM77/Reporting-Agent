"""
SGX fetch spike — probes multiple candidate endpoints to see how the Singapore
Exchange serves company announcements today, so `sgx_fetch.py` can be designed
against real responses instead of my memory of them.

Runs from `.github/workflows/sgx_spike.yml` (manual dispatch, ubuntu-latest).

What it tries, in order, capturing raw output to outputs/sgx_spike/ so the
Actions run uploads them as an artifact:

  1. General listing:      GET  api.sgx.com/announcements/v1.1/?...
  2. Alt version:          GET  api.sgx.com/announcements/v1.0/?...
  3. Ticker-filtered list: GET  same + code=<D05> variants (code, stockcode,
                                securities, and via POST /search body)
  4. PDF download:         GET  the titleLink URL of the first announcement
                                found — verifies links.sgx.com is direct-
                                download with no consent gate.

Nothing here is production code. It intentionally uses print + write-to-disk
so a Actions log + one artifact tell us everything we need to design the real
fetch layer.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import requests

TICKER = os.environ.get("SGX_SPIKE_TICKER", "D05").strip().upper()
OUT_DIR = Path("outputs/sgx_spike")
OUT_DIR.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.sgx.com/",
    "Origin": "https://www.sgx.com",
}

PARAMS_FIELDS = (
    "announcementNumber,companyName,headline,securities,titleLink,"
    "submittedDate,isPriceSensitive"
)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _dump(name: str, resp: requests.Response) -> Path:
    """Write status + headers + body to outputs/ so the artifact carries a
    complete record of each probe. Returns the body path so callers can chain
    (e.g. parse JSON out of it)."""
    meta_path = OUT_DIR / f"{name}.meta.txt"
    body_path = OUT_DIR / f"{name}.body"
    meta_path.write_text(
        f"URL: {resp.url}\n"
        f"HTTP: {resp.status_code}\n"
        f"Content-Type: {resp.headers.get('Content-Type', '')}\n"
        f"Content-Length: {resp.headers.get('Content-Length', '')}\n"
        f"Server: {resp.headers.get('Server', '')}\n"
        f"CF-Ray: {resp.headers.get('CF-Ray', '')}\n",
        encoding="utf-8",
    )
    body_path.write_bytes(resp.content)
    _log(f"  -> HTTP {resp.status_code}, {len(resp.content)} bytes -> {body_path.name}")
    return body_path


def _get(session: requests.Session, url: str, name: str) -> requests.Response | None:
    _log(f"[probe] {name}: GET {url}")
    try:
        r = session.get(url, timeout=25)
    except Exception as exc:
        _log(f"  -> EXCEPTION: {exc.__class__.__name__}: {exc}")
        (OUT_DIR / f"{name}.error.txt").write_text(
            f"URL: {url}\nEXCEPTION: {exc.__class__.__name__}: {exc}\n",
            encoding="utf-8",
        )
        return None
    _dump(name, r)
    return r


def _post(
    session: requests.Session, url: str, body: dict, name: str
) -> requests.Response | None:
    _log(f"[probe] {name}: POST {url}  body={body}")
    try:
        r = session.post(url, json=body, timeout=25)
    except Exception as exc:
        _log(f"  -> EXCEPTION: {exc.__class__.__name__}: {exc}")
        (OUT_DIR / f"{name}.error.txt").write_text(
            f"URL: {url}\nBODY: {body}\nEXCEPTION: {exc.__class__.__name__}: {exc}\n",
            encoding="utf-8",
        )
        return None
    _dump(name, r)
    return r


def _try_json(resp: requests.Response | None) -> dict | list | None:
    if resp is None or resp.status_code >= 400:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _first_pdf_url(payload: object) -> str | None:
    """Walk a JSON payload looking for the first titleLink / documentUrl /
    downloadUrl. The exact field name has shifted between SGX API versions,
    so we tolerate several."""
    if isinstance(payload, dict):
        for key in ("data", "items", "results", "value"):
            if key in payload:
                found = _first_pdf_url(payload[key])
                if found:
                    return found
        for key in ("titleLink", "documentUrl", "downloadUrl", "attachmentUrl", "url"):
            v = payload.get(key)
            if isinstance(v, str) and v.startswith("http"):
                return v
    if isinstance(payload, list):
        for item in payload:
            found = _first_pdf_url(item)
            if found:
                return found
    return None


def main() -> int:
    session = requests.Session()
    session.headers.update(BASE_HEADERS)

    _log(f"SGX spike — ticker={TICKER}")
    _log(f"Output dir: {OUT_DIR.resolve()}")
    _log("")

    # 1) General listing, v1.1
    r_v11 = _get(
        session,
        f"https://api.sgx.com/announcements/v1.1/?pagesize=20&pagestart=0&params={PARAMS_FIELDS}",
        "01_list_v11",
    )
    # 2) General listing, v1.0
    r_v10 = _get(
        session,
        f"https://api.sgx.com/announcements/v1.0/?pagesize=20&pagestart=0&params={PARAMS_FIELDS}",
        "02_list_v10",
    )

    # 3) Ticker-filter attempts. SGX has renamed this field between revisions
    # (`code`, `stockcode`, `securitycode`, `securities`). Try each so we
    # learn from one run which is live today.
    for i, key in enumerate(("code", "stockcode", "securitycode", "securities"), start=3):
        _get(
            session,
            f"https://api.sgx.com/announcements/v1.1/?pagesize=20&pagestart=0"
            f"&params={PARAMS_FIELDS}&{key}={TICKER}",
            f"{i:02d}_list_v11_{key}={TICKER}",
        )

    # 4) POST /search variant — the SPA sometimes uses this for filtered views
    _post(
        session,
        "https://api.sgx.com/announcements/v1.1/search/",
        {"code": TICKER, "pagesize": 20, "pagestart": 0},
        "07_search_v11_post",
    )

    # 5) Alternate frontend hosts — cover the case where api.sgx.com is
    # firewalled but the SPA calls something else.
    _get(
        session,
        f"https://links.sgx.com/1.0.0/corporate-announcements?code={TICKER}",
        "08_links_root",
    )

    # 6) Try to fetch the first PDF we can find in whichever list responded.
    # This tells us whether links.sgx.com PDFs are direct-download or gated.
    pdf_url = None
    for payload in filter(None, (_try_json(r_v11), _try_json(r_v10))):
        pdf_url = _first_pdf_url(payload)
        if pdf_url:
            break

    if pdf_url:
        _log(f"[probe] found candidate PDF url: {pdf_url}")
        # Save the URL itself so it survives in the artifact even if the fetch
        # below fails.
        (OUT_DIR / "09_pdf_url.txt").write_text(pdf_url, encoding="utf-8")
        r_pdf = _get(session, pdf_url, "10_pdf_fetch")
        if r_pdf is not None and r_pdf.status_code == 200:
            head = r_pdf.content[:4]
            _log(f"  -> first 4 bytes: {head!r} (expect b'%PDF' if native PDF)")
    else:
        _log("[probe] no PDF url found in any listing response — skipping PDF fetch")
        (OUT_DIR / "09_pdf_url.txt").write_text("(no candidate found)", encoding="utf-8")

    # Summary file — one place to look after the run.
    summary = {
        "ticker": TICKER,
        "probes": sorted(p.name for p in OUT_DIR.iterdir()),
        "pdf_candidate_url": pdf_url,
    }
    (OUT_DIR / "00_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    _log("")
    _log(f"Done. Artifact contents: {sorted(p.name for p in OUT_DIR.iterdir())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
