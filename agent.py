# agent.py
#
# Investor-grade ASX announcements agent ("Bob the Bot"):
# - Includes ALL announcements in an FYI section (headline-only, 2 lines + link)
# - Uses Requests first for PDFs; if ASX consent gate blocks, falls back to Playwright
# - Runs deep analysis only for HY/FY results, acquisitions, and capital/debt raises
# - Produces clean email: HIGH IMPACT + MATERIAL + FYI or SILENCE
# - Uploads PDFs to Google Drive for big announcements and includes Drive view links
# - Generates a separate Strawman-ready post (<= ~500 words) for big announcements
# - Never hallucinates off the ASX "Access to this site" legal page
# - Optionally emails AR9 items to your brother (BROTHER_EMAIL secret)

import argparse
import os
import re
import sys
import json
import ssl
import base64
import hashlib
import smtplib
import tempfile
import datetime as dt
import html as htmlmod
from pathlib import Path
from email.message import EmailMessage
from typing import Dict, List, Tuple, Optional

import requests
from bs4 import BeautifulSoup
import yaml
from pypdf import PdfReader
import anthropic

import asyncio
from playwright_fetch import fetch_pdf_with_playwright  # must exist in repo root

from prompts import (
    DEFAULT_2LINE_PROMPT,
    ACQUISITION_PROMPT,
    CAPITAL_OR_DEBT_RAISE_PROMPT,
    RESULTS_HYFY_PROMPT,
    RESULTS_HYFY_PACK_PROMPT,
    STRAWMAN_500W_PROMPT,
)

BOB_NAME = "Bob the Bot"
VERSION_LABEL = "V2"

# ----------------------------
# Settings / Guardrails
# ----------------------------
HOURS_BACK = 24
SEEN_STATE_PATH = Path(os.environ.get("SEEN_STATE_PATH", "state_seen.json"))
SEEN_STATE_RETENTION_HOURS = 72

# Comma-separated list of ASX tickers that should bypass the 24 hr dedup
# check on this run (e.g. "NHC" or "NHC,BHP").  Set via the FORCE_RERUN_TICKERS
# environment variable or the matching workflow_dispatch input.
FORCE_RERUN_TICKERS: frozenset = frozenset(
    t.strip().upper()
    for t in os.environ.get("FORCE_RERUN_TICKERS", "").split(",")
    if t.strip()
)

# Force-reprocess ALL announcements (regardless of seen state).
# Set via FORCE env var or --force CLI flag.
FORCE: bool = os.environ.get("FORCE", "").strip().lower() in ("1", "true", "yes")

# ----------------------------
# Announcement status machine
# ----------------------------
STATUS_NEW = "NEW"
STATUS_PROCESSING = "PROCESSING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
# Result-day pack statuses (used for HY/FY multi-PDF processing)
STATUS_PACK_COLLECTED = "PACK_COLLECTED"
STATUS_SENT_TO_CLAUDE = "SENT_TO_CLAUDE"
STATUS_ANALYZED = "ANALYZED"

MAX_RETRIES = 3

# Keywords that make an announcement high-priority (always reprocess if not COMPLETED,
# and reprocess even if COMPLETED when --force / FORCE_RERUN_TICKERS applies).
HIGH_PRIORITY_KEYWORDS: List[str] = [
    "half year", "h1", "interim", "appendix 4d", "appendix 4e",
    "results", "earnings", "guidance",
]

MAX_ANNOUNCEMENTS_PER_TICKER = 12
MAX_PDFS_PER_RUN = 10
MAX_LLM_CALLS_PER_RUN = 15

MIN_RESULTS_TEXT_CHARS = 2500
MODEL_DEFAULT = "claude-haiku-4-5-20251001"

# Hard time caps (keep runs reasonable)
REQUESTS_PDF_TIMEOUT_SECS = 20
HTML_TIMEOUT_SECS = 30

# Result-day pack settings
RESULT_ARTIFACTS_DIR = Path(os.environ.get("RESULT_ARTIFACTS_DIR", "outputs/results"))
# Max size per individual PDF sent to Claude (bytes); larger PDFs are skipped but pack still proceeds
MAX_RESULT_PDF_BYTES = 10 * 1024 * 1024  # 10 MB

# Sentinel strings returned by deep-analysis helpers when the LLM fails or
# cannot run.  Used to detect analysis failure and mark the announcement FAILED.
_LLM_FAIL_MSGS: frozenset = frozenset([
    "LLM could not run (limit/billing).",
    "__LLM_FAILED__",
    "__LLM_SKIPPED__",
])
_TEXT_UNAVAIL = (
    "Could not extract meaningful announcement text automatically. "
    "Open the link and review manually."
)

# Email styling colours (match your diagram intent)
COLOR_HIGH_IMPACT = "#F59E0B"  # amber/gold
COLOR_MATERIAL = "#3B82F6"     # blue
COLOR_FYI = "#10B981"          # green
COLOR_BG = "#0B1220"           # dark navy
COLOR_PANEL = "#111B2E"        # panel
COLOR_TEXT = "#E5E7EB"         # light text

# ----------------------------
# Daily joke (silence mode only)
# ----------------------------
_JOKES_FILE = Path(__file__).parent / "config" / "daily_jokes.txt"


def _get_daily_joke() -> str:
    """Return a deterministic daily joke from a local file.

    The joke rotates once per calendar day using today's ordinal so no
    network access is required.  Any error returns an empty string so
    Bob never crashes because of the joke feature.
    """
    try:
        jokes = [
            line.strip()
            for line in _JOKES_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not jokes:
            return ""
        return jokes[dt.date.today().toordinal() % len(jokes)]
    except Exception:  # noqa: BLE001
        return ""


# ----------------------------
# Minimal logging
# ----------------------------
def log(msg: str):
    print(f"[agent] {msg}", flush=True)


# ----------------------------
# Time helpers (SGT = UTC+8)
# ----------------------------
def now_sgt() -> dt.datetime:
    return dt.datetime.utcnow() + dt.timedelta(hours=8)


def today_sgt_date() -> dt.date:
    return now_sgt().date()


def cutoff_dt_sgt(hours_back: int) -> dt.datetime:
    return now_sgt() - dt.timedelta(hours=hours_back)


def announcement_key(ticker: str, url: str) -> str:
    raw = f"{ticker}|{url}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def load_seen_state(path: Path) -> Dict[str, Dict]:
    """Load announcement state from disk.

    Supports both the legacy format ``{key: iso_timestamp_str}`` and the
    current format ``{key: {status, ticker, headline, retry_count, …}}``.
    Legacy entries are promoted to status=COMPLETED.
    """
    if not path.exists():
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"Could not read seen state file: {e}")
        return {}

    now_iso = now_sgt().isoformat(timespec="seconds")

    if isinstance(data, list):
        # Very old format: plain list of key strings
        return {
            k: {
                "announcement_id": k, "ticker": "", "headline": "",
                "status": STATUS_COMPLETED, "retry_count": 0,
                "last_attempt": now_iso, "error": "",
            }
            for k in data if isinstance(k, str)
        }

    if not isinstance(data, dict):
        return {}

    state: Dict[str, Dict] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, str):
            # Legacy format: {key: iso_timestamp}
            state[key] = {
                "announcement_id": key, "ticker": "", "headline": "",
                "status": STATUS_COMPLETED, "retry_count": 0,
                "last_attempt": value, "error": "",
            }
        elif isinstance(value, dict):
            state[key] = value
    return state


def prune_seen_state(state: Dict[str, Dict], retention_hours: int) -> Dict[str, Dict]:
    """Remove entries whose last_attempt is older than retention_hours."""
    cutoff = now_sgt() - dt.timedelta(hours=retention_hours)
    out: Dict[str, Dict] = {}
    for key, entry in state.items():
        last_attempt = entry.get("last_attempt", "")
        try:
            entry_dt = dt.datetime.fromisoformat(last_attempt)
        except Exception:
            continue
        if entry_dt >= cutoff:
            out[key] = entry
    return out


