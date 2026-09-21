"""
us_docs.py -- download EDGAR filings and render them to PDF for the LLM.

The SGX equivalent (sgx_pdf.py) had it easy: SGX serves the actual
filing PDFs at links.sgx.com, so a plain `requests.get` gives us a PDF
ready to hand Claude. EDGAR does not: every filing on sec.gov is HTML
(the primary document) with a scatter of accompanying exhibits, some
of which are HTML, some plain text, and a few genuine PDFs (usually
investor decks attached to an 8-K).

Since shared/pdf_llm.py's native-attach path expects PDF bytes, we
render every HTML doc to a PDF with Playwright + Chromium in headless
mode. A PDF that is already a PDF is passed through as-is.

Public API
----------
    fetch_filing_documents(
        selected: List[Dict],   # rows from us_fetch, one filing per dict
        out_dir: Path,
        max_pdfs: int = 6,
        log=print,
    ) -> List[FetchedPdf]

`FetchedPdf` mirrors sgx_pdf.FetchedPdf exactly (path, source_url, name)
so downstream code in us_agent can reuse the same shape sgx_agent uses.

Runs on ubuntu-latest -- SEC does not fingerprint TLS or gate on real
Chrome, so bundled Chromium works fine. UA still has to be set (SEC
requires it) but it can be a generic Chrome UA plus the SEC contact.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional
from urllib.parse import urlparse

import requests


# Import FetchedPdf shape from sgx_pdf so downstream helpers work on
# either exchange interchangeably. Also copy the safe-name helper.
from sgx_pdf import FetchedPdf, _safe_name  # noqa: F401  # re-exported for callers


# SEC UA -- same one us_fetch uses. Keep them consistent so a policy
# review only has one place to update.
from us_fetch import _sec_ua, RATE_LIMIT_SLEEP_SECS  # noqa: E402


HTTP_TIMEOUT_SECS = 30
PDF_MAGIC = b"%PDF"


def _sec_session() -> requests.Session:
    """A `requests` session pre-configured for sec.gov's fair-use rules.
    SEC does not require a browser UA, just a non-empty identifying one."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": _sec_ua(),
        "Accept": "application/pdf, text/html, */*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    return s


def _throttle() -> None:
    """Same sleep cadence us_fetch uses -- combined over a run we still
    stay well under the ~10 req/sec fair-use ceiling."""
    time.sleep(RATE_LIMIT_SLEEP_SECS)


def _download_bytes(
    session: requests.Session,
    url: str,
    log: Callable[[str], None],
) -> Optional[bytes]:
    """GET `url` and return the raw bytes, or None on any failure."""
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT_SECS, allow_redirects=True)
        _throttle()
    except Exception as exc:
        log(f"[us-docs] download failed {url}: "
            f"{exc.__class__.__name__}: {exc}")
        return None
    if r.status_code != 200:
        log(f"[us-docs] {url} -> HTTP {r.status_code}")
        return None
    return r.content


