# master_engine/notifier.py
#
# Sends the Master Investor digest by email and/or saves it to disk.

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from shared.email_service import send_email as shared_send_email

logger = logging.getLogger(__name__)


def send_email(
    subject: str,
    plain_text: str,
    html_body: str,
    to_addr: Optional[str] = None,
) -> bool:
    """
    Send the digest email via the shared SMTP transport.

    Uses the same env var names as Bob / Wally, so no new secrets are
    required. Returns True on success, False on failure (errors are logged,
    not raised).
    """
    return shared_send_email(
        subject,
        plain_text,
        html_body,
        to_addr=to_addr,
        raise_on_error=False,
        log=logger.info,
        log_prefix="[notifier]",
    )


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
