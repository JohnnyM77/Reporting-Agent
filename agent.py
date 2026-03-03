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

# ============================================================
# SETTINGS / GUARDRAILS (prevent runaway loops/cost)
# ============================================================

DAYS_BACK = 2

# Hard caps to prevent endless loops / large spends:
MAX_ANNOUNCEMENTS_PER_TICKER = 12   # max announcements processed per ticker (per run)
MAX_PDFS_PER_RUN = 10               # max PDFs downloaded per run (HY/FY + others combined)
MAX_LLM_CALLS_PER_RUN = 15          # max LLM calls per run

# Min text length to attempt HY/FY deep analysis
MIN_RESULTS_TEXT_CHARS = 2500

# ============================================================
# Time helpers (SGT = UTC+8)
# ============================================================

def now_sgt() -> dt.datetime:
    return dt.datetime.utcnow() + dt.timedelta(hours=8)

def today_sgt_date() -> dt.date:
    return now_sgt().date()

def cutoff_date(days_back: int) -> dt.date:
    return today_sgt_date() - dt.timedelta(days=days_back)

# ============================================================
# Logging (shows up in Actions logs)
# ============================================================

def log(msg: str):
    print(f"[agent] {msg}", flush=True)

# ============================================================
# Email
# ============================================================

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

# ============================================================
# Config
# ============================================================