def save_seen_state(path: Path, state: Dict[str, Dict]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


# ----------------------------
# State-machine helpers
# ----------------------------
def is_high_priority_title(title: str) -> bool:
    """Return True if the headline matches any high-priority keyword."""
    t = title.lower()
    return any(kw in t for kw in HIGH_PRIORITY_KEYWORDS)


def should_process_item(
    key: str,
    ticker: str,
    title: str,
    state: Dict[str, Dict],
    force: bool,
    force_tickers: frozenset,
) -> Tuple[bool, str]:
    """Return (should_process, reason) for an announcement.

    Decision logic (in priority order):
    1. --force / FORCE env var → always process
    2. ticker in FORCE_RERUN_TICKERS → always process
    3. High-priority title (results, earnings, guidance…) → process if not COMPLETED
    4. COMPLETED → skip
    5. FAILED within 24 h AND retry_count < MAX_RETRIES → retry
    6. FAILED beyond 24 h or max retries → skip
    7. PROCESSING (crashed mid-run) → retry
    8. NEW / unknown → process
    """
    if force or ticker in force_tickers:
        return True, "force reprocess"

    high_pri = is_high_priority_title(title)

    if key not in state:
        return True, "new announcement"

    entry = state[key]
    status = entry.get("status", STATUS_COMPLETED)

    if status == STATUS_COMPLETED:
        if high_pri:
            return True, "high-priority: reprocessing despite COMPLETED"
        return False, "already COMPLETED"

    if status == STATUS_FAILED:
        retry_count = entry.get("retry_count", 0)
        if retry_count >= MAX_RETRIES:
            return False, f"FAILED {retry_count}x — max retries reached"
        last_attempt = entry.get("last_attempt", "")
        try:
            last_dt = dt.datetime.fromisoformat(last_attempt)
            if now_sgt() - last_dt > dt.timedelta(hours=HOURS_BACK):
                return False, "FAILED but outside 24 h retry window"
        except Exception:
            pass
        return True, f"status=FAILED → retrying (attempt {retry_count + 1}/{MAX_RETRIES})"

    if status == STATUS_PROCESSING:
        return True, "status=PROCESSING → treating as crashed, retrying"

    # STATUS_NEW or unrecognised
    return True, "new announcement"


def mark_state(
    state: Dict[str, Dict],
    key: str,
    ticker: str,
    title: str,
    status: str,
    error: str = "",
) -> None:
    """Write or update the state entry for a given announcement."""
    existing = state.get(key, {})
    retry_count = existing.get("retry_count", 0)
    if status == STATUS_FAILED:
        retry_count += 1
    state[key] = {
        "announcement_id": key,
        "ticker": ticker,
        "headline": title[:200],
        "status": status,
        "retry_count": retry_count,
        "last_attempt": now_sgt().isoformat(timespec="seconds"),
        "error": error,
    }


# ----------------------------
# Email
# ----------------------------
def send_email(subject: str, body_text: str, body_html: Optional[str] = None, to_addr: Optional[str] = None):
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

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(email_from, app_password)
        server.send_message(msg)


# ----------------------------
# Config
# ----------------------------
def read_tickers() -> Tuple[List[str], List[str]]:
    with open("tickers.yaml", "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    asx = data.get("asx", {})
    lse = data.get("lse", {})
    # Support both old list format and new {TICKER: "Company Name"} dict format
    asx_list = list(asx.keys()) if isinstance(asx, dict) else list(asx)
    lse_list = list(lse.keys()) if isinstance(lse, dict) else list(lse)
    return asx_list, lse_list


# ----------------------------
# HTTP session
# ----------------------------
def http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": "https://www.asx.com.au/",
        }
    )
    return s


# ----------------------------
# Headline / classification helpers
# ----------------------------
def is_price_sensitive_title(title: str) -> bool:
    t = title.lower()
    keywords = [
        # Results / trading / guidance
        "appendix 4e",
        "appendix 4d",
        "results",
        "half year",
        "half-year",
        "full year",
        "annual report",
        "interim financial report",
        "financial report",
        "trading update",
        "guidance",
        "earnings",
        "profit",
        "revenue",
        "eps",
        "ebit",
        "ebitda",
        "investor presentation",
        "presentation",
        # Capital / debt
        "placement",
        "rights issue",
        "entitlement",
        "spp",
        "capital raising",
        "issue of shares",
        "convertible",
        "notes",
        "bond",
        "debt facility",
        "refinance",
        "term loan",
        "facility",
        # M&A
        "acquisition",
        "acquire",
        "merger",
        "scheme",
        "takeover",
        "transaction",
        # Material contracts / other
        "contract",
        "award",
        "termination",
        "litigation",
        "regulatory",
        "material",
        "strategic",
        "halt",
        "suspension",
        "ceo",
        "cfo",
        "resignation",
        "retirement",
    ]
    return any(k in t for k in keywords)


def looks_like_results_title(title: str) -> bool:
    t = title.lower()

    hard_yes = [
        "appendix 4e",
        "appendix 4d",
        "half year results",
        "half-year results",
        "half year",
        "half-year",
        "h1",
        "hy results",
        "1h fy",
        "1hfy",
        "interim results",
        "interim financial report",
        "annual report",
        "full year results",
        "full-year results",
        "fy results",
        "financial report",
        "results announcement",
        "results presentation",
        "investor presentation",
        "dividend",
        "distribution",
        "fy ",
    ]

    hard_no = [
        "investor call transcript",
        "transcript",
        "webcast",
        "conference call",
    ]

    if any(x in t for x in hard_no):
        return False
    if any(x in t for x in hard_yes):
        return True
    # Match 'fyNN' or 'fy20NN' patterns (e.g. fy26, fy2026) without hard-coding years
    if re.search(r"\bfy\d{2,4}\b", t):
        return True
    return False


# HY/FY trigger keywords — a subset of looks_like_results_title that specifically
# identifies the *primary* results announcement (as opposed to a supplementary
# document like a dividend notice or presentation that may appear on the same day).
_RESULT_DAY_TRIGGER_KEYWORDS: List[str] = [
    "half year results",
    "half-year results",
    "full year results",
    "full-year results",
    "fy results",
    "hy results",
    "h1 results",
    "interim results",
    "appendix 4d",
    "appendix 4e",
    "results announcement",
    "1h fy",
    "1hfy",
]


def is_result_day_trigger(title: str) -> bool:
    """Return True when a title is a primary HY/FY result-day trigger.

    A trigger is the announcement that kicks off result-day pack collection.
    It must contain one of the specific results keywords rather than just any
    reference to a dividend, presentation, or annual report (which may appear
    on non-results days).
    """
    t = title.lower()
    hard_no = ["transcript", "webcast", "conference call"]
    if any(x in t for x in hard_no):
        return False
    return any(x in t for x in _RESULT_DAY_TRIGGER_KEYWORDS)


def group_same_day_items(items_for_ticker: List[Dict], trigger_date: str) -> List[Dict]:
    """Return all announcements for a ticker published on *trigger_date*.

    ``trigger_date`` is in the ASX date format ``DD/MM/YYYY``.
    """
    return [it for it in items_for_ticker if it.get("date", "") == trigger_date]


def classify_from_title_only(title: str) -> str:
    t = title.lower()

    if looks_like_results_title(title):
        return "RESULTS_HY_FY"

    if any(k in t for k in ["acquisition", "acquire", "merger", "scheme", "takeover", "transaction"]):
        return "ACQUISITION"

    if any(
        k in t
        for k in [
            "placement",
            "spp",
            "entitlement",
            "rights issue",
            "capital raising",
            "convertible",
            "notes",
            "debt facility",
            "refinance",
            "term loan",
            "bond",
        ]
    ):
        return "CAPITAL_OR_DEBT_RAISE"

    if any(k in t for k in ["contract", "award", "termination", "trading update", "guidance"]):
        return "CONTRACT_MATERIAL"

    return "OTHER"


# ----------------------------
# LLM (with caps)
# ----------------------------
def llm_chat(system_prompt: str, user_content: str, counters: Dict) -> str:
    if counters["llm_calls"] >= counters["MAX_LLM_CALLS_PER_RUN"]:
        return "__LLM_SKIPPED__"

    counters["llm_calls"] += 1

    model = os.environ.get("MODEL_NAME", MODEL_DEFAULT)
    user_content = user_content[:60_000]

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        return (resp.content[0].text or "").strip()
    except Exception as e:
        log(f"LLM failed: {e}")
        return "__LLM_FAILED__"


