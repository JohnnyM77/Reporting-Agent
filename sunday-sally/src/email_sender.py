from __future__ import annotations

import mimetypes
import os
import smtplib
import ssl
from email import encoders
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path


def send_summary_email(
    subject: str,
    body_text: str,
    attachments: list[Path | tuple[Path, str]] | None = None,
    inline_images: list[tuple[str, Path]] | None = None,
    body_html: str | None = None,
) -> bool:
    """Send the summary email, optionally with an HTML body and inline chart images.

    Args:
        subject:       Email subject line.
        body_text:     Plain-text body (always included as fallback).
        attachments:   File attachments — plain Path or (Path, display_name) tuple.
        inline_images: List of (content_id, image_path) for inline <img src="cid:..."/>.
                       Only used when body_html is also supplied.
        body_html:     Optional HTML body. When provided alongside inline_images the
                       images are embedded as multipart/related CID references.
    """
    email_from = os.environ.get("EMAIL_FROM") or os.environ.get("EMAIL_USER")
    email_to = os.environ.get("EMAIL_TO")
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = os.environ.get("SMTP_USER") or email_from
    smtp_pass = os.environ.get("SMTP_PASS") or os.environ.get("EMAIL_APP_PASSWORD")

    if not all([email_from, email_to, smtp_user, smtp_pass]):
        missing = []
        if not email_from:  missing.append("EMAIL_FROM / EMAIL_USER")
        if not email_to:    missing.append("EMAIL_TO")
        if not smtp_user:   missing.append("SMTP_USER")
        if not smtp_pass:   missing.append("SMTP_PASS / EMAIL_APP_PASSWORD")
        print(f"[email_sender] Cannot send — missing env vars: {', '.join(missing)}")
        return False

    # ── Build MIME structure ───────────────────────────────────────────────────
    # With HTML + inline images:
    #   multipart/mixed
    #   └── multipart/related
    #       ├── multipart/alternative
    #       │   ├── text/plain
    #       │   └── text/html
    #       └── image/png  (one per inline image, Content-ID: <cid>)
    # Plain text only:
    #   multipart/mixed
    #   └── text/plain

    outer = MIMEMultipart("mixed")
    outer["From"]    = email_from
    outer["To"]      = email_to
    outer["Subject"] = subject

    if body_html and inline_images:
        related = MIMEMultipart("related")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text, "plain", "utf-8"))
        alt.attach(MIMEText(body_html, "html",  "utf-8"))
        related.attach(alt)

        for cid, img_path in inline_images:
            try:
                img_data = Path(img_path).read_bytes()
                img_part = MIMEImage(img_data, _subtype="png")
                img_part.add_header("Content-ID",          f"<{cid}>")
                img_part.add_header("Content-Disposition", "inline",
                                    filename=Path(img_path).name)
                related.attach(img_part)
            except Exception as exc:
                print(f"[email_sender] Could not embed inline image {img_path}: {exc}")

        outer.attach(related)

    elif body_html:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text, "plain", "utf-8"))
        alt.attach(MIMEText(body_html, "html",  "utf-8"))
        outer.attach(alt)

    else:
        outer.attach(MIMEText(body_text, "plain", "utf-8"))

    # ── File attachments ───────────────────────────────────────────────────────
    for entry in attachments or []:
        if isinstance(entry, tuple):
            p, display_name = entry
        else:
            p, display_name = entry, entry.name

        mime, _ = mimetypes.guess_type(display_name)
        maintype, subtype = (mime.split("/", 1) if mime
                             else ("application", "octet-stream"))
        part = MIMEBase(maintype, subtype)
        part.set_payload(Path(p).read_bytes())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment",
                        filename=display_name)
        outer.attach(part)

    # ── Send ───────────────────────────────────────────────────────────────────
    try:
        with smtplib.SMTP_SSL(smtp_host, smtp_port,
                              context=ssl.create_default_context()) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(email_from, email_to, outer.as_string())
    except Exception as exc:
        print(f"[email_sender] SMTP send failed: {exc}")
        return False
    return True
