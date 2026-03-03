import os
import re
import json
import ssl
import smtplib
import tempfile
import datetime as dt
from pathlib import Path
from email.message import EmailMessage

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

# -----------------------------
# Time helpers (SGT = UTC+8)
# -----------------------------
def now_sgt():
    return dt.datetime.utcnow() + dt.timedelta(hours=8)

def today_sgt_date():
    return now_sgt().date()

def cutoff_date(days_back: int):
    return today_sgt_date() - dt.timedelta(days=days_back)

# -----------------------------
# Email
# -----------------------------
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

# -----------------------------
# Config
# -----------------------------
def read_tickers():
    with open("tickers.yaml", "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("asx", []), data.get("lse", [])

# -----------------------------
# OpenAI (LLM)
# -----------------------------
def llm_client():
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])

def llm_chat(system_prompt: str, user_content: str) -> str:
    model = os.environ.get("MODEL_NAME", "gpt-4o-mini")
    client = llm_client()

    user_content = user_content[:60_000]

    resp = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    return resp.choices[0].message.content.strip()

# -----------------------------
# HTTP session + headers
# -----------------------------
def http_session():
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

# -----------------------------
# PDF handling
# -----------------------------
def download_pdf(session: requests.Session, url: str, out_path: Path) -> bool:
    """
    Returns True if a real PDF was downloaded.
    Returns False if ASX returns HTML gate or non-PDF.
    Never raises for 'not a PDF' cases; only raises for real HTTP errors.
    """
    r = session.get(url, timeout=60, allow_redirects=True)
    r.raise_for_status()

    # Check PDF signature
    if r.content[:4] != b"%PDF":
        return False

    out_path.write_bytes(r.content)
    return True

def extract_pdf_text(pdf_path: Path) -> str:
    try:
        reader = PdfReader(str(pdf_path))
        texts = []
        for page in reader.pages:
            t = page.extract_text() or ""
            if t.strip():
                texts.append(t)
        return "\n\n".join(texts).strip()
    except Exception as e:
        return f"[PDF_TEXT_EXTRACTION_FAILED: {e}]"

# -----------------------------
# ASX HTML extraction fallback
# -----------------------------
def fetch_html_text(session: requests.Session, url: str) -> str:
    """
    Fetches an HTML page and extracts visible text.
    Useful fallback when PDF is gated.
    """
    r = session.get(url, timeout=60, allow_redirects=True)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    # Remove scripts/styles/nav junk
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:80_000]  # cap

# -----------------------------
# Drive upload (service account)
# -----------------------------
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

# -----------------------------
# Announcement fetching (ASX)
# -----------------------------
def fetch_asx_announcements(session: requests.Session, ticker: str, days_back: int = 2):
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

    return items

def find_pdf_url_from_asx_link(url: str) -> str | None:
    """
    If ASX link already looks like displayAnnouncement PDF, return it.
    Otherwise, return None and we will use HTML fallback.
    """
    if "displayAnnouncement.do" in url:
        # Often already usable as a PDF endpoint (but may still be gated)
        return url
    return None

# -----------------------------
# Classification
# -----------------------------
def classify_announcement(title: str, text: str) -> str:
    t = (title + "\n" + text).lower()

    results_keywords = [
        "appendix 4e", "appendix 4d", "half year", "half-year", "h1", "hy",
        "full year", "fy", "annual report", "results", "investor presentation",
        "results presentation", "presentation",
    ]
    if any(k in t for k in results_keywords):
        return "RESULTS_HY_FY"

    acq_keywords = [
        "acquisition", "acquire", "merger", "scheme", "takeover", "bid",
        "sale and purchase", "transaction", "purchase of",
    ]
    if any(k in t for k in acq_keywords):
        return "ACQUISITION"

    cap_keywords = [
        "placement", "spp", "entitlement", "rights issue", "capital raising",
        "raise", "issuance", "offer", "convertible", "notes", "debt facility",
        "refinance", "term loan", "syndicated", "bond",
    ]
    if any(k in t for k in cap_keywords):
        return "CAPITAL_OR_DEBT_RAISE"

    contract_keywords = ["contract", "award", "order", "customer", "renewal", "termination"]
    if any(k in t for k in contract_keywords):
        return "CONTRACT_MATERIAL"

    return "OTHER"

