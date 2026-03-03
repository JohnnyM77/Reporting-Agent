import os
import smtplib
import ssl
import datetime as dt
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup
import yaml


# ---------- Helpers ----------
def now_sgt_date():
    # Singapore is UTC+8. We'll use UTC time + 8 hours to get "today" in Singapore.
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()


def read_tickers():
    with open("tickers.yaml", "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("asx", []), data.get("lse", [])


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


# ---------- ASX announcements ----------
def fetch_asx_announcements(ticker: str, days_back: int = 2):
    """
    Uses the ASX announcements search page (HTML) which is easier to parse than the JS-heavy company page.
    """
    # We ask for the last 6 months and then filter locally to last X days.
    url = (
        "https://www.asx.com.au/asx/v2/statistics/announcements.do"
        f"?asxCode={ticker}&by=asxCode&period=M6&timeframe=D"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    # This table structure can change; this parsing is intentionally conservative:
    rows = soup.select("table tr")
    items = []

    cutoff = now_sgt_date() - dt.timedelta(days=days_back)

    for row in rows:
        cols = [c.get_text(strip=True) for c in row.select("td")]
        if len(cols) < 2:
            continue

        # Typical columns include date/time and headline, but formats vary.
        # We'll look for a link (announcement detail)
        link = row.select_one("a")
        if not link or not link.get("href"):
            continue

        title = link.get_text(strip=True)
        href = link["href"]
        if href.startswith("/"):
            href = "https://www.asx.com.au" + href

        # Attempt to parse a date from first column
        # If it fails, keep it anyway (still useful)
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
                "ticker": ticker,
                "date": date_text,
                "title": title,
                "url": href,
            }
        )

    return items


# ---------- LSE RNS (Rolls-Royce RR.) ----------
def fetch_lse_rns_rr(days_back: int = 2):
    url = "https://www.lse.co.uk/rns/RR./"
    r = requests.get(url, timeout=30)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    cutoff = now_sgt_date() - dt.timedelta(days=days_back)

    items = []
    # LSE page structure can vary; we grab links that look like RNS items.
    for a in soup.select("a"):
        text = a.get_text(" ", strip=True)
        href = a.get("href", "")
        if not href:
            continue

        # Heuristic: RNS links often contain "/rns/"
        if "/rns/" not in href and "rns" not in href.lower():
            continue

        if href.startswith("/"):
            href = "https://www.lse.co.uk" + href

        # Try to find a nearby date (best-effort)
        items.append(
            {
                "ticker": "RR.",
                "date": "",
                "title": text[:200],
                "url": href,
            }
        )

    # We can't reliably filter by date without deeper parsing; keep it simple for phase 1.
    # You’ll still get "last 2 days-ish" once we improve parsing or use a better feed.
    return items[:20]


def build_digest(asx_items, lse_items):
    lines = []
    lines.append(f"Daily Announcements Digest (last 2 days) - {now_sgt_date().isoformat()} (SGT)")
    lines.append("")
    lines.append("ASX")
    lines.append("-" * 60)

    if not asx_items:
        lines.append("No ASX announcements found (or parsing needs adjustment).")
    else:
        for it in asx_items:
            lines.append(f"{it['ticker']} | {it['date']} | {it['title']}")
            lines.append(f"  Link: {it['url']}")
            lines.append("  Summary: (Phase 1) Headline-only summary. PDF summarisation comes next.")
            lines.append("")

    lines.append("")
    lines.append("LSE (RR.)")
    lines.append("-" * 60)

    if not lse_items:
        lines.append("No LSE items found (or parsing needs adjustment).")
    else:
        for it in lse_items:
            lines.append(f"{it['ticker']} | {it['title']}")
            lines.append(f"  Link: {it['url']}")
            lines.append("  Summary: (Phase 1) Headline-only summary. RNS parsing comes next.")
            lines.append("")

    return "\n".join(lines)


def main():
    asx_tickers, lse_tickers = read_tickers()

    all_asx = []
    for t in asx_tickers:
        try:
            all_asx.extend(fetch_asx_announcements(t, days_back=2))
        except Exception as e:
            all_asx.append({"ticker": t, "date": "", "title": f"ERROR fetching announcements: {e}", "url": ""})

    all_lse = []
    if "RR." in lse_tickers:
        try:
            all_lse = fetch_lse_rns_rr(days_back=2)
        except Exception as e:
            all_lse = [{"ticker": "RR.", "date": "", "title": f"ERROR fetching RNS: {e}", "url": ""}]

    digest = build_digest(all_asx, all_lse)
    subject = f"Announcements Digest - {now_sgt_date().isoformat()} (SGT)"
    send_email(subject, digest)


if __name__ == "__main__":
    main()
