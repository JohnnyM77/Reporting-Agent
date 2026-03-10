from __future__ import annotations

import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


def send_summary_email(
    subject: str,
    body_text: str,
    attachments: list[Path | tuple[Path, str]] | None = None,
) -> bool:
    """Send the summary email.

    attachments accepts either plain Path objects (filename taken from the path)
    or (Path, display_name) tuples so you can name a file anything you like in
    the email (e.g. ("BHP_memo.md", path_to_memo)).
    """
    email_from = os.environ.get("EMAIL_FROM") or os.environ.get("EMAIL_USER")
    email_to = os.environ.get("EMAIL_TO")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = os.environ.get("SMTP_USER") or email_from
    smtp_pass = os.environ.get("SMTP_PASS") or os.environ.get("EMAIL_APP_PASSWORD")

    if not all([email_from, email_to, smtp_user, smtp_pass]):
        return False

    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = subject
    msg.set_content(body_text)

    for entry in attachments or []:
        if isinstance(entry, tuple):
            p, display_name = entry
        else:
            p, display_name = entry, entry.name
        mime, _ = mimetypes.guess_type(display_name)
        maintype, subtype = (mime.split("/", 1) if mime else ("application", "octet-stream"))
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=display_name)

    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ssl.create_default_context()) as server:
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
    except Exception as exc:
        print(f"[email_sender] SMTP send failed: {exc}")
        return False
    return True
