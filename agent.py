import os
import re
import json
import ssl
import smtplib
import tempfile
import datetime as dt
from pathlib import Path
from email.message import EmailMessage
from typing import Dict, List, Tuple, Optional

import requests
from bs4 import BeautifulSoup
import yaml
from pypdf import PdfReader
from openai import OpenAI

from prompts import (
    DEFAULT_2LINE_PROMPT,
    ACQUISITION_PROMPT,
    CAPITAL_OR_DEBT_RAISE_PROMPT,
    RESULTS_HYFY_PROMPT,
)
from prompts import (
    DEFAULT_2LINE_PROMPT,
    ACQUISITION_PROMPT,
    CAPITAL_OR_DEBT_RAISE_PROMPT,
    RESULTS_HYFY_PROMPT,
)

# 👇 ADD THESE TWO LINES RIGHT HERE
from playwright_fetch import fetch_pdf_with_playwright
import asyncio

# ----------------------------
# Settings / Guardrails
# ----------------------------
# ----------------------------
# Settings / Guardrails
# ----------------------------
DAYS_BACK = 2

MAX_ANNOUNCEMENTS_PER_TICKER = 12
MAX_PDFS_PER_RUN = 10
MAX_LLM_CALLS_PER_RUN = 15

MIN_RESULTS_TEXT_CHARS = 2500

MODEL_DEFAULT = "gpt-4o-mini"


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
# Logging (keep minimal)
# ----------------------------
def log(msg: str):
    print(f"[agent] {msg}", flush=True)


# ----------------------------
# Email
# ----------------------------
def send_email(subject: str, body: str):
    email_from = os.environ["EMAIL_FROM"]
    email_to = os.environ["EMAIL_TO"]
    app_password = os.environ["EMAIL_APP_PASSWORD"]

    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = subject
    msg.set_content(body)

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
# HTTP session (cookies + headers)
# ----------------------------
def http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": "https://www.asx.com.au/",
    })
    return s


# ----------------------------
# Price-sensitive headline filter
# ----------------------------
def is_price_sensitive_title(title: str) -> bool:
    t = title.lower()
    keywords = [
        # Results / trading / guidance
        "appendix 4e", "appendix 4d", "results", "half year", "half-year",
        "full year", "annual report", "trading update", "guidance",
        "earnings", "profit", "revenue", "eps", "ebit", "ebitda",
        "investor presentation", "presentation",

        # Capital / debt
        "placement", "rights issue", "entitlement", "spp", "capital raising",
        "issue of shares", "convertible", "notes", "bond", "debt facility",
        "refinance", "term loan", "facility",

        # M&A
        "acquisition", "acquire", "merger", "scheme", "takeover", "transaction",

        # Material contracts / regulatory / other
        "contract", "award", "termination", "litigation", "regulatory", "material",
        "strategic", "halt", "suspension",
    ]
    return any(k in t for k in keywords)

def looks_like_results_title(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in [
        "appendix 4e", "appendix 4d", "results", "half year", "half-year",
        "full year", "annual report", "investor presentation", "presentation",
        "results presentation"
    ])


# ----------------------------
# LLM with caps + graceful failure
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
# PDF + HTML helpers
# ----------------------------
def download_pdf(session: requests.Session, url: str, out_path: Path) -> bool:
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
    except Exception as e:
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
    return ("access to this site" in t and "agree and proceed" in t) or ("general conditions" in t and "agree and proceed" in t)


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
    items = []
    cutoff = cutoff_date(days_back)

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

        items.append({
            "exchange": "ASX",
            "ticker": ticker,
            "date": date_text,
            "title": title,
            "url": href,
        })

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
    if "displayAnnouncement.do" in url:
        return url
    return None


# ----------------------------
# Classification
# ----------------------------
def classify_announcement(title: str, text: str) -> str:
    t = (title + "\n" + (text or "")).lower()

    if any(k in t for k in [
        "appendix 4e", "appendix 4d", "half year", "half-year",
        "full year", "annual report", "results",
        "investor presentation", "results presentation",
    ]):
        return "RESULTS_HY_FY"

    if any(k in t for k in [
        "acquisition", "acquire", "merger", "scheme", "takeover",
        "transaction", "sale and purchase", "purchase of",
    ]):
        return "ACQUISITION"

    if any(k in t for k in [
        "placement", "spp", "entitlement", "rights issue", "capital raising",
        "offer", "issuance", "convertible", "notes", "debt facility",
        "refinance", "term loan", "bond",
    ]):
        return "CAPITAL_OR_DEBT_RAISE"

    if any(k in t for k in ["contract", "award", "termination", "order", "customer", "renewal"]):
        return "CONTRACT_MATERIAL"

    return "OTHER"