def _render_html_bytes_to_pdf(
    html_bytes: bytes,
    source_url: str,
    out_path: Path,
    log: Callable[[str], None],
) -> Optional[Path]:
    """Render one HTML filing to a PDF at `out_path`. Uses Playwright's
    bundled Chromium (not installed Chrome), so ubuntu-latest works
    without a `--channel chrome` requirement.

    We hand Chromium the HTML as an in-memory data URL rather than a
    fresh navigation to sec.gov, so:
      1. We don't hit SEC twice for the same file, and
      2. `page.pdf()` uses the exact bytes we already validated,
         instead of re-fetching over network that might flake."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        log(f"[us-docs] playwright unavailable: {exc}")
        return None
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Try to decode as utf-8; fall back to latin-1 if the filing
        # ships in some legacy encoding (some old 10-Ks are Windows-1252).
        try:
            html_text = html_bytes.decode("utf-8")
        except UnicodeDecodeError:
            html_text = html_bytes.decode("latin-1", errors="replace")

        # Guard against Chromium waiting on cross-origin resources: some
        # older EDGAR HTML has absolute image refs that 404. We use
        # `wait_until='load'` and let missing sub-resources drop.
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=_sec_ua())
                page = context.new_page()
                page.set_content(html_text, wait_until="load", timeout=60_000)
                page.pdf(
                    path=str(out_path),
                    format="Letter",
                    print_background=True,
                    margin={"top": "14mm", "right": "14mm",
                            "bottom": "18mm", "left": "14mm"},
                )
            finally:
                browser.close()
        log(f"[us-docs] rendered {out_path.name} "
            f"({out_path.stat().st_size}B) <- {source_url}")
        return out_path
    except Exception as exc:
        log(f"[us-docs] render failed for {source_url}: "
            f"{exc.__class__.__name__}: {exc}")
        return None


def _document_display_name(row: dict, doc_url: str) -> str:
    """A short filename for the downloaded document. Prefer the SEC
    primaryDocDescription (e.g. "10-K", "EX-99.1"), falling back to the
    URL basename."""
    desc = (row.get("primary_doc_description") or "").strip()
    if desc:
        return _safe_name(desc)
    basename = urlparse(doc_url).path.rsplit("/", 1)[-1]
    return _safe_name(basename or "filing.pdf")


def _fetch_filing_index_docs(
    session: requests.Session,
    filing_dir_url: str,
    log: Callable[[str], None],
) -> List[str]:
    """Return the list of HTML/PDF documents in an accession's directory.

    EDGAR ships an `index.json` next to every accession's documents so we
    can enumerate exhibits without scraping HTML. Falls back to just the
    primary document if index.json is unreachable."""
    if not filing_dir_url.endswith("/"):
        filing_dir_url += "/"
    index_url = filing_dir_url + "index.json"
    try:
        r = session.get(index_url, timeout=HTTP_TIMEOUT_SECS)
        _throttle()
    except Exception as exc:
        log(f"[us-docs] index.json GET failed {index_url}: {exc}")
        return []
    if r.status_code != 200:
        log(f"[us-docs] index.json HTTP {r.status_code} for {filing_dir_url}")
        return []
    try:
        idx = r.json()
    except Exception as exc:
        log(f"[us-docs] index.json parse failed: {exc}")
        return []
    directory = (idx.get("directory") or {}).get("item") or []
    names = []
    for entry in directory:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        low = name.lower()
        if low.endswith(".htm") or low.endswith(".html") or low.endswith(".pdf"):
            names.append(name)
    return names


# --- doc-content filter ----------------------------------------------------
# Files EDGAR ships that carry ~no financial analysis value and would
# otherwise eat native-PDF slots. The RMD FY26 run hit this: the 10-K's
# own primary body (215p, forced to text fallback) + index headers +
# index + CEO cert + CFO cert + SOX cert filled all 6 slots, so the
# paired 8-K's earnings press release (ex-99.1, where the non-GAAP
# framing lives) never got fetched. Skipping the boilerplate here frees
# those slots for real content on the paired 8-K and any substantive
# exhibits.
#
# Patterns are lowercased-substring matches against the filename.

# Sarbanes-Oxley cert exhibits: two-page boilerplate signed by CEO/CFO,
# zero financial data. Names typically ex-31.1/ex-31.2 (302 certs) and
# ex-32.1/ex-32.2 (906 certs); some filers prefix as ex31/ex32.
_CERT_EXHIBIT_PATTERNS = (
    "ex-31.", "ex-32.", "ex31", "ex32",
    "ex-31_", "ex-32_",
    "ceocertificat", "cfocertificat", "ceoandcfocertificat",
    "certificationq",
)

# EDGAR-generated wrapper pages: filing header + directory listing.
# The primary_document already gives us the substantive filing; these
# are metadata about the submission itself.
_INDEX_WRAPPER_PATTERNS = (
    "-index.htm", "-index.html",
    "-index-headers.htm", "-index-headers.html",
    "financial_report.htm",   # inline XBRL viewer wrapper
)


def _is_low_value_doc(name: str) -> bool:
    """True when `name` is a doc EDGAR ships but the LLM should not spend
    a slot on (SOX certifications, EDGAR wrapper index files). The tests
    for RMD's 10-K show these can outnumber real content in a filing --
    on that release, primary + 5 of these = the whole 6-slot budget."""
    low = (name or "").lower()
    if any(p in low for p in _CERT_EXHIBIT_PATTERNS):
        return True
    if any(p in low for p in _INDEX_WRAPPER_PATTERNS):
        return True
    return False


# --- public entry ----------------------------------------------------------

def fetch_filing_documents(
    selected: List[dict],
    out_dir: Path,
    max_pdfs: int = 6,
    log: Callable[[str], None] = print,
) -> List[FetchedPdf]:
    """Download and (when needed) render every relevant document from
    `selected` filings. `selected` is the list of filing dicts the
    caller has chosen to analyse -- typically the anchor 10-K/10-Q plus
    the paired earnings 8-K. Returns FetchedPdf tuples in filing order,
    capped at `max_pdfs` total across all filings.

    Prioritisation, per filing:
      1. The primary document (10-K / 10-Q body, or 8-K cover) -- always.
      2. Exhibit 99.x on 8-K filings (the press release, non-GAAP tables,
         supplemental slides) -- crucial for reading management framing.
      3. Any other .htm exhibit up to the cap.
    """
    if not selected:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    session = _sec_session()
    saved: List[FetchedPdf] = []

    for f_idx, row in enumerate(selected):
        cik_int = row.get("cik_int") or 0
        acc_nd = row.get("accession_nodash") or ""
        dir_url = row.get("filing_dir_url") or ""
        primary_document = row.get("primary_document") or ""
        form = row.get("form") or ""
        if not (cik_int and acc_nd and dir_url):
            log(f"[us-docs] skipping row without accession/dir: {row}")
            continue

        # Enumerate exhibits so we can grab the earnings press release
        # on an 8-K, not just the boilerplate cover. Fine to no-op if
        # the index isn't reachable -- we still have primary_document.
        exhibit_names = _fetch_filing_index_docs(session, dir_url, log)

        # Build an ordered download queue: primary first, then Ex-99
        # exhibits (press release + supplemental tables usually), then
        # anything else. Dedupe against primary_document.
        # SOX cert exhibits + EDGAR wrapper index files are filtered
        # out via _is_low_value_doc -- they carry no analysis content
        # and would otherwise eat native-PDF slots earmarked for real
        # exhibits (see RMD FY26 run for the failure mode this fixes).
        queue: List[str] = []
        if primary_document:
            queue.append(primary_document)
        for name in exhibit_names:
            if name == primary_document or _is_low_value_doc(name):
                continue
            low = name.lower()
            # SEC exhibit naming: ex-99*, ex99* -- both variants exist.
            if "ex-99" in low or "ex99" in low or low.startswith("ex99"):
                queue.append(name)
        for name in exhibit_names:
            if name in queue or _is_low_value_doc(name):
                continue
            queue.append(name)

        # Actually download + render each, honouring the global cap.
        for doc_name in queue:
            if len(saved) >= max_pdfs:
                break
            source_url = dir_url + doc_name
            data = _download_bytes(session, source_url, log)
            if not data:
                continue

            display = _document_display_name(row, source_url)
            file_stem = f"{f_idx:02d}_{form.replace('/', '_')}_{doc_name}".replace("/", "_")
            file_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", file_stem).strip("_")

            if data[:4] == PDF_MAGIC:
                # Already a PDF -- pass through.
                out_path = out_dir / (file_stem if file_stem.lower().endswith(".pdf")
                                       else file_stem + ".pdf")
                out_path.write_bytes(data)
                log(f"[us-docs] pass-through PDF {out_path.name} ({len(data)}B)")
                saved.append(FetchedPdf(
                    path=out_path, source_url=source_url, name=display,
                ))
                continue

            # HTML (or plaintext-in-html) -- render to PDF.
            pdf_out = out_dir / (file_stem.rsplit(".", 1)[0] + ".pdf")
            got = _render_html_bytes_to_pdf(data, source_url, pdf_out, log)
            if got:
                saved.append(FetchedPdf(
                    path=got, source_url=source_url, name=display,
                ))
        if len(saved) >= max_pdfs:
            break

    log(f"[us-docs] {len(saved)} PDF(s) ready for LLM")
    return saved
