from __future__ import annotations

from pathlib import Path

from .pathing import ensure_repo_root_on_path

ensure_repo_root_on_path()

from shared.email_service import send_email  # noqa: E402


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

    Never raises: a missing env var or SMTP failure is logged and returns False.
    """
    return send_email(
        subject,
        body_text,
        body_html,
        attachments=attachments,
        inline_images=inline_images,
        raise_on_error=False,
        log_prefix="[email_sender]",
    )