# ----------------------------
# PDF / HTML helpers
# ----------------------------
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
    """Return True when text is ASX consent-gate or terms-of-service boilerplate.

    ``fetch_html_text`` strips <header>, <footer>, and <nav> tags before
    extracting text.  The ASX consent page often places the "Agree and proceed"
    button inside one of those stripped elements, so the phrase may be absent
    from the extracted text even though the page is just the gate.  We therefore
    treat "access to this site" alone as sufficient evidence of the gate — that
    heading is in the page <body> and survives the strip.  The secondary pattern
    (general conditions + agree and proceed) is kept as a belt-and-braces check.
    """
    t = (text or "").lower()
    if "access to this site" in t:
        return True
    if "general conditions" in t and "agree and proceed" in t:
        return True
    return False


def is_meaningful_text(text: str, min_chars: int = 1200) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t) < min_chars:
        return False
    if looks_like_asx_access_gate(t):
        return False
    return True


# ----------------------------
# ASX announcement fetching
# ----------------------------
def fetch_asx_announcements(session: requests.Session, ticker: str, hours_back: int = 24) -> List[Dict]:
    url = (
        "https://www.asx.com.au/asx/v2/statistics/announcements.do"
        f"?asxCode={ticker}&by=asxCode&period=M6&timeframe=D"
    )
    r = session.get(url, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("table tr")

    cutoff_dt = cutoff_dt_sgt(hours_back)
    items: List[Dict] = []

    for row in rows:
        cols = [c.get_text(" ", strip=True) for c in row.select("td")]
        if len(cols) < 2:
            continue

        link = row.select_one("a")
        if not link or not link.get("href"):
            continue

        title = link.get_text(" ", strip=True)
        href = link["href"]
        if href.startswith("/"):
            href = "https://www.asx.com.au" + href

        # Typical ASX: col0 = date, col1 = time (sometimes blank / not parseable)
        date_text = cols[0]
        time_text = cols[1] if len(cols) > 1 else ""

        # Parse datetime in SGT-ish format shown on ASX pages (we treat as local SGT for cutoff)
        dt_str = f"{date_text} {time_text}".strip()
        item_dt: Optional[dt.datetime] = None

        # Common patterns like: "26/02/2026 6:09 pm"
        for fmt in ("%d/%m/%Y %I:%M %p", "%d/%m/%Y %I:%M%p", "%d/%m/%Y"):
            try:
                parsed = dt.datetime.strptime(dt_str, fmt)
                if fmt == "%d/%m/%Y":
                    parsed = dt.datetime.combine(parsed.date(), dt.time(23, 59))
                item_dt = parsed
                break
            except Exception:
                continue

        # If time parsing fails, try date-only
        if not item_dt:
            try:
                parsed_date = dt.datetime.strptime(date_text, "%d/%m/%Y").date()
                item_dt = dt.datetime.combine(parsed_date, dt.time(23, 59))
            except Exception:
                continue

        if item_dt < cutoff_dt:
            continue

        items.append(
            {
                "exchange": "ASX",
                "ticker": ticker,
                "date": date_text,
                "time": time_text,
                "title": title,
                "url": href,
            }
        )

    # de-dupe by URL
    seen = set()
    out: List[Dict] = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)

    return out[:MAX_ANNOUNCEMENTS_PER_TICKER]


def fetch_asx_announcements_replay(
    session: requests.Session,
    ticker: str,
    from_date: dt.date,
    to_date: dt.date,
) -> List[Dict]:
    """Fetch announcements for *ticker* on dates between *from_date* and *to_date* inclusive.

    Unlike ``fetch_asx_announcements`` this does **not** apply a 24-hour
    cutoff — it filters purely by calendar date so historical packs can be
    retrieved for replay / debugging.
    """
    url = (
        "https://www.asx.com.au/asx/v2/statistics/announcements.do"
        f"?asxCode={ticker}&by=asxCode&period=M6&timeframe=D"
    )
    r = session.get(url, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("table tr")

    items: List[Dict] = []
    for row in rows:
        cols = [c.get_text(" ", strip=True) for c in row.select("td")]
        if len(cols) < 2:
            continue
        link = row.select_one("a")
        if not link or not link.get("href"):
            continue
        title = link.get_text(" ", strip=True)
        href = link["href"]
        if href.startswith("/"):
            href = "https://www.asx.com.au" + href

        date_text = cols[0]
        time_text = cols[1] if len(cols) > 1 else ""

        try:
            item_date = dt.datetime.strptime(date_text, "%d/%m/%Y").date()
        except Exception:
            continue

        if not (from_date <= item_date <= to_date):
            continue

        items.append({
            "exchange": "ASX",
            "ticker": ticker,
            "date": date_text,
            "time": time_text,
            "title": title,
            "url": href,
        })

    seen: set = set()
    out: List[Dict] = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return out


def fetch_asx_announcements_by_ids(
    session: requests.Session,
    ticker: str,
    announcement_ids: List[str],
) -> List[Dict]:
    """Fetch announcements for *ticker* whose ``idsId`` appears in *announcement_ids*.

    Scans the 6-month history and returns only matching items, bypassing any
    time-window filter.
    """
    id_set = set(announcement_ids)
    url = (
        "https://www.asx.com.au/asx/v2/statistics/announcements.do"
        f"?asxCode={ticker}&by=asxCode&period=M6&timeframe=D"
    )
    r = session.get(url, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("table tr")

    items: List[Dict] = []
    for row in rows:
        cols = [c.get_text(" ", strip=True) for c in row.select("td")]
        if len(cols) < 2:
            continue
        link = row.select_one("a")
        if not link or not link.get("href"):
            continue
        title = link.get_text(" ", strip=True)
        href = link["href"]
        if href.startswith("/"):
            href = "https://www.asx.com.au" + href

        if not any(ids_id in href for ids_id in id_set):
            continue

        date_text = cols[0]
        time_text = cols[1] if len(cols) > 1 else ""
        items.append({
            "exchange": "ASX",
            "ticker": ticker,
            "date": date_text,
            "time": time_text,
            "title": title,
            "url": href,
        })

    seen: set = set()
    out: List[Dict] = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)
    return out


def asx_pdf_url_from_item_url(url: str) -> Optional[str]:
    if "displayAnnouncement.do" in url:
        return url
    return None


# ----------------------------
# Classification (title + text)
# ----------------------------
def classify_announcement(title: str, text: str) -> str:
    # Prefer title signals; text is unreliable if we got HTML or partial
    cls = classify_from_title_only(title)
    if cls != "OTHER":
        return cls

    t = (title + "\n" + (text or "")).lower()
    if any(k in t for k in ["acquisition", "acquire", "merger", "scheme", "takeover", "transaction"]):
        return "ACQUISITION"
    if any(k in t for k in ["placement", "capital raising", "debt facility", "refinance", "bond", "notes"]):
        return "CAPITAL_OR_DEBT_RAISE"
    if any(k in t for k in ["contract", "award", "termination", "guidance", "trading update"]):
        return "CONTRACT_MATERIAL"
    return "OTHER"


# ----------------------------
# Summaries / Deep analysis wrappers
# ----------------------------
def summarise_headline_two_lines(ticker: str, title: str) -> str:
    line1 = f"{ticker}: {title[:160]}"
    line2 = "So what: FYI — open link if you want the details."
    return line1 + "\n" + line2


def summarise_two_lines_llm(ticker: str, title: str, text: str, counters: Dict) -> Optional[str]:
    if not is_meaningful_text(text, min_chars=600):
        return None

    user = f"Ticker: {ticker}\nTitle: {title}\n\nText:\n{text}"
    out = llm_chat(DEFAULT_2LINE_PROMPT, user, counters)

    if out in ("__LLM_SKIPPED__", "__LLM_FAILED__"):
        return f"{ticker}: {title[:160]}\nSo what: price-sensitive headline; open link for details."

    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(lines) >= 2:
        return lines[0] + "\n" + lines[1]
    if len(lines) == 1:
        return lines[0] + "\nSo what: open link for details."
    return None


def deep_acquisition_memo(ticker: str, title: str, text: str, counters: Dict) -> str:
    if not is_meaningful_text(text, min_chars=900):
        return "Could not extract meaningful announcement text automatically. Open the link and review manually."
    user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
    out = llm_chat(ACQUISITION_PROMPT, user, counters)
    if out in ("__LLM_SKIPPED__", "__LLM_FAILED__"):
        return "LLM could not run (limit/billing)."
    return out


def deep_capital_memo(ticker: str, title: str, text: str, counters: Dict) -> str:
    if not is_meaningful_text(text, min_chars=900):
        return "Could not extract meaningful announcement text automatically. Open the link and review manually."
    user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
    out = llm_chat(CAPITAL_OR_DEBT_RAISE_PROMPT, user, counters)
    if out in ("__LLM_SKIPPED__", "__LLM_FAILED__"):
        return "LLM could not run (limit/billing)."
    return out


def deep_results_analysis(ticker: str, report_text: str, deck_text: str, counters: Dict) -> str:
    user = (
        f"Ticker: {ticker}\n\n"
        f"=== OFFICIAL REPORT TEXT ===\n{report_text}\n\n"
        f"=== INVESTOR DECK TEXT ===\n{deck_text}\n"
    )
    out = llm_chat(RESULTS_HYFY_PROMPT, user, counters)
    if out in ("__LLM_SKIPPED__", "__LLM_FAILED__"):
        return "LLM could not run (limit/billing)."
    return out


def strawman_post(ticker: str, kind: str, analysis_text: str, counters: Dict) -> str:
    user = (
        f"Ticker: {ticker}\n"
        f"Announcement type: {kind}\n\n"
        f"Notes / analysis:\n{analysis_text}\n"
    )
    out = llm_chat(STRAWMAN_500W_PROMPT, user, counters)
    if out in ("__LLM_SKIPPED__", "__LLM_FAILED__"):
        return "Could not generate Strawman draft (LLM limit/billing)."
    return out


# ----------------------------
# Google Drive upload
# ----------------------------
def drive_service():
    from googleapiclient.discovery import build

    client_id     = os.environ.get("GDRIVE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN", "").strip()

    if client_id and client_secret and refresh_token:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        creds.refresh(Request())
        return build("drive", "v3", credentials=creds)

    # Fallback: service account (only works with Google Workspace Shared Drives)
    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        raise RuntimeError("No Drive credentials: set GDRIVE_CLIENT_ID/SECRET/REFRESH_TOKEN")
    from google.oauth2.service_account import Credentials
    info = json.loads(sa_json)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)


