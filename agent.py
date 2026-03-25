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
# - Uses Anthropic Claude API for all AI analysis (native PDF support)

import os
import re
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
from playwright_fetch import fetch_pdf_with_playwright
from asx_fetch import fetch_asx_announcements_html

from prompts import (
    DEFAULT_2LINE_PROMPT,
    ACQUISITION_PROMPT,
    CAPITAL_OR_DEBT_RAISE_PROMPT,
    RESULTS_HYFY_PROMPT,
    STRAWMAN_500W_PROMPT,
)

BOB_NAME = "Bob the Bot"
VERSION_LABEL = "V3"

HOURS_BACK = 24
SEEN_STATE_PATH = Path(os.environ.get("SEEN_STATE_PATH", "state_seen.json"))
SEEN_STATE_RETENTION_HOURS = 72

MAX_ANNOUNCEMENTS_PER_TICKER = 12
MAX_PDFS_PER_RUN = 10
MAX_LLM_CALLS_PER_RUN = 15

MIN_RESULTS_TEXT_CHARS = 2500
MODEL_DEFAULT = "claude-sonnet-4-6"

REQUESTS_PDF_TIMEOUT_SECS = 20
HTML_TIMEOUT_SECS = 30
FUN_CONTENT_TIMEOUT_SECS = 10

COLOR_HIGH_IMPACT = "#F59E0B"
COLOR_MATERIAL = "#3B82F6"
COLOR_FYI = "#10B981"
COLOR_SILENCE = "#6B7280"
COLOR_BG = "#0B1220"
COLOR_PANEL = "#111B2E"
COLOR_TEXT = "#E5E7EB"

JOKE_API_URL = os.environ.get(
    "JOKE_API_URL",
    "https://v2.jokeapi.dev/joke/Any?blacklistFlags=nsfw,religious,racist,sexist,explicit&type=single",
).strip()
CARTOON_PAGE_URL = os.environ.get(
    "CARTOON_PAGE_URL",
    "https://www.cagle.com/category/political-cartoon/",
).strip()


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


def fetch_joke_of_the_day(session: requests.Session) -> str:
    try:
        resp = session.get(JOKE_API_URL, timeout=FUN_CONTENT_TIMEOUT_SECS, headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            if data.get("type") == "single" and isinstance(data.get("joke"), str):
                joke = data["joke"].strip()
                if joke:
                    return joke
            if data.get("type") == "twopart":
                setup = str(data.get("setup", "")).strip()
                delivery = str(data.get("delivery", "")).strip()
                joined = " — ".join([p for p in [setup, delivery] if p])
                if joined:
                    return joined
    except Exception as e:
        log(f"Joke fetch failed: {e}")
    return "Why did the investor bring a ladder? To reach higher returns."


def fetch_cartoon_of_the_day(session: requests.Session) -> Tuple[str, str]:
    fallback_title = "Political cartoon pick"
    fallback_url = CARTOON_PAGE_URL
    try:
        resp = session.get(CARTOON_PAGE_URL, timeout=FUN_CONTENT_TIMEOUT_SECS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.select("a[href]"):
            href = (a.get("href") or "").strip()
            if not href:
                continue
            lowered = href.lower()
            if "cagle.com" in lowered and "/cartoon/" in lowered:
                title = " ".join(a.get_text(" ", strip=True).split()) or fallback_title
                return title, href
    except Exception as e:
        log(f"Cartoon fetch failed: {e}")
    return fallback_title, fallback_url


def build_silence_line(session: requests.Session) -> str:
    base = f"No announcements found in the last {HOURS_BACK} hours."
    joke = fetch_joke_of_the_day(session)
    cartoon_title, cartoon_url = fetch_cartoon_of_the_day(session)
    return (
        f"{base}\n"
        f"Joke of the day: {joke}\n"
        f"Cartoon of the day: {cartoon_title} — {cartoon_url}"
    )


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
    if any(k in t for k in ["contract", "award", "termination", "trading update", "guidance"]):
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


def llm_chat(
    system_prompt: str,
    user_content: str,
    counters: Dict,
    pdf_path: Optional[Path] = None,
) -> str:
    if counters["llm_calls"] >= counters["MAX_LLM_CALLS_PER_RUN"]:
        return "__LLM_SKIPPED__"
    counters["llm_calls"] += 1
    model = os.environ.get("CLAUDE_MODEL", MODEL_DEFAULT)

    if pdf_path and pdf_path.exists():
        try:
            pdf_bytes = pdf_path.read_bytes()
            pdf_b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")
            content: object = [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_b64,
                    },
                },
                {"type": "text", "text": user_content[:50_000]},
            ]
            log(f"Sending PDF ({len(pdf_bytes)//1024}KB) natively to Claude")
        except Exception as e:
            log(f"PDF encoding failed, falling back to text: {e}")
            content = user_content[:100_000]
    else:
        content = user_content[:100_000]

    try:
        client = _anthropic_client()
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )
        return (resp.content[0].text or "").strip()
    except Exception as e:
        log(f"Claude API failed: {e}")
        return "__LLM_FAILED__"


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
    try:
        html_text = fetch_html_text(session, url)
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
    silence_line: str,
) -> Tuple[str, str]:
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
        lines.append(silence_line)
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
    if not high_impact and not material and not fyi:
        sections_html += f"""
        <div style="margin:18px 0;">
          <div style="padding:10px 12px; background:{COLOR_SILENCE}; color:#0B1220; font-weight:800; border-radius:10px; letter-spacing:0.6px;">
            SILENCE
          </div>
          <div style="margin-top:10px; padding:12px; background:{COLOR_PANEL}; border-radius:10px; color:{COLOR_TEXT};">
            {htmlmod.escape(silence_line)}
          </div>
        </div>
        """
    footer_html = "</div>"
    body_html = header_html + sections_html + footer_html
    return body_text, body_html


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


