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
    """
    Keep output tight. Use a small model by default.
    You can override with env MODEL_NAME if you like.
    """
    model = os.environ.get("MODEL_NAME", "gpt-4o-mini")
    client = llm_client()

    # Hard cap content to avoid huge token usage
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
# PDF handling
# -----------------------------
def download_file(url: str, out_path: Path):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Reporting-Agent/1.0; +https://github.com/JohnnyM77/Reporting-Agent)"
    }

    r = requests.get(
        url,
        timeout=60,
        headers=headers,
        allow_redirects=True
    )

    r.raise_for_status()

    # Validate we actually received a PDF
    content_type = (r.headers.get("Content-Type") or "").lower()
    first_bytes = r.content[:10]

    if ("pdf" not in content_type) and (not first_bytes.startswith(b"%PDF")):
        # Save first part for debugging
        debug_path = out_path.with_suffix(".html")
        debug_path.write_bytes(r.content[:200_000])
        raise ValueError(
            f"Expected PDF but got Content-Type={content_type}. "
            f"Saved response to {debug_path.name} for debugging."
        )

    out_path.write_bytes(r.content)

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
# Drive upload (service account)
# -----------------------------
def drive_service():
    """
    Requires env:
    - GDRIVE_SERVICE_ACCOUNT_JSON (full JSON text)
    - GDRIVE_FOLDER_ID
    """
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        raise RuntimeError("Missing GDRIVE_SERVICE_ACCOUNT_JSON secret/env")
    info = json.loads(sa_json)

    scopes = ["https://www.googleapis.com/auth/drive.file"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return build("drive", "v3", credentials=creds)

def upload_to_drive(local_path: Path, folder_id: str, drive_filename: str):
    from googleapiclient.http import MediaFileUpload

    service = drive_service()
    file_metadata = {"name": drive_filename, "parents": [folder_id]}
    media = MediaFileUpload(str(local_path), resumable=False)
    created = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
    return created.get("id")

# -----------------------------
# Announcement fetching (ASX)
# -----------------------------
def fetch_asx_announcements(ticker: str, days_back: int = 2):
    """
    Pulls ASX announcements search HTML, filters by last N days.
    """
    url = (
        "https://www.asx.com.au/asx/v2/statistics/announcements.do"
        f"?asxCode={ticker}&by=asxCode&period=M6&timeframe=D"
    )
    r = requests.get(url, timeout=30)
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

        # If we can parse date and it's older than cutoff, skip
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

def find_pdf_url_from_asx_announcement(announcement_url: str) -> str | None:
    """
    If the announcement URL is already a PDF display link, use it.
    Otherwise, open the page and try to find a PDF link.
    """
    # Many ASX links already look like displayAnnouncement.do?...display=pdf...
    if "displayAnnouncement.do" in announcement_url and "pdf" in announcement_url.lower():
        return announcement_url

    try:
        r = requests.get(announcement_url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.select("a[href]"):
            href = a["href"]
            if "displayAnnouncement.do" in href and "pdf" in href.lower():
                if href.startswith("/"):
                    return "https://www.asx.com.au" + href
                return href
    except Exception:
        return None

    return None

# -----------------------------
# LSE RR. (simple placeholder)
# -----------------------------
def fetch_lse_rns_rr(days_back: int = 2):
    # Minimal: list a handful of RNS links. You can improve parsing later.
    url = "https://www.lse.co.uk/rns/RR./"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    items = []
    for a in soup.select("a[href]"):
        text = a.get_text(" ", strip=True)
        href = a.get("href", "")
        if not href:
            continue
        if "/rns/" not in href.lower():
            continue
        if href.startswith("/"):
            href = "https://www.lse.co.uk" + href

        items.append({
            "exchange": "LSE",
            "ticker": "RR.",
            "date": "",
            "title": text[:200],
            "url": href,
        })

    return items[:20]

# -----------------------------
# Classification
# -----------------------------
def classify_announcement(title: str, text: str) -> str:
    t = (title + "\n" + text).lower()

    # Results / HY / FY
    results_keywords = [
        "appendix 4e", "appendix 4d", "half year", "half-year", "h1", "hy",
        "full year", "fy", "annual report", "results", "investor presentation",
        "results presentation", "presentation",
    ]
    if any(k in t for k in results_keywords):
        return "RESULTS_HY_FY"

    # Acquisition / M&A
    acq_keywords = [
        "acquisition", "acquire", "merger", "scheme", "takeover", "bid",
        "sale and purchase", "transaction", "purchase of", "strategic acquisition",
    ]
    if any(k in t for k in acq_keywords):
        return "ACQUISITION"

    # Capital / Debt raise
    cap_keywords = [
        "placement", "spp", "entitlement", "rights issue", "capital raising",
        "raise", "issuance", "offer", "convertible", "notes", "debt facility",
        "refinance", "term loan", "syndicated", "bond",
    ]
    if any(k in t for k in cap_keywords):
        return "CAPITAL_OR_DEBT_RAISE"

    # Contracts / material
    contract_keywords = ["contract", "award", "order", "customer", "renewal", "termination"]
    if any(k in t for k in contract_keywords):
        return "CONTRACT_MATERIAL"

    return "OTHER"

# -----------------------------
# Summaries
# -----------------------------
def summarise_two_lines(ticker: str, title: str, text: str) -> str:
    user = f"Ticker: {ticker}\nTitle: {title}\n\nText:\n{text}"
    out = llm_chat(DEFAULT_2LINE_PROMPT, user)

    # Enforce exactly two lines (best-effort)
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

def deep_results_analysis(company: str, ticker: str, report_text: str, deck_text: str) -> str:
    user = (
        f"Company: {company}\nTicker: {ticker}\n\n"
        f"=== OFFICIAL REPORT TEXT ===\n{report_text}\n\n"
        f"=== INVESTOR DECK TEXT ===\n{deck_text}\n"
    )
    return llm_chat(RESULTS_HYFY_PROMPT, user)

# -----------------------------
# HY/FY bundling logic
# -----------------------------
def likely_results_bundle_items(items_for_ticker: list[dict]) -> list[dict]:
    """
    For a given ticker, pick announcements likely to be part of results pack.
    """
    bundle = []
    for it in items_for_ticker:
        ttl = it["title"].lower()
        if any(k in ttl for k in ["results", "appendix", "presentation", "annual report", "half year", "full year", "fy", "h1", "hy"]):
            bundle.append(it)
    return bundle

def pick_report_and_deck_text(downloaded_texts: list[tuple[str, str]]) -> tuple[str, str]:
    """
    downloaded_texts = [(title, extracted_text), ...]
    Heuristic: choose "presentation" as deck; choose longest non-presentation as report.
    """
    deck = ""
    report = ""

    pres = [(t, x) for (t, x) in downloaded_texts if "presentation" in t.lower() or "deck" in t.lower()]
    non_pres = [(t, x) for (t, x) in downloaded_texts if (t, x) not in pres]

    if pres:
        # take the one with most text
        deck = max(pres, key=lambda tx: len(tx[1] or ""))[1]
    if non_pres:
        report = max(non_pres, key=lambda tx: len(tx[1] or ""))[1]

    # Fallbacks
    if not report and downloaded_texts:
        report = max(downloaded_texts, key=lambda tx: len(tx[1] or ""))[1]
    if not deck and downloaded_texts:
        deck = min(downloaded_texts, key=lambda tx: len(tx[1] or ""))[1]

    return report, deck

# -----------------------------
# Main run
# -----------------------------
def main():
    asx_tickers, lse_tickers = read_tickers()

    # 1) fetch announcements
    all_items = []
    for t in asx_tickers:
        try:
            all_items.extend(fetch_asx_announcements(t, days_back=2))
        except Exception as e:
            all_items.append({"exchange":"ASX","ticker":t,"date":"","title":f"ERROR fetching announcements: {e}","url":""})

    if "RR." in lse_tickers:
        try:
            all_items.extend(fetch_lse_rns_rr(days_back=2))
        except Exception as e:
            all_items.append({"exchange":"LSE","ticker":"RR.","date":"","title":f"ERROR fetching RNS: {e}","url":""})

    # Group by ticker for HY/FY bundling
    by_ticker = {}
    for it in all_items:
        by_ticker.setdefault(it["ticker"], []).append(it)

    high_impact_sections = []
    normal_sections = []

    drive_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        processed_hyfy_tickers = set()

        for ticker, items in by_ticker.items():
            # Skip error placeholders
            items = [x for x in items if x.get("url")]

            # Check if any item looks like results pack
            any_results = False
            for it in items:
                pdf_url = find_pdf_url_from_asx_announcement(it["url"]) if it["exchange"] == "ASX" else None
                it["pdf_url"] = pdf_url
                it["class"] = classify_announcement(it["title"], "")

                if it["class"] == "RESULTS_HY_FY":
                    any_results = True

            # HY/FY bundle path
            if any_results and ticker not in processed_hyfy_tickers and ticker != "RR.":
                processed_hyfy_tickers.add(ticker)

                bundle_items = likely_results_bundle_items(items)
                downloaded_texts = []
                uploaded_files = []

                for b in bundle_items:
                    pdf_url = b.get("pdf_url") or find_pdf_url_from_asx_announcement(b["url"])
                    if not pdf_url:
                        continue

                    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{b['title'][:80]}.pdf")
                    pdf_path = tmpdir / safe_name

                    try:
                        download_file(pdf_url, pdf_path)
                        text = extract_pdf_text(pdf_path)
                        downloaded_texts.append((b["title"], text))

                        # Upload PDFs to Drive for HY/FY
                        if drive_folder_id:
                            drive_name = f"{today_sgt_date().isoformat()}_{ticker}_{safe_name}"
                            file_id = upload_to_drive(pdf_path, drive_folder_id, drive_name)
                            uploaded_files.append((drive_name, file_id))

                    finally:
                        # Always delete local
                        if pdf_path.exists():
                            pdf_path.unlink()

                report_text, deck_text = pick_report_and_deck_text(downloaded_texts)
                # Truncate to keep costs sane
                report_text = report_text[:120_000]
                deck_text = deck_text[:120_000]

                company_name = ticker  # optional: map ticker to name later
                analysis = deep_results_analysis(company_name, ticker, report_text, deck_text)

                hdr = f"{ticker} — HY/FY Results (deep analysis)"
                if uploaded_files:
                    hdr += f" — Saved {len(uploaded_files)} PDF(s) to Drive folder"
                high_impact_sections.append(hdr + "\n" + analysis + "\n")

                # Skip normal summaries for those same items (avoid duplication)
                continue

            # Otherwise, process each announcement normally
            for it in items:
                title = it["title"]
                exchange = it["exchange"]

                # LSE path: headline-only for now
                if exchange == "LSE":
                    summary = summarise_two_lines(ticker, title, "")
                    normal_sections.append(f"{ticker} | {title}\n{summary}\n")
                    continue

                pdf_url = it.get("pdf_url") or find_pdf_url_from_asx_announcement(it["url"])
                if not pdf_url:
                    # No PDF: still two-line summary based on title only
                    summary = summarise_two_lines(ticker, title, "")
                    normal_sections.append(f"{ticker} | {title}\n{summary}\n")
                    continue

                # Download -> extract -> classify -> summarise -> delete
                safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}.pdf")
                pdf_path = tmpdir / safe_name

                try:
                    download_file(pdf_url, pdf_path)
                    text = extract_pdf_text(pdf_path)
                    text = text[:80_000]  # cap per-announcement

                    cls = classify_announcement(title, text)

                    if cls == "ACQUISITION":
                        memo = deep_acquisition_memo(ticker, title, text)
                        high_impact_sections.append(f"{ticker} — Acquisition\n{memo}\n")
                    elif cls == "CAPITAL_OR_DEBT_RAISE":
                        memo = deep_capital_memo(ticker, title, text)
                        high_impact_sections.append(f"{ticker} — Capital/Debt Raise\n{memo}\n")
                    else:
                        # Default: 2 lines
                        summary = summarise_two_lines(ticker, title, text)
                        normal_sections.append(f"{ticker} | {title}\n{summary}\n")

                finally:
                    # Delete local PDF always (non-results)
                    if pdf_path.exists():
                        pdf_path.unlink()

    # Build email
    subject = f"Announcements Digest - {today_sgt_date().isoformat()} (SGT)"

    lines = []
    lines.append(f"Daily Announcements Digest (last 2 days) - {today_sgt_date().isoformat()} (SGT)")
    lines.append("")
    lines.append("HIGH IMPACT (read now)")
    lines.append("=" * 60)
    if high_impact_sections:
        lines.extend(high_impact_sections)
    else:
        lines.append("None detected.")
    lines.append("")
    lines.append("EVERYTHING ELSE (2-line summaries)")
    lines.append("=" * 60)
    if normal_sections:
        lines.extend(normal_sections)
    else:
        lines.append("No other announcements found.")

    send_email(subject, "\n".join(lines))

if __name__ == "__main__":
    main()