def upload_to_drive(local_path: Path, folder_id: str, drive_filename: str) -> str:
    from googleapiclient.http import MediaFileUpload

    service = drive_service()
    file_metadata = {"name": drive_filename, "parents": [folder_id]}
    media = MediaFileUpload(str(local_path), resumable=False)
    created = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    file_id = created.get("id") or ""
    if not file_id:
        return ""
    return f"https://drive.google.com/file/d/{file_id}/view"


# ----------------------------
# Results helpers
# ----------------------------
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


# ----------------------------
# Core fetch: requests -> playwright -> html fallback
# ----------------------------
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
        # 1) requests
        try:
            got_pdf = download_pdf_requests(session, pdf_url, pdf_path)
        except Exception:
            got_pdf = False

        # 2) playwright fallback
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

    # 3) HTML fallback (safe, but often useless)
    try:
        html_text = fetch_html_text(session, url)
        return html_text, False
    except Exception:
        return "", False


# ----------------------------
# Result-day pack helpers
# ----------------------------

def download_pdf_bytes(session: requests.Session, url: str) -> Optional[bytes]:
    """Download a PDF and return its raw bytes, or None on any failure.

    Unlike ``download_pdf_requests`` this does NOT write a file; the bytes
    are returned directly so they can be base64-encoded and sent to Claude.
    """
    try:
        r = session.get(url, timeout=REQUESTS_PDF_TIMEOUT_SECS, allow_redirects=True)
        r.raise_for_status()
        if r.content[:4] != b"%PDF":
            return None
        return r.content
    except Exception:
        return None


def llm_chat_with_pdfs(
    system_prompt: str,
    text_context: str,
    pdf_items: List[Dict],
    counters: Dict,
) -> str:
    """Send a prompt plus one or more PDFs (as base64 documents) to Claude.

    Each entry in ``pdf_items`` must have keys:
      - ``title`` (str): document title shown to Claude
      - ``pdf_bytes`` (bytes): raw PDF content

    Items without ``pdf_bytes`` are silently skipped; they are still
    represented in ``text_context``.

    Returns the LLM response text, or a sentinel from ``_LLM_FAIL_MSGS``.
    """
    if counters["llm_calls"] >= counters["MAX_LLM_CALLS_PER_RUN"]:
        return "__LLM_SKIPPED__"

    counters["llm_calls"] += 1
    model = os.environ.get("MODEL_NAME", MODEL_DEFAULT)

    content: List[Dict] = []

    # Add each PDF as a base64 document block
    for item in pdf_items:
        raw = item.get("pdf_bytes")
        if not raw:
            continue
        if len(raw) > MAX_RESULT_PDF_BYTES:
            log(f"[bob] skipping oversized PDF ({len(raw)//1024}KB): {item.get('title','?')[:60]}")
            continue
        content.append({
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(raw).decode("utf-8"),
            },
            "title": item.get("title", "Document"),
        })

    # Add the text context as the final user message
    content.append({
        "type": "text",
        "text": text_context[:30_000],
    })

    if len(content) == 1:
        # Only the text block — no PDFs were attached; treat as LLM failure
        # so the caller can fall back gracefully.
        log("[bob] llm_chat_with_pdfs: no PDF documents could be attached")
        return "__LLM_FAILED__"

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )
        return (resp.content[0].text or "").strip()
    except Exception as e:
        log(f"[bob] LLM (with PDFs) failed: {e}")
        return "__LLM_FAILED__"


def deep_results_pack_analysis(
    ticker: str,
    pack_items: List[Dict],
    date_str: str,
    counters: Dict,
) -> str:
    """Analyse a full HY/FY result-day PDF pack by sending it directly to Claude.

    ``pack_items`` is a list of dicts with keys:
      - ``title``   (str): announcement title
      - ``url``     (str): ASX announcement URL
      - ``pdf_url`` (Optional[str]): direct PDF URL
      - ``pdf_bytes`` (Optional[bytes]): downloaded PDF bytes

    Returns the Claude analysis string, or a sentinel from ``_LLM_FAIL_MSGS``.
    """
    titles_text = "\n".join(f"  - {it['title']}" for it in pack_items)
    urls_text = "\n".join(
        f"  - {it['title'][:80]}: {it.get('pdf_url') or it['url']}"
        for it in pack_items
    )
    attached = sum(1 for it in pack_items if it.get("pdf_bytes"))
    text_context = (
        f"Ticker: {ticker}\n"
        f"Announcement date: {date_str}\n"
        f"Number of documents in pack: {len(pack_items)} ({attached} PDFs attached)\n\n"
        f"Document titles:\n{titles_text}\n\n"
        f"Document URLs:\n{urls_text}\n"
    )

    log(f"[bob] sending result-day pack to Claude — ticker={ticker}, docs={len(pack_items)}, pdfs_attached={attached}")
    result = llm_chat_with_pdfs(RESULTS_HYFY_PACK_PROMPT, text_context, pack_items, counters)

    if result in ("__LLM_SKIPPED__", "__LLM_FAILED__"):
        return "LLM could not run (limit/billing)."
    return result


