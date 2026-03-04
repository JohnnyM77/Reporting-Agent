# agent.py
#
# Investor-grade ASX announcements agent ("Bob the Bot"):
# - Ignores non-price-sensitive announcements completely (no mention in email)
# - Uses Requests first for PDFs; if ASX consent gate blocks, falls back to Playwright
# - Runs deep analysis only for HY/FY results, acquisitions, and capital/debt raises
# - Produces clean email: HIGH IMPACT + MATERIAL (only price-sensitive) or SILENCE
# - Uploads PDFs to Google Drive for big announcements and includes Drive view links
# - Generates a separate Strawman-ready post (<= ~500 words) for big announcements
# - Never hallucinates off the ASX "Access to this site" legal page

import os
import re
import json
import ssl
import smtplib
import tempfile
import datetime as dt
import html
from pathlib import Path
from email.message import EmailMessage
from typing import Dict, List, Tuple, Optional

import requests
from bs4 import BeautifulSoup
import yaml
from pypdf import PdfReader
from openai import OpenAI

# Playwright fallback for ASX consent gate
import asyncio
from playwright_fetch import fetch_pdf_with_playwright  # must exist in repo root

from prompts import (
    DEFAULT_2LINE_PROMPT,
    ACQUISITION_PROMPT,
    CAPITAL_OR_DEBT_RAISE_PROMPT,
    RESULTS_HYFY_PROMPT,
    STRAWMAN_500W_PROMPT,  # ensure this exists in prompts.py
)

BOB_NAME = "Bob the Bot"

# ----------------------------
# Settings / Guardrails
# ----------------------------
DAYS_BACK = 2

MAX_ANNOUNCEMENTS_PER_TICKER = 12
MAX_PDFS_PER_RUN = 10
MAX_LLM_CALLS_PER_RUN = 15

MIN_RESULTS_TEXT_CHARS = 2500
MODEL_DEFAULT = "gpt-4o-mini"

# Email styling colours (match your diagram intent)
COLOR_HIGH_IMPACT = "#F59E0B"  # amber/gold
COLOR_MATERIAL = "#3B82F6"     # blue
COLOR_SILENCE = "#6B7280"      # grey
COLOR_BG = "#0B1220"           # dark navy
COLOR_PANEL = "#111B2E"        # panel
COLOR_TEXT = "#E5E7EB"         # light text


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


def cutoff_date(days_back: int) -> dt.date:
    return today_sgt_date() - dt.timedelta(days=days_back)


# ----------------------------
# Email
# ----------------------------
def send_email(subject: str, body_text: str, body_html: str):
    email_from = os.environ["EMAIL_FROM"]
    email_to = os.environ["EMAIL_TO"]
    app_password = os.environ["EMAIL_APP_PASSWORD"]

    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = subject

    # Plain text fallback
    msg.set_content(body_text)

    # HTML version
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
    return data.get("asx", []), data.get("lse", [])


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
# Headline filters
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
    ]
    return any(k in t for k in keywords)


def looks_like_results_title(title: str) -> bool:
    t = title.lower()
    return any(
        k in t
        for k in [
            "appendix 4e",
            "appendix 4d",
            "results",
            "half year",
            "half-year",
            "full year",
            "annual report",
            "investor presentation",
            "results presentation",
            "presentation",
        ]
    )


# ----------------------------
# LLM (with caps)
# ----------------------------
def llm_client() -> OpenAI:
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])