# ----------------------------
# LLM outputs
# ----------------------------
def summarise_two_lines_llm(ticker: str, title: str, text: str, counters: Dict) -> Optional[str]:
    user = f"Ticker: {ticker}\nTitle: {title}\n\nText:\n{text}"
    out = llm_chat(DEFAULT_2LINE_PROMPT, user, counters)

    if out in ("__LLM_SKIPPED__", "__LLM_FAILED__"):
        # If LLM isn't available, still show a minimal signal-only line (because it was price-sensitive)
        return f"{ticker}: {title[:140]}\nSo what: price-sensitive headline; open link for details."

    # Enforce exactly 2 lines
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
        return f"{ticker} — Acquisition\nCould not run LLM (limit/billing). Open link for details."
    return out

def deep_capital_memo(ticker: str, title: str, text: str, counters: Dict) -> str:
    user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
    out = llm_chat(CAPITAL_OR_DEBT_RAISE_PROMPT, user, counters)
    if out in ("__LLM_SKIPPED__", "__LLM_FAILED__"):
        return f"{ticker} — Capital/Debt Raise\nCould not run LLM (limit/billing). Open link for details."
    return out

def deep_results_analysis(ticker: str, report_text: str, deck_text: str, counters: Dict) -> str:
    user = (
        f"Ticker: {ticker}\n\n"
        f"=== OFFICIAL REPORT TEXT ===\n{report_text}\n\n"
        f"=== INVESTOR DECK TEXT ===\n{deck_text}\n"
    )
    out = llm_chat(RESULTS_HYFY_PROMPT, user, counters)
    if out in ("__LLM_SKIPPED__", "__LLM_FAILED__"):
        return f"{ticker} — Results\nCould not run LLM (limit/billing). Open documents manually."
    return out


# ----------------------------
# Google Drive upload (HY/FY only)
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
    from googleapiclient.http import MediaFileUpload
    service = drive_service()
    file_metadata = {"name": drive_filename, "parents": [folder_id]}
    media = MediaFileUpload(str(local_path), resumable=False)
    created = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    return created.get("id")


