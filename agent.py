# agent.py
#
# Investor-grade ASX announcements agent ("Bob the Bot"):
# - Includes ALL announcements in an FYI section (headline-only, 2 lines + link)
# - Uses Requests first for PDFs; if ASX consent gate blocks, falls back to Playwright
# - Runs deep analysis only for HY/FY results, acquisitions, and capital/debt raises
# - Produces clean email: HIGH IMPACT + MATERIAL + FYI, or a plain
#   no-announcements message with a local joke of the day
# - Uploads PDFs to Google Drive for big announcements and includes Drive view links
# - Never hallucinates off the ASX "Access to this site" legal page
# - Optionally emails AR9 items to your brother (BROTHER_EMAIL secret)
# - Uses Anthropic Claude API for all AI analysis (native PDF support)

from __future__ import annotations

import os
import re
import json
import ssl
import hashlib
import smtplib
import tempfile
import time
import datetime as dt
import textwrap
import html as htmlmod
from pathlib import Path
from email.message import EmailMessage
from typing import Dict, List, Tuple, Optional

import requests
from bs4 import BeautifulSoup
import yaml
from pypdf import PdfReader
import anthropic
try:
    from weasyprint import HTML, CSS
except ImportError:
    HTML = CSS = None

import asyncio
from playwright_fetch import fetch_pdf_with_playwright
from asx_fetch import fetch_asx_announcements_html
from shared.pdf_llm import (
    LLM_FAILED,
    LLM_SKIPPED,
    PdfAttachment,
    build_pdf_attachments,
    build_single_pdf_content,
)

from prompts import (
    DEFAULT_2LINE_PROMPT,
    ACQUISITION_PROMPT,
    CAPITAL_OR_DEBT_RAISE_PROMPT,
    RESULTS_HYFY_PROMPT,
    TRADING_UPDATE_PROMPT,
    PRICE_SENSITIVE_PROMPT,
)

BOB_NAME = "Bob the Bot"
VERSION_LABEL = "V3"

HOURS_BACK = 24
SEEN_STATE_PATH = Path(os.environ.get("SEEN_STATE_PATH", "state_seen.json"))
SEEN_STATE_RETENTION_HOURS = 72

MAX_ANNOUNCEMENTS_PER_TICKER = 12
MAX_PDFS_PER_RUN = 10


def _int_env(name: str, default: int) -> int:
    """Read a positive integer from the environment, falling back on junk."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"[agent] Ignoring non-integer {name}={raw!r} — using {default}", flush=True)
        return default
    return value if value > 0 else default


# Reporting season regularly puts several portfolio names on the same morning,
# and each results item now costs exactly one call (see deep_results_analysis),
# so the run budget is sized for that rather than for a quiet day. Override
# with the MAX_LLM_CALLS env var.
MAX_LLM_CALLS_PER_RUN = _int_env("MAX_LLM_CALLS", 25)

# Per-call output-token budgets. The default (4096) is enough for the terse
# two-liners and the short structured memos on the other paths. The results
# path is different: the model returns metrics JSON *plus* a full markdown
# analysis with ~11 sections, which for a large reporter (BHP, CSL) blows
# past 4096 mid-string and leaves the JSON truncated — the caller then sees
# a parse_error and the digest shows ANALYSIS FAILED. The results ceiling is
# sized so no realistic results announcement can be truncated; override with
# CLAUDE_RESULTS_MAX_TOKENS.
DEFAULT_MAX_TOKENS = _int_env("CLAUDE_MAX_TOKENS", 4096)
RESULTS_MAX_TOKENS = _int_env("CLAUDE_RESULTS_MAX_TOKENS", 50000)

MIN_RESULTS_TEXT_CHARS = 2500
MODEL_DEFAULT = "claude-sonnet-4-6"

REQUESTS_PDF_TIMEOUT_SECS = 20
HTML_TIMEOUT_SECS = 30

COLOR_HIGH_IMPACT = "#F59E0B"
COLOR_MATERIAL = "#3B82F6"
COLOR_FYI = "#10B981"
COLOR_BG = "#0B1220"
COLOR_PANEL = "#111B2E"
COLOR_TEXT = "#E5E7EB"

# Results card ("dark-green finance theme") — a light, high-contrast card that
# reads on a phone against the dark digest background.
COLOR_RESULTS_HEADER = "#14532D"
COLOR_RESULTS_HEADER_SUB = "#A7F3D0"
COLOR_RESULTS_CARD = "#FFFFFF"
COLOR_RESULTS_ROW_SHADE = "#F1F5F2"
COLOR_RESULTS_LABEL = "#374151"
COLOR_RESULTS_VALUE = "#111827"
COLOR_RESULTS_MUTED = "#6B7280"
COLOR_CHANGE_UP = "#15803D"
COLOR_CHANGE_DOWN = "#B91C1C"
COLOR_CHANGE_NEUTRAL = "#9CA3AF"
COLOR_FAILURE = "#B91C1C"

# The five locked results metrics, in the order they are rendered.
RESULTS_METRIC_ROWS: List[Tuple[str, str]] = [
    ("revenue", "Revenue"),
    ("underlying_npat", "Underlying NPAT"),
    ("underlying_eps", "Underlying EPS"),
    ("dividend_ordinary", "Ordinary dividend"),
    ("operating_cash_flow", "Operating cash flow"),
]

# Rendered prefix per reporting currency. Values are never converted — the
# prefix only makes the reporting currency unmistakable (US$4,180m can't be
# misread as A$4,180m).
CURRENCY_PREFIXES = {
    "AUD": "A$", "USD": "US$", "NZD": "NZ$", "SGD": "S$",
    "HKD": "HK$", "CAD": "C$", "GBP": "£", "EUR": "€", "JPY": "¥",
}

PERIOD_TYPE_LABELS = {
    "full_year": "Full-year",
    "half_year": "Half-year",
    "quarterly": "Quarterly",
    "other": "Results",
}

# deep_results_analysis outcome, carried on the analysis dict as "_status".
RESULTS_STATUS_OK = "ok"
RESULTS_STATUS_SKIPPED = "skipped"
RESULTS_STATUS_FAILED = "failed"
RESULTS_STATUS_PARSE_ERROR = "parse_error"
RESULTS_STATUS_NO_CONTENT = "no_content"

_JOKES_FILE = Path(__file__).resolve().parent / "config" / "daily_jokes.txt"


def log(msg: str):
    print(f"[agent] {msg}", flush=True)


def now_sgt() -> dt.datetime:
    return dt.datetime.utcnow() + dt.timedelta(hours=8)


def today_sgt_date() -> dt.date:
    return now_sgt().date()


def cutoff_dt_sgt(hours_back: int) -> dt.datetime:
    return now_sgt() - dt.timedelta(hours=hours_back)


def announcement_key(ticker: str, url: str) -> str:
    raw = f"{ticker}|{url}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def load_seen_state(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"Could not read seen state file: {e}")
        return {}
    if isinstance(data, list):
        now_iso = now_sgt().isoformat(timespec="seconds")
        return {k: now_iso for k in data if isinstance(k, str)}
    if not isinstance(data, dict):
        return {}
    state: Dict[str, str] = {}
    for key, seen_iso in data.items():
        if isinstance(key, str) and isinstance(seen_iso, str):
            state[key] = seen_iso
    return state


def prune_seen_state(state: Dict[str, str], retention_hours: int) -> Dict[str, str]:
    cutoff = now_sgt() - dt.timedelta(hours=retention_hours)
    out: Dict[str, str] = {}
    for key, seen_iso in state.items():
        try:
            seen_dt = dt.datetime.fromisoformat(seen_iso)
        except Exception:
            continue
        if seen_dt >= cutoff:
            out[key] = seen_iso
    return out


def save_seen_state(path: Path, state: Dict[str, str]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _get_daily_joke() -> str:
    """Return a deterministic joke-of-the-day from the local jokes file.

    No network calls — reads config/daily_jokes.txt and rotates by date so
    the same joke shows all day. Returns "" (never raises) if the file is
    missing or empty.
    """
    try:
        lines = [
            ln.strip() for ln in _JOKES_FILE.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
    except Exception:
        return ""
    if not lines:
        return ""
    return lines[today_sgt_date().toordinal() % len(lines)]


def send_email(
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    to_addr: Optional[str] = None,
    attachments: Optional[List[Path]] = None,
):
    email_from = os.environ["EMAIL_FROM"]
    email_to = to_addr or os.environ["EMAIL_TO"]
    app_password = os.environ["EMAIL_APP_PASSWORD"]
    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = subject
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    # Attach any PDF files
    if attachments:
        for pdf_path in attachments:
            if pdf_path and pdf_path.exists():
                try:
                    with open(pdf_path, "rb") as f:
                        msg.add_attachment(
                            f.read(),
                            maintype="application",
                            subtype="pdf",
                            filename=pdf_path.name,
                        )
                    log(f"[Email] Attached {pdf_path.name}")
                except Exception as e:
                    log(f"[Email] Failed to attach {pdf_path}: {e}")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(email_from, app_password)
        server.send_message(msg)


def read_tickers() -> Tuple[List[str], List[str]]:
    with open("tickers.yaml", "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("asx", []), data.get("lse", [])


def http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/html, */*",
        "Referer": "https://www.asx.com.au/",
    })
    return s


def is_price_sensitive_title(title: str) -> bool:
    t = title.lower()
    keywords = [
        "appendix 4e", "appendix 4d", "results", "half year", "half-year",
        "full year", "annual report", "interim financial report", "financial report",
        "trading update", "guidance", "earnings", "profit", "revenue", "eps",
        "ebit", "ebitda", "investor presentation", "presentation",
        "placement", "rights issue", "entitlement", "spp", "capital raising",
        "issue of shares", "convertible", "notes", "bond", "debt facility",
        "refinance", "term loan", "facility",
        "acquisition", "acquire", "merger", "scheme", "takeover", "transaction",
        "contract", "award", "termination", "litigation", "regulatory",
        "material", "strategic", "halt", "suspension",
        "ceo", "cfo", "resignation", "retirement",
        "quarterly", "quarterly activities", "quarterly report", "appendix 4c", "appendix 5b",
    ]
    return any(k in t for k in keywords)


def looks_like_results_title(title: str) -> bool:
    t = title.lower()
    hard_yes = [
        "appendix 4e", "appendix 4d", "half year results", "half-year results",
        "interim financial report", "annual report", "full year results",
        "full-year results", "financial report", "results announcement",
        "results presentation",
    ]
    hard_no = ["investor call transcript", "transcript", "webcast", "conference call"]
    if any(x in t for x in hard_no):
        return False
    return any(x in t for x in hard_yes)


def classify_from_title_only(title: str) -> str:
    t = title.lower()
    if looks_like_results_title(title):
        return "RESULTS_HY_FY"
    if any(k in t for k in ["acquisition", "acquire", "merger", "scheme", "takeover", "transaction"]):
        return "ACQUISITION"
    if any(k in t for k in [
        "placement", "spp", "entitlement", "rights issue", "capital raising",
        "convertible", "notes", "debt facility", "refinance", "term loan", "bond",
    ]):
        return "CAPITAL_OR_DEBT_RAISE"
    if any(k in t for k in [
        "trading update", "guidance", "profit warning", "earnings revision",
        "outlook", "forecast update", "revenue update", "downgrade", "upgrade",
        "earnings guidance", "sales update", "quarterly", "quarterly activities",
        "quarterly report", "appendix 4c", "appendix 5b", "quarterly cashflow",
    ]):
        return "TRADING_UPDATE"
    if any(k in t for k in ["contract", "award", "termination"]):
        return "CONTRACT_MATERIAL"
    return "OTHER"


def _parse_asx_date(released) -> Optional[dt.datetime]:
    if released is None:
        return None
    if isinstance(released, (int, float)):
        try:
            return dt.datetime.utcfromtimestamp(float(released) / 1000.0) + dt.timedelta(hours=8)
        except Exception:
            return None
    released = str(released).strip()
    if not released:
        return None
    if released.isdigit() and len(released) >= 10:
        try:
            return dt.datetime.utcfromtimestamp(int(released) / 1000.0) + dt.timedelta(hours=8)
        except Exception:
            pass
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %I:%M %p",
        "%d/%m/%Y %I:%M%p",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
    ):
        try:
            return dt.datetime.strptime(released, fmt)
        except Exception:
            continue
    return None


def fetch_asx_announcements(session: requests.Session, ticker: str, hours_back: int = 24) -> List[Dict]:
    """
    Fetch ASX announcements via the JSON API endpoint.
    KEY FIX: the endpoint returns JSON not HTML — must use r.json() not BeautifulSoup.
    """
    url = (
        "https://www.asx.com.au/asx/v2/statistics/announcements.do"
        f"?asxCode={ticker}"
    )
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        log(f"Announcement fetch failed for {ticker}: {e}")
        return []

    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not rows:
        log(f"No data rows for {ticker} (keys: {list(payload.keys()) if isinstance(payload, dict) else 'not a dict'})")
        return []

    cutoff = cutoff_dt_sgt(hours_back)
    items: List[Dict] = []
    seen_urls: set = set()

    for row in rows:
        if not isinstance(row, dict):
            continue
        title = (row.get("header") or row.get("headline") or "").strip()
        if not title:
            continue
        doc_url = (row.get("url") or "").strip()
        if not doc_url:
            doc_key = (row.get("documentKey") or "").strip()
            if doc_key:
                doc_key = doc_key.lstrip("/")
                doc_url = f"https://www.asx.com.au/{doc_key}"
        if not doc_url:
            continue
        if doc_url.startswith("/"):
            doc_url = "https://www.asx.com.au" + doc_url
        if doc_url in seen_urls:
            continue
        seen_urls.add(doc_url)

        released = row.get("releasedDate") or row.get("issueDate") or row.get("date")
        item_dt = _parse_asx_date(released)
        if item_dt is None:
            log(f"{ticker}: unparseable date '{released}' for '{title[:60]}' — including anyway")
            item_dt = now_sgt()
        if item_dt < cutoff:
            continue

        items.append({
            "exchange": "ASX",
            "ticker": ticker,
            "date": item_dt.strftime("%d/%m/%Y"),
            "time": item_dt.strftime("%I:%M %p"),
            "title": title,
            "url": doc_url,
        })
        if len(items) >= MAX_ANNOUNCEMENTS_PER_TICKER:
            break

    log(f"{ticker}: found {len(items)} fresh announcement(s) in last {hours_back}h")
    return items


def asx_pdf_url_from_item_url(url: str) -> Optional[str]:
    if not url:
        return None
    low = url.lower()
    m = re.search(r"[?&]idsid=([^&]+)", url, re.IGNORECASE)
    if m:
        return (
            "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do"
            f"?display=pdf&idsId={m.group(1)}"
        )
    if "displayannouncement.do" in low:
        return url
    if low.endswith(".pdf"):
        return url
    if "asxpdf" in low:
        return url
    if "/pdf/" in low:
        return url
    return None


def _anthropic_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _llm_cap_reached(counters: Dict) -> bool:
    """Return True (and log clearly) when the run's LLM call budget is used up.

    This is an intentional, expected stop — NOT an error — so it must never
    be confused with a genuine API failure downstream.
    """
    if counters["llm_calls"] >= counters["MAX_LLM_CALLS_PER_RUN"]:
        log(
            f"LLM call skipped: call cap reached "
            f"({counters['llm_calls']}/{counters['MAX_LLM_CALLS_PER_RUN']} this run)"
        )
        return True
    return False


# Anthropic's Python SDK refuses non-streaming create() calls whose expected
# duration exceeds ~10 minutes (a hard client-side check to avoid the HTTP
# read-timeout that kills long requests). At Sonnet's output rate a large
# max_tokens hits that threshold quickly — the results path's 50k budget
# came back as ``Streaming is required for operations that may take longer
# than 10 minutes.`` So above this threshold ``_call_anthropic`` routes
# through ``client.messages.stream`` instead of ``.create``.
_STREAMING_MIN_TOKENS = 8192


def _call_anthropic(
    system_prompt: str,
    content: object,
    counters: Dict,
    max_retries: int = 1,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Send *content* to Claude, incrementing the run's call counter once.

    Retries a genuine exception (timeout, transient 5xx, rate limit, etc.)
    once before giving up. On final failure this logs at ERROR level and
    returns LLM_FAILED — callers must surface this distinctly from
    LLM_SKIPPED rather than substituting the same placeholder text for both.

    *max_tokens* caps the response length. The API's ``stop_reason`` for the
    last successful call is stashed on ``counters["last_stop_reason"]`` so a
    caller doing JSON parsing can tell the difference between "model
    genuinely returned bad JSON" and "model was cut off at max_tokens
    mid-string" — those need different remedies and different failure text.
    """
    counters["llm_calls"] += 1
    counters.pop("last_stop_reason", None)
    model = os.environ.get("CLAUDE_MODEL", MODEL_DEFAULT)
    client = _anthropic_client()

    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            if max_tokens >= _STREAMING_MIN_TOKENS:
                text, stop_reason = _stream_message(
                    client, model, max_tokens, system_prompt, content,
                )
            else:
                resp = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": content}],
                )
                text = (resp.content[0].text or "")
                stop_reason = getattr(resp, "stop_reason", None)
            if stop_reason:
                counters["last_stop_reason"] = str(stop_reason)
                if stop_reason == "max_tokens":
                    log(
                        f"WARNING: Claude response was truncated at max_tokens={max_tokens} "
                        f"(model={model}); downstream JSON parsing will likely fail"
                    )
            return text.strip()
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                log(f"WARNING: Claude API call failed (attempt {attempt + 1}/{max_retries + 1}, model={model}): {e} — retrying once")
                time.sleep(2)
                continue
            log(f"ERROR: Claude API call failed after {attempt + 1} attempt(s) (model={model}): {e}")

    counters["last_llm_error"] = str(last_err) if last_err else "unknown error"
    return LLM_FAILED