def llm_chat(system_prompt: str, user_content: str, counters: Dict) -> str:
    if counters["llm_calls"] >= counters["MAX_LLM_CALLS_PER_RUN"]:
        return "__LLM_SKIPPED__"

    counters["llm_calls"] += 1

    model = os.environ.get("MODEL_NAME", MODEL_DEFAULT)
    user_content = user_content[:60_000]

    try:
        client = llm_client()
        resp = client.chat.completions.create(
            model=model,
            temperature=0.2,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log(f"LLM failed: {e}")
        return "__LLM_FAILED__"


# ----------------------------
# PDF / HTML helpers
# ----------------------------
def download_pdf_requests(session: requests.Session, url: str, out_path: Path) -> bool:
    r = session.get(url, timeout=60, allow_redirects=True)
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
    r = session.get(url, timeout=60, allow_redirects=True)
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


# ----------------------------
# ASX announcement fetching
# ----------------------------
def fetch_asx_announcements(session: requests.Session, ticker: str, days_back: int = 2) -> List[Dict]:
    url = (
        "https://www.asx.com.au/asx/v2/statistics/announcements.do"
        f"?asxCode={ticker}&by=asxCode&period=M6&timeframe=D"
    )
    r = session.get(url, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("table tr")

    cutoff = cutoff_date(days_back)
    items: List[Dict] = []

    for row in rows:
        cols = [c.get_text(strip=True) for c in row.select("td")]
        if len(cols) < 2:
            continue

        link = row.select_one("a")
        if not link or not link.get("href"):
            continue

        title = link.get_text(strip=True)
        href = link["href"]
        if href.startswith("/"):
            href = "https://www.asx.com.au" + href

        date_text = cols[0]
        item_date = None
        for fmt in ("%d/%m/%Y", "%d %b %Y"):
            try:
                item_date = dt.datetime.strptime(date_text, fmt).date()
                break
            except Exception:
                pass

        if item_date and item_date < cutoff:
            continue

        items.append(
            {
                "exchange": "ASX",
                "ticker": ticker,
                "date": date_text,
                "title": title,
                "url": href,
            }
        )

    # de-dupe by URL
    seen = set()
    out = []
    for it in items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        out.append(it)

    return out


def asx_pdf_url_from_item_url(url: str) -> Optional[str]:
    # Many ASX announcements already use displayAnnouncement.do?display=pdf&idsId=...
    if "displayAnnouncement.do" in url:
        return url
    return None


# ----------------------------
# Classification
# ----------------------------
def classify_announcement(title: str, text: str) -> str:
    t = (title + "\n" + (text or "")).lower()

    if any(
        k in t
        for k in [
            "appendix 4e",
            "appendix 4d",
            "half year",
            "half-year",
            "full year",
            "annual report",
            "results",
            "investor presentation",
            "results presentation",
        ]
    ):
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
            "offer",
            "issuance",
            "convertible",
            "notes",
            "debt facility",
            "refinance",
            "term loan",
            "bond",
        ]
    ):
        return "CAPITAL_OR_DEBT_RAISE"

    if any(k in t for k in ["contract", "award", "termination", "order", "customer", "renewal"]):
        return "CONTRACT_MATERIAL"

    return "OTHER"


# ----------------------------
# LLM wrappers (clean outputs)
# ----------------------------
def summarise_two_lines_llm(ticker: str, title: str, text: str, counters: Dict) -> Optional[str]:
    user = f"Ticker: {ticker}\nTitle: {title}\n\nText:\n{text}"
    out = llm_chat(DEFAULT_2LINE_PROMPT, user, counters)

    if out in ("__LLM_SKIPPED__", "__LLM_FAILED__"):
        # fallback but still 2 lines
        return f"{ticker}: {title[:140]}\nSo what: price-sensitive headline; open link for details."

    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(lines) >= 2:
        return lines[0] + "\n" + lines[1]
    if len(lines) == 1:
        return lines[0] + "\nSo what: open link for details."
    return None


def deep_acquisition_memo(ticker: str, title: str, text: str, counters: Dict) -> str:
    user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
    out = llm_chat(ACQUISITION_PROMPT, user, counters)
    if out in ("__LLM_SKIPPED__", "__LLM_FAILED__"):
        return "LLM could not run (limit/billing)."
    return out


