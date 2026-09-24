"""Generic "send a summary email" entry point used by Harry and Theo's season job.

The SMTP plumbing lives in ``shared/email_service.py``. This wrapper keeps
the signature and the never-raise contract its callers rely on.
"""

from __future__ import annotations

from pathlib import Path

from shared.email_service import send_email


def send_summary_email(
    subject: str,
    body_text: str,
    attachments: list[Path] | None = None,
    body_html: str | None = None,
) -> bool:
    return send_email(
        subject,
        body_text,
        body_html,
        attachments=attachments,
        raise_on_error=False,
        log_prefix="[email_sender]",
    )
