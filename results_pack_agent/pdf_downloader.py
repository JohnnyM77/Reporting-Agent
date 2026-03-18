# results_pack_agent/pdf_downloader.py
# Download PDFs for a ResultPack.
# Tries requests first; falls back to Playwright for consent-gated PDFs.

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

import requests

from .config import MAX_PDF_BYTES, PDF_DOWNLOAD_TIMEOUT_SECS
from .models import Announcement, ResultPack
from .utils import log, safe_filename

# Optional Playwright helper (same module used by agent.py / Bob).
# Imported lazily to avoid a hard dependency in test environments.
try:
    from playwright_fetch import fetch_pdf_with_playwright as _fetch_pdf_with_playwright  # type: ignore[import]
except ImportError:
    _fetch_pdf_with_playwright = None  # type: ignore[assignment]


# ── Single PDF download ────────────────────────────────────────────────────────

def _resolve_pdf_url(ann: Announcement, session: requests.Session) -> Optional[str]:
    """Try to resolve the direct PDF URL for an announcement.

    ASX announcement pages sometimes serve the PDF directly at the
    ``displayAnnouncement.do`` URL; otherwise we scrape the page for a link
    ending in ``.pdf``.
    """
    # If already a direct PDF URL, return as-is
    if ann.pdf_url:
        return ann.pdf_url

    url = ann.url
    if "displayAnnouncement.do" in url:
        return url

    # Try to find a .pdf link on the page
    try:
        from bs4 import BeautifulSoup
        r = session.get(url, timeout=PDF_DOWNLOAD_TIMEOUT_SECS)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a_tag in soup.find_all("a", href=True):
            href = str(a_tag["href"])
            if href.lower().endswith(".pdf"):
                if href.startswith("/"):
                    href = "https://www.asx.com.au" + href
                return href
    except Exception:
        pass

    return None


def _download_via_requests(url: str, session: requests.Session) -> Optional[bytes]:
    """Download a PDF via requests and return raw bytes, or None on failure."""
    try:
        r = session.get(url, timeout=PDF_DOWNLOAD_TIMEOUT_SECS, allow_redirects=True)
        r.raise_for_status()
        if not r.content[:4] == b"%PDF":
            return None
        if len(r.content) > MAX_PDF_BYTES:
            log(f"[pdf_downloader] PDF too large ({len(r.content) // 1024} KB), skipping: {url[:80]}")
            return None
        return r.content
    except Exception as exc:
        log(f"[pdf_downloader] requests download failed for {url[:80]}: {exc}")
        return None


def _download_via_playwright(url: str) -> Optional[bytes]:
    """Download a PDF via Playwright for consent-gated ASX pages."""
    if _fetch_pdf_with_playwright is None:
        log("[pdf_downloader] playwright_fetch module not available — skipping Playwright fallback.")
        return None
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        success = asyncio.run(_fetch_pdf_with_playwright(url, tmp_path))
        if success and tmp_path.exists():
            data = tmp_path.read_bytes()
            tmp_path.unlink(missing_ok=True)
            if data[:4] == b"%PDF":
                return data
        return None
    except Exception as exc:
        log(f"[pdf_downloader] Playwright download failed for {url[:80]}: {exc}")
        return None


def download_pdf(
    ann: Announcement,
    session: requests.Session,
    use_playwright_fallback: bool = True,
) -> Optional[bytes]:
    """Download the PDF for *ann* and return raw bytes.

    Tries requests first, then Playwright if enabled and requests fails.
    Returns None if the PDF cannot be obtained.
    """
    pdf_url = _resolve_pdf_url(ann, session)
    if not pdf_url:
        log(f"[pdf_downloader] No PDF URL found for: {ann.title[:60]}")
        return None

    ann.pdf_url = pdf_url  # store resolved URL on the announcement

    # 1. requests (fast path)
    data = _download_via_requests(pdf_url, session)
    if data:
        return data

    # 2. Playwright fallback (consent-gated pages)
    if use_playwright_fallback:
        log(f"[pdf_downloader] Falling back to Playwright for: {ann.title[:60]}")
        data = _download_via_playwright(pdf_url)
        if data:
            return data

    log(f"[pdf_downloader] Could not download PDF for: {ann.title[:60]}")
    return None


# ── Pack-level download ────────────────────────────────────────────────────────

def download_pack_pdfs(
    pack: ResultPack,
    output_folder: Path,
    session: Optional[requests.Session] = None,
    use_playwright_fallback: bool = True,
    dry_run: bool = False,
) -> int:
    """Download all PDFs for *pack* and save them to *output_folder*.

    Each ``Announcement`` in the pack has its ``pdf_bytes`` and ``pdf_path``
    fields populated after a successful download.

    Returns the number of PDFs successfully downloaded.
    """
    from .utils import http_session as _http_session

    s = session or _http_session()
    output_folder.mkdir(parents=True, exist_ok=True)
    downloaded = 0

    for ann in pack.announcements:
        local_path = output_folder / f"{safe_filename(ann.title[:80])}.pdf"

        if dry_run:
            log(f"[pdf_downloader] [DRY-RUN] Would download: {ann.title[:60]}")
            continue

        log(f"[pdf_downloader] Downloading: {ann.title[:60]}")
        data = download_pdf(ann, s, use_playwright_fallback=use_playwright_fallback)

        if data:
            ann.pdf_bytes = data
            ann.pdf_path = str(local_path)
            local_path.write_bytes(data)
            downloaded += 1
            log(f"[pdf_downloader] Saved {len(data) // 1024} KB → {local_path.name}")
        else:
            log(f"[pdf_downloader] Skipped (no PDF): {ann.title[:60]}")

    log(f"[pdf_downloader] Downloaded {downloaded}/{len(pack.announcements)} PDFs.")
    return downloaded


# ── Metadata ───────────────────────────────────────────────────────────────────

def save_pack_metadata(pack: ResultPack, output_folder: Path) -> Path:
    """Write a JSON metadata file describing the pack to *output_folder*."""
    import datetime as dt

    metadata = {
        "ticker": pack.ticker,
        "company_name": pack.company_name,
        "result_date": pack.result_date,
        "result_type": pack.result_type,
        "saved_at": dt.datetime.utcnow().isoformat() + "Z",
        "documents": [
            {
                "title": a.title,
                "date": a.date,
                "url": a.url,
                "pdf_url": a.pdf_url,
                "pdf_saved": a.pdf_path is not None,
                "pdf_size_kb": len(a.pdf_bytes) // 1024 if a.pdf_bytes else None,
            }
            for a in pack.announcements
        ],
    }

    meta_file = output_folder / f"{pack.file_prefix}-Pack-Metadata.json"
    meta_file.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log(f"[pdf_downloader] Metadata saved → {meta_file.name}")
    return meta_file
