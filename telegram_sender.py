"""
telegram_sender.py
------------------
Reusable Telegram delivery module for Bob, Wally, and Sunday Sally.

Usage:
    from telegram_sender import send_message, send_document, send_run_summary

Reads from environment variables:
    TELEGRAM_BOT_TOKEN  — bot token from @BotFather
    TELEGRAM_CHAT_ID    — your personal chat ID (or group chat ID)
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = [2, 5, 10]

# Telegram hard limit for sendMessage text is 4096 chars
MAX_MESSAGE_CHARS = 4000

# File size limit Telegram accepts via Bot API (50 MB)
MAX_FILE_BYTES = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN environment variable is not set. "
            "Add it as a GitHub secret and pass it to the workflow env block."
        )
    return token


def _chat_id() -> str:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID environment variable is not set. "
            "Add it as a GitHub secret and pass it to the workflow env block."
        )
    return chat_id


def _url(method: str) -> str:
    # Never log the token — keep it out of build logs
    return TELEGRAM_API_BASE.format(token=_token(), method=method)


def _post_with_retry(method: str, **kwargs) -> dict:
    """POST to Telegram API with simple retry + backoff."""
    url = _url(method)
    last_exc: Optional[Exception] = None

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.post(url, timeout=30, **kwargs)
            data = resp.json()

            if resp.status_code == 200 and data.get("ok"):
                return data

            # Telegram returned an error response
            error_desc = data.get("description", "unknown error")
            log.warning(
                "[telegram] API error on %s (attempt %d/%d): %s",
                method, attempt + 1, MAX_RETRIES, error_desc,
            )
            last_exc = RuntimeError(f"Telegram API error: {error_desc}")

        except requests.RequestException as exc:
            log.warning(
                "[telegram] Network error on %s (attempt %d/%d): %s",
                method, attempt + 1, MAX_RETRIES, exc,
            )
            last_exc = exc

        if attempt < MAX_RETRIES - 1:
            sleep_for = RETRY_BACKOFF_SECONDS[attempt]
            log.info("[telegram] Retrying in %ds...", sleep_for)
            time.sleep(sleep_for)

    raise RuntimeError(
        f"Telegram {method} failed after {MAX_RETRIES} attempts. Last error: {last_exc}"
    )


def _truncate(text: str, max_chars: int = MAX_MESSAGE_CHARS) -> str:
    """Truncate a message that would exceed Telegram's character limit."""
    if len(text) <= max_chars:
        return text
    tail = "\n\n…(message truncated)"
    return text[: max_chars - len(tail)] + tail


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_message(text: str, parse_mode: str = "HTML", raise_on_error: bool = False) -> dict:
    """
    Send a plain text (or HTML-formatted) message to the configured chat.

    Args:
        text:           The message body. HTML tags like <b> and <i> are safe.
        parse_mode:     "HTML" (default) or "Markdown". Use "HTML" — it's safer.
        raise_on_error: If False (default), log errors but don't crash the run.

    Returns:
        Telegram API response dict, or {} on failure.
    """
    text = _truncate(text)
    try:
        result = _post_with_retry(
            "sendMessage",
            data={
                "chat_id": _chat_id(),
                "text": text,
                "parse_mode": parse_mode,
            },
        )
        log.info("[telegram] Message sent OK.")
        return result
    except Exception as exc:
        log.error("[telegram] send_message failed: %s", exc)
        if raise_on_error:
            raise
        return {}


def send_document(
    file_path: str,
    caption: Optional[str] = None,
    raise_on_error: bool = False,
) -> dict:
    """
    Send a file (xlsx, pdf, md, txt, csv, etc.) as a Telegram document.

    Args:
        file_path:      Absolute or relative path to the file.
        caption:        Optional short caption shown under the file.
        raise_on_error: If False (default), log errors but don't crash.

    Returns:
        Telegram API response dict, or {} on failure.
    """
    path = Path(file_path)

    if not path.exists():
        log.warning("[telegram] send_document: file not found: %s", file_path)
        if raise_on_error:
            raise FileNotFoundError(f"File not found: {file_path}")
        return {}

    file_size = path.stat().st_size
    if file_size > MAX_FILE_BYTES:
        msg = f"[telegram] File too large to send ({file_size / 1024 / 1024:.1f} MB): {path.name}"
        log.warning(msg)
        if raise_on_error:
            raise ValueError(msg)
        return {}

    try:
        with path.open("rb") as fh:
            data: dict = {"chat_id": _chat_id()}
            if caption:
                data["caption"] = caption[:1024]  # Telegram caption limit

            result = _post_with_retry(
                "sendDocument",
                data=data,
                files={"document": (path.name, fh)},
            )
        log.info("[telegram] Document sent OK: %s", path.name)
        return result
    except Exception as exc:
        log.error("[telegram] send_document failed for %s: %s", path.name, exc)
        if raise_on_error:
            raise
        return {}


def send_run_summary(
    summary_text: str,
    attachments: Optional[list[str]] = None,
    agent_name: str = "Agent",
    raise_on_error: bool = False,
) -> None:
    """
    High-level helper: send a summary message then each attachment file.

    This is the main function to call from main.py at the end of each agent run.

    Args:
        summary_text:   The text summary to send first.
        attachments:    List of file paths to send as documents.
        agent_name:     Used in log messages only.
        raise_on_error: Propagate exceptions instead of swallowing them.
    """
    log.info("[telegram] Sending run summary for %s", agent_name)

    # 1. Send text summary
    send_message(summary_text, raise_on_error=raise_on_error)

    # 2. Send each file
    for file_path in (attachments or []):
        path = Path(file_path)
        caption = path.name
        send_document(str(path), caption=caption, raise_on_error=raise_on_error)
        # Small pause between files to avoid hitting Telegram rate limits
        time.sleep(0.5)

    log.info("[telegram] Run summary delivery complete for %s.", agent_name)


def send_error_alert(
    agent_name: str,
    error_summary: str,
    raise_on_error: bool = False,
) -> None:
    """
    Send a short error alert message. Redacts common secret patterns.

    Call this from an except block in main.py.
    """
    # Redact anything that looks like a token/key/password
    safe_summary = error_summary
    for env_var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "SMTP_PASS",
                    "EMAIL_APP_PASSWORD", "OPENAI_API_KEY", "GDRIVE_SERVICE_ACCOUNT_JSON"):
        val = os.environ.get(env_var, "")
        if val:
            safe_summary = safe_summary.replace(val, f"[{env_var} REDACTED]")

    text = (
        f"<b>⚠️ {agent_name} run FAILED</b>\n\n"
        f"{_truncate(safe_summary, 1000)}"
    )
    send_message(text, raise_on_error=raise_on_error)