def deep_acquisition_memo(
    ticker: str, title: str, text: str, counters: Dict, pdf_path: Optional[Path] = None,
) -> str:
    if pdf_path and pdf_path.exists():
        user = f"Ticker: {ticker}\nTitle: {title}\n\nPlease analyse the attached acquisition announcement PDF."
        out = llm_chat(ACQUISITION_PROMPT, user, counters, pdf_path=pdf_path)
    elif is_meaningful_text(text, min_chars=900):
        user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
        out = llm_chat(ACQUISITION_PROMPT, user, counters)
    else:
        return "Could not extract meaningful announcement text automatically. Open the link and review manually."
    if out in ("__LLM_SKIPPED__", "__LLM_FAILED__"):
        return "LLM could not run (limit/billing)."
    return out


def deep_capital_memo(
    ticker: str, title: str, text: str, counters: Dict, pdf_path: Optional[Path] = None,
) -> str:
    if pdf_path and pdf_path.exists():
        user = f"Ticker: {ticker}\nTitle: {title}\n\nPlease analyse the attached capital/debt raise announcement PDF."
        out = llm_chat(CAPITAL_OR_DEBT_RAISE_PROMPT, user, counters, pdf_path=pdf_path)
    elif is_meaningful_text(text, min_chars=900):
        user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
        out = llm_chat(CAPITAL_OR_DEBT_RAISE_PROMPT, user, counters)
    else:
        return "Could not extract meaningful announcement text automatically. Open the link and review manually."
    if out in ("__LLM_SKIPPED__", "__LLM_FAILED__"):
        return "LLM could not run (limit/billing)."
    return out


def deep_results_analysis(
    ticker: str,
    report_text: str,
    deck_text: str,
    counters: Dict,
    report_pdf: Optional[Path] = None,
    deck_pdf: Optional[Path] = None,
) -> str:
    if report_pdf and report_pdf.exists():
        deck_context = f"\n\n=== INVESTOR DECK TEXT (extracted) ===\n{deck_text[:30_000]}" if deck_text else ""
        user = (
            f"Ticker: {ticker}\n"
            f"Please analyse the attached financial results PDF as the primary source.{deck_context}"
        )
        out = llm_chat(RESULTS_HYFY_PROMPT, user, counters, pdf_path=report_pdf)
    elif is_meaningful_text(report_text, min_chars=MIN_RESULTS_TEXT_CHARS) or is_meaningful_text(deck_text, min_chars=MIN_RESULTS_TEXT_CHARS):
        user = (
            f"Ticker: {ticker}\n\n"
            f"=== OFFICIAL REPORT TEXT ===\n{report_text}\n\n"
            f"=== INVESTOR DECK TEXT ===\n{deck_text}\n"
        )
        out = llm_chat(RESULTS_HYFY_PROMPT, user, counters)
    else:
        return "No meaningful report text or PDF available for analysis."
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