def _stream_message(
    client: "anthropic.Anthropic",
    model: str,
    max_tokens: int,
    system_prompt: str,
    content: object,
) -> Tuple[str, Optional[str]]:
    """Drive the Anthropic streaming API and return (text, stop_reason).

    Streaming keeps the HTTP connection alive with periodic events instead
    of forcing one long read, so the SDK's 10-minute non-streaming ceiling
    doesn't apply. The generated text is identical to what .create() would
    have returned — we just accumulate it as it comes in.
    """
    parts: List[str] = []
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        for chunk in stream.text_stream:
            parts.append(chunk)
        final = stream.get_final_message()
    stop_reason = getattr(final, "stop_reason", None)
    return "".join(parts), stop_reason


def llm_chat(
    system_prompt: str,
    user_content: str,
    counters: Dict,
    pdf_path: Optional[Path] = None,
    fallback_text: str = "",
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Call Claude with an optional single native PDF attachment.

    *fallback_text* should be the already-extracted text for *pdf_path* (if
    any) — it's used only if the PDF turns out to be missing, malformed, or
    too large for a native document block, so a downgrade never means
    sending an empty/near-empty message.
    """
    if _llm_cap_reached(counters):
        return LLM_SKIPPED
    content, _used_native = build_single_pdf_content(
        pdf_path, user_content, fallback_text=fallback_text, log=log,
    )
    return _call_anthropic(system_prompt, content, counters, max_tokens=max_tokens)


def llm_chat_with_pdfs(
    system_prompt: str,
    user_content: str,
    counters: Dict,
    pdf_attachments: List[PdfAttachment],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    """Call Claude with zero or more native PDF attachments (e.g. the primary
    financial report plus the investor presentation deck, sent together so
    both retain full table/chart fidelity instead of the deck being
    downgraded to extracted text)."""
    if _llm_cap_reached(counters):
        return LLM_SKIPPED
    batch = build_pdf_attachments(pdf_attachments, log=log)
    if batch.any_native:
        content: object = list(batch.document_blocks) + [
            {"type": "text", "text": user_content[:50_000]}
        ]
    else:
        prefix = "\n\n".join(batch.fallback_sections)
        content = (f"{prefix}\n\n{user_content}" if prefix else user_content)[:100_000]
    return _call_anthropic(system_prompt, content, counters, max_tokens=max_tokens)


def download_pdf_requests(session: requests.Session, url: str, out_path: Path) -> bool:
    r = session.get(url, timeout=REQUESTS_PDF_TIMEOUT_SECS, allow_redirects=True)
    r.raise_for_status()
    if r.content[:4] != b"%PDF":
        return False
    out_path.write_bytes(r.content)
    return True


def extract_pdf_text(pdf_path: Path) -> str:
    try:
        reader = PdfReader(str(pdf_path))
        parts = []
        for page in reader.pages:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
        return "\n\n".join(parts).strip()
    except Exception:
        return ""


def fetch_html_text(session: requests.Session, url: str) -> str:
    r = session.get(url, timeout=HTML_TIMEOUT_SECS, allow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:80_000]


def looks_like_asx_access_gate(text: str) -> bool:
    t = (text or "").lower()
    return ("access to this site" in t and "agree and proceed" in t) or (
        "general conditions" in t and "agree and proceed" in t
    )


def is_meaningful_text(text: str, min_chars: int = 1200) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t) < min_chars:
        return False
    if looks_like_asx_access_gate(t):
        return False
    return True


def fetch_announcement_text(
    session: requests.Session,
    url: str,
    pdf_url: Optional[str],
    pdf_path: Path,
    counters: Dict,
) -> Tuple[str, bool]:
    got_pdf = False
    text = ""
    if pdf_url and counters["pdfs_downloaded"] < counters["MAX_PDFS_PER_RUN"]:
        try:
            got_pdf = download_pdf_requests(session, pdf_url, pdf_path)
        except Exception:
            got_pdf = False
        if not got_pdf:
            try:
                got_pdf = asyncio.run(fetch_pdf_with_playwright(pdf_url, pdf_path))
            except Exception as e:
                log(f"Playwright fetch failed: {e}")
                got_pdf = False
    if got_pdf:
        counters["pdfs_downloaded"] += 1
        text = extract_pdf_text(pdf_path) or ""
        return text, True
    log(f"PDF fetch failed for {url} — falling back to HTML text")
    try:
        html_text = fetch_html_text(session, url)
        if looks_like_asx_access_gate(html_text):
            return "", False
        return html_text, False
    except Exception:
        return "", False


def _linkify_urls(text: str) -> str:
    escaped = htmlmod.escape(text)
    return re.sub(
        r"(https?://[^\s<]+)",
        r"<a href='\1' style='color:#93C5FD; text-decoration:underline;'>\1</a>",
        escaped,
    )


def _html_block(b: str) -> str:
    return (
        "<div style='margin:12px 0; padding:12px; background:"
        + COLOR_PANEL
        + "; border-radius:10px; white-space:pre-wrap; line-height:1.5; font-size:14px; color:"
        + COLOR_TEXT
        + ";'>"
        + _linkify_urls(b)
        + "</div>"
    )


def _block_text(block) -> str:
    """Digest blocks are either a plain string (rendered as escaped pre-wrap
    text) or a {"text": ..., "html": ...} pair for blocks that build their own
    email-safe HTML, e.g. the results card."""
    if isinstance(block, dict):
        return str(block.get("text", "") or "")
    return str(block or "")


def _block_html(block) -> str:
    if isinstance(block, dict) and block.get("html"):
        return str(block["html"])
    return _html_block(_block_text(block))


def _html_section(title: str, color: str, blocks: List) -> str:
    if not blocks:
        return ""
    items_html = "".join(_block_html(b) for b in blocks)
    return f"""
    <div style="margin:18px 0;">
      <div style="padding:10px 12px; background:{color}; color:#0B1220; font-weight:800; border-radius:10px; letter-spacing:0.6px;">
        {htmlmod.escape(title)}
      </div>
      {items_html}
    </div>
    """


def build_email(
    high_impact: List,
    material: List,
    fyi: List,
) -> Tuple[str, str]:
    no_announcements = not high_impact and not material and not fyi
    no_announcements_msg = f"No reportable announcements found in the last {HOURS_BACK} hours."
    if no_announcements:
        joke = _get_daily_joke()
        if joke:
            no_announcements_msg += f"\nJoke of the day: {joke}"

    lines: List[str] = []
    lines.append(f"{BOB_NAME} {VERSION_LABEL}")
    lines.append("=" * len(BOB_NAME))
    lines.append(f"Daily Announcements Digest — last {HOURS_BACK} hours — {today_sgt_date().isoformat()} (SGT)")
    lines.append(f"Run caps: MAX_PDFS={MAX_PDFS_PER_RUN}, MAX_LLM_CALLS={MAX_LLM_CALLS_PER_RUN}, MAX_PER_TICKER={MAX_ANNOUNCEMENTS_PER_TICKER}")
    lines.append("")
    if high_impact:
        lines.append("HIGH IMPACT")
        lines.append("-" * 60)
        lines.extend(_block_text(b) for b in high_impact)
        lines.append("")
    if material:
        lines.append("MATERIAL")
        lines.append("-" * 60)
        lines.extend(_block_text(b) for b in material)
        lines.append("")
    if fyi:
        lines.append("FYI (ALL ANNOUNCEMENTS)")
        lines.append("-" * 60)
        lines.extend(_block_text(b) for b in fyi)
        lines.append("")
    if no_announcements:
        lines.append(no_announcements_msg)
    body_text = "\n".join(lines)

    header_html = f"""
    <div style="padding:18px; background:{COLOR_BG}; color:{COLOR_TEXT}; font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif;">
      <div style="font-size:22px; font-weight:900; margin-bottom:6px;">{htmlmod.escape(f"{BOB_NAME} {VERSION_LABEL}")}</div>
      <div style="opacity:0.9; font-size:14px; margin-bottom:10px;">
        Daily Announcements Digest — last {HOURS_BACK} hours — {today_sgt_date().isoformat()} (SGT)
      </div>
      <div style="opacity:0.75; font-size:12px; margin-bottom:18px;">
        Run caps: MAX_PDFS={MAX_PDFS_PER_RUN}, MAX_LLM_CALLS={MAX_LLM_CALLS_PER_RUN}, MAX_PER_TICKER={MAX_ANNOUNCEMENTS_PER_TICKER}
        &nbsp;|&nbsp; AI: Anthropic {os.environ.get("CLAUDE_MODEL", MODEL_DEFAULT)}
      </div>
    """
    sections_html = ""
    sections_html += _html_section("HIGH IMPACT", COLOR_HIGH_IMPACT, high_impact)
    sections_html += _html_section("MATERIAL", COLOR_MATERIAL, material)
    sections_html += _html_section("FYI (ALL ANNOUNCEMENTS)", COLOR_FYI, fyi)
    if no_announcements:
        sections_html += f"""
        <div style="margin:18px 0; padding:12px; background:{COLOR_PANEL}; border-radius:10px; color:{COLOR_TEXT};">
          {htmlmod.escape(no_announcements_msg).replace(chr(10), "<br>")}
        </div>
        """
    footer_html = "</div>"
    body_html = header_html + sections_html + footer_html
    return body_text, body_html


def summarise_headline_two_lines(ticker: str, title: str) -> str:
    line1 = f"{ticker}: {title[:160]}"
    line2 = "So what: FYI — open link if you want the details."
    return line1 + "\n" + line2


def summarise_two_lines_llm(ticker: str, title: str, text: str, counters: Dict) -> Optional[dict]:
    if not is_meaningful_text(text, min_chars=600):
        return None
    user = f"Ticker: {ticker}\nTitle: {title}\n\nText:\n{text}"
    out = llm_chat(DEFAULT_2LINE_PROMPT, user, counters)
    if out == LLM_SKIPPED:
        return {
            "what_happened": f"{ticker}: {title[:160]}",
            "so_what": LLM_SKIP_MESSAGE,
        }
    if out == LLM_FAILED:
        marker = _failed_marker("so_what", counters)
        return {
            "what_happened": f"{ticker}: {title[:160]}",
            "so_what": marker["so_what"],
        }
    parsed = _parse_analysis_json(out)
    if parsed and "what_happened" in parsed:
        return parsed
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(lines) >= 2:
        return {"what_happened": lines[0], "so_what": lines[1]}
    if len(lines) == 1:
        return {"what_happened": lines[0], "so_what": "Open link for details."}
    return None


def _prettify_analysis_text(text: str, width: int = 140) -> str:
    if not text:
        return ""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    wrapped: List[str] = []
    for block in blocks:
        raw = re.sub(r"\s+", " ", block).strip()
        if raw:
            wrapped.append(textwrap.fill(raw, width=width))
    return "\n\n".join(wrapped)


def _parse_analysis_json(text: str) -> Optional[dict]:
    """Parse structured JSON from LLM output, stripping any markdown code fences."""
    if not text or text in (LLM_SKIPPED, LLM_FAILED):
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned.strip(), flags=re.MULTILINE)
    cleaned = cleaned.strip()
    try:
        result = json.loads(cleaned)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        try:
            result = json.loads(m.group())
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass
    return None


def _analysis_to_email_text(analysis: dict, kind: str) -> str:
    """Render a structured analysis dict as readable plain text for the email."""
    if not analysis:
        return "No analysis available."

    lines: List[str] = []

    if kind == "results":
        if "executive_summary" in analysis:
            lines.append("EXECUTIVE SUMMARY:")
            for b in analysis.get("executive_summary", []):
                lines.append(f"  • {b}")
        if "key_numbers" in analysis:
            lines.append("\nKEY NUMBERS:")
            labels = {
                "revenue": "Revenue", "ebitda_margin": "EBITDA margin",
                "npat_statutory": "NPAT (statutory)", "npat_underlying": "NPAT (underlying)",
                "eps": "EPS", "operating_cashflow": "Op. cash flow",
                "free_cashflow": "Free cash flow", "net_debt_cash": "Net debt/cash",
            }
            for k, v in analysis["key_numbers"].items():
                lines.append(f"  {labels.get(k, k)}: {v}")
        if analysis.get("quality_of_earnings"):
            lines.append(f"\nQUALITY OF EARNINGS:\n{analysis['quality_of_earnings']}")
        if analysis.get("management_framing"):
            lines.append(f"\nMANAGEMENT FRAMING:\n{analysis['management_framing']}")
        if analysis.get("positives"):
            lines.append("\nPOSITIVES:")
            for p in analysis["positives"]:
                lines.append(f"  ✓ {p}")
        if analysis.get("negatives"):
            lines.append("\nNEGATIVES / RED FLAGS:")
            for n in analysis["negatives"]:
                lines.append(f"  ✗ {n}")
        bl = analysis.get("bottom_line", {})
        if isinstance(bl, dict):
            lines.append("\nBOTTOM LINE:")
            if bl.get("bull"):
                lines.append(f"  Bull: {bl['bull']}")
            if bl.get("bear"):
                lines.append(f"  Bear: {bl['bear']}")
            if bl.get("what_changes_mind"):
                lines.append(f"  Watch: {bl['what_changes_mind']}")
        elif bl:
            lines.append(f"\nBOTTOM LINE: {bl}")

    elif kind == "acquisition":
        for key, label in [
            ("deal_summary", "DEAL SUMMARY"), ("what_they_bought", "WHAT THEY BOUGHT"),
            ("price_check", "PRICE CHECK"), ("strategic_fit", "STRATEGIC FIT"),
            ("integration_risk", "INTEGRATION RISK"), ("balance_sheet_impact", "BALANCE SHEET"),
        ]:
            if analysis.get(key):
                lines.append(f"\n{label}:\n{analysis[key]}")
        if analysis.get("red_flags"):
            lines.append("\nRED FLAGS:")
            for f in analysis["red_flags"]:
                lines.append(f"  ⚠ {f}")
        bl = analysis.get("bottom_line", {})
        if isinstance(bl, dict):
            lines.append("\nBOTTOM LINE:")
            if bl.get("bull"):
                lines.append(f"  Bull: {bl['bull']}")
            if bl.get("bear"):
                lines.append(f"  Bear: {bl['bear']}")
            if bl.get("key_questions"):
                lines.append("  Key questions:")
                for q in bl["key_questions"]:
                    lines.append(f"    → {q}")
        elif bl:
            lines.append(f"\nBOTTOM LINE: {bl}")

    elif kind in ("capital", "trading_update", "price_sensitive"):
        list_keys = {"key_questions", "risks_questions", "red_flags"}
        for key, value in analysis.items():
            if not value:
                continue
            label = key.replace("_", " ").upper()
            if key in list_keys:
                lines.append(f"\n{label}:")
                for item in (value if isinstance(value, list) else [value]):
                    lines.append(f"  → {item}")
            elif isinstance(value, dict):
                lines.append(f"\n{label}:")
                for k, v in value.items():
                    lines.append(f"  {k.title()}: {v}")
            else:
                lines.append(f"\n{label}:\n{value}")

    else:
        if analysis.get("what_happened"):
            lines.append(analysis["what_happened"])
        if analysis.get("so_what"):
            lines.append(f"So what: {analysis['so_what']}")

    return "\n".join(lines).strip() if lines else str(analysis)


LLM_FAILURE_FLAG = "⚠️ Analysis failed — manual review needed"
LLM_SKIP_MESSAGE = "Analysis skipped — LLM call-cap reached for this run (not an error)."
RESULTS_SKIP_MESSAGE = "Analysis skipped: run-call cap reached"
RESULTS_FAILURE_BADGE = "ANALYSIS FAILED"


def _skip_marker(key: str, as_list: bool = False) -> dict:
    """Placeholder for the MAX_LLM_CALLS_PER_RUN cap — expected, not an error.
    Deliberately does not resemble real analysis output."""
    return {key: [LLM_SKIP_MESSAGE] if as_list else LLM_SKIP_MESSAGE}


def _failed_marker(key: str, counters: Dict, as_list: bool = False) -> dict:
    """Placeholder for a genuine API exception — must read as a clear failure,
    never as a terse-but-real analysis."""
    detail = (counters.get("last_llm_error") or "").strip()
    suffix = f" ({detail})" if detail else ""
    message = f"{LLM_FAILURE_FLAG}{suffix}."
    return {key: [message] if as_list else message}


def _is_llm_failure(memo: dict) -> bool:
    def _flagged(v: object) -> bool:
        if isinstance(v, str):
            return v.startswith(LLM_FAILURE_FLAG)
        if isinstance(v, list):
            return any(_flagged(x) for x in v)
        return False

    return any(_flagged(v) for v in memo.values())


def build_high_impact_block(
    ticker: str,
    heading: str,
    memo,
    url: str,
    analysed_via_pdf: bool = False,
    kind: Optional[str] = None,
    failed: bool = False,
) -> str:
    lines: List[str] = [f"{ticker} — {heading}"]
    if failed:
        lines[-1] = f"{LLM_FAILURE_FLAG} — {ticker} — {heading}"
    elif analysed_via_pdf:
        lines[-1] += " (analysed via PDF)"
    if isinstance(memo, dict):
        memo_text = _analysis_to_email_text(memo, kind or "generic")
    else:
        memo_text = memo or ""
    lines.extend(["", "Key analysis:", _prettify_analysis_text(memo_text), "", f"Open: {url}", ""])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Results output: five-metric table + <=5-sentence summary + two links.
#
# The long-form analysis no longer lives in the email — it goes to a Google
# Doc (create_analysis_doc) and the email links to it. Everything below is
# built for a phone: table layout, inline styles only, ~360px card.
# ---------------------------------------------------------------------------

EMAIL_FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif"

_NA_TOKENS = {"", "n/a", "na", "n.a.", "none", "null", "not disclosed", "unknown", "-", "--", "—"}
_NEUTRAL_CHANGE_TOKENS = _NA_TOKENS | {"n/m", "nm", "n.m.", "not meaningful", "nil"}


def _is_na(value: object) -> bool:
    return str(value or "").strip().lower() in _NA_TOKENS


def _apply_currency_prefix(value: str, currency: str) -> str:
    """Make a non-AUD reporting currency explicit without ever converting it.

    A USD reporter's "$4,180m" becomes "US$4,180m" so it can't be misread as
    Australian dollars. AUD is left as a bare "$" — it is the digest's implied
    default and the card's context line already says "reported in A$", so
    prefixing every row would just add noise. Values that already carry a
    prefix are left alone.
    """
    code = str(currency or "").strip().upper()
    prefix = "" if code in ("", "AUD") else CURRENCY_PREFIXES.get(code, "")
    text = str(value or "").strip()
    if not prefix or not text:
        return text
    sign = ""
    if text[0] in "+-−":
        sign, text = text[0], text[1:].lstrip()
    if text.startswith("$") and not text.startswith(prefix):
        text = prefix + text[1:]
    return sign + text


def _change_colour(change: str) -> str:
    """Green for a rise, red for a fall, muted grey for n/a and n/m."""
    text = str(change or "").strip()
    if text.lower() in _NEUTRAL_CHANGE_TOKENS:
        return COLOR_CHANGE_NEUTRAL
    if text.startswith("+"):
        return COLOR_CHANGE_UP
    if text.startswith("-") or text.startswith("−"):
        return COLOR_CHANGE_DOWN
    if re.match(r"^\d", text):
        return COLOR_CHANGE_UP
    return COLOR_CHANGE_NEUTRAL


def _results_metric(analysis: dict, key: str, label: str = "") -> Dict[str, str]:
    """Normalise one metric into {value, change, sub} with "n/a" for anything
    the model could not find. Never invents or infers a figure.

    *label* is only used to drop a basis note the row label already states
    (no "Underlying NPAT (underlying)").
    """
    metrics = analysis.get("metrics")
    raw = metrics.get(key) if isinstance(metrics, dict) else None

    value = change = note = basis = ""
    if isinstance(raw, dict):
        value = str(raw.get("value", "") or "").strip()
        change = str(raw.get("change_pct", raw.get("change", "")) or "").strip()
        note = str(raw.get("note", "") or "").strip()
        basis = str(raw.get("basis", "") or "").strip()
    elif raw not in (None, ""):
        value = str(raw).strip()

    value = _apply_currency_prefix(value, analysis.get("currency", ""))
    if _is_na(value):
        value = "n/a"
    if _is_na(change):
        change = "n/a"

    sub = note
    if not sub and basis and basis.lower() not in ("reported", "n/a", ""):
        if basis.lower() not in label.lower():
            sub = basis
    return {"value": value, "change": change, "sub": sub}


def _results_period_label(analysis: dict) -> str:
    return str(analysis.get("period") or "").strip()


def _results_title(ticker: str, analysis: dict) -> str:
    period = _results_period_label(analysis)
    return f"{ticker} — {period} results" if period else f"{ticker} — Results"


def _results_context_line(analysis: dict) -> str:
    """e.g. 'Full-year, reported in A$'."""
    label = PERIOD_TYPE_LABELS.get(
        str(analysis.get("period_type") or "").strip().lower(), "Results"
    )
    currency = str(analysis.get("currency") or "").strip().upper()
    if not currency:
        return label
    return f"{label}, reported in {CURRENCY_PREFIXES.get(currency, currency)}"


def _results_summary_text(analysis: dict) -> str:
    summary = str(analysis.get("summary") or "").strip()
    if summary:
        return summary
    exec_summary = analysis.get("executive_summary")
    if isinstance(exec_summary, list) and exec_summary:
        return " ".join(str(b).strip() for b in exec_summary if str(b).strip())
    if isinstance(exec_summary, str):
        return exec_summary.strip()
    return ""


def _results_status(analysis: dict) -> str:
    status = str(analysis.get("_status") or "").strip()
    if status:
        return status
    return RESULTS_STATUS_FAILED if _is_llm_failure(analysis) else RESULTS_STATUS_OK


def build_results_block(
    ticker: str,
    analysis: dict,
    asx_url: str,
    doc_link: str = "",
    doc_error: str = "",
) -> Dict[str, str]:
    """Render the RESULTS_HY_FY digest block as {"text": ..., "html": ...}.

    The HTML is deliberately table-based with inline styles only: email
    clients strip <style> blocks and mishandle flexbox/grid, so a two-column
    table is the only reliable way to get label-left / value-right rows.

    *doc_error* — if the Google Doc failed to save, this is a short
    description of why. It is surfaced as a visible warning in the card so
    a broken Drive stops looking like a normal quiet day. That silent-fail
    mode is exactly how the storage-quota bug hid for weeks.
    """
    analysis = analysis or {}
    status = _results_status(analysis)
    failed = status in (RESULTS_STATUS_FAILED, RESULTS_STATUS_PARSE_ERROR)
    title = _results_title(ticker, analysis)
    context = _results_context_line(analysis)
    summary = _results_summary_text(analysis) or "No summary returned."
    rows = [(label, _results_metric(analysis, key, label)) for key, label in RESULTS_METRIC_ROWS]
    esc = htmlmod.escape

    # ---- plain text ----------------------------------------------------
    text_lines: List[str] = []
    if failed:
        text_lines.append(f"{RESULTS_FAILURE_BADGE} — {title}")
    else:
        text_lines.append(title)
    text_lines.append(context)
    text_lines.append("")
    for label, metric in rows:
        text_lines.append(f"  {label:<22} {metric['value']:>12}   {metric['change']}")
        if metric["sub"]:
            # Its own line so a long note can't break the value column.
            text_lines.append(f"      ({metric['sub']})")
    text_lines.append("")
    text_lines.append(summary)
    text_lines.append("")
    if doc_error and not doc_link:
        text_lines.append(f"⚠️ Drive save failed — {doc_error}")
    if doc_link:
        text_lines.append(f"Full analysis (Google Drive): {doc_link}")
    if asx_url:
        text_lines.append(f"Source announcement (ASX): {asx_url}")
    text_lines.append("")

    # ---- html ----------------------------------------------------------
    badge = RESULTS_FAILURE_BADGE if failed else "HIGH IMPACT"
    badge_bg = COLOR_FAILURE if failed else COLOR_HIGH_IMPACT
    badge_fg = "#FFFFFF" if failed else "#0B1220"

    metric_rows_html = ""
    for i, (label, metric) in enumerate(rows):
        bg = COLOR_RESULTS_ROW_SHADE if i % 2 == 0 else COLOR_RESULTS_CARD
        sub_html = (
            f'<div style="font-size:11px; color:{COLOR_RESULTS_MUTED}; margin-top:2px;">'
            f'{esc(metric["sub"])}</div>'
            if metric["sub"] else ""
        )
        metric_rows_html += (
            f'<tr>'
            f'<td style="background:{bg}; padding:11px 14px; font-size:15px; line-height:1.3;'
            f' color:{COLOR_RESULTS_LABEL}; font-family:{EMAIL_FONT};">{esc(label)}{sub_html}</td>'
            f'<td align="right" style="background:{bg}; padding:11px 14px; font-size:15px;'
            f' line-height:1.3; color:{COLOR_RESULTS_VALUE}; font-weight:700; white-space:nowrap;'
            f' font-family:{EMAIL_FONT};">{esc(metric["value"])}'
            f'&nbsp;&nbsp;<span style="color:{_change_colour(metric["change"])}; font-weight:700;">'
            f'{esc(metric["change"])}</span></td>'
            f'</tr>'
        )

    def _link_row(label: str, href: str) -> str:
        return (
            f'<tr><td colspan="2" style="padding:0 14px 10px 14px; font-family:{EMAIL_FONT};">'
            f'<a href="{esc(href)}" style="display:block; padding:11px 12px; background:{COLOR_RESULTS_ROW_SHADE};'
            f' border:1px solid #D5E0D8; border-radius:8px; color:{COLOR_RESULTS_HEADER};'
            f' font-size:14px; font-weight:700; text-decoration:none;">{esc(label)} &rsaquo;</a>'
            f'</td></tr>'
        )

    warning_row = ""
    if doc_error and not doc_link:
        warning_row = (
            f'<tr><td colspan="2" style="padding:10px 14px; background:#FEE2E2;'
            f' border-top:1px solid #FCA5A5; color:{COLOR_FAILURE}; font-family:{EMAIL_FONT};'
            f' font-size:13px; line-height:1.4;">'
            f'<strong>⚠️ Drive save failed.</strong> The card is complete but the '
            f'full analysis Doc was not written to Google Drive: '
            f'<span style="font-family:monospace; font-size:12px;">{esc(doc_error)}</span>'
            f'</td></tr>'
        )

    links_html = ""
    if doc_link:
        links_html += _link_row("Full analysis (Google Drive)", doc_link)
    else:
        # PDF is attached to the email instead of linking to Drive
        links_html += (
            f'<tr><td colspan="2" style="padding:10px 14px; background:{COLOR_RESULTS_ROW_SHADE};'
            f' color:{COLOR_RESULTS_MUTED}; font-family:{EMAIL_FONT};'
            f' font-size:13px;">📎 Full analysis PDF attached to this email</td></tr>'
        )
    if asx_url:
        links_html += _link_row("Source announcement (ASX)", asx_url)

    html = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"'
        f' style="border-collapse:collapse; margin:14px 0;"><tr><td align="center">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="360"'
        f' style="width:100%; max-width:360px; border-collapse:collapse;'
        f' background:{COLOR_RESULTS_CARD}; border-radius:10px; overflow:hidden;">'
        # header bar
        f'<tr><td colspan="2" style="background:{COLOR_RESULTS_HEADER}; padding:13px 14px;'
        f' font-family:{EMAIL_FONT};">'
        f'<span style="display:inline-block; background:{badge_bg}; color:{badge_fg};'
        f' font-size:10px; font-weight:800; letter-spacing:0.8px; padding:3px 7px;'
        f' border-radius:4px;">{esc(badge)}</span>'
        f'<div style="color:#FFFFFF; font-size:17px; font-weight:800; margin-top:7px;">{esc(title)}</div>'
        f'<div style="color:{COLOR_RESULTS_HEADER_SUB}; font-size:12px; margin-top:3px;">{esc(context)}</div>'
        f'</td></tr>'
        # metric rows
        f'{metric_rows_html}'
        # summary
        f'<tr><td colspan="2" style="padding:13px 14px; font-family:{EMAIL_FONT}; font-size:15px;'
        f' line-height:1.5; color:{COLOR_RESULTS_VALUE}; border-top:2px solid {COLOR_RESULTS_HEADER};">'
        f'{esc(summary)}</td></tr>'
        # visible Drive warning if the Doc failed to write
        f'{warning_row}'
        # links
        f'{links_html}'
        f'</table></td></tr></table>'
    )

    return {"text": "\n".join(text_lines), "html": html}


# ---------------------------------------------------------------------------
# Markdown -> simple HTML (for the Google Doc body Drive converts to a Doc)
# ---------------------------------------------------------------------------

def _md_inline(text: str) -> str:
    out = htmlmod.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", out)
    out = re.sub(r"`(.+?)`", r"<code>\1</code>", out)
    out = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', out)
    return out


def _md_table_to_html(rows: List[str]) -> str:
    """Render a markdown pipe-table as a shaded HTML table."""
    parsed: List[List[str]] = []
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells if c != ""):
            continue  # separator row
        parsed.append(cells)
    if not parsed:
        return ""
    header, body = parsed[0], parsed[1:]
    head_html = "".join(
        f'<td style="background:{COLOR_RESULTS_HEADER}; color:#FFFFFF; font-weight:700;'
        f' padding:6px 9px; border:1px solid #D5E0D8;">{_md_inline(c)}</td>'
        for c in header
    )
    body_html = ""
    for i, cells in enumerate(body):
        bg = COLOR_RESULTS_ROW_SHADE if i % 2 == 0 else "#FFFFFF"
        body_html += "<tr>" + "".join(
            f'<td style="background:{bg}; padding:6px 9px; border:1px solid #D5E0D8;">'
            f'{_md_inline(c)}</td>' for c in cells
        ) + "</tr>"
    return (
        '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse; width:100%;'
        ' margin:10px 0; font-size:11pt;">'
        f'<tr>{head_html}</tr>{body_html}</table>'
    )


def _markdown_to_html(md: str) -> str:
    """Convert the model's markdown analysis to the simple HTML subset Google
    Drive converts cleanly into a native Doc (headings, paragraphs, lists,
    tables). Not a general markdown engine — just what the prompt emits."""
    lines = str(md or "").replace("\r\n", "\n").split("\n")
    parts: List[str] = []
    para: List[str] = []
    items: List[str] = []
    list_tag = ""
    table: List[str] = []

    def flush_para():
        if para:
            parts.append(f'<p style="margin:8px 0; line-height:1.5;">{_md_inline(" ".join(para))}</p>')
            para.clear()

    def flush_list():
        nonlocal list_tag
        if items:
            body = "".join(f'<li style="margin:3px 0; line-height:1.45;">{_md_inline(i)}</li>' for i in items)
            parts.append(f'<{list_tag} style="margin:8px 0 8px 22px; padding:0;">{body}</{list_tag}>')
            items.clear()
            list_tag = ""

    def flush_table():
        if table:
            parts.append(_md_table_to_html(table))
            table.clear()

    def flush_all():
        flush_para()
        flush_list()
        flush_table()

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()

        if stripped.startswith("|") and stripped.count("|") >= 2:
            flush_para()
            flush_list()
            table.append(stripped)
            continue
        flush_table()

        if not stripped:
            flush_para()
            flush_list()
            continue

        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            flush_all()
            # The Doc's own title is the h1, so the prompt's "## Section"
            # headings stay h2 rather than being demoted a level.
            level = min(max(len(heading.group(1)), 2), 4)
            size = {2: "15pt", 3: "13pt", 4: "12pt"}[level]
            parts.append(
                f'<h{level} style="color:{COLOR_RESULTS_HEADER}; font-size:{size};'
                f' margin:16px 0 6px 0;">{_md_inline(heading.group(2).strip())}</h{level}>'
            )
            continue

        bullet = re.match(r"^[-*•]\s+(.*)$", stripped)
        if bullet:
            flush_para()
            if list_tag and list_tag != "ul":
                flush_list()
            list_tag = "ul"
            items.append(bullet.group(1).strip())
            continue

        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if numbered:
            flush_para()
            if list_tag and list_tag != "ol":
                flush_list()
            list_tag = "ol"
            items.append(numbered.group(1).strip())
            continue

        if re.fullmatch(r"[-*_]{3,}", stripped):
            flush_all()
            parts.append('<hr style="border:0; border-top:1px solid #D5E0D8; margin:14px 0;">')
            continue

        flush_list()
        para.append(stripped)

    flush_all()
    return "".join(parts)


def build_analysis_doc_html(
    ticker: str,
    analysis: dict,
    asx_url: str = "",
) -> str:
    """Build the HTML body Drive converts into the native Google Doc.

    On a parse failure the raw model text is preserved verbatim so nothing is
    lost — the Doc is the place the unusable output goes, and the email block
    is flagged ANALYSIS FAILED rather than looking clean.
    """
    analysis = analysis or {}
    status = _results_status(analysis)
    esc = htmlmod.escape
    title = _results_title(ticker, analysis)
    context = _results_context_line(analysis)

    parts: List[str] = [
        f'<h1 style="color:{COLOR_RESULTS_HEADER}; font-size:19pt; margin:0 0 2px 0;">{esc(title)}</h1>',
        f'<p style="color:{COLOR_RESULTS_MUTED}; font-size:10pt; margin:0 0 14px 0;">'
        f'{esc(context)} &nbsp;|&nbsp; Generated by {esc(BOB_NAME)} {esc(VERSION_LABEL)} '
        f'on {today_sgt_date().isoformat()} (SGT)</p>',
    ]

    if status in (RESULTS_STATUS_FAILED, RESULTS_STATUS_PARSE_ERROR):
        parts.append(
            f'<p style="background:#FEE2E2; border:1px solid {COLOR_FAILURE}; color:{COLOR_FAILURE};'
            f' padding:10px 12px; font-weight:700;">{esc(RESULTS_FAILURE_BADGE)}: '
            f'{esc(_results_summary_text(analysis))}</p>'
        )
    elif status == RESULTS_STATUS_SKIPPED:
        parts.append(
            f'<p style="background:{COLOR_RESULTS_ROW_SHADE}; padding:10px 12px;">'
            f'{esc(RESULTS_SKIP_MESSAGE)}</p>'
        )

    if isinstance(analysis.get("metrics"), dict):
        rows_html = ""
        for i, (key, label) in enumerate(RESULTS_METRIC_ROWS):
            metric = _results_metric(analysis, key, label)
            bg = COLOR_RESULTS_ROW_SHADE if i % 2 == 0 else "#FFFFFF"
            sub = f' <span style="color:{COLOR_RESULTS_MUTED}; font-size:9pt;">({esc(metric["sub"])})</span>' if metric["sub"] else ""
            rows_html += (
                f'<tr>'
                f'<td style="background:{bg}; padding:7px 10px; border:1px solid #D5E0D8;">{esc(label)}{sub}</td>'
                f'<td style="background:{bg}; padding:7px 10px; border:1px solid #D5E0D8; text-align:right;'
                f' font-weight:700;">{esc(metric["value"])}</td>'
                f'<td style="background:{bg}; padding:7px 10px; border:1px solid #D5E0D8; text-align:right;'
                f' color:{_change_colour(metric["change"])}; font-weight:700;">{esc(metric["change"])}</td>'
                f'</tr>'
            )
        parts.append(
            '<table cellpadding="0" cellspacing="0" style="border-collapse:collapse; width:100%;'
            ' margin:10px 0 16px 0; font-size:11pt;">'
            f'<tr>'
            f'<td style="background:{COLOR_RESULTS_HEADER}; color:#FFFFFF; font-weight:700; padding:7px 10px; border:1px solid #D5E0D8;">Metric</td>'
            f'<td style="background:{COLOR_RESULTS_HEADER}; color:#FFFFFF; font-weight:700; padding:7px 10px; border:1px solid #D5E0D8; text-align:right;">Value</td>'
            f'<td style="background:{COLOR_RESULTS_HEADER}; color:#FFFFFF; font-weight:700; padding:7px 10px; border:1px solid #D5E0D8; text-align:right;">vs pcp</td>'
            f'</tr>{rows_html}</table>'
        )

    summary = _results_summary_text(analysis)
    if summary:
        parts.append(f'<h2 style="color:{COLOR_RESULTS_HEADER}; font-size:15pt; margin:16px 0 6px 0;">Summary</h2>')
        parts.append(f'<p style="margin:8px 0; line-height:1.5;">{esc(summary)}</p>')

    full_analysis = str(analysis.get("full_analysis") or "").strip()
    if full_analysis:
        parts.append(_markdown_to_html(full_analysis))

    raw_text = str(analysis.get("_raw_text") or "").strip()
    if raw_text:
        parts.append(
            f'<h2 style="color:{COLOR_FAILURE}; font-size:15pt; margin:16px 0 6px 0;">'
            'Raw model output (unparseable)</h2>'
            f'<p style="color:{COLOR_RESULTS_MUTED}; font-size:10pt;">The model did not return valid JSON. '
            'Its full response is preserved verbatim below.</p>'
            f'<pre style="white-space:pre-wrap; font-size:10pt; background:{COLOR_RESULTS_ROW_SHADE};'
            f' padding:10px; border:1px solid #D5E0D8;">{esc(raw_text)}</pre>'
        )

    if asx_url:
        parts.append(
            f'<p style="margin:16px 0 0 0; font-size:10pt;">Source announcement (ASX): '
            f'<a href="{esc(asx_url)}">{esc(asx_url)}</a></p>'
        )

    return (
        '<html><head><meta charset="utf-8">'
        f'<title>{esc(title)}</title></head>'
        '<body style="font-family:Arial, sans-serif; font-size:11pt; color:#111827;">'
        + "".join(parts) +
        "</body></html>"
    )


def generate_analysis_pdf(
    ticker: str,
    period: str,
    body_html: str,
) -> Optional[Path]:
    """Generate a professional PDF from the analysis HTML and return its file path.

    Returns None if weasyprint is not installed; raises on actual errors.
    The PDF is saved to a temporary file and should be attached to the email.
    """
    if HTML is None:
        log(f"[PDF] weasyprint not installed — skipping PDF generation for {ticker}")
        return None

    try:
        # CSS for professional, mobile-friendly PDF rendering
        css_string = """
        html, body {
            width: 100%;
            height: 100%;
        }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            font-size: 11pt;
            line-height: 1.6;
            color: #1f2937;
            margin: 0;
            padding: 28pt;
            max-width: 800px;
        }
        h1 {
            font-size: 20pt;
            color: #1e3a8a;
            margin: 0 0 6pt 0;
            font-weight: 700;
            line-height: 1.3;
        }
        h2 {
            font-size: 13pt;
            color: #1e3a8a;
            margin: 14pt 0 6pt 0;
            font-weight: 700;
            line-height: 1.3;
        }
        h3 {
            font-size: 11pt;
            color: #1e3a8a;
            margin: 10pt 0 4pt 0;
            font-weight: 700;
        }
        h4 {
            font-size: 10pt;
            color: #374151;
            margin: 8pt 0 3pt 0;
            font-weight: 700;
        }
        p {
            margin: 6pt 0;
            line-height: 1.6;
            text-align: justify;
        }
        table {
            border-collapse: collapse;
            width: 100%;
            margin: 10pt 0;
            font-size: 9pt;
        }
        table td, table th {
            border: 1px solid #d1d5db;
            padding: 5pt 6pt;
        }
        table th {
            background-color: #1e3a8a;
            color: white;
            font-weight: 700;
        }
        table tr:nth-child(even) {
            background-color: #f3f4f6;
        }
        ul, ol {
            margin: 6pt 0;
            padding-left: 20pt;
            line-height: 1.6;
        }
        li {
            margin: 2pt 0;
        }
        code {
            background-color: #f3f4f6;
            padding: 1pt 3pt;
            font-family: 'Courier New', monospace;
            font-size: 9pt;
            word-break: break-word;
        }
        pre {
            background-color: #f3f4f6;
            padding: 8pt;
            border: 1px solid #d1d5db;
            font-family: 'Courier New', monospace;
            font-size: 8pt;
            white-space: pre-wrap;
            word-wrap: break-word;
            overflow-x: auto;
        }
        a {
            color: #1e3a8a;
            text-decoration: none;
        }
        a:hover {
            text-decoration: underline;
        }
        """

        # Create HTML document with embedded CSS
        full_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>{css_string}</style>
</head>
<body>
{body_html}
</body>
</html>"""

        # Generate PDF to temporary file
        temp_pdf = Path(tempfile.gettempdir()) / f"{ticker}_{period}_{today_sgt_date().isoformat()}.pdf"
        HTML(string=full_html).write_pdf(str(temp_pdf))

        log(f"[PDF] Generated {ticker} analysis PDF: {temp_pdf} ({temp_pdf.stat().st_size} bytes)")
        return temp_pdf

    except Exception as e:
        log(f"[PDF] Error generating PDF for {ticker}: {type(e).__name__}: {e}")
        return None


