"""
sgx_pdf.py -- fetch the PDF(s) behind an SGX announcement URL.

Each row from sgx_fetch has a `url` like:
    https://links.sgx.com/1.0.0/corporate-announcements/{id}/{opaque_hash}

The URL either resolves to a single PDF directly (many announcements) or to
an HTML landing page listing multiple attached PDFs (e.g. a bank's
half-year: performance summary + CFO deck + CEO deck + press statement,
per the user's screenshot). We detect which at runtime by inspecting the
response's Content-Type, and in both cases return one or more local PDF
file paths.

Public API
----------
    fetch_announcement_pdfs(
        url: str,
        out_dir: Path,
        max_pdfs: int = 6,
        log=print,
    ) -> List[FetchedPdf]

Each `FetchedPdf` carries the local `path` (bytes-on-disk for the LLM
call), the `source_url` on SGX (so the email can link straight to the
PDF instead of attaching it), and the `name` from the landing page.
Returned in landing-page order. Empty list on any error.

Does NOT need the SGX API's authorizationtoken — links.sgx.com is the
public document host and serves PDFs to plain `requests` calls from any
IP, no browser fingerprint required (verified in the v4 spike). So this
module runs on cloud runners too, unlike sgx_fetch.py.
"""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


class FetchedPdf(NamedTuple):
    """One downloaded PDF plus enough context to link back to it.

    - `path` is the local file, for feeding to Anthropic as a document.
    - `source_url` is the live URL on SGX / links.sgx.com. The email
      builder renders these as clickable "Source PDFs" so users can
      forward or share the originals without us re-hosting them.
    - `name` is the display name from the landing page, used both for
      the local filename and as the link text.
    """
    path: Path
    source_url: str
    name: str

# Chrome-ish UA -- links.sgx.com is loose but a real UA avoids default filters.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/153.0.0.0 Safari/537.36"
)

HTTP_TIMEOUT_SECS = 30
PDF_MAGIC = b"%PDF"

# Filename sanitiser. Windows disallows < > : " / \ | ? * plus control chars.
# We're stricter than that: only keep [A-Za-z0-9._-] and collapse the rest.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(raw: str, fallback: str = "attachment.pdf") -> str:
    name = (raw or "").strip()
    if not name:
        return fallback
    name = _SAFE_NAME_RE.sub("_", name).strip("_")
    if not name:
        return fallback
    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"
    return name[:200]  # cap length


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "application/pdf,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://investors.sgx.com/",
    })
    return s


def _is_pdf_response(resp: requests.Response) -> bool:
    ct = (resp.headers.get("Content-Type") or "").lower()
    if "pdf" in ct:
        return True
    # Fall back to magic-byte sniff: some SGX responses come back with a
    # generic content-type but real PDF bytes.
    return resp.content[:4] == PDF_MAGIC


def _extract_pdf_links(html: str, base_url: str) -> List[tuple[str, str]]:
    """Parse a landing-page HTML for its PDF attachments. Returns a list
    of (absolute_url, display_name) tuples in document order."""
    soup = BeautifulSoup(html, "html.parser")
    out: List[tuple[str, str]] = []
    seen: set = set()
    for a in soup.select("a[href]"):
        href = a.get("href") or ""
        if not href or href.startswith("javascript:") or href.startswith("#"):
            continue
        href_lower = href.lower()
        # Direct .pdf links, OR any link inside an <div class="attachments">
        # style container -- SGX's landing pages sometimes route via .ashx.
        looks_pdf = (
            href_lower.endswith(".pdf")
            or ".pdf?" in href_lower
            or "fileopen" in href_lower  # SGX FileOpen.ashx proxy
        )
        if not looks_pdf:
            continue
        abs_url = urljoin(base_url, href)
        if abs_url in seen:
            continue
        seen.add(abs_url)
        # Prefer the anchor's text, fall back to the URL's last path segment.
        name = (a.get_text(strip=True) or "").strip()
        if not name:
            name = urlparse(abs_url).path.rsplit("/", 1)[-1] or "attachment.pdf"
        out.append((abs_url, name))
    return out