# -----------------------------
# LLM outputs
# -----------------------------
def summarise_two_lines(ticker: str, title: str, text: str) -> str:
    user = f"Ticker: {ticker}\nTitle: {title}\n\nText:\n{text}"
    out = llm_chat(DEFAULT_2LINE_PROMPT, user)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(lines) >= 2:
        return lines[0] + "\n" + lines[1]
    if len(lines) == 1:
        return lines[0] + "\nSo what: unclear/immaterial from available text."
    return "Admin/immaterial update.\nSo what: no economic impact detected."

def deep_acquisition_memo(ticker: str, title: str, text: str) -> str:
    user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
    return llm_chat(ACQUISITION_PROMPT, user)

def deep_capital_memo(ticker: str, title: str, text: str) -> str:
    user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
    return llm_chat(CAPITAL_OR_DEBT_RAISE_PROMPT, user)

def deep_results_analysis(ticker: str, report_text: str, deck_text: str) -> str:
    user = (
        f"Ticker: {ticker}\n\n"
        f"=== OFFICIAL REPORT TEXT ===\n{report_text}\n\n"
        f"=== INVESTOR DECK TEXT ===\n{deck_text}\n"
    )
    return llm_chat(RESULTS_HYFY_PROMPT, user)

# -----------------------------
# HY/FY bundling
# -----------------------------
def likely_results_bundle_items(items_for_ticker: list[dict]) -> list[dict]:
    bundle = []
    for it in items_for_ticker:
        ttl = it["title"].lower()
        if any(k in ttl for k in ["results", "appendix", "presentation", "annual report", "half year", "full year", "fy", "h1", "hy"]):
            bundle.append(it)
    return bundle

def pick_report_and_deck_text(downloaded_texts: list[tuple[str, str]]) -> tuple[str, str]:
    pres = [(t, x) for (t, x) in downloaded_texts if "presentation" in t.lower() or "deck" in t.lower()]
    non_pres = [(t, x) for (t, x) in downloaded_texts if (t, x) not in pres]

    deck = max(pres, key=lambda tx: len(tx[1] or ""), default=("", ""))[1] if pres else ""
    report = max(non_pres, key=lambda tx: len(tx[1] or ""), default=("", ""))[1] if non_pres else ""

    if not report and downloaded_texts:
        report = max(downloaded_texts, key=lambda tx: len(tx[1] or ""))[1]
    if not deck and downloaded_texts:
        deck = min(downloaded_texts, key=lambda tx: len(tx[1] or ""))[1]

    return report, deck