def create_analysis_doc(
    ticker: str,
    period: str,
    body_html: str,
    folder_id: str,
) -> str:
    """Create a native Google Doc from *body_html* and return its webViewLink.

    Uploading HTML with mimeType application/vnd.google-apps.document makes
    Drive convert it to a real Doc, which reflows on a phone — a PDF or docx
    would not. Returns "" if Drive isn't configured; raises only on a genuine
    API error (callers treat Doc creation as non-fatal).

    Uses OAuth-preferred auth (see drive_service): with OAuth the file is
    owned by the human user and lives on the user's Drive quota, so no
    per-file permission grant is needed and the webViewLink resolves for the
    owner immediately.
    """
    if not folder_id:
        return ""
    from googleapiclient.http import MediaInMemoryUpload

    service = drive_service()
    name = f"{ticker} {period or 'results'} analysis {today_sgt_date().isoformat()}"
    metadata = {
        "name": name,
        "parents": [folder_id],
        "mimeType": "application/vnd.google-apps.document",
    }
    media = MediaInMemoryUpload(body_html.encode("utf-8"), mimetype="text/html", resumable=False)
    created = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,webViewLink",
        # Works for both My Drive (with OAuth) and Shared Drives — safe to
        # always pass. Without it the v3 API silently refuses to touch
        # anything outside plain My Drive.
        supportsAllDrives=True,
    ).execute()
    file_id = created.get("id") or ""
    if not file_id:
        return ""
    link = created.get("webViewLink") or f"https://docs.google.com/document/d/{file_id}/view"
    log(f"Analysis Doc created for {ticker}: {link}")
    return link