def deep_capital_memo(ticker: str, title: str, text: str, counters: Dict) -> str:
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
    """
    Returns a VIEW LINK (not just file id).
    Note: Drive permissions depend on your Drive folder settings.
    """
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
    # only bundle items that look like results-related
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
# Core fetch: get PDF text with requests -> playwright -> html fallback
# ----------------------------
def fetch_announcement_text(
    session: requests.Session,
    url: str,
    pdf_url: Optional[str],
    pdf_path: Path,
    counters: Dict,
) -> Tuple[str, bool]:
    """
    Returns: (text, got_pdf)
    """
    got_pdf = False
    text = ""

    if pdf_url and counters["pdfs_downloaded"] < counters["MAX_PDFS_PER_RUN"]:
        # 1) try requests
        try:
            got_pdf = download_pdf_requests(session, pdf_url, pdf_path)
        except Exception:
            got_pdf = False

        # 2) try playwright if blocked / not PDF
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

    # 3) HTML fallback
    try:
        html_text = fetch_html_text(session, url)
        return html_text, False
    except Exception:
        return "", False


# ----------------------------
# Email formatting helpers
# ----------------------------
def _html_section(title: str, color: str, blocks: List[str]) -> str:
    safe_title = html.escape(title)
    if not blocks:
        return ""
    items_html = ""
    for b in blocks:
        # Convert plaintext blocks to HTML safely, preserving line breaks
        items_html += f"<div style='margin:12px 0; padding:12px; background:{COLOR_PANEL}; border-radius:10px; white-space:pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; font-size:13px; color:{COLOR_TEXT};'>{html.escape(b)}</div>"
    return f"""
    <div style="margin:18px 0;">
      <div style="padding:10px 12px; background:{color}; color:#0B1220; font-weight:800; border-radius:10px; letter-spacing:0.6px;">
        {safe_title}
      </div>
      {items_html}
    </div>
    """


def build_email(subject: str, high_impact: List[str], material: List[str], silence_line: str, counters: Dict) -> Tuple[str, str]:
    # Plain text
    lines: List[str] = []
    lines.append(f"{BOB_NAME}")
    lines.append("=" * len(BOB_NAME))
    lines.append(f"Daily Announcements Digest — last {DAYS_BACK} days — {today_sgt_date().isoformat()} (SGT)")
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

    if not high_impact and not material:
        lines.append(silence_line)

    body_text = "\n".join(lines)

    # HTML
    header_html = f"""
    <div style="padding:18px; background:{COLOR_BG}; color:{COLOR_TEXT}; font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif;">
      <div style="font-size:22px; font-weight:900; margin-bottom:6px;">{html.escape(BOB_NAME)}</div>
      <div style="opacity:0.9; font-size:14px; margin-bottom:10px;">
        Daily Announcements Digest — last {DAYS_BACK} days — {today_sgt_date().isoformat()} (SGT)
      </div>
      <div style="opacity:0.75; font-size:12px; margin-bottom:18px;">
        Run caps: MAX_PDFS={MAX_PDFS_PER_RUN}, MAX_LLM_CALLS={MAX_LLM_CALLS_PER_RUN}, MAX_PER_TICKER={MAX_ANNOUNCEMENTS_PER_TICKER}
      </div>
    """

    sections_html = ""
    sections_html += _html_section("HIGH IMPACT", COLOR_HIGH_IMPACT, high_impact)
    sections_html += _html_section("MATERIAL", COLOR_MATERIAL, material)

    if not high_impact and not material:
        sections_html += f"""
        <div style="margin:18px 0;">
          <div style="padding:10px 12px; background:{COLOR_SILENCE}; color:#0B1220; font-weight:800; border-radius:10px; letter-spacing:0.6px;">
            SILENCE
          </div>
          <div style="margin-top:10px; padding:12px; background:{COLOR_PANEL}; border-radius:10px; color:{COLOR_TEXT};">
            {html.escape(silence_line)}
          </div>
        </div>
        """

    footer_html = "</div>"

    body_html = header_html + sections_html + footer_html
    return body_text, body_html