# -----------------------------
# Main
# -----------------------------
def main():
    session = http_session()
    asx_tickers, lse_tickers = read_tickers()

    drive_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()

    # Fetch announcements
    all_items = []
    for t in asx_tickers:
        try:
            all_items.extend(fetch_asx_announcements(session, t, days_back=2))
        except Exception as e:
            all_items.append({"exchange":"ASX","ticker":t,"date":"","title":f"ERROR fetching announcements: {e}","url":""})

    # Group by ticker
    by_ticker: dict[str, list[dict]] = {}
    for it in all_items:
        by_ticker.setdefault(it["ticker"], []).append(it)

    high_impact_sections = []
    normal_sections = []

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        processed_hyfy = set()

        for ticker, items in by_ticker.items():
            items = [x for x in items if x.get("url")]

            # Detect any results-like item
            looks_results = any(classify_announcement(i["title"], "") == "RESULTS_HY_FY" for i in items)

            # HY/FY bundle path
            if looks_results and ticker not in processed_hyfy:
                processed_hyfy.add(ticker)

                bundle_items = likely_results_bundle_items(items)
                downloaded_texts = []
                uploaded_count = 0

                for b in bundle_items:
                    title = b["title"]
                    url = b["url"]
                    pdf_url = find_pdf_url_from_asx_link(url)

                    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                    pdf_path = tmpdir / f"{safe_name}.pdf"

                    # Try PDF first
                    got_pdf = False
                    if pdf_url:
                        try:
                            got_pdf = download_pdf(session, pdf_url, pdf_path)
                        except Exception:
                            got_pdf = False

                    if got_pdf:
                        text = extract_pdf_text(pdf_path)
                        downloaded_texts.append((title, text))

                        # Upload PDF to Drive (HY/FY only)
                        if drive_folder_id:
                            try:
                                drive_name = f"{today_sgt_date().isoformat()}_{ticker}_{safe_name}.pdf"
                                upload_to_drive(pdf_path, drive_folder_id, drive_name)
                                uploaded_count += 1
                            except Exception as e:
                                # Don’t fail the run if upload fails
                                downloaded_texts.append((f"{title} [Drive upload failed]", f"[DRIVE_UPLOAD_FAILED: {e}]"))

                    else:
                        # Fallback: HTML text
                        try:
                            html_text = fetch_html_text(session, url)
                            downloaded_texts.append((title, html_text))
                        except Exception as e:
                            downloaded_texts.append((title, f"[HTML_FALLBACK_FAILED: {e}]"))

                    # Always delete local PDF (even for HY/FY, after upload)
                    if pdf_path.exists():
                        pdf_path.unlink()

                report_text, deck_text = pick_report_and_deck_text(downloaded_texts)
                report_text = (report_text or "")[:120_000]
                deck_text = (deck_text or "")[:120_000]

                if not report_text.strip() and not deck_text.strip():
                    high_impact_sections.append(
                        f"{ticker} — HY/FY Results\n"
                        "Could not extract text (ASX gate + HTML fallback failed).\n"
                        "So what: manual open required.\n"
                    )
                    continue

                analysis = deep_results_analysis(ticker, report_text, deck_text)
                high_impact_sections.append(
                    f"{ticker} — HY/FY Results (deep analysis) — PDFs saved to Drive: {uploaded_count}\n{analysis}\n"
                )
                continue

            # Normal processing path
            for it in items:
                title = it["title"]
                url = it["url"]
                pdf_url = find_pdf_url_from_asx_link(url)

                safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                pdf_path = tmpdir / f"{safe_name}.pdf"

                text = ""
                got_pdf = False

                # Try PDF
                if pdf_url:
                    try:
                        got_pdf = download_pdf(session, pdf_url, pdf_path)
                    except Exception:
                        got_pdf = False

                if got_pdf:
                    text = extract_pdf_text(pdf_path)
                else:
                    # HTML fallback
                    try:
                        text = fetch_html_text(session, url)
                    except Exception:
                        text = ""

                # Delete any local PDF
                if pdf_path.exists():
                    pdf_path.unlink()

                cls = classify_announcement(title, text)

                if cls == "ACQUISITION":
                    memo = deep_acquisition_memo(ticker, title, text)
                    high_impact_sections.append(f"{ticker} — Acquisition\n{memo}\n")
                elif cls == "CAPITAL_OR_DEBT_RAISE":
                    memo = deep_capital_memo(ticker, title, text)
                    high_impact_sections.append(f"{ticker} — Capital/Debt Raise\n{memo}\n")
                else:
                    summary = summarise_two_lines(ticker, title, text)
                    normal_sections.append(f"{ticker} | {title}\n{summary}\n")

    subject = f"Announcements Digest - {today_sgt_date().isoformat()} (SGT)"

    body_lines = []
    body_lines.append(f"Daily Announcements Digest (last 2 days) - {today_sgt_date().isoformat()} (SGT)")
    body_lines.append("")
    body_lines.append("HIGH IMPACT (read now)")
    body_lines.append("=" * 60)
    body_lines.append("\n".join(high_impact_sections) if high_impact_sections else "None detected.")
    body_lines.append("")
    body_lines.append("EVERYTHING ELSE (2-line summaries)")
    body_lines.append("=" * 60)
    body_lines.append("\n".join(normal_sections) if normal_sections else "No other announcements found.")

    send_email(subject, "\n".join(body_lines))

if __name__ == "__main__":
    main()
