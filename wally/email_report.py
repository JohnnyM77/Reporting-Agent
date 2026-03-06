from __future__ import annotations

import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from .config import EmailSettings
from .screening import TickerScreenResult


def _fmt(n: float) -> str:
    return f"{n:.2f}"


def build_html(watchlist_name: str, run_date: str, results: list[TickerScreenResult], flagged: list[TickerScreenResult], chart_notes: dict[str, str]) -> str:
    rows = []
    for r in flagged:
        rows.append(
            f"<tr><td>{r.ticker}</td><td>{r.company_name}</td><td>{_fmt(r.current_price)}</td><td>{_fmt(r.low_52w)}</td>"
            f"<td>{_fmt(r.high_52w)}</td><td>{_fmt(r.distance_to_low_pct)}%</td><td>{_fmt(r.below_high_pct)}%</td></tr>"
        )

    flagged_table = (
        "<table border='1' cellspacing='0' cellpadding='6'><tr><th>Ticker</th><th>Name</th><th>Current</th><th>52W Low</th><th>52W High</th><th>% Above Low</th><th>% Below High</th></tr>"
        + "".join(rows)
        + "</table>"
    ) if flagged else "<p><strong>No stocks within 5% of 52-week low.</strong></p>"

    details = []
    for r in flagged:
        details.append(
            f"<h4>{r.ticker} — {r.company_name}</h4>"
            f"<ul><li>Range chart attachment: <code>{r.ticker.lower().replace('.', '_')}_range.png</code></li>"
            f"<li>Value chart: {chart_notes.get(r.ticker, 'No valuation config found yet for this ticker')}</li></ul>"
        )

    return (
        f"<h2>Wally — {watchlist_name}</h2>"
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
) -> None:
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = settings.email_to
    msg["Subject"] = subject
    msg.set_content(body_text)
    msg.add_alternative(body_html, subtype="html")

    for path in attachments:
        mime, _ = mimetypes.guess_type(path.name)
        maintype, subtype = (mime.split("/", 1) if mime else ("application", "octet-stream"))
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=path.name)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context) as server:
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