def _download_pdf(
    session: requests.Session,
    url: str,
    out_path: Path,
    log: Callable[[str], None],
) -> Optional[Path]:
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT_SECS, allow_redirects=True)
    except Exception as exc:
        log(f"[sgx-pdf] download failed {url}: {exc.__class__.__name__}: {exc}")
        return None
    if r.status_code != 200:
        log(f"[sgx-pdf] {url} -> HTTP {r.status_code}")
        return None
    if r.content[:4] != PDF_MAGIC:
        log(f"[sgx-pdf] {url} did not return PDF magic bytes "
            f"(got {r.content[:8]!r})")
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(r.content)
    return out_path


def fetch_announcement_pdfs(
    url: str,
    out_dir: Path,
    max_pdfs: int = 6,
    log: Callable[[str], None] = print,
    session: Optional[requests.Session] = None,
) -> List[FetchedPdf]:
    """Resolve `url` to one or more local PDFs plus their source URLs.
    Returns FetchedPdf tuples in landing-page order (or a single-item
    list for direct PDF URLs). Returns [] on any failure -- callers
    should treat that as "PDF not available, fall back to link-only in
    the email"."""
    if not url or not url.startswith("http"):
        log(f"[sgx-pdf] refusing non-http url {url!r}")
        return []
    session = session or _session()

    # First GET the URL. We follow redirects, so a redirect-to-PDF resolves
    # to the PDF response here. If the response body is a PDF, we're done.
    try:
        resp = session.get(url, timeout=HTTP_TIMEOUT_SECS, allow_redirects=True)
    except Exception as exc:
        log(f"[sgx-pdf] initial GET failed {url}: "
            f"{exc.__class__.__name__}: {exc}")
        return []
    if resp.status_code != 200:
        log(f"[sgx-pdf] initial GET -> HTTP {resp.status_code} on {url}")
        return []

    # Direct-PDF case.
    if _is_pdf_response(resp):
        # Filename comes from Content-Disposition if present, else the URL.
        cd = resp.headers.get("Content-Disposition") or ""
        m = re.search(r'filename\*?="?([^";]+)"?', cd, re.IGNORECASE)
        name = _safe_name(
            m.group(1) if m else urlparse(resp.url).path.rsplit("/", 1)[-1],
            fallback=f"{urlparse(url).path.rsplit('/', 1)[-1] or 'attachment'}.pdf",
        )
        out_path = out_dir / name
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(resp.content)
        log(f"[sgx-pdf] direct PDF -> {out_path.name} ({len(resp.content)} bytes)")
        # source_url is the final URL after redirects, so it points at
        # the actual PDF rather than the announcement's opaque landing
        # URL -- that way an email link opens the PDF directly.
        return [FetchedPdf(path=out_path, source_url=resp.url, name=name)]

    # Landing-page case -- parse for PDF links.
    ct = (resp.headers.get("Content-Type") or "").lower()
    if "html" not in ct and "xml" not in ct:
        log(f"[sgx-pdf] unexpected content-type {ct!r} for {url} -- treating "
            f"as failure")
        return []

    try:
        html = resp.text
    except Exception as exc:
        log(f"[sgx-pdf] could not decode HTML: {exc}")
        return []

    pdf_links = _extract_pdf_links(html, base_url=resp.url)
    if not pdf_links:
        log(f"[sgx-pdf] landing page at {url} has no PDF links")
        return []

    log(f"[sgx-pdf] landing page has {len(pdf_links)} PDF link(s); "
        f"downloading up to {max_pdfs}")
    saved: List[FetchedPdf] = []
    for i, (pdf_url, name) in enumerate(pdf_links[:max_pdfs]):
        # Prefix numeric index to preserve landing-page order even if names
        # collide.
        out_path = out_dir / f"{i:02d}_{_safe_name(name)}"
        got = _download_pdf(session, pdf_url, out_path, log)
        if got:
            saved.append(FetchedPdf(path=got, source_url=pdf_url, name=name))
            log(f"[sgx-pdf] saved {got.name}")
    return saved
