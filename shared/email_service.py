# shared/email_service.py
#
# One SMTP transport for every agent. The agent decides WHAT to send
# (subject, body, HTML, which files); this module decides HOW (env config,
# MIME structure, attachment encoding, SMTP_SSL + login).
#
# Named email_service rather than email so nothing can ever shadow the stdlib
# ``email`` package this module itself imports.
#
# Failure policy stays with the caller. Bob and Ned must raise when email
# can't be sent (Bob writes bob.json only after the send returns, so a
# swallowed failure would record a digest nobody received); Slinger, Bob USA,
# Sally and Harry log and carry on. ``send_email(raise_on_error=...)`` exposes
# both, and never picks one on the caller's behalf.

from __future__ import annotations

import mimetypes
import os
import smtplib
import ssl
from dataclasses import dataclass
from email import encoders
from email.message import EmailMessage, Message
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence, Tuple, Union

DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465

Log = Callable[[str], None]


class EmailConfigError(RuntimeError):
    """Required email environment variables are missing."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SmtpSettings:
    email_from: str
    email_to: str
    smtp_user: str
    smtp_password: str
    smtp_host: str = DEFAULT_SMTP_HOST
    smtp_port: int = DEFAULT_SMTP_PORT

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        to_addr: Optional[str] = None,
    ) -> "SmtpSettings":
        """Read the env var names every agent already uses.

        EMAIL_FROM (or EMAIL_USER), EMAIL_TO, EMAIL_APP_PASSWORD. SMTP_USER,
        SMTP_PASS, SMTP_HOST and SMTP_PORT override the Gmail defaults; only
        Wally's and Sally's workflows set them. An empty value falls back to
        the default, because a GitHub secret that isn't set arrives as "".
        """
        e = os.environ if env is None else env

        def get(name: str) -> str:
            return (e.get(name) or "").strip()

        email_from = get("EMAIL_FROM") or get("EMAIL_USER")
        port = get("SMTP_PORT")
        return cls(
            email_from=email_from,
            email_to=(to_addr or "").strip() or get("EMAIL_TO"),
            smtp_user=get("SMTP_USER") or email_from,
            smtp_password=get("SMTP_PASS") or get("EMAIL_APP_PASSWORD"),
            smtp_host=get("SMTP_HOST") or DEFAULT_SMTP_HOST,
            smtp_port=int(port) if port else DEFAULT_SMTP_PORT,
        )

    def missing(self) -> list[str]:
        """Names of the settings that are empty, in the wording agents log."""
        out = []
        if not self.email_from:
            out.append("EMAIL_FROM / EMAIL_USER")
        if not self.email_to:
            out.append("EMAIL_TO")
        if not self.smtp_user:
            out.append("SMTP_USER")
        if not self.smtp_password:
            out.append("SMTP_PASS / EMAIL_APP_PASSWORD")
        return out


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Attachment:
    data: bytes
    filename: str
    maintype: str = "application"
    subtype: str = "octet-stream"

    @classmethod
    def from_path(
        cls,
        path: Union[str, Path],
        filename: Optional[str] = None,
        maintype: Optional[str] = None,
        subtype: Optional[str] = None,
    ) -> "Attachment":
        """Read *path*. The MIME type is guessed from *filename* unless given."""
        p = Path(path)
        name = filename or p.name
        if not (maintype and subtype):
            guessed, _ = mimetypes.guess_type(name)
            maintype, subtype = guessed.split("/", 1) if guessed else ("application", "octet-stream")
        return cls(data=p.read_bytes(), filename=name, maintype=maintype, subtype=subtype)


# A plain Path, a (Path, display_name) tuple, or a ready-made Attachment.
AttachmentLike = Union[Path, str, Tuple[Path, str], Attachment]


def _print(msg: str) -> None:
    print(msg, flush=True)


def load_attachments(
    items: Optional[Iterable[AttachmentLike]],
    *,
    max_total_bytes: Optional[int] = None,
    force_type: Optional[Tuple[str, str]] = None,
    log: Log = _print,
    log_prefix: str = "[email]",
) -> list[Attachment]:
    """Resolve *items* into Attachments.

    An unreadable file is logged and skipped rather than failing the send:
    the email body still carries the links. When *max_total_bytes* is set,
    files that would push the running total past it are skipped too (Gmail
    rejects messages over ~25MB). *force_type* (e.g. ``("application",
    "pdf")``) overrides the guessed MIME type for path items.
    """
    maintype, subtype = force_type or (None, None)
    out: list[Attachment] = []
    total = 0
    for item in items or []:
        try:
            if isinstance(item, Attachment):
                att = item
            elif isinstance(item, tuple):
                att = Attachment.from_path(item[0], filename=item[1], maintype=maintype, subtype=subtype)
            else:
                att = Attachment.from_path(item, maintype=maintype, subtype=subtype)
        except Exception as exc:
            name = item[0] if isinstance(item, tuple) else item
            log(f"{log_prefix} could not read attachment {name}: {exc}")
            continue
        size = len(att.data)
        if max_total_bytes is not None and total + size > max_total_bytes:
            log(f"{log_prefix} attachment cap reached ({max_total_bytes}B) "
                f"-- skipping {att.filename} ({size}B)")
            continue
        total += size
        out.append(att)
    return out


# ---------------------------------------------------------------------------
# MIME building
# ---------------------------------------------------------------------------

def build_message(
    *,
    email_from: str,
    email_to: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    attachments: Sequence[Attachment] = (),
    inline_images: Optional[Sequence[Tuple[str, Union[str, Path]]]] = None,
    log: Log = _print,
    log_prefix: str = "[email]",
) -> Message:
    """Build the MIME message.

    Without inline images this is a stdlib ``EmailMessage``: text/plain with
    an optional text/html alternative, plus attachments.

    With HTML and inline images (Wally's charts, Sally's charts) the
    structure is the one those agents always sent, so ``cid:`` references
    in their HTML keep resolving::

        multipart/mixed
        +-- multipart/related
        |   +-- multipart/alternative (text/plain, text/html)
        |   +-- image/png  (Content-ID: <cid>, one per image)
        +-- attachments
    """
    if body_html and inline_images:
        root = MIMEMultipart("mixed")
        root["From"] = email_from
        root["To"] = email_to
        root["Subject"] = subject

        related = MIMEMultipart("related")
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text, "plain", "utf-8"))
        alt.attach(MIMEText(body_html, "html", "utf-8"))
        related.attach(alt)
        for cid, img_path in inline_images:
            try:
                img = MIMEImage(Path(img_path).read_bytes(), _subtype="png")
            except Exception as exc:
                log(f"{log_prefix} could not embed inline image {img_path}: {exc}")
                continue
            img.add_header("Content-ID", f"<{cid}>")
            img.add_header("Content-Disposition", "inline", filename=Path(img_path).name)
            related.attach(img)
        root.attach(related)

        for att in attachments:
            part = MIMEBase(att.maintype, att.subtype)
            part.set_payload(att.data)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=att.filename)
            root.attach(part)
        return root

    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = subject
    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")
    for att in attachments:
        msg.add_attachment(att.data, maintype=att.maintype, subtype=att.subtype, filename=att.filename)
    return msg


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send_message(msg: Message, settings: SmtpSettings) -> None:
    """Send *msg* over SMTP_SSL. Raises on any failure."""
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context) as server:
        server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)


def send_email(
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    *,
    attachments: Optional[Iterable[AttachmentLike]] = None,
    inline_images: Optional[Sequence[Tuple[str, Union[str, Path]]]] = None,
    to_addr: Optional[str] = None,
    settings: Optional[SmtpSettings] = None,
    raise_on_error: bool = False,
    max_total_bytes: Optional[int] = None,
    force_type: Optional[Tuple[str, str]] = None,
    log: Log = _print,
    log_prefix: str = "[email]",
) -> bool:
    """Build and send one email.

    Returns True when sent. With ``raise_on_error=False`` a missing setting
    or an SMTP failure is logged and returns False; with ``True`` it raises
    (``EmailConfigError`` for missing settings, the SMTP error otherwise).
    """
    settings = settings or SmtpSettings.from_env(to_addr=to_addr)
    missing = settings.missing()
    if missing:
        msg = f"{log_prefix} Cannot send -- missing env vars: {', '.join(missing)}"
        if raise_on_error:
            raise EmailConfigError(msg)
        log(msg)
        return False

    atts = load_attachments(attachments, max_total_bytes=max_total_bytes, force_type=force_type,
                            log=log, log_prefix=log_prefix)
    for att in atts:
        log(f"{log_prefix} attached {att.filename} ({len(att.data)}B)")
    message = build_message(
        email_from=settings.email_from,
        email_to=settings.email_to,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        attachments=atts,
        inline_images=inline_images,
        log=log,
        log_prefix=log_prefix,
    )
    try:
        send_message(message, settings)
    except Exception as exc:
        if raise_on_error:
            raise
        log(f"{log_prefix} send failed: {type(exc).__name__}: {exc}")
        return False
    log(f"{log_prefix} sent -> {settings.email_to}")
    return True