def deep_acquisition_memo(
    ticker: str, title: str, text: str, counters: Dict, pdf_path: Optional[Path] = None,
) -> dict:
    if pdf_path and pdf_path.exists():
        user = f"Ticker: {ticker}\nTitle: {title}\n\nPlease analyse the attached acquisition announcement PDF."
        out = llm_chat(ACQUISITION_PROMPT, user, counters, pdf_path=pdf_path, fallback_text=text)
    elif is_meaningful_text(text, min_chars=900):
        user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
        out = llm_chat(ACQUISITION_PROMPT, user, counters)
    else:
        return {"deal_summary": "Could not extract meaningful announcement text. Open the link and review manually."}
    if out == LLM_SKIPPED:
        return _skip_marker("deal_summary")
    if out == LLM_FAILED:
        return _failed_marker("deal_summary", counters)
    parsed = _parse_analysis_json(out)
    return parsed if parsed else {"deal_summary": out}


def deep_capital_memo(
    ticker: str, title: str, text: str, counters: Dict, pdf_path: Optional[Path] = None,
) -> dict:
    if pdf_path and pdf_path.exists():
        user = f"Ticker: {ticker}\nTitle: {title}\n\nPlease analyse the attached capital/debt raise announcement PDF."
        out = llm_chat(CAPITAL_OR_DEBT_RAISE_PROMPT, user, counters, pdf_path=pdf_path, fallback_text=text)
    elif is_meaningful_text(text, min_chars=900):
        user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
        out = llm_chat(CAPITAL_OR_DEBT_RAISE_PROMPT, user, counters)
    else:
        return {"what_happened": "Could not extract meaningful announcement text. Open the link and review manually."}
    if out == LLM_SKIPPED:
        return _skip_marker("what_happened")
    if out == LLM_FAILED:
        return _failed_marker("what_happened", counters)
    parsed = _parse_analysis_json(out)
    return parsed if parsed else {"what_happened": out}


