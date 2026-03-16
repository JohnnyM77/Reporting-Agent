# master_engine/notifier.py
#
# Sends the Master Investor digest by email and/or saves it to disk.

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _load_smtp_settings() -> dict[str, str | int]:
    """
    Load SMTP settings from environment variables.

    Reuses the same env var names as the existing Bob / Wally agents so no
    new secrets are required.
    """
    email_from = os.environ.get("EMAIL_FROM") or os.environ.get("EMAIL_USER", "")
    email_to = os.environ.get("EMAIL_TO", "")
    smtp_user = os.environ.get("SMTP_USER") or email_from
    smtp_password = (
        os.environ.get("SMTP_PASS")
        or os.environ.get("EMAIL_APP_PASSWORD", "")
    )
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))

    return {
        "email_from": email_from,
        "email_to": email_to,
        "smtp_user": smtp_user,
        "smtp_password": smtp_password,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
    }


def send_email(
    subject: str,
    plain_text: str,
    html_body: str,
    to_addr: Optional[str] = None,
) -> bool:
    """
    Send the digest email via SMTP SSL.

    Returns True on success, False on failure (errors are logged, not raised).
    """
    settings = _load_smtp_settings()
    email_from = str(settings["email_from"])
    email_to = to_addr or str(settings["email_to"])
    smtp_user = str(settings["smtp_user"])
    smtp_password = str(settings["smtp_password"])
    smtp_host = str(settings["smtp_host"])
    smtp_port = int(str(settings["smtp_port"]))

    missing = [
        name
        for name, value in {
            "EMAIL_FROM": email_from,
            "EMAIL_TO": email_to,
            "SMTP_PASSWORD": smtp_password,
        }.items()
        if not value
    ]
    if missing:
        logger.error(
            "[notifier] Cannot send email — missing env vars: %s",
            ", ".join(missing),
        )
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content(plain_text)
    msg.add_alternative(html_body, subtype="html")

    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        logger.info("[notifier] Email sent to %s — subject: %s", email_to, subject)
        return True
    except Exception as exc:
        logger.error("[notifier] Email send failed: %s", exc)
        return False


def save_digest(
    html_body: str,
    markdown_body: str,
    json_archive: str,
    output_dir: Path,
    run_date: str,
) -> dict[str, Path]:
    """
    Save digest files to *output_dir*.

    Returns a dict mapping format key to written path.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    def _write(filename: str, content: str, key: str) -> None:
        path = output_dir / filename
        try:
            path.write_text(content, encoding="utf-8")
            written[key] = path
            logger.info("[notifier] Saved %s → %s", key, path)
        except Exception as exc:
            logger.error("[notifier] Failed to save %s: %s", filename, exc)

    _write(f"master_investor_digest_{run_date}.html", html_body, "html")
    _write(f"master_investor_digest_{run_date}.md", markdown_body, "markdown")
    _write(f"master_investor_archive_{run_date}.json", json_archive, "json")

    return written


def notify(
    subject: str,
    plain_text: str,
    html_body: str,
    markdown_body: str,
    json_archive: str,
    output_dir: Path,
    run_date: str,
    send_email_flag: bool = True,
    to_addr: Optional[str] = None,
) -> dict[str, object]:
    """
    Full notification pipeline: save files and optionally send email.

    Returns a summary dict with ``email_sent`` and ``files`` keys.
    """
    files = save_digest(html_body, markdown_body, json_archive, output_dir, run_date)
    email_sent = False
    if send_email_flag:
        email_sent = send_email(subject, plain_text, html_body, to_addr=to_addr)

    return {"email_sent": email_sent, "files": files}