def save_result_artifacts(
    ticker: str,
    date_str: str,
    pack_items: List[Dict],
    analysis: str,
    dir_prefix: str = "",
) -> Path:
    """Persist result-day analysis artifacts to disk.

    Saves:
      - ``pack_metadata.json``  : titles, URLs, PDF availability
      - ``claude_analysis.txt`` : raw Claude response
      - ``summary.md``          : Markdown-formatted summary

    Returns the directory path where artifacts were saved.
    """
    # Normalise date for filesystem (DD/MM/YYYY -> YYYY-MM-DD)
    try:
        dir_date = dt.datetime.strptime(date_str, "%d/%m/%Y").strftime("%Y-%m-%d")
    except Exception:
        dir_date = date_str.replace("/", "-")

    out_dir = RESULT_ARTIFACTS_DIR / ticker / f"{dir_prefix}{dir_date}"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)

        # Metadata (exclude raw PDF bytes — not serialisable)
        metadata = {
            "ticker": ticker,
            "date": date_str,
            "documents": [
                {
                    "title": it["title"],
                    "url": it["url"],
                    "pdf_url": it.get("pdf_url"),
                    "pdf_bytes_size": len(it["pdf_bytes"]) if it.get("pdf_bytes") else None,
                }
                for it in pack_items
            ],
        }
        (out_dir / "pack_metadata.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )

        (out_dir / "claude_analysis.txt").write_text(analysis, encoding="utf-8")

        titles_md = "\n".join(f"- [{it['title']}]({it['url']})" for it in pack_items)
        md = (
            f"# {ticker} — Result-Day Analysis\n\n"
            f"**Date:** {date_str}\n\n"
            f"## Documents\n\n{titles_md}\n\n"
            f"## Analysis\n\n{analysis}\n"
        )
        (out_dir / "summary.md").write_text(md, encoding="utf-8")

    except Exception as e:
        log(f"[bob] save_result_artifacts failed for {ticker}: {e}")

    return out_dir


def format_result_fallback_block(
    ticker: str,
    pack_items: List[Dict],
    date_str: str,
) -> str:
    """Build a fallback output block when Claude analysis cannot be completed."""
    lines = [
        f"{ticker} reported today ({date_str}). Full automated pack analysis failed, "
        f"but the following result-day PDFs were identified:",
        "",
    ]
    for it in pack_items:
        url = it.get("pdf_url") or it["url"]
        lines.append(f"  - {it['title']}")
        lines.append(f"    {url}")
    lines.append("")
    lines.append("Open the links above to review the result-day documents manually.")
    return "\n".join(lines)


# ----------------------------
# Email formatting helpers
# ----------------------------
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
        + "; border-radius:10px; white-space:pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size:13px; color:"
        + COLOR_TEXT
        + ";'>"
        + _linkify_urls(b)
        + "</div>"
    )


def _html_section(title: str, color: str, blocks: List[str]) -> str:
    if not blocks:
        return ""
    items_html = "".join(_html_block(b) for b in blocks)
    return f"""
    <div style="margin:18px 0;">
      <div style="padding:10px 12px; background:{color}; color:#0B1220; font-weight:800; border-radius:10px; letter-spacing:0.6px;">
        {htmlmod.escape(title)}
      </div>
      {items_html}
    </div>
    """


def build_email(
    high_impact: List[str],
    material: List[str],
    fyi: List[str],
) -> Tuple[str, str]:
    no_announcements_msg = f"No reportable announcements found in the last {HOURS_BACK} hours."
    # Plain text
    lines: List[str] = []
    lines.append(f"{BOB_NAME} {VERSION_LABEL}")
    lines.append("=" * len(BOB_NAME))
    lines.append(f"Daily Announcements Digest — last {HOURS_BACK} hours — {today_sgt_date().isoformat()} (SGT)")
    lines.append(f"Run caps: MAX_PDFS={MAX_PDFS_PER_RUN}, MAX_LLM_CALLS={MAX_LLM_CALLS_PER_RUN}, MAX_PER_TICKER={MAX_ANNOUNCEMENTS_PER_TICKER}")
    lines.append("")

    if high_impact:
        lines.append("HIGH IMPACT")
        lines.append("-" * 60)
        lines.extend(high_impact)
        lines.append("")

    if material:
        lines.append("MATERIAL")
        lines.append("-" * 60)
        lines.extend(material)
        lines.append("")

    if fyi:
        lines.append("FYI (ALL ANNOUNCEMENTS)")
        lines.append("-" * 60)
        lines.extend(fyi)
        lines.append("")

    if not high_impact and not material and not fyi:
        joke = _get_daily_joke()
        lines.append(no_announcements_msg)
        if joke:
            lines.append(f"Joke of the day: {joke}")

    body_text = "\n".join(lines)

    # HTML
    header_html = f"""
    <div style="padding:18px; background:{COLOR_BG}; color:{COLOR_TEXT}; font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif;">
      <div style="font-size:22px; font-weight:900; margin-bottom:6px;">{htmlmod.escape(f"{BOB_NAME} {VERSION_LABEL}")}</div>
      <div style="opacity:0.9; font-size:14px; margin-bottom:10px;">
        Daily Announcements Digest — last {HOURS_BACK} hours — {today_sgt_date().isoformat()} (SGT)
      </div>
      <div style="opacity:0.75; font-size:12px; margin-bottom:18px;">
        Run caps: MAX_PDFS={MAX_PDFS_PER_RUN}, MAX_LLM_CALLS={MAX_LLM_CALLS_PER_RUN}, MAX_PER_TICKER={MAX_ANNOUNCEMENTS_PER_TICKER}
      </div>
    """

    sections_html = ""
    sections_html += _html_section("HIGH IMPACT", COLOR_HIGH_IMPACT, high_impact)
    sections_html += _html_section("MATERIAL", COLOR_MATERIAL, material)
    sections_html += _html_section("FYI (ALL ANNOUNCEMENTS)", COLOR_FYI, fyi)

    if not high_impact and not material and not fyi:
        joke = _get_daily_joke()
        joke_html = (
            f'<div style="margin-top:8px; opacity:0.75; font-size:13px;">'
            f'Joke of the day: {htmlmod.escape(joke)}</div>'
            if joke else ""
        )
        sections_html += f"""
        <div style="margin:18px 0;">
          <div style="margin-top:10px; padding:12px; background:{COLOR_PANEL}; border-radius:10px; color:{COLOR_TEXT};">
            {htmlmod.escape(no_announcements_msg)}{joke_html}
          </div>
        </div>
        """

    footer_html = "</div>"
    body_html = header_html + sections_html + footer_html
    return body_text, body_html


# ----------------------------
# CLI arg parsing
# ----------------------------
def _parse_cli_args() -> argparse.Namespace:
    """Parse CLI arguments for Bob.

    Uses ``parse_known_args`` so that unknown flags (e.g. pytest internals)
    are silently ignored rather than raising errors when agent.py is imported
    inside a test.
    """
    parser = argparse.ArgumentParser(
        description="Bob the Bot — ASX Announcements Agent",
        add_help=True,
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Force reprocess all announcements regardless of state",
    )
    parser.add_argument(
        "--replay-ticker", metavar="TICKER", default="",
        help="Ticker symbol to replay (enables replay mode)",
    )
    parser.add_argument(
        "--replay-date", metavar="YYYY-MM-DD", default="",
        help="Single date to replay (shorthand for --from-date and --to-date)",
    )
    parser.add_argument(
        "--from-date", metavar="YYYY-MM-DD", default="",
        help="Start of replay date range (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--to-date", metavar="YYYY-MM-DD", default="",
        help="End of replay date range (YYYY-MM-DD, defaults to --from-date)",
    )
    parser.add_argument(
        "--announcement-ids", metavar="IDS", default="",
        help="Comma-separated ASX announcement idsId values to replay",
    )
    parser.add_argument(
        "--update-production-state", action="store_true", default=False,
        help="Write replay results back into the production state file (default: off)",
    )
    args, _unknown = parser.parse_known_args()
    return args