def deep_trading_update_memo(
    ticker: str, title: str, text: str, counters: Dict, pdf_path: Optional[Path] = None,
) -> dict:
    if pdf_path and pdf_path.exists():
        user = f"Ticker: {ticker}\nTitle: {title}\n\nPlease analyse the attached trading update / guidance announcement PDF."
        out = llm_chat(TRADING_UPDATE_PROMPT, user, counters, pdf_path=pdf_path, fallback_text=text)
    elif is_meaningful_text(text, min_chars=600):
        user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
        out = llm_chat(TRADING_UPDATE_PROMPT, user, counters)
    else:
        return {"what_they_said": "Could not extract meaningful announcement text. Open the link and review manually."}
    if out == LLM_SKIPPED:
        return _skip_marker("what_they_said")
    if out == LLM_FAILED:
        return _failed_marker("what_they_said", counters)
    parsed = _parse_analysis_json(out)
    return parsed if parsed else {"what_they_said": out}


def deep_price_sensitive_memo(
    ticker: str, title: str, text: str, counters: Dict, pdf_path: Optional[Path] = None,
) -> dict:
    if pdf_path and pdf_path.exists():
        user = f"Ticker: {ticker}\nTitle: {title}\n\nPlease analyse the attached ASX price-sensitive announcement PDF."
        out = llm_chat(PRICE_SENSITIVE_PROMPT, user, counters, pdf_path=pdf_path, fallback_text=text)
    elif is_meaningful_text(text, min_chars=600):
        user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
        out = llm_chat(PRICE_SENSITIVE_PROMPT, user, counters)
    else:
        return {"what_happened": "Could not extract meaningful announcement text. Open the link and review manually."}
    if out == LLM_SKIPPED:
        return _skip_marker("what_happened")
    if out == LLM_FAILED:
        return _failed_marker("what_happened", counters)
    parsed = _parse_analysis_json(out)
    return parsed if parsed else {"what_happened": out}