def read_tickers() -> Tuple[List[str], List[str]]:
    """
    Expects tickers.yaml with keys:
      asx: [DRO, RMD, ...]
      lse: [RR., ...]   (optional)
    """
    with open("tickers.yaml", "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("asx", []), data.get("lse", [])

# ============================================================
# HTTP session (cookies + headers)
# ============================================================

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

# ============================================================
# Price-sensitive headline filter (NO PDF needed)
# ============================================================

def is_price_sensitive_title(title: str) -> bool:
    t = title.lower()

    keywords = [
        # Results / trading / guidance
        "appendix 4e", "appendix 4d", "results", "half year", "half-year",
        "full year", "annual report", "trading update", "guidance",
        "earnings", "profit", "revenue", "eps", "ebit", "ebitda",

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

# ============================================================
# OpenAI (LLM) with hard caps + graceful failure
# ============================================================

def llm_client() -> OpenAI:
    return OpenAI(api_key=os.environ["OPENAI_API_KEY"])

def llm_chat(system_prompt: str, user_content: str, counters: Dict) -> str:
    """
    Returns model output, or a safe fallback string.
    Never raises (so workflow doesn't crash).
    """
    if counters["llm_calls"] >= counters["MAX_LLM_CALLS_PER_RUN"]:
        return "LLM skipped (max LLM calls reached)."

    counters["llm_calls"] += 1

    model = os.environ.get("MODEL_NAME", "gpt-4o-mini")
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
        # This catches quota/billing errors too (429 insufficient_quota)
        log(f"LLM call failed: {e}")
        return "LLM unavailable (quota/billing)."

# ============================================================
# Cheap 2-line summary WITHOUT LLM (deterministic)
# ============================================================

def two_line_no_llm(ticker: str, title: str) -> str:
    line1 = f"{ticker}: {title[:140]}"
    line2 = "So what: likely non-material/admin — skipped PDF + skipped LLM."
    return line1 + "\n" + line2

# ============================================================
# PDF + HTML helpers
# ============================================================

def download_pdf(session: requests.Session, url: str, out_path: Path) -> bool:
    """
    Returns True if a real PDF was downloaded.
    Returns False if HTML or non-PDF content returned.
    Raises only for real HTTP errors.
    """
    r = session.get(url, timeout=60, allow_redirects=True)
    r.raise_for_status()

    # PDF magic bytes
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

def fetch_html_text(session: requests.Session, url: str) -> str:
    """
    Fetch HTML and extract visible text.
    If the page is the ASX 'Access to this site' gate, this will be junk,
    but still safe (we can fall back to title-only).
    """
    r = session.get(url, timeout=60, allow_redirects=True)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:80_000]

# ============================================================
# ASX announcement fetching
# ============================================================

def fetch_asx_announcements(session: requests.Session, ticker: str, days_back: int = 2) -> List[Dict]:
    """
    Pull ASX announcements list HTML, filter to last N days.
    """
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

def asx_pdf_url_from_item_url(url: str) -> Optional[str]:
    """
    Many ASX announcement links already point at displayAnnouncement.do?display=pdf&idsId=...
    If present, treat as PDF endpoint (though ASX may still gate it).
    """
    if "displayAnnouncement.do" in url:
        return url
    return None

# ============================================================
# Classification (title + optional extracted text)
# ============================================================

def classify_announcement(title: str, text: str) -> str:
    t = (title + "\n" + (text or "")).lower()

    # Results
    if any(k in t for k in [
        "appendix 4e", "appendix 4d", "half year", "half-year", "h1", "hy",
        "full year", "fy", "annual report", "results", "investor presentation",
        "results presentation", "presentation",
    ]):
        return "RESULTS_HY_FY"

    # Acquisition / M&A
    if any(k in t for k in [
        "acquisition", "acquire", "merger", "scheme", "takeover", "bid",
        "sale and purchase", "transaction", "purchase of",
    ]):
        return "ACQUISITION"

    # Capital / Debt raise
    if any(k in t for k in [
        "placement", "spp", "entitlement", "rights issue", "capital raising",
        "raise", "issuance", "offer", "convertible", "notes", "debt facility",
        "refinance", "term loan", "syndicated", "bond",
    ]):
        return "CAPITAL_OR_DEBT_RAISE"

    # Contracts / material
    if any(k in t for k in ["contract", "award", "order", "customer", "renewal", "termination"]):
        return "CONTRACT_MATERIAL"

    return "OTHER"

# ============================================================
# LLM-based outputs (only for price-sensitive)
# ============================================================

def summarise_two_lines_llm(ticker: str, title: str, text: str, counters: Dict) -> str:
    user = f"Ticker: {ticker}\nTitle: {title}\n\nText:\n{text}"
    out = llm_chat(DEFAULT_2LINE_PROMPT, user, counters)

    if out.startswith("LLM unavailable") or out.startswith("LLM skipped"):
        # deterministic fallback
        return f"{ticker}: {title[:140]}\nSo what: LLM unavailable/limited — open announcement manually."

    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(lines) >= 2:
        return lines[0] + "\n" + lines[1]
    if len(lines) == 1:
        return lines[0] + "\nSo what: unclear/immaterial from available text."
    return f"{ticker}: {title[:140]}\nSo what: unclear — open announcement manually."

def deep_acquisition_memo(ticker: str, title: str, text: str, counters: Dict) -> str:
    user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
    return llm_chat(ACQUISITION_PROMPT, user, counters)

def deep_capital_memo(ticker: str, title: str, text: str, counters: Dict) -> str:
    user = f"Ticker: {ticker}\nTitle: {title}\n\nAnnouncement text:\n{text}"
    return llm_chat(CAPITAL_OR_DEBT_RAISE_PROMPT, user, counters)

def deep_results_analysis(ticker: str, report_text: str, deck_text: str, counters: Dict) -> str:
    user = (
        f"Ticker: {ticker}\n\n"
        f"=== OFFICIAL REPORT TEXT ===\n{report_text}\n\n"
        f"=== INVESTOR DECK TEXT ===\n{deck_text}\n"
    )
    return llm_chat(RESULTS_HYFY_PROMPT, user, counters)

# ============================================================
# Google Drive upload (HY/FY only)
# ============================================================

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

# ============================================================
# HY/FY bundling helpers
# ============================================================

def likely_results_bundle_items(items_for_ticker: List[Dict]) -> List[Dict]:
    # pick only results-ish titles
    out = []
    for it in items_for_ticker:
        if looks_like_results_title(it["title"]):
            out.append(it)
    return out

def pick_report_and_deck_text(downloaded_texts: List[Tuple[str, str]]) -> Tuple[str, str]:
    """
    Heuristic:
      - deck: title contains presentation/deck
      - report: longest non-presentation text
    """
    pres = [(t, x) for (t, x) in downloaded_texts if "presentation" in t.lower() or "deck" in t.lower()]
    non_pres = [(t, x) for (t, x) in downloaded_texts if (t, x) not in pres]

    deck = max(pres, key=lambda tx: len(tx[1] or ""), default=("", ""))[1] if pres else ""
    report = max(non_pres, key=lambda tx: len(tx[1] or ""), default=("", ""))[1] if non_pres else ""

    # fallback to anything we have
    if not report and downloaded_texts:
        report = max(downloaded_texts, key=lambda tx: len(tx[1] or ""))[1]
    if not deck and downloaded_texts:
        deck = min(downloaded_texts, key=lambda tx: len(tx[1] or ""))[1]

    return report, deck

# ============================================================
# MAIN
# ============================================================

def main():
    subject = f"Announcements Digest - {today_sgt_date().isoformat()} (SGT)"

    counters = {
        "MAX_LLM_CALLS_PER_RUN": MAX_LLM_CALLS_PER_RUN,
        "llm_calls": 0,
    }

    session = http_session()
    asx_tickers, lse_tickers = read_tickers()  # lse not used yet (safe)

    drive_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()

    pdfs_downloaded = 0

    # Fetch announcements
    all_items: List[Dict] = []
    for t in asx_tickers:
        try:
            items = fetch_asx_announcements(session, t, days_back=DAYS_BACK)
            all_items.extend(items[:MAX_ANNOUNCEMENTS_PER_TICKER])
        except Exception as e:
            all_items.append({"exchange": "ASX", "ticker": t, "date": "", "title": f"ERROR fetching announcements: {e}", "url": ""})

    # Group by ticker
    by_ticker: Dict[str, List[Dict]] = {}
    for it in all_items:
        by_ticker.setdefault(it["ticker"], []).append(it)

    high_impact_sections: List[str] = []
    normal_sections: List[str] = []

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            processed_results_tickers = set()

            for ticker, items in by_ticker.items():
                items = [x for x in items if x.get("url")]
                if not items:
                    continue

                # HY/FY bundle path: only if we see results-ish headlines
                has_results = any(looks_like_results_title(i["title"]) for i in items)

                if has_results and ticker not in processed_results_tickers:
                    processed_results_tickers.add(ticker)

                    bundle = likely_results_bundle_items(items)
                    downloaded_texts: List[Tuple[str, str]] = []
                    uploaded_pdf_count = 0

                    for b in bundle:
                        title = b["title"]
                        url = b["url"]
                        pdf_url = asx_pdf_url_from_item_url(url)

                        safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                        pdf_path = tmpdir / f"{safe_name}.pdf"

                        got_pdf = False

                        # Only download PDFs if we have remaining PDF budget
                        if pdf_url and pdfs_downloaded < MAX_PDFS_PER_RUN:
                            try:
                                got_pdf = download_pdf(session, pdf_url, pdf_path)
                            except Exception as e:
                                log(f"HY/FY PDF download error {ticker}: {e}")
                                got_pdf = False

                        if got_pdf:
                            pdfs_downloaded += 1
                            text = extract_pdf_text(pdf_path)
                            downloaded_texts.append((title, text))

                            # Save HY/FY PDFs to Drive (only if we actually downloaded PDF)
                            if drive_folder_id:
                                try:
                                    drive_name = f"{today_sgt_date().isoformat()}_{ticker}_{safe_name}.pdf"
                                    upload_to_drive(pdf_path, drive_folder_id, drive_name)
                                    uploaded_pdf_count += 1
                                except Exception as e:
                                    log(f"Drive upload failed for {ticker}: {e}")
                        else:
                            # Fallback to HTML text (may still be junk, but safe)
                            try:
                                html_text = fetch_html_text(session, url)
                                downloaded_texts.append((title, html_text))
                            except Exception as e:
                                downloaded_texts.append((title, f"[HTML_FALLBACK_FAILED: {e}]"))

                        # Always delete local PDF
                        if pdf_path.exists():
                            pdf_path.unlink()

                    report_text, deck_text = pick_report_and_deck_text(downloaded_texts)
                    report_text = (report_text or "")[:120_000]
                    deck_text = (deck_text or "")[:120_000]

                    # If we don’t have enough substance, don’t waste LLM calls
                    if len(report_text) < MIN_RESULTS_TEXT_CHARS and len(deck_text) < MIN_RESULTS_TEXT_CHARS:
                        high_impact_sections.append(
                            f"{ticker} — HY/FY Results\n"
                            f"Not enough text extracted (likely ASX gating/scanned PDF). PDFs saved to Drive: {uploaded_pdf_count}\n"
                            "So what: open the announcements manually for the full documents.\n"
                        )
                        continue

                    analysis = deep_results_analysis(ticker, report_text, deck_text, counters)
                    high_impact_sections.append(
                        f"{ticker} — HY/FY Results (deep analysis) — PDFs saved to Drive: {uploaded_pdf_count}\n{analysis}\n"
                    )

                    # Don’t duplicate the individual announcements as normal items
                    continue

                # Normal per-item processing:
                for it in items:
                    title = it["title"]
                    url = it["url"]

                    # 1) If NOT price-sensitive => deterministic 2-line, no PDF, no LLM
                    if not is_price_sensitive_title(title):
                        normal_sections.append(f"{ticker} | {title}\n{two_line_no_llm(ticker, title)}\n")
                        continue

                    # 2) Price-sensitive => try to fetch some text (PDF if allowed; else HTML)
                    pdf_url = asx_pdf_url_from_item_url(url)
                    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", f"{ticker}_{title[:80]}")
                    pdf_path = tmpdir / f"{safe_name}.pdf"

                    text = ""
                    got_pdf = False

                    if pdf_url and pdfs_downloaded < MAX_PDFS_PER_RUN:
                        try:
                            got_pdf = download_pdf(session, pdf_url, pdf_path)
                        except Exception as e:
                            log(f"PDF download error {ticker}: {e}")
                            got_pdf = False

                    if got_pdf:
                        pdfs_downloaded += 1
                        text = extract_pdf_text(pdf_path)
                    else:
                        # HTML fallback
                        try:
                            text = fetch_html_text(session, url)
                        except Exception:
                            text = ""

                    if pdf_path.exists():
                        pdf_path.unlink()

                    cls = classify_announcement(title, text)

                    # 3) High impact deep memos (still price-sensitive)
                    if cls == "ACQUISITION":
                        memo = deep_acquisition_memo(ticker, title, text, counters)
                        high_impact_sections.append(f"{ticker} — Acquisition\n{memo}\n")
                    elif cls == "CAPITAL_OR_DEBT_RAISE":
                        memo = deep_capital_memo(ticker, title, text, counters)
                        high_impact_sections.append(f"{ticker} — Capital/Debt Raise\n{memo}\n")
                    elif cls == "RESULTS_HY_FY":
                        # If a results-ish announcement slipped through (not bundled), do a 2-line LLM summary only
                        summary = summarise_two_lines_llm(ticker, title, text, counters)
                        normal_sections.append(f"{ticker} | {title}\n{summary}\n")
                    else:
                        # Default price-sensitive: LLM 2-line summary (cheap)
                        summary = summarise_two_lines_llm(ticker, title, text, counters)
                        normal_sections.append(f"{ticker} | {title}\n{summary}\n")

        # Build email
        lines: List[str] = []
        lines.append(f"Daily Announcements Digest (last {DAYS_BACK} days) - {today_sgt_date().isoformat()} (SGT)")
        lines.append(f"Run caps: MAX_PDFS={MAX_PDFS_PER_RUN}, MAX_LLM_CALLS={MAX_LLM_CALLS_PER_RUN}, MAX_PER_TICKER={MAX_ANNOUNCEMENTS_PER_TICKER}")
        lines.append("")

        lines.append("HIGH IMPACT (read now)")
        lines.append("=" * 70)
        if high_impact_sections:
            lines.extend(high_impact_sections)
        else:
            lines.append("None detected.")
        lines.append("")

        lines.append("EVERYTHING ELSE")
        lines.append("=" * 70)
        if normal_sections:
            lines.extend(normal_sections)
        else:
            lines.append("No other announcements found.")

        send_email(subject, "\n".join(lines))
        log("Email sent successfully.")

    except Exception as e:
        # Absolute safety net: never leave you with nothing
        log(f"Unexpected failure in main: {e}")
        body = (
            f"Daily Announcements Digest FAILED - {today_sgt_date().isoformat()} (SGT)\n\n"
            f"Error: {e}\n\n"
            "Actions: check GitHub Actions logs. The pipeline caught this error and still emailed you.\n"
        )
        try:
            send_email(subject.replace("Digest", "Digest FAILED"), body)
        except Exception as e2:
            log(f"Failed to send failure email: {e2}")

if __name__ == "__main__":
    main()