# ----------------------------
# MAIN
# ----------------------------
def main():
    subject = f"{BOB_NAME} — Daily Announcements Digest — {today_sgt_date().isoformat()} (SGT)"

    session = http_session()
    asx_tickers, _lse_tickers = read_tickers()

    drive_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()

    counters = {
        "MAX_LLM_CALLS_PER_RUN": MAX_LLM_CALLS_PER_RUN,
        "llm_calls": 0,
        "MAX_PDFS_PER_RUN": MAX_PDFS_PER_RUN,
        "pdfs_downloaded": 0,
    }

    # Fetch announcements by ticker
    by_ticker: Dict[str, List[Dict]] = {}
    for t in asx_tickers:
        try:
            items = fetch_asx_announcements(session, t, days_back=DAYS_BACK)
            by_ticker[t] = items[:MAX_ANNOUNCEMENTS_PER_TICKER]
        except Exception as e:
            log(f"Fetch failed for {t}: {e}")
            by_ticker[t] = []

    high_impact_blocks: List[str] = []
    material_blocks: List[str] = []

    processed_results = set()

    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)

        for ticker, items in by_ticker.items():
            if not items:
                continue

            # ----------------------------
            # RESULTS (bundle per ticker)
            # ----------------------------
            if any(looks_like_results_title(i["title"]) for i in items) and ticker not in processed_results:
                processed_results.add(ticker)

                bundle = likely_results_bundle_items(items)
                downloaded_texts: List[Tuple[str, str]] = []
                drive_links: List[str] = []
                any_results_link = bundle[0]["url"] if bundle else items[0]["url"]

                for b in bundle:
                    title = b["title"]
                    url = b["url"]
                    pdf_url = asx_pdf_url_from_item_url(url)

                    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                    pdf_path = tmpdir / f"{safe_name}.pdf"

                    text, got_pdf = fetch_announcement_text(session, url, pdf_url, pdf_path, counters)
                    downloaded_texts.append((title, text))

                    # Upload PDFs for HY/FY bundle items (Drive configured)
                    if got_pdf and drive_folder_id:
                        try:
                            drive_name = f"{today_sgt_date().isoformat()}_{ticker}_{safe_name}.pdf"
                            link = upload_to_drive(pdf_path, drive_folder_id, drive_name)
                            if link:
                                drive_links.append(link)
                        except Exception as e:
                            log(f"Drive upload failed for {ticker}: {e}")

                    # delete local pdf after processing
                    if pdf_path.exists():
                        try:
                            pdf_path.unlink()
                        except Exception:
                            pass

                report_text, deck_text = pick_report_and_deck_text(downloaded_texts)
                report_text = (report_text or "")[:120_000]
                deck_text = (deck_text or "")[:120_000]

                # If we only captured ASX legal gate / junk text, do NOT deep analyse
                if (
                    looks_like_asx_access_gate(report_text)
                    or looks_like_asx_access_gate(deck_text)
                    or (len(report_text) < MIN_RESULTS_TEXT_CHARS and len(deck_text) < MIN_RESULTS_TEXT_CHARS)
                ):
                    block = (
                        f"{ticker} — Results detected, but PDF could not be fetched automatically.\n"
                        f"Open manually: {any_results_link}\n"
                    )
                    high_impact_blocks.append(block)
                    continue

                analysis = deep_results_analysis(ticker, report_text, deck_text, counters)

                # Strawman draft (<= ~500 words)
                straw = strawman_post(ticker, "HY/FY Results", analysis, counters)

                header = f"{ticker} — Results (HY/FY)"
                if drive_links:
                    header += f" — Drive PDFs: {len(drive_links)}"

                block = f"{header}\n\n{analysis}\n\nOpen: {any_results_link}\n"
                if drive_links:
                    block += "Drive links:\n" + "\n".join(drive_links) + "\n"

                block += "\nSTRAWMAN DRAFT (paste-ready, ~500w max)\n" + "-" * 45 + "\n" + straw + "\n"
                high_impact_blocks.append(block)
                continue

            # ----------------------------
            # Normal per-item processing
            # ----------------------------
            for it in items:
                title = it["title"]
                url = it["url"]

                # Ignore non-price-sensitive completely
                if not is_price_sensitive_title(title):
                    continue

                pdf_url = asx_pdf_url_from_item_url(url)
                safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                pdf_path = tmpdir / f"{safe_name}.pdf"

                text, got_pdf = fetch_announcement_text(session, url, pdf_url, pdf_path, counters)

                # If ASX gate page, do not hallucinate
                if looks_like_asx_access_gate(text):
                    # Still price sensitive title; but we don't want spam — keep it minimal
                    material_blocks.append(
                        f"{ticker}: {title[:140]}\nSo what: could not fetch PDF automatically. Open: {url}\n"
                    )
                    # delete local pdf if any
                    if pdf_path.exists():
                        try:
                            pdf_path.unlink()
                        except Exception:
                            pass
                    continue

                cls = classify_announcement(title, text)

                # Big announcements: upload PDF to Drive (if we got it) and include link
                drive_links: List[str] = []
                if got_pdf and drive_folder_id and cls in ("ACQUISITION", "CAPITAL_OR_DEBT_RAISE"):
                    try:
                        drive_name = f"{today_sgt_date().isoformat()}_{ticker}_{safe_name}.pdf"
                        link = upload_to_drive(pdf_path, drive_folder_id, drive_name)
                        if link:
                            drive_links.append(link)
                    except Exception as e:
                        log(f"Drive upload failed for {ticker}: {e}")

                # delete local pdf after processing
                if pdf_path.exists():
                    try:
                        pdf_path.unlink()
                    except Exception:
                        pass

                if cls == "ACQUISITION":
                    memo = deep_acquisition_memo(ticker, title, text, counters)
                    straw = strawman_post(ticker, "Acquisition", memo, counters)
                    block = f"{ticker} — Acquisition\n{memo}\nOpen: {url}\n"
                    if drive_links:
                        block += "Drive link:\n" + "\n".join(drive_links) + "\n"
                    block += "\nSTRAWMAN DRAFT (paste-ready, ~500w max)\n" + "-" * 45 + "\n" + straw + "\n"
                    high_impact_blocks.append(block)

                elif cls == "CAPITAL_OR_DEBT_RAISE":
                    memo = deep_capital_memo(ticker, title, text, counters)
                    straw = strawman_post(ticker, "Capital/Debt Raise", memo, counters)
                    block = f"{ticker} — Capital/Debt Raise\n{memo}\nOpen: {url}\n"
                    if drive_links:
                        block += "Drive link:\n" + "\n".join(drive_links) + "\n"
                    block += "\nSTRAWMAN DRAFT (paste-ready, ~500w max)\n" + "-" * 45 + "\n" + straw + "\n"
                    high_impact_blocks.append(block)

                elif cls == "RESULTS_HY_FY":
                    # Results handled by bundle above; if one slips through, keep it clean
                    material_blocks.append(
                        f"{ticker}: {title[:140]}\nSo what: results item detected. Open: {url}\n"
                    )

                else:
                    summary = summarise_two_lines_llm(ticker, title, text, counters)
                    if summary:
                        material_blocks.append(f"{summary}\nOpen: {url}\n")

    # ----------------------------
    # Build email (clean + colour-coded HTML)
    # ----------------------------
    silence_line = "No price-sensitive announcements in the last 2 days."
    body_text, body_html = build_email(subject, high_impact_blocks, material_blocks, silence_line, counters)

    send_email(subject, body_text, body_html)
    log("Email sent.")


if __name__ == "__main__":
    main()