def deep_results_analysis(
    ticker: str,
    report_text: str,
    deck_text: str,
    counters: Dict,
    report_pdf: Optional[Path] = None,
    deck_pdf: Optional[Path] = None,
) -> dict:
    has_pdf = (report_pdf and report_pdf.exists()) or (deck_pdf and deck_pdf.exists())
    if has_pdf:
        # Send BOTH the financial report and the investor deck as native PDF
        # document blocks (not extracted text) so table/chart fidelity is
        # preserved for both — the deck is often the more table-heavy of
        # the two. Each falls back independently to its extracted text if
        # it's individually malformed/too large, or if attaching both would
        # exceed Claude's combined per-request PDF budget.
        attachments: List[PdfAttachment] = []
        if report_pdf and report_pdf.exists():
            try:
                attachments.append(PdfAttachment(
                    name=f"{report_pdf.name} (financial report)",
                    pdf_bytes=report_pdf.read_bytes(),
                    fallback_text=report_text,
                ))
            except Exception as e:
                log(f"Could not read report PDF {report_pdf}: {e}")
        if deck_pdf and deck_pdf.exists() and deck_pdf != report_pdf:
            try:
                attachments.append(PdfAttachment(
                    name=f"{deck_pdf.name} (investor presentation)",
                    pdf_bytes=deck_pdf.read_bytes(),
                    fallback_text=deck_text,
                ))
            except Exception as e:
                log(f"Could not read deck PDF {deck_pdf}: {e}")
        user = f"Ticker: {ticker}\n\nPlease analyse the attached financial results PDF(s) as the primary source."
        out = llm_chat_with_pdfs(
            RESULTS_HYFY_PROMPT, user, counters, attachments,
            max_tokens=RESULTS_MAX_TOKENS,
        )
    elif is_meaningful_text(report_text, min_chars=MIN_RESULTS_TEXT_CHARS) or is_meaningful_text(deck_text, min_chars=MIN_RESULTS_TEXT_CHARS):
        # Text-only fallback: no PDF could be attached natively, so table
        # fidelity is degraded. Tell the model so it can be conservative
        # (n/a beats a misread number) rather than guessing off broken tables.
        user = (
            f"Ticker: {ticker}\n\n"
            "NOTE: no PDF could be attached — the text below was extracted with pypdf, "
            "so table layout may be mangled. Prefer \"n/a\" over any figure you cannot "
            "read with confidence, and flag the lower confidence in the summary.\n\n"
            f"=== OFFICIAL REPORT TEXT ===\n{report_text}\n\n"
            f"=== INVESTOR DECK TEXT ===\n{deck_text}\n"
        )
        out = llm_chat(RESULTS_HYFY_PROMPT, user, counters, max_tokens=RESULTS_MAX_TOKENS)
    else:
        return {
            "executive_summary": ["No meaningful report text or PDF available for analysis."],
            "summary": "No meaningful report text or PDF available for analysis. Open the announcement manually.",
            "_status": RESULTS_STATUS_NO_CONTENT,
        }

    if out == LLM_SKIPPED:
        marker = _skip_marker("executive_summary", as_list=True)
        marker["summary"] = RESULTS_SKIP_MESSAGE
        marker["_status"] = RESULTS_STATUS_SKIPPED
        return marker

    if out == LLM_FAILED:
        marker = _failed_marker("executive_summary", counters, as_list=True)
        marker["summary"] = marker["executive_summary"][0]
        marker["_status"] = RESULTS_STATUS_FAILED
        return marker

    parsed = _parse_analysis_json(out)
    if not parsed:
        # The model answered, but not with parseable JSON. Never dress this up
        # as a clean (if terse) analysis: flag it loudly and keep the raw text
        # so it can be written to the Doc for manual reading.
        #
        # Truncation at max_tokens is by far the most common cause on large
        # reporters (BHP, CSL) — the JSON ends mid-string somewhere inside
        # full_analysis and no bracket-balancing can save it. Naming the
        # cause in the failure text is the difference between "raise the
        # budget" and "read the raw output to find out what's wrong".
        stop_reason = (counters.get("last_stop_reason") or "").lower()
        if stop_reason == "max_tokens":
            detail = (
                f"model output was truncated at max_tokens="
                f"{RESULTS_MAX_TOKENS} — raise CLAUDE_RESULTS_MAX_TOKENS"
            )
        else:
            detail = "model returned unparseable output (not valid JSON)"
        log(
            f"ERROR: results JSON parse failed for {ticker} — "
            f"{len(out)} chars, stop_reason={stop_reason or 'unknown'}"
        )
        return {
            "executive_summary": [f"{LLM_FAILURE_FLAG} ({detail})."],
            "summary": f"{LLM_FAILURE_FLAG} ({detail}). Raw model output saved to the analysis Doc.",
            "_status": RESULTS_STATUS_PARSE_ERROR,
            "_raw_text": out,
        }

    parsed["_status"] = RESULTS_STATUS_OK
    return parsed


