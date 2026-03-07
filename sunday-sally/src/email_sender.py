from __future__ import annotations

import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


def send_summary_email(subject: str, body_text: str, attachments: list[Path] | None = None) -> bool:
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

    for p in attachments or []:
        mime, _ = mimetypes.guess_type(p.name)
        maintype, subtype = (mime.split("/", 1) if mime else ("application", "octet-stream"))
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)

    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ssl.create_default_context()) as server:
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)
    return True