# ----------------------------
# HY/FY helpers
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
# MAIN
# ----------------------------
def main():
    subject = f"Daily Announcements Digest – {today_sgt_date().isoformat()} (SGT)"

    counters = {"MAX_LLM_CALLS_PER_RUN": MAX_LLM_CALLS_PER_RUN, "llm_calls": 0}
    session = http_session()
    asx_tickers, _lse_tickers = read_tickers()

    drive_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
    pdfs_downloaded = 0

    # Fetch announcements
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

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        processed_results = set()

        for ticker, items in by_ticker.items():
            if not items:
                continue

            # ---- RESULTS (bundle) ----
            has_results = any(looks_like_results_title(i["title"]) for i in items)
            if has_results and ticker not in processed_results:
                processed_results.add(ticker)

                bundle = likely_results_bundle_items(items)
                downloaded_texts: List[Tuple[str, str]] = []
                uploaded = 0
                any_results_link = bundle[0]["url"] if bundle else items[0]["url"]

                for b in bundle:
                    title = b["title"]
                    url = b["url"]
                    pdf_url = asx_pdf_url_from_item_url(url)

                    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                    pdf_path = tmpdir / f"{safe_name}.pdf"

                    got_pdf = False
                    if pdf_url and pdfs_downloaded < MAX_PDFS_PER_RUN:
                        try:
                            got_pdf = False

                    if pdf_url and pdfs_downloaded < MAX_PDFS_PER_RUN:
                    try:
                        got_pdf = download_pdf(session, pdf_url, pdf_path)
                    except Exception:
                        got_pdf = False

                     # If normal request failed (likely ASX gate), try Playwright
                     if not got_pdf:
                         try:
                             got_pdf = asyncio.run(fetch_pdf_with_playwright(pdf_url, pdf_path))
                         except Exception as e:
                             log(f"Playwright fetch failed: {e}")
                             got_pdf = False
                        except Exception:
                            got_pdf = False

                    if got_pdf:
                        pdfs_downloaded += 1
                        text = extract_pdf_text(pdf_path)
                        downloaded_texts.append((title, text))

                        if drive_folder_id:
                            try:
                                drive_name = f"{today_sgt_date().isoformat()}_{ticker}_{safe_name}.pdf"
                                upload_to_drive(pdf_path, drive_folder_id, drive_name)
                                uploaded += 1
                            except Exception as e:
                                log(f"Drive upload failed for {ticker}: {e}")
                    else:
                        # HTML fallback
                        try:
                            html_text = fetch_html_text(session, url)
                            downloaded_texts.append((title, html_text))
                        except Exception:
                            downloaded_texts.append((title, ""))

                    if pdf_path.exists():
                        pdf_path.unlink()

                report_text, deck_text = pick_report_and_deck_text(downloaded_texts)
                report_text = (report_text or "")[:120_000]
                deck_text = (deck_text or "")[:120_000]

                # If we only captured the ASX access-gate / legal page, do NOT run deep analysis
                if looks_like_asx_access_gate(report_text) or looks_like_asx_access_gate(deck_text) or (
                    len(report_text) < MIN_RESULTS_TEXT_CHARS and len(deck_text) < MIN_RESULTS_TEXT_CHARS
                ):
                    high_impact_blocks.append(
                        f"{ticker} — Results detected, but document could not be fetched automatically.\n"
                        f"Open manually: {any_results_link}\n"
                    )
                    continue

                analysis = deep_results_analysis(ticker, report_text, deck_text, counters)
                # Keep the output clean (no “PDFs saved: 0” noise)
                if uploaded > 0:
                    high_impact_blocks.append(f"{ticker} — Results (HY/FY) — PDFs saved to Drive: {uploaded}\n{analysis}\n")
                else:
                    high_impact_blocks.append(f"{ticker} — Results (HY/FY)\n{analysis}\n")

                continue  # don’t also process the individual results announcements

            # ---- Normal per-item processing ----
            for it in items:
                title = it["title"]
                url = it["url"]

                # IMPORTANT: ignore non price-sensitive completely (no mention)
                if not is_price_sensitive_title(title):
                    continue

                # Fetch text only for price-sensitive
                pdf_url = asx_pdf_url_from_item_url(url)
                safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                pdf_path = tmpdir / f"{safe_name}.pdf"

                text = ""
                got_pdf = False

                if pdf_url and pdfs_downloaded < MAX_PDFS_PER_RUN:
                    try:
                        got_pdf = download_pdf(session, pdf_url, pdf_path)
                    except Exception:
                        got_pdf = False

                if got_pdf:
                    pdfs_downloaded += 1
                    text = extract_pdf_text(pdf_path)
                else:
                    try:
                        text = fetch_html_text(session, url)
                    except Exception:
                        text = ""

                if pdf_path.exists():
                    pdf_path.unlink()

                # If we only got the ASX access-gate page, don’t hallucinate
                if looks_like_asx_access_gate(text):
                    material_blocks.append(
                        f"{ticker}: {title[:140]}\nSo what: could not fetch PDF automatically. Open: {url}\n"
                    )
                    continue

                cls = classify_announcement(title, text)

                if cls == "ACQUISITION":
                    memo = deep_acquisition_memo(ticker, title, text, counters)
                    high_impact_blocks.append(f"{ticker} — Acquisition\n{memo}\nOpen: {url}\n")
                elif cls == "CAPITAL_OR_DEBT_RAISE":
                    memo = deep_capital_memo(ticker, title, text, counters)
                    high_impact_blocks.append(f"{ticker} — Capital/Debt Raise\n{memo}\nOpen: {url}\n")
                else:
                    summary = summarise_two_lines_llm(ticker, title, text, counters)
                    if summary:
                        material_blocks.append(f"{summary}\nOpen: {url}\n")

    # Build email (clean and short)
    lines: List[str] = []
    lines.append(f"Daily Announcements Digest – last {DAYS_BACK} days – {today_sgt_date().isoformat()} (SGT)")
    lines.append("")

    if high_impact_blocks:
        lines.append("HIGH IMPACT")
        lines.append("-" * 60)
        lines.extend(high_impact_blocks)
        lines.append("")

    if material_blocks:
        lines.append("MATERIAL")
        lines.append("-" * 60)
        lines.extend(material_blocks)
        lines.append("")

    if not high_impact_blocks and not material_blocks:
        lines.append("No price-sensitive announcements in the last 2 days.")

    send_email(subject, "\n".join(lines))
    log("Email sent.")


if __name__ == "__main__":
    main()
