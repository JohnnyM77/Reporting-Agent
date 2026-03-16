from __future__ import annotations

import mimetypes
import smtplib
import ssl
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import TypedDict

from .config import EmailSettings
from .screening import TickerScreenResult


class WatchlistEmailData(TypedDict):
    """Data structure for a single watchlist in combined email."""
    watchlist_name: str
    run_date: str
    results: list[TickerScreenResult]
    flagged: list[TickerScreenResult]
    chart_notes: dict[str, str]
    inline_pngs: dict[str, str] | None  # ticker -> content-id


def _fmt(n: float) -> str:
    return f"{n:.2f}"


def build_combined_html(
    watchlist_data: list[WatchlistEmailData],
) -> str:
    """Build HTML for multiple watchlists combined in one email."""
    if not watchlist_data:
        return "<p>No watchlist data available.</p>"
    
    # Get run date from first watchlist (should be same for all)
    run_date = watchlist_data[0]["run_date"]
    
    # Overall summary
    total_checked = sum(len(w["results"]) for w in watchlist_data)
    total_flagged = sum(len(w["flagged"]) for w in watchlist_data)
    
    html_parts = [
        f"<h1>Wally the Watcher — Combined Report</h1>",
        f"<p>Run date: {run_date}</p>",
        f"<p><strong>Summary:</strong> Checked {total_checked} tickers across {len(watchlist_data)} watchlist(s) | Flagged {total_flagged} ticker(s)</p>",
        "<hr>"
    ]
    
    # Add each watchlist section
    for wl_data in watchlist_data:
        watchlist_name = wl_data["watchlist_name"]
        results = wl_data["results"]
        flagged = wl_data["flagged"]
        chart_notes = wl_data.get("chart_notes", {})
        inline_pngs = wl_data.get("inline_pngs")
        
        html_parts.append(f"<h2>{watchlist_name}</h2>")
        html_parts.append(f"<p>Checked: <strong>{len(results)}</strong> | Flagged: <strong>{len(flagged)}</strong></p>")
        
        # Build table for this watchlist
        if flagged:
            rows = []
            for r in flagged:
                rows.append(
                    f"<tr><td>{r.ticker}</td><td>{r.company_name}</td><td>{_fmt(r.current_price)}</td><td>{_fmt(r.low_52w)}</td>"
                    f"<td>{_fmt(r.high_52w)}</td><td>{_fmt(r.distance_to_low_pct)}%</td><td>{_fmt(r.below_high_pct)}%</td></tr>"
                )
            
            flagged_table = (
                "<table border='1' cellspacing='0' cellpadding='6' style='border-collapse:collapse'>"
                "<tr style='background:#1F2D4E;color:white'>"
                "<th>Ticker</th><th>Name</th><th>Current</th><th>52W Low</th><th>52W High</th><th>% Above Low</th><th>% Below High</th></tr>"
                + "".join(rows)
                + "</table>"
            )
            html_parts.append(flagged_table)
            
            # Add details for each flagged ticker
            for r in flagged:
                cid = (inline_pngs or {}).get(r.ticker)
                chart_img = (
                    f"<img src='cid:{cid}' style='max-width:100%;border:1px solid #ccc'><br>"
                    if cid
                    else f"<p><em>Value chart: {chart_notes.get(r.ticker, 'No valuation config found yet for this ticker')}</em></p>"
                )
                html_parts.append(
                    f"<h3>{r.ticker} — {r.company_name}</h3>"
                    f"{chart_img}"
                )
        else:
            html_parts.append("<p><strong>No stocks within 5% of 52-week low.</strong></p>")
        
        html_parts.append("<hr>")
    
    return "".join(html_parts)


def build_html(
    watchlist_name: str,
    run_date: str,
    results: list[TickerScreenResult],
    flagged: list[TickerScreenResult],
    chart_notes: dict[str, str],
    inline_pngs: dict[str, str] | None = None,  # ticker -> content-id
) -> str:
    rows = []
    for r in flagged:
        rows.append(
            f"<tr><td>{r.ticker}</td><td>{r.company_name}</td><td>{_fmt(r.current_price)}</td><td>{_fmt(r.low_52w)}</td>"
            f"<td>{_fmt(r.high_52w)}</td><td>{_fmt(r.distance_to_low_pct)}%</td><td>{_fmt(r.below_high_pct)}%</td></tr>"
        )

    flagged_table = (
        "<table border='1' cellspacing='0' cellpadding='6' style='border-collapse:collapse'>"
        "<tr style='background:#1F2D4E;color:white'>"
        "<th>Ticker</th><th>Name</th><th>Current</th><th>52W Low</th><th>52W High</th><th>% Above Low</th><th>% Below High</th></tr>"
        + "".join(rows)
        + "</table>"
    ) if flagged else "<p><strong>No stocks within 5% of 52-week low.</strong></p>"

    details = []
    for r in flagged:
        cid = (inline_pngs or {}).get(r.ticker)
        chart_img = (
            f"<img src='cid:{cid}' style='max-width:100%;border:1px solid #ccc'><br>"
            if cid
            else f"<p><em>Value chart: {chart_notes.get(r.ticker, 'No valuation config found yet for this ticker')}</em></p>"
        )
        details.append(
            f"<h3>{r.ticker} — {r.company_name}</h3>"
            f"{chart_img}"
        )

    return (
        f"<h2>Wally the Watcher — {watchlist_name}</h2>"
        f"<p>Run date: {run_date}</p>"
        f"<p>Checked: <strong>{len(results)}</strong> | Flagged: <strong>{len(flagged)}</strong></p>"
        f"{flagged_table}"
        f"{''.join(details)}"
    )


def send_email(
    settings: EmailSettings,
    subject: str,
    body_text: str,
    body_html: str,
    attachments: list[Path],
    inline_images: list[tuple[str, Path]] | None = None,  # [(content-id, png_path), ...]
) -> bool:
    if not all([settings.email_from, settings.email_to, settings.smtp_user, settings.smtp_password]):
        missing = []
        if not settings.email_from:
            missing.append("EMAIL_FROM")
        if not settings.email_to:
            missing.append("EMAIL_TO")
        if not settings.smtp_user:
            missing.append("SMTP_USER")
        if not settings.smtp_password:
            missing.append("SMTP_PASS / EMAIL_APP_PASSWORD")
        print(f"[email_report] Cannot send — missing env vars: {', '.join(missing)}")
        return False

    # Root: multipart/mixed → holds everything
    root = MIMEMultipart("mixed")
    root["From"] = settings.email_from
    root["To"] = settings.email_to
    root["Subject"] = subject

    if inline_images:
        # multipart/related wraps html + inline images
        related = MIMEMultipart("related")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text, "plain"))
        alt.attach(MIMEText(body_html, "html"))
        related.attach(alt)
        for cid, png_path in inline_images:
            with open(png_path, "rb") as f:
                img = MIMEImage(f.read(), _subtype="png")
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=png_path.name)
            related.attach(img)
        root.attach(related)
    else:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text, "plain"))
        alt.attach(MIMEText(body_html, "html"))
        root.attach(alt)

    for path in attachments:
        # Skip PNG files that are already embedded inline
        if inline_images and path.suffix.lower() == ".png":
            if any(str(path) == str(p) for _, p in inline_images):
                continue
        mime, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (mime.split("/", 1) if mime else ("application", "octet-stream"))
        part = MIMEBase(maintype, subtype)
        part.set_payload(path.read_bytes())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=path.name)
        root.attach(part)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context) as server:
        server.login(settings.smtp_user, settings.smtp_password)
        server.sendmail(settings.email_from, settings.email_to, root.as_string())

    return True