def drive_service():
    """Build a Google Drive API service, preferring OAuth over service-account.

    History that motivated this: service accounts have NO Drive storage
    quota of their own. Writing into a plain "My Drive" folder — even one
    that has been *shared with* the service account as Editor — returns
    HTTP 403 ``Service Accounts do not have storage quota``, because
    Google needs to charge every new file against some owner's quota. That
    is exactly what silently killed every Bob upload for weeks: the
    try/except in ``main()`` swallowed the 403 and the digest looked fine.

    So this function prefers OAuth2 user credentials (client_id +
    client_secret + refresh_token). Authenticated as the human user, every
    file created is owned by *them* and lives on *their* quota — no Shared
    Drive required, works on plain personal Gmail. This is the same
    pattern Sunday Sally already uses (sunday-sally/src/main.py).

    Falls back to the service account only if OAuth secrets aren't set,
    with a loud warning so nobody mistakes it for a working configuration.
    """
    from googleapiclient.discovery import build

    client_id = os.environ.get("GDRIVE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN", "").strip()
    if client_id and client_secret and refresh_token:
        from google.oauth2.credentials import Credentials as UserCredentials
        creds = UserCredentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        return build("drive", "v3", credentials=creds, cache_discovery=False)

    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        raise RuntimeError(
            "No Drive credentials set: need either OAuth2 "
            "(GDRIVE_CLIENT_ID/SECRET/REFRESH_TOKEN) or a service account "
            "(GDRIVE_SERVICE_ACCOUNT_JSON)"
        )
    log(
        "WARNING: Drive auth is using a service account. Uploads to a plain "
        "My Drive folder will fail with 403 'Service Accounts do not have "
        "storage quota'. Set GDRIVE_CLIENT_ID/SECRET/REFRESH_TOKEN to use "
        "OAuth against the folder's owner instead."
    )
    from google.oauth2.service_account import Credentials as SACredentials
    info = json.loads(sa_json)
    creds = SACredentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def likely_results_bundle_items(items_for_ticker: List[Dict]) -> List[Dict]:
    return [it for it in items_for_ticker if looks_like_results_title(it["title"])]


def pick_report_and_deck_text(downloaded_texts: List[Tuple[str, str]]) -> Tuple[str, str]:
    pres = [(t, x) for (t, x) in downloaded_texts if "presentation" in t.lower() or "deck" in t.lower()]
    non_pres = [(t, x) for (t, x) in downloaded_texts if (t, x) not in pres]
    deck = max(pres, key=lambda tx: len(tx[1] or ""), default=("", ""))[1] if pres else ""
    report = max(non_pres, key=lambda tx: len(tx[1] or ""), default=("", ""))[1] if non_pres else ""
    if not report and downloaded_texts:
        report = max(downloaded_texts, key=lambda tx: len(tx[1] or ""))[1]
    if not deck and downloaded_texts:
        deck = min(downloaded_texts, key=lambda tx: len(tx[1] or ""))[1]
    return report, deck


def pick_report_and_deck_pdfs(
    bundle: List[Dict], pdf_map: Dict[str, Path],
) -> Tuple[Optional[Path], Optional[Path]]:
    pres_pdfs = []
    report_pdfs = []
    for item in bundle:
        url = item["url"]
        p = pdf_map.get(url)
        if p and p.exists():
            title_low = item["title"].lower()
            if "presentation" in title_low or "deck" in title_low:
                pres_pdfs.append(p)
            else:
                report_pdfs.append(p)
    report_pdf = report_pdfs[0] if report_pdfs else (pres_pdfs[0] if pres_pdfs else None)
    deck_pdf = pres_pdfs[0] if pres_pdfs else None
    return report_pdf, deck_pdf


def _emit_bob_dashboard_json(
    high_impact: List[dict],
    material: List[dict],
    fyi: List[dict],
    silence: bool,
) -> None:
    out_path = Path(__file__).resolve().parent / "docs" / "data" / "bob.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "last_run": today_sgt_date().isoformat(),
        "silence": silence,
        "high_impact": high_impact,
        "material": material,
        "fyi": fyi,
    }
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"[bob] Dashboard JSON written → {out_path}")