def drive_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        raise RuntimeError("Missing GDRIVE_SERVICE_ACCOUNT_JSON")
    info = json.loads(sa_json)
    scopes = ["https://www.googleapis.com/auth/drive.file"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
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


def main():
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
    run_seen_count = 0

    high_impact_blocks: List[str] = []
    material_blocks: List[str] = []
    fyi_blocks: List[str] = []
    brother_blocks: List[str] = []

    from_date = cutoff_dt_sgt(HOURS_BACK).date()
    by_ticker: Dict[str, List[Dict]] = {}
    for t in asx_tickers:
        try:
            by_ticker[t] = fetch_asx_announcements_html(session, t, from_date=from_date)
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
                if key in seen_state:
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
                drive_links: List[str] = []
                any_results_link = bundle[0]["url"] if bundle else fresh_items[0]["url"]

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
                    if got_pdf and drive_folder_id:
                        try:
                            drive_name = f"{today_sgt_date().isoformat()}_{ticker}_{safe_name}.pdf"
                            link = upload_to_drive(pdf_path, drive_folder_id, drive_name)
                            if link:
                                drive_links.append(link)
                        except Exception as e:
                            log(f"Drive upload failed for {ticker}: {e}")

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
                    if drive_links:
                        block += "Drive links:\n" + "\n".join(drive_links) + "\n"
                    high_impact_blocks.append(block)
                    if ticker == "AR9":
                        brother_blocks.append(block)
                    continue

                analysis = deep_results_analysis(
                    ticker, report_text, deck_text, counters,
                    report_pdf=report_pdf, deck_pdf=deck_pdf,
                )
                straw = strawman_post(ticker, "HY/FY Results", analysis, counters)
                header = f"{ticker} — Results (HY/FY)"
                if drive_links:
                    header += f" — Drive PDFs: {len(drive_links)}"
                if has_pdf:
                    header += " — analysed via native PDF"
                block = f"{header}\n\n{analysis}\n\nOpen: {any_results_link}\n"
                if drive_links:
                    block += "Drive links:\n" + "\n".join(drive_links) + "\n"
                block += "\nSTRAWMAN DRAFT (paste-ready, ~500w max)\n" + "-" * 45 + "\n" + straw + "\n"
                high_impact_blocks.append(block)
                if ticker == "AR9":
                    brother_blocks.append(block)
                continue

            # Per-item MATERIAL / HIGH IMPACT
            for it in fresh_items:
                title = it["title"]
                url = it["url"]
                is_price = is_price_sensitive_title(title)
                cls_title = classify_from_title_only(title)

                if not is_price:
                    continue

                if cls_title in ("ACQUISITION", "CAPITAL_OR_DEBT_RAISE"):
                    pdf_url = asx_pdf_url_from_item_url(url)
                    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                    pdf_path = tmpdir / f"{safe_name}.pdf"
                    text, got_pdf = fetch_announcement_text(session, url, pdf_url, pdf_path, counters)

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
                        continue

                    drive_links_item: List[str] = []
                    if got_pdf and drive_folder_id:
                        try:
                            drive_name = f"{today_sgt_date().isoformat()}_{ticker}_{safe_name}.pdf"
                            link = upload_to_drive(pdf_path, drive_folder_id, drive_name)
                            if link:
                                drive_links_item.append(link)
                        except Exception as e:
                            log(f"Drive upload failed for {ticker}: {e}")

                    current_pdf = pdf_path if got_pdf else None
                    if cls_title == "ACQUISITION":
                        memo = deep_acquisition_memo(ticker, title, text, counters, pdf_path=current_pdf)
                        straw = strawman_post(ticker, "Acquisition", memo, counters)
                        block = f"{ticker} — Acquisition"
                    else:
                        memo = deep_capital_memo(ticker, title, text, counters, pdf_path=current_pdf)
                        straw = strawman_post(ticker, "Capital/Debt Raise", memo, counters)
                        block = f"{ticker} — Capital/Debt Raise"
                    if got_pdf:
                        block += " — analysed via native PDF"
                    block += f"\n{memo}\nOpen: {url}\n"
                    if drive_links_item:
                        block += "Drive link(s):\n" + "\n".join(drive_links_item) + "\n"
                    block += "\nSTRAWMAN DRAFT (paste-ready, ~500w max)\n" + "-" * 45 + "\n" + straw + "\n"
                    if pdf_path.exists():
                        try:
                            pdf_path.unlink()
                        except Exception:
                            pass
                    high_impact_blocks.append(block)
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

                summary = None
                if text and is_meaningful_text(text, min_chars=600):
                    summary = summarise_two_lines_llm(ticker, title, text, counters)
                if not summary:
                    summary = f"{ticker}: {title[:160]}\nSo what: price-sensitive headline — open link for details."
                block = f"{summary}\nOpen: {url}\n"
                material_blocks.append(block)
                if ticker == "AR9":
                    brother_blocks.append(block)

    silence_line = build_silence_line(session)
    body_text, body_html = build_email(high_impact_blocks, material_blocks, fyi_blocks, silence_line)
    send_email(subject, body_text, body_html)

    brother_email = os.environ.get("BROTHER_EMAIL", "").strip()
    if brother_email and brother_blocks:
        bro_subject = f"{BOB_NAME} {VERSION_LABEL} — AR9 Digest — {today_sgt_date().isoformat()} (SGT)"
        bro_text = "\n".join([bro_subject, "", *brother_blocks])
        bro_html = "<div style='padding:18px; background:%s; color:%s; font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif;'>" % (COLOR_BG, COLOR_TEXT)
        bro_html += f"<div style='font-size:20px; font-weight:900; margin-bottom:8px;'>{htmlmod.escape(bro_subject)}</div>"
        bro_html += _html_section("AR9 ONLY", COLOR_MATERIAL, brother_blocks)
        bro_html += "</div>"
        send_email(bro_subject, bro_text, bro_html, to_addr=brother_email)

    save_seen_state(SEEN_STATE_PATH, prune_seen_state(seen_state_updated, SEEN_STATE_RETENTION_HOURS))
    log(f"Seen-state updated with {run_seen_count} new announcement(s).")
    log("Email sent.")


if __name__ == "__main__":
    main()