# ----------------------------
# Replay / test mode
# ----------------------------
def run_replay(
    replay_ticker: str,
    from_date: dt.date,
    to_date: dt.date,
    announcement_ids: Optional[List[str]] = None,
    update_production_state: bool = False,
) -> None:
    """Run Bob in replay/test mode for a specific ticker and date range.

    Replay mode:
    - Bypasses the normal 24-hour production scan window
    - Ignores COMPLETED/FAILED state by default (force-reprocesses)
    - Uses the same HY/FY result-pack logic as production
    - Clearly labels all log and digest output as REPLAY
    - Does NOT update production state unless *update_production_state* is True
    - Saves artifacts to ``outputs/results/{ticker}/replay_{date}/``
    """
    log("[bob] REPLAY MODE enabled")
    log(f"[bob] replay_ticker={replay_ticker}")
    if announcement_ids:
        log(f"[bob] announcement_ids={','.join(announcement_ids)}")
    else:
        log(f"[bob] replay_from_date={from_date.isoformat()} replay_to_date={to_date.isoformat()}")
    log(f"[bob] ignore_state=True (replay mode always force-reprocesses)")
    log(f"[bob] update_production_state={update_production_state}")

    session = http_session()

    # Fetch announcements — bypass 24-hour window
    if announcement_ids:
        log(f"[bob] REPLAY: fetching by announcement IDs: {','.join(announcement_ids)}")
        items = fetch_asx_announcements_by_ids(session, replay_ticker, announcement_ids)
    else:
        log(f"[bob] REPLAY: fetching by date range: {from_date.isoformat()} → {to_date.isoformat()}")
        items = fetch_asx_announcements_replay(session, replay_ticker, from_date, to_date)

    log(f"[bob] REPLAY: announcements found: {len(items)}")

    if not items:
        log(f"[bob] REPLAY: no announcements found for {replay_ticker} in the specified range — nothing to replay")
        return

    drive_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()

    counters: Dict = {
        "MAX_LLM_CALLS_PER_RUN": MAX_LLM_CALLS_PER_RUN,
        "llm_calls": 0,
        "MAX_PDFS_PER_RUN": MAX_PDFS_PER_RUN,
        "pdfs_downloaded": 0,
    }

    high_impact_blocks: List[str] = []
    fyi_blocks: List[str] = []

    # Tag each item with its state key (replay mode always processes them all)
    for it in items:
        it["seen_key"] = announcement_key(replay_ticker, it["url"])

    replay_label = f"Replay/Test Run — {replay_ticker} — {from_date.isoformat()}"
    if from_date != to_date:
        replay_label += f" to {to_date.isoformat()}"
    if announcement_ids:
        replay_label += f" — IDs: {','.join(announcement_ids)}"

    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)

        # Check for HY/FY result-day trigger
        trigger_item = next((i for i in items if is_result_day_trigger(i["title"])), None)

        if trigger_item:
            trigger_date = trigger_item.get("date", "")
            log(f"[bob] REPLAY: HY/FY trigger detected: '{trigger_item['title'][:60]}' (date={trigger_date})")

            # Collect all same-day announcements for the pack
            pack_candidates = (
                group_same_day_items(items, trigger_date) if trigger_date else list(items)
            )
            log(f"[bob] REPLAY: PDF pack count: {len(pack_candidates)}")

            drive_links: List[str] = []
            pack_items: List[Dict] = []

            for ann in pack_candidates:
                pdf_url = asx_pdf_url_from_item_url(ann["url"])
                pdf_bytes: Optional[bytes] = None
                if pdf_url:
                    pdf_bytes = download_pdf_bytes(session, pdf_url)
                    if pdf_bytes:
                        log(f"[bob] REPLAY: downloaded PDF ({len(pdf_bytes)//1024}KB): {ann['title'][:60]}")
                    else:
                        log(f"[bob] REPLAY: PDF download failed (pack continues): {ann['title'][:60]}")

                    if pdf_bytes and drive_folder_id:
                        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{replay_ticker}_{ann['title'][:80]}")
                        pdf_path = tmpdir / f"{safe_name}.pdf"
                        try:
                            pdf_path.write_bytes(pdf_bytes)
                            drive_name = f"REPLAY_{from_date.isoformat()}_{replay_ticker}_{safe_name}.pdf"
                            link = upload_to_drive(pdf_path, drive_folder_id, drive_name)
                            if link:
                                drive_links.append(link)
                        except Exception as e:
                            log(f"[bob] REPLAY: Drive upload failed for {replay_ticker}: {e}")
                        finally:
                            try:
                                if pdf_path.exists():
                                    pdf_path.unlink()
                            except Exception:
                                pass

                pack_items.append({
                    "title": ann["title"],
                    "url": ann["url"],
                    "pdf_url": pdf_url,
                    "pdf_bytes": pdf_bytes,
                })

            # Send the full pack to Claude — same logic as production
            analysis = deep_results_pack_analysis(replay_ticker, pack_items, trigger_date, counters)
            analysis_ok = analysis not in _LLM_FAIL_MSGS

            if analysis_ok:
                log(f"[bob] REPLAY: Claude analysis succeeded for {replay_ticker}")
                straw = strawman_post(replay_ticker, "HY/FY Results", analysis, counters)

                header = f"[REPLAY] {replay_ticker} — Results (HY/FY) — {replay_label}"
                if drive_links:
                    header += f" — Drive PDFs: {len(drive_links)}"
                block = f"{header}\n\n{analysis}\n\nOpen: {trigger_item['url']}\n"
                if drive_links:
                    block += "Drive links:\n" + "\n".join(drive_links) + "\n"
                block += "\nSTRAWMAN DRAFT (paste-ready, ~500w max)\n" + "-" * 45 + "\n" + straw + "\n"

                # Save artifacts to a replay-prefixed subdirectory
                try:
                    art_dir = save_result_artifacts(
                        replay_ticker,
                        trigger_date or from_date.strftime("%d/%m/%Y"),
                        pack_items,
                        analysis,
                        dir_prefix="replay_",
                    )
                    log(f"[bob] REPLAY: artifacts saved to {art_dir}")
                except Exception as e:
                    log(f"[bob] REPLAY: artifact save failed for {replay_ticker}: {e}")
            else:
                log(f"[bob] REPLAY: Claude analysis failed for {replay_ticker} — publishing fallback with raw links")
                block = "[REPLAY] " + format_result_fallback_block(replay_ticker, pack_items, trigger_date)
                if drive_links:
                    block += "\nDrive links:\n" + "\n".join(drive_links) + "\n"

            high_impact_blocks.append(block)

        else:
            # No HY/FY trigger — add all items to FYI
            log(f"[bob] REPLAY: no HY/FY trigger found — listing {len(items)} items as FYI")
            for it in items:
                fyi_entry = (
                    f"[REPLAY] {summarise_headline_two_lines(replay_ticker, it['title'])}\n"
                    f"Open: {it['url']}\n"
                )
                fyi_blocks.append(fyi_entry)

    # Print replay digest to stdout
    lines: List[str] = [
        f"{BOB_NAME} {VERSION_LABEL} — {replay_label}",
        "=" * 60,
        "",
    ]
    if high_impact_blocks:
        lines.append("HIGH IMPACT")
        lines.append("-" * 60)
        lines.extend(high_impact_blocks)
        lines.append("")
    if fyi_blocks:
        lines.append("FYI (ALL ANNOUNCEMENTS)")
        lines.append("-" * 60)
        lines.extend(fyi_blocks)
        lines.append("")
    if not high_impact_blocks and not fyi_blocks:
        lines.append("No reportable announcements found for replay range.")

    digest_text = "\n".join(lines)
    print(digest_text)

    # Save replay digest to file
    replay_out_dir = RESULT_ARTIFACTS_DIR / replay_ticker
    try:
        replay_out_dir.mkdir(parents=True, exist_ok=True)
        digest_file = replay_out_dir / f"replay_{from_date.isoformat()}_digest.txt"
        digest_file.write_text(digest_text, encoding="utf-8")
        log(f"[bob] REPLAY: output file path: {digest_file}")
    except Exception as e:
        log(f"[bob] REPLAY: digest save failed: {e}")

    # Update production state only if explicitly requested
    if update_production_state:
        prod_state = prune_seen_state(load_seen_state(SEEN_STATE_PATH), SEEN_STATE_RETENTION_HOURS)
        for it in items:
            _k = it.get("seen_key") or announcement_key(replay_ticker, it["url"])
            mark_state(prod_state, _k, replay_ticker, it["title"], STATUS_COMPLETED)
        save_seen_state(SEEN_STATE_PATH, prod_state)
        log("[bob] REPLAY: production state updated (--update-production-state was set)")
    else:
        log("[bob] REPLAY: production state NOT updated (replay mode default — use --update-production-state to override)")