def main():
    subject = f"{BOB_NAME} {VERSION_LABEL} — Daily Announcements Digest — {today_sgt_date().isoformat()} (SGT)"
    session = http_session()
    asx_tickers, _lse_tickers = read_tickers()
    drive_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()

    # FORCE_RERUN_TICKERS lets a manual run re-process a ticker's announcements
    # even if they're already recorded in seen_state — e.g. to verify a fix
    # against a known past result-day PDF without clearing the whole state
    # file. Forced tickers also skip the normal HOURS_BACK fetch window so a
    # PDF from days/weeks ago can still be picked up.
    force_rerun_tickers = frozenset(
        t.strip().upper()
        for t in os.environ.get("FORCE_RERUN_TICKERS", "").split(",")
        if t.strip()
    )
    if force_rerun_tickers:
        log(f"FORCE_RERUN_TICKERS active: {sorted(force_rerun_tickers)}")

    # MANUAL_TICKER allows one-off analysis of a ticker not in the portfolio
    manual_ticker = os.environ.get("MANUAL_TICKER", "").strip().upper()
    if manual_ticker:
        log(f"MANUAL_TICKER: analyzing {manual_ticker} (one-off, not saved to state)")
        # Add to asx_tickers for processing, but mark it so we don't save to seen state
        asx_tickers = list(asx_tickers) + [manual_ticker]

    # Add force_rerun_tickers that aren't already in the portfolio
    asx_ticker_set = set(asx_tickers)
    for t in force_rerun_tickers:
        if t not in asx_ticker_set:
            asx_tickers = list(asx_tickers) + [t]
            asx_ticker_set.add(t)

    counters = {
        "MAX_LLM_CALLS_PER_RUN": MAX_LLM_CALLS_PER_RUN,
        "llm_calls": 0,
        "MAX_PDFS_PER_RUN": MAX_PDFS_PER_RUN,
        "pdfs_downloaded": 0,
    }

    seen_state = prune_seen_state(load_seen_state(SEEN_STATE_PATH), SEEN_STATE_RETENTION_HOURS)
    seen_state_updated = dict(seen_state)
    run_seen_count = 0

    high_impact_blocks: List[str] = []
    material_blocks: List[str] = []
    fyi_blocks: List[str] = []
    brother_blocks: List[str] = []

    # Structured items for docs/data/bob.json dashboard
    high_impact_items: List[dict] = []
    material_items: List[dict] = []
    fyi_items: List[dict] = []

    from_date = cutoff_dt_sgt(HOURS_BACK).date()
    by_ticker: Dict[str, List[Dict]] = {}
    for t in asx_tickers:
        try:
            # Forced tickers get the full ASX history window (no from_date
            # filter) so a past result-day PDF can be found regardless of
            # how long ago it was released.
            fetch_from = None if t in force_rerun_tickers else from_date
            by_ticker[t] = fetch_asx_announcements_html(session, t, from_date=fetch_from)
        except Exception as e:
            log(f"Fetch failed for {t}: {e}")
            by_ticker[t] = []

    processed_results: set = set()

    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)

        for ticker, items in by_ticker.items():
            if not items:
                continue

            fresh_items: List[Dict] = []
            for it in items:
                key = announcement_key(ticker, it["url"])
                if key in seen_state and ticker not in force_rerun_tickers:
                    continue
                it["seen_key"] = key
                fresh_items.append(it)

            if not fresh_items:
                continue

            # FYI: every fresh announcement gets a headline entry
            for it in fresh_items:
                title = it["title"]
                url = it["url"]
                fyi_entry = f"{summarise_headline_two_lines(ticker, title)}\nOpen: {url}\n"
                fyi_blocks.append(fyi_entry)
                fyi_items.append({"ticker": ticker, "title": title[:200], "url": url})
                if ticker == "AR9":
                    brother_blocks.append(fyi_entry)
                key = it.get("seen_key")
                if key:
                    seen_state_updated[key] = now_sgt().isoformat(timespec="seconds")
                    run_seen_count += 1

            # RESULTS bundle
            if any(looks_like_results_title(i["title"]) for i in fresh_items) and ticker not in processed_results:
                processed_results.add(ticker)
                bundle = likely_results_bundle_items(fresh_items)
                downloaded_texts: List[Tuple[str, str]] = []
                pdf_map: Dict[str, Path] = {}
                any_results_link = bundle[0]["url"] if bundle else fresh_items[0]["url"]

                # Raw PDFs are no longer copied to Drive — the ASX link in the
                # email points at the same PDF and the Google Doc is the
                # actual archive. See CLAUDE.md for the rationale.
                for b in bundle:
                    title = b["title"]
                    url = b["url"]
                    pdf_url = asx_pdf_url_from_item_url(url)
                    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                    pdf_path = tmpdir / f"{safe_name}.pdf"
                    text, got_pdf = fetch_announcement_text(session, url, pdf_url, pdf_path, counters)
                    downloaded_texts.append((title, text))
                    if got_pdf:
                        pdf_map[url] = pdf_path

                report_text, deck_text = pick_report_and_deck_text(downloaded_texts)
                report_pdf, deck_pdf = pick_report_and_deck_pdfs(bundle, pdf_map)
                report_text = (report_text or "")[:120_000]
                deck_text = (deck_text or "")[:120_000]

                has_pdf = (report_pdf and report_pdf.exists()) or (deck_pdf and deck_pdf.exists())
                has_text = (
                    is_meaningful_text(report_text, min_chars=MIN_RESULTS_TEXT_CHARS)
                    or is_meaningful_text(deck_text, min_chars=MIN_RESULTS_TEXT_CHARS)
                )

                if not has_pdf and not has_text:
                    block = (
                        f"{ticker} — Results detected, but Bob couldn't extract meaningful content.\n"
                        f"Open manually: {any_results_link}\n"
                    )
                    high_impact_blocks.append(block)
                    high_impact_items.append({
                        "ticker": ticker, "title": "Results (HY/FY)", "url": any_results_link, "type": "results",
                    })
                    if ticker == "AR9":
                        brother_blocks.append(block)
                    continue

                analysis = deep_results_analysis(
                    ticker, report_text, deck_text, counters,
                    report_pdf=report_pdf, deck_pdf=deck_pdf,
                )

                # The long-form analysis leaves the email and goes to a native
                # Generate a professional PDF of the full analysis and attach it to
                # the email. This avoids Google Drive auth issues and gives the user
                # a clean, downloadable file. PDF generation is non-fatal — if
                # weasyprint isn't available, the email still ships without it.
                analysis_pdf_path = None
                try:
                    analysis_pdf_path = generate_analysis_pdf(
                        ticker,
                        _results_period_label(analysis),
                        build_analysis_doc_html(ticker, analysis, any_results_link),
                    )
                except Exception as e:
                    log(f"ERROR: analysis PDF generation failed for {ticker}: {type(e).__name__}: {e}")

                block = build_results_block(
                    ticker=ticker,
                    analysis=analysis,
                    asx_url=any_results_link,
                    doc_link="",
                    doc_error="",
                )
                high_impact_blocks.append(block)
                high_impact_items.append({
                    "ticker": ticker, "title": bundle[0]["title"] if bundle else "Results (HY/FY)",
                    "url": any_results_link, "type": "results", "analysis": analysis,
                    "doc_link": "", "doc_error": "", "pdf_path": str(analysis_pdf_path) if analysis_pdf_path else None,
                })
                if ticker == "AR9":
                    brother_blocks.append(block)
                continue

            # Per-item MATERIAL / HIGH IMPACT
            for it in fresh_items:
                title = it["title"]
                url = it["url"]
                asx_flagged = it.get("price_sensitive", False)
                is_price = is_price_sensitive_title(title) or asx_flagged
                cls_title = classify_from_title_only(title)

                if not is_price:
                    continue

                if cls_title in ("ACQUISITION", "CAPITAL_OR_DEBT_RAISE", "TRADING_UPDATE"):
                    pdf_url = asx_pdf_url_from_item_url(url)
                    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                    pdf_path = tmpdir / f"{safe_name}.pdf"
                    text, got_pdf = fetch_announcement_text(session, url, pdf_url, pdf_path, counters)

                    if looks_like_asx_access_gate(text):
                        block = f"{ticker}: {title[:160]}\nSo what: could not fetch PDF automatically. Open: {url}\n"
                        material_blocks.append(block)
                        material_items.append({"ticker": ticker, "title": title[:200], "url": url})
                        if ticker == "AR9":
                            brother_blocks.append(block)
                        if pdf_path.exists():
                            try:
                                pdf_path.unlink()
                            except Exception:
                                pass
                        continue

                    current_pdf = pdf_path if got_pdf else None
                    if cls_title == "ACQUISITION":
                        memo = deep_acquisition_memo(ticker, title, text, counters, pdf_path=current_pdf)
                        heading = "Acquisition"
                        item_type = "acquisition"
                        item_kind = "acquisition"
                    elif cls_title == "TRADING_UPDATE":
                        memo = deep_trading_update_memo(ticker, title, text, counters, pdf_path=current_pdf)
                        heading = "Trading Update / Guidance"
                        item_type = "trading_update"
                        item_kind = "trading_update"
                    else:
                        memo = deep_capital_memo(ticker, title, text, counters, pdf_path=current_pdf)
                        heading = "Capital/Debt Raise"
                        item_type = "capital"
                        item_kind = "capital"
                    block = build_high_impact_block(
                        ticker=ticker,
                        heading=heading,
                        memo=memo,
                        url=url,
                        analysed_via_pdf=got_pdf,
                        kind=item_kind,
                        failed=_is_llm_failure(memo),
                    )
                    if pdf_path.exists():
                        try:
                            pdf_path.unlink()
                        except Exception:
                            pass
                    high_impact_blocks.append(block)
                    high_impact_items.append({
                        "ticker": ticker, "title": title[:200], "url": url,
                        "type": item_type, "analysis": memo,
                    })
                    if ticker == "AR9":
                        brother_blocks.append(block)
                    continue

                # ASX-flagged price sensitive items that aren't already handled by a
                # specific deep-analysis category get the full HIGH IMPACT treatment.
                if asx_flagged and cls_title not in ("ACQUISITION", "CAPITAL_OR_DEBT_RAISE", "TRADING_UPDATE"):
                    pdf_url = asx_pdf_url_from_item_url(url)
                    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                    pdf_path = tmpdir / f"{safe_name}.pdf"
                    text, got_pdf = fetch_announcement_text(session, url, pdf_url, pdf_path, counters)

                    if looks_like_asx_access_gate(text):
                        block = f"{ticker}: {title[:160]}\nSo what: could not fetch PDF automatically. Open: {url}\n"
                        material_blocks.append(block)
                        material_items.append({"ticker": ticker, "title": title[:200], "url": url})
                        if ticker == "AR9":
                            brother_blocks.append(block)
                        if pdf_path.exists():
                            try:
                                pdf_path.unlink()
                            except Exception:
                                pass
                        continue

                    current_pdf = pdf_path if got_pdf else None
                    memo = deep_price_sensitive_memo(ticker, title, text, counters, pdf_path=current_pdf)
                    block = build_high_impact_block(
                        ticker=ticker,
                        heading="ASX Price Sensitive",
                        memo=memo,
                        url=url,
                        analysed_via_pdf=got_pdf,
                        kind="price_sensitive",
                        failed=_is_llm_failure(memo),
                    )
                    if pdf_path.exists():
                        try:
                            pdf_path.unlink()
                        except Exception:
                            pass
                    high_impact_blocks.append(block)
                    high_impact_items.append({
                        "ticker": ticker, "title": title[:200], "url": url,
                        "type": "price_sensitive", "analysis": memo,
                    })
                    if ticker == "AR9":
                        brother_blocks.append(block)
                    continue

                text = ""
                if cls_title == "CONTRACT_MATERIAL":
                    pdf_url = asx_pdf_url_from_item_url(url)
                    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                    pdf_path = tmpdir / f"{safe_name}.pdf"
                    text, got_pdf = fetch_announcement_text(session, url, pdf_url, pdf_path, counters)
                    if pdf_path.exists():
                        try:
                            pdf_path.unlink()
                        except Exception:
                            pass

                analysis_2l: Optional[dict] = None
                if text and is_meaningful_text(text, min_chars=600):
                    analysis_2l = summarise_two_lines_llm(ticker, title, text, counters)
                if analysis_2l:
                    summary_text = _analysis_to_email_text(analysis_2l, "default")
                else:
                    summary_text = f"{ticker}: {title[:160]}\nSo what: price-sensitive headline — open link for details."
                    analysis_2l = {
                        "what_happened": f"{ticker}: {title[:160]}",
                        "so_what": "Price-sensitive headline — open link for details.",
                    }
                block = f"{summary_text}\nOpen: {url}\n"
                material_blocks.append(block)
                material_items.append({
                    "ticker": ticker, "title": title[:200], "url": url, "analysis": analysis_2l,
                })
                if ticker == "AR9":
                    brother_blocks.append(block)

    body_text, body_html = build_email(high_impact_blocks, material_blocks, fyi_blocks)

    # Collect PDF attachments from results items
    attachments = []
    for item in high_impact_items:
        pdf_path_str = item.get("pdf_path")
        if pdf_path_str:
            pdf_path = Path(pdf_path_str)
            if pdf_path.exists():
                attachments.append(pdf_path)

    send_email(subject, body_text, body_html, attachments=attachments if attachments else None)

    brother_email = os.environ.get("BROTHER_EMAIL", "").strip()
    if brother_email and brother_blocks:
        bro_subject = f"{BOB_NAME} {VERSION_LABEL} — AR9 Digest — {today_sgt_date().isoformat()} (SGT)"
        bro_text = "\n".join([bro_subject, "", *(_block_text(b) for b in brother_blocks)])
        bro_html = "<div style='padding:18px; background:%s; color:%s; font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif;'>" % (COLOR_BG, COLOR_TEXT)
        bro_html += f"<div style='font-size:20px; font-weight:900; margin-bottom:8px;'>{htmlmod.escape(bro_subject)}</div>"
        bro_html += _html_section("AR9 ONLY", COLOR_MATERIAL, brother_blocks)
        bro_html += "</div>"
        send_email(bro_subject, bro_text, bro_html, to_addr=brother_email)

    _emit_bob_dashboard_json(
        high_impact=high_impact_items,
        material=material_items,
        fyi=fyi_items,
        silence=not (high_impact_blocks or material_blocks or fyi_blocks),
    )

    save_seen_state(SEEN_STATE_PATH, prune_seen_state(seen_state_updated, SEEN_STATE_RETENTION_HOURS))
    log(f"Seen-state updated with {run_seen_count} new announcement(s).")
    log("Email sent.")


if __name__ == "__main__":
    main()