# ----------------------------
# MAIN
# ----------------------------
def main():
    args = _parse_cli_args()

    # Dispatch to replay mode when --replay-ticker is provided
    if args.replay_ticker:
        # Resolve dates: --replay-date takes priority, then --from-date; fallback to today
        from_date_str = args.replay_date or args.from_date
        to_date_str = args.to_date or from_date_str

        try:
            from_date = dt.date.fromisoformat(from_date_str) if from_date_str else today_sgt_date()
        except ValueError:
            log(f"[bob] ERROR: invalid --from-date / --replay-date: {from_date_str!r} (expected YYYY-MM-DD)")
            sys.exit(1)
        try:
            to_date = dt.date.fromisoformat(to_date_str) if to_date_str else from_date
        except ValueError:
            log(f"[bob] ERROR: invalid --to-date: {to_date_str!r} (expected YYYY-MM-DD)")
            sys.exit(1)

        ann_ids: Optional[List[str]] = (
            [x.strip() for x in args.announcement_ids.split(",") if x.strip()]
            if args.announcement_ids else None
        )

        run_replay(
            replay_ticker=args.replay_ticker.upper(),
            from_date=from_date,
            to_date=to_date,
            announcement_ids=ann_ids,
            update_production_state=args.update_production_state,
        )
        return

    # --- Normal production mode ---
    _force = FORCE or args.force

    subject = f"{BOB_NAME} {VERSION_LABEL} — Daily Announcements Digest — {today_sgt_date().isoformat()} (SGT)"

    session = http_session()
    asx_tickers, _lse_tickers = read_tickers()

    drive_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()

    counters = {
        "MAX_LLM_CALLS_PER_RUN": MAX_LLM_CALLS_PER_RUN,
        "llm_calls": 0,
        "MAX_PDFS_PER_RUN": MAX_PDFS_PER_RUN,
        "pdfs_downloaded": 0,
    }

    seen_state = prune_seen_state(load_seen_state(SEEN_STATE_PATH), SEEN_STATE_RETENTION_HOURS)
    seen_state_updated = dict(seen_state)
    run_completed_count = 0

    if _force:
        log("[bob] --force active — reprocessing all announcements regardless of state")
    if FORCE_RERUN_TICKERS:
        log(f"[bob] FORCE_RERUN_TICKERS active — bypassing state for: {', '.join(sorted(FORCE_RERUN_TICKERS))}")

    # Bucket outputs
    high_impact_blocks: List[str] = []
    material_blocks: List[str] = []
    fyi_blocks: List[str] = []
    brother_blocks: List[str] = []
    # Structured data for dashboard
    _hi_items: List[Dict] = []
    _mat_items: List[Dict] = []
    _fyi_items: List[Dict] = []

    # Fetch announcements by ticker
    by_ticker: Dict[str, List[Dict]] = {}
    for t in asx_tickers:
        try:
            by_ticker[t] = fetch_asx_announcements(session, t, hours_back=HOURS_BACK)
        except Exception as e:
            log(f"Fetch failed for {t}: {e}")
            by_ticker[t] = []

    processed_results = set()

    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)

        for ticker, items in by_ticker.items():
            if not items:
                continue

            fresh_items: List[Dict] = []
            for it in items:
                key = announcement_key(ticker, it["url"])
                title = it["title"]
                process, reason = should_process_item(
                    key, ticker, title, seen_state, _force, FORCE_RERUN_TICKERS
                )
                cur_status = seen_state.get(key, {}).get("status", STATUS_NEW)
                detected_type = classify_from_title_only(title)
                log(
                    f"[bob] {ticker} | '{title[:60]}' | type={detected_type} | "
                    f"status={cur_status} | decision={'process' if process else 'skip'} | reason={reason}"
                )
                if not process:
                    continue
                it["seen_key"] = key
                fresh_items.append(it)

            items = fresh_items
            if not items:
                continue

            # Always add FYI entries (all announcements) – quick, headline-only + link
            for it in items:
                title = it["title"]
                url = it["url"]
                fyi_entry = f"{summarise_headline_two_lines(ticker, title)}\nOpen: {url}\n"
                fyi_blocks.append(fyi_entry)
                _fyi_items.append({"ticker": ticker, "title": title, "url": url})
                if ticker == "AR9":
                    brother_blocks.append(fyi_entry)

            # ----------------------------
            # RESULTS bundle (per ticker) — HY/FY pack analysis
            # ----------------------------
            trigger_item = next(
                (i for i in items if is_result_day_trigger(i["title"])), None
            )
            if trigger_item and ticker not in processed_results:
                processed_results.add(ticker)
                trigger_date = trigger_item.get("date", "")
                log(f"[bob] HY/FY trigger detected for {ticker}: '{trigger_item['title'][:60]}' (date={trigger_date})")

                # Collect ALL same-day announcements for the triggered ticker.
                # Use the full by_ticker set (not just the filtered fresh_items) so
                # we pick up any same-day item that may have been skipped by the
                # dedup filter but still represents a results-day document.
                all_same_day = group_same_day_items(
                    by_ticker.get(ticker, []), trigger_date
                ) if trigger_date else list(items)
                log(f"[bob] collected {len(all_same_day)} same-day announcements for {ticker}")

                any_results_link = trigger_item["url"]
                drive_links: List[str] = []

                # Build result-day pack: download PDF bytes for each same-day item
                pack_items: List[Dict] = []
                for ann in all_same_day:
                    pdf_url = asx_pdf_url_from_item_url(ann["url"])
                    pdf_bytes: Optional[bytes] = None
                    if pdf_url:
                        pdf_bytes = download_pdf_bytes(session, pdf_url)
                        if pdf_bytes:
                            log(f"[bob] {ticker} | downloaded PDF ({len(pdf_bytes)//1024}KB): {ann['title'][:60]}")
                        else:
                            log(f"[bob] {ticker} | PDF download failed (pack continues): {ann['title'][:60]}")

                        # Optional: upload PDF to Drive when available
                        if pdf_bytes and drive_folder_id:
                            safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{ann['title'][:80]}")
                            pdf_path = tmpdir / f"{safe_name}.pdf"
                            try:
                                pdf_path.write_bytes(pdf_bytes)
                                drive_name = f"{today_sgt_date().isoformat()}_{ticker}_{safe_name}.pdf"
                                link = upload_to_drive(pdf_path, drive_folder_id, drive_name)
                                if link:
                                    drive_links.append(link)
                            except Exception as e:
                                log(f"[bob] Drive upload failed for {ticker}: {e}")
                            finally:
                                try:
                                    if pdf_path.exists():
                                        pdf_path.unlink()
                                except Exception:
                                    pass

                    pack_items.append({
                        "title": ann["title"],
                        "url": ann["url"],
                        "pdf_url": pdf_url,
                        "pdf_bytes": pdf_bytes,
                    })

                # Send the full pack to Claude — do NOT gate on local text extraction
                analysis = deep_results_pack_analysis(ticker, pack_items, trigger_date, counters)
                analysis_ok = analysis not in _LLM_FAIL_MSGS

                if analysis_ok:
                    log(f"[bob] Claude analysis succeeded for {ticker}")
                    straw = strawman_post(ticker, "HY/FY Results", analysis, counters)

                    header = f"{ticker} — Results (HY/FY)"
                    if drive_links:
                        header += f" — Drive PDFs: {len(drive_links)}"
                    block = f"{header}\n\n{analysis}\n\nOpen: {any_results_link}\n"
                    if drive_links:
                        block += "Drive links:\n" + "\n".join(drive_links) + "\n"
                    block += "\nSTRAWMAN DRAFT (paste-ready, ~500w max)\n" + "-" * 45 + "\n" + straw + "\n"

                    # Save analysis artifacts
                    try:
                        art_dir = save_result_artifacts(ticker, trigger_date, pack_items, analysis)
                        log(f"[bob] digest section published — artifacts saved to {art_dir}")
                    except Exception as e:
                        log(f"[bob] artifact save failed for {ticker}: {e}")
                else:
                    log(f"[bob] Claude analysis failed for {ticker} — publishing fallback with raw links")
                    block = format_result_fallback_block(ticker, pack_items, trigger_date)
                    if drive_links:
                        block += "\nDrive links:\n" + "\n".join(drive_links) + "\n"

                high_impact_blocks.append(block)
                _hi_items.append({"ticker": ticker, "title": "Results (HY/FY)", "url": any_results_link, "type": "results"})
                if ticker == "AR9":
                    brother_blocks.append(block)

                # Mark all same-day items based on outcome
                for ann in all_same_day:
                    _k = ann.get("seen_key") or announcement_key(ticker, ann["url"])
                    if analysis_ok:
                        log(f"[bob] {ticker} | '{ann['title'][:60]}' | SUCCESS → marked COMPLETED")
                        mark_state(seen_state_updated, _k, ticker, ann["title"], STATUS_COMPLETED)
                        run_completed_count += 1
                    else:
                        log(f"[bob] {ticker} | '{ann['title'][:60]}' | FAILED → Claude analysis failed, will retry")
                        mark_state(seen_state_updated, _k, ticker, ann["title"], STATUS_FAILED, "Claude analysis failed")

                # Non-same-day items for this ticker: FYI-only → mark COMPLETED
                same_day_urls = {a["url"] for a in all_same_day}
                for it in items:
                    if it["url"] not in same_day_urls:
                        _k = it.get("seen_key") or announcement_key(ticker, it["url"])
                        mark_state(seen_state_updated, _k, ticker, it["title"], STATUS_COMPLETED)
                        run_completed_count += 1

                # Results bundle handled — continue to next ticker
                continue

            # ----------------------------
            # Per-item MATERIAL / HIGH IMPACT
            # ----------------------------
            for it in items:
                title = it["title"]
                url = it["url"]
                item_key = it.get("seen_key") or announcement_key(ticker, url)

                is_price = is_price_sensitive_title(title)
                cls_title = classify_from_title_only(title)

                # Non-price-sensitive: FYI already captured; mark COMPLETED and move on
                if not is_price:
                    mark_state(seen_state_updated, item_key, ticker, title, STATUS_COMPLETED)
                    run_completed_count += 1
                    continue

                # High impact types (even if title-based)
                if cls_title in ("ACQUISITION", "CAPITAL_OR_DEBT_RAISE"):
                    pdf_url = asx_pdf_url_from_item_url(url)
                    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                    pdf_path = tmpdir / f"{safe_name}.pdf"

                    text, got_pdf = fetch_announcement_text(session, url, pdf_url, pdf_path, counters)

                    # Gate: if we got ASX consent HTML, don’t hallucinate
                    if looks_like_asx_access_gate(text):
                        block = f"{ticker}: {title[:160]}\nSo what: could not fetch PDF automatically. Open: {url}\n"
                        material_blocks.append(block)
                        if ticker == "AR9":
                            brother_blocks.append(block)
                        if pdf_path.exists():
                            try:
                                pdf_path.unlink()
                            except Exception:
                                pass
                        log(f"[bob] {ticker} | '{title[:60]}' | FAILED → ASX consent gate blocked PDF fetch")
                        mark_state(seen_state_updated, item_key, ticker, title, STATUS_FAILED, "ASX consent gate")
                        continue

                    drive_links: List[str] = []
                    if got_pdf and drive_folder_id:
                        try:
                            drive_name = f"{today_sgt_date().isoformat()}_{ticker}_{safe_name}.pdf"
                            link = upload_to_drive(pdf_path, drive_folder_id, drive_name)
                            if link:
                                drive_links.append(link)
                        except Exception as e:
                            log(f"Drive upload failed for {ticker}: {e}")

                    if pdf_path.exists():
                        try:
                            pdf_path.unlink()
                        except Exception:
                            pass

                    if cls_title == "ACQUISITION":
                        memo = deep_acquisition_memo(ticker, title, text, counters)
                        straw = strawman_post(ticker, "Acquisition", memo, counters)
                        block = f"{ticker} — Acquisition\n{memo}\nOpen: {url}\n"
                        _hi_items.append({"ticker": ticker, "title": title[:120], "url": url, "type": "acquisition"})
                    else:
                        memo = deep_capital_memo(ticker, title, text, counters)
                        straw = strawman_post(ticker, "Capital/Debt Raise", memo, counters)
                        block = f"{ticker} — Capital/Debt Raise\n{memo}\nOpen: {url}\n"
                        _hi_items.append({"ticker": ticker, "title": title[:120], "url": url, "type": "capital"})

                    memo_ok = memo not in _LLM_FAIL_MSGS and memo != _TEXT_UNAVAIL

                    if drive_links:
                        block += "Drive link(s):\n" + "\n".join(drive_links) + "\n"
                    block += "\nSTRAWMAN DRAFT (paste-ready, ~500w max)\n" + "-" * 45 + "\n" + straw + "\n"

                    high_impact_blocks.append(block)
                    if ticker == "AR9":
                        brother_blocks.append(block)

                    if memo_ok:
                        log(f"[bob] {ticker} | '{title[:60]}' | SUCCESS → marked COMPLETED")
                        mark_state(seen_state_updated, item_key, ticker, title, STATUS_COMPLETED)
                        run_completed_count += 1
                    else:
                        log(f"[bob] {ticker} | '{title[:60]}' | FAILED → LLM memo failed, will retry")
                        mark_state(seen_state_updated, item_key, ticker, title, STATUS_FAILED, "LLM memo failed")
                    continue

                # Otherwise: MATERIAL (price-sensitive but not deep)
                # Only try PDF+LLM when it’s worth it (contracts/guidance/trading updates)
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

                # If we have meaningful text, do 2-line LLM. Else fallback headline 2-line.
                summary = None
                if text and is_meaningful_text(text, min_chars=600):
                    summary = summarise_two_lines_llm(ticker, title, text, counters)

                if not summary:
                    summary = f"{ticker}: {title[:160]}\nSo what: price-sensitive headline — open link for details."

                block = f"{summary}\nOpen: {url}\n"
                material_blocks.append(block)
                _mat_items.append({"ticker": ticker, "title": title[:160], "url": url, "summary": summary or ""})
                if ticker == "AR9":
                    brother_blocks.append(block)

                log(f"[bob] {ticker} | '{title[:60]}' | SUCCESS → marked COMPLETED (material)")
                mark_state(seen_state_updated, item_key, ticker, title, STATUS_COMPLETED)
                run_completed_count += 1

    # ----------------------------
    # Build email (clean + colour-coded HTML)
    # ----------------------------
    reportable_count = len(high_impact_blocks) + len(material_blocks) + len(fyi_blocks)
    log(f"[bob] reportable_announcements={reportable_count}")
    if reportable_count == 0:
        log("[bob] silence_mode=True")
        log("[bob] joke_of_the_day_selected=True")
    else:
        log("[bob] silence_mode=False")
        log("[bob] joke skipped because announcements exist")
    body_text, body_html = build_email(high_impact_blocks, material_blocks, fyi_blocks)

    send_email(subject, body_text, body_html)

    # Brother email (AR9 only)
    brother_email = os.environ.get("BROTHER_EMAIL", "").strip()
    if brother_email and brother_blocks:
        bro_subject = f"{BOB_NAME} {VERSION_LABEL} — AR9 Digest — {today_sgt_date().isoformat()} (SGT)"
        bro_text = "\n".join([bro_subject, "", *brother_blocks])
        bro_html = "<div style='padding:18px; background:%s; color:%s; font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif;'>" % (COLOR_BG, COLOR_TEXT)
        bro_html += f"<div style='font-size:20px; font-weight:900; margin-bottom:8px;'>{htmlmod.escape(bro_subject)}</div>"
        bro_html += _html_section("AR9 ONLY", COLOR_MATERIAL, brother_blocks)
        bro_html += "</div>"
        send_email(bro_subject, bro_text, bro_html, to_addr=brother_email)

    # Write dashboard data
    _dashboard_dir = Path("docs/data")
    try:
        _dashboard_dir.mkdir(parents=True, exist_ok=True)
        (_dashboard_dir / "bob.json").write_text(
            json.dumps({
                "last_run": today_sgt_date().isoformat(),
                "silence": not (high_impact_blocks or material_blocks or fyi_blocks),
                "high_impact": _hi_items,
                "material": _mat_items,
                "fyi": _fyi_items,
            }, indent=2),
            encoding="utf-8",
        )
        log("Dashboard data written → docs/data/bob.json")
    except Exception as _e:
        log(f"Dashboard write failed: {_e}")

    save_seen_state(SEEN_STATE_PATH, prune_seen_state(seen_state_updated, SEEN_STATE_RETENTION_HOURS))
    log(f"State updated: {run_completed_count} announcement(s) marked COMPLETED this run.")
    log("Email sent.")


if __name__ == "__main__":
    main()
