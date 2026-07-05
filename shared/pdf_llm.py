# shared/pdf_llm.py
#
# Shared native-PDF-to-Claude helpers used by Bob (agent.py) and the Results
# Pack Agent (results_pack_agent/), and available for Wally once its
# announcement-analysis path lands.
#
# Centralizes two concerns that were previously duplicated (and inconsistent)
# across callers:
#
#   1. Claude's native PDF document-block limits, and a graceful downgrade to
#      pypdf-extracted text when a PDF can't be attached natively (missing,
#      malformed, or too large).
#
#   2. LLM outcome sentinels so every caller can distinguish "skipped
#      (intentional — call-cap reached)" from "failed (genuine API
#      exception)" instead of collapsing both into the same placeholder text
#      that looks like a real (if terse) analysis.
#
# Per the current Anthropic PDF support docs, the 32MB / 100-page limits are
# on the ENTIRE request payload (all PDFs plus any other content), not per
# file — https://platform.claude.com/docs/en/build-with-claude/pdf-support
# The 100-page figure applies to standard 200k-context-window models (what
# every caller here uses); models with a 1M-context window get 600 pages.

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Claude native PDF document-block limits (whole-request, not per-file).
# ---------------------------------------------------------------------------
MAX_PDF_BYTES = 32 * 1024 * 1024   # 32 MB per request
MAX_PDF_PAGES = 100                 # 200k-context models; 600 for 1M-context

# ---------------------------------------------------------------------------
# LLM outcome sentinels.
# ---------------------------------------------------------------------------
LLM_SKIPPED = "__LLM_SKIPPED__"   # intentional: run's call-cap reached — not an error
LLM_FAILED = "__LLM_FAILED__"     # genuine exception: timeout, auth, rate limit, malformed response


def validate_pdf_bytes_for_native_block(pdf_bytes: bytes) -> Tuple[bool, str, int]:
    """Check whether *pdf_bytes* alone fits Claude's native document-block limits.

    Returns ``(ok, reason, page_count)``. ``page_count`` is 0 when it can't be
    determined (malformed PDF, in which case ``ok`` is always False).

    This only checks the single file in isolation — callers attaching more
    than one PDF to the same request must also track the running total
    against ``MAX_PDF_BYTES`` / ``MAX_PDF_PAGES`` (see ``build_pdf_attachments``),
    since the real Claude limit applies to the whole request, not per file.
    """
    size = len(pdf_bytes)
    if size > MAX_PDF_BYTES:
        return False, f"{size // 1024}KB exceeds the {MAX_PDF_BYTES // (1024 * 1024)}MB Claude request limit", 0
    try:
        from pypdf import PdfReader
        n_pages = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception as exc:
        return False, f"malformed/unreadable PDF ({exc})", 0
    if n_pages > MAX_PDF_PAGES:
        return False, f"{n_pages} pages exceeds the {MAX_PDF_PAGES}-page Claude request limit", n_pages
    return True, "", n_pages


def extract_pdf_text_safe(pdf_bytes: bytes) -> str:
    """Best-effort pypdf text extraction.

    This is intentionally the *fallback* path only — used when a PDF can't
    go into the request as a native document block (too large, malformed,
    or never downloaded) or for lightweight pre-checks (e.g. detecting
    empty/disclaimer-only documents). Never raises; returns "" on failure.
    """
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts = [p.extract_text() or "" for p in reader.pages]
        return "\n\n".join(t for t in parts if t.strip()).strip()
    except Exception:
        return ""


@dataclass
class PdfAttachment:
    """One candidate PDF to attach to a Claude call."""

    name: str
    pdf_bytes: Optional[bytes] = None
    fallback_text: str = ""   # pre-extracted text; used if native attach isn't possible


@dataclass
class PdfBatchResult:
    document_blocks: List[dict] = field(default_factory=list)
    fallback_sections: List[str] = field(default_factory=list)

    @property
    def any_native(self) -> bool:
        return bool(self.document_blocks)


def build_pdf_attachments(
    attachments: Sequence[PdfAttachment],
    log: Callable[[str], None] = print,
    fallback_text_limit: int = 60_000,
) -> PdfBatchResult:
    """Build native PDF document blocks for *attachments*.

    Any PDF that is missing, malformed, too large alone, or would push the
    *combined* request over Claude's 32MB/100-page limit is gracefully
    downgraded to its (pre-supplied or freshly-extracted) text instead of
    being silently dropped — attachments are processed in order, so earlier
    ones (e.g. the primary financial report) get priority over later ones
    (e.g. an investor deck) when the shared budget is tight.
    """
    result = PdfBatchResult()
    total_bytes = 0
    total_pages = 0

    for att in attachments:
        if not att.pdf_bytes:
            if att.fallback_text:
                result.fallback_sections.append(
                    f"=== {att.name} (text only — no PDF attached) ===\n"
                    f"{att.fallback_text[:fallback_text_limit]}"
                )
            continue

        ok, reason, n_pages = validate_pdf_bytes_for_native_block(att.pdf_bytes)
        if ok:
            projected_bytes = total_bytes + len(att.pdf_bytes)
            projected_pages = total_pages + n_pages
            if projected_bytes > MAX_PDF_BYTES or projected_pages > MAX_PDF_PAGES:
                ok = False
                reason = (
                    f"would push this request over the combined "
                    f"{MAX_PDF_BYTES // (1024 * 1024)}MB/{MAX_PDF_PAGES}-page Claude limit "
                    f"({projected_bytes // 1024}KB / {projected_pages} pages so far)"
                )

        if ok:
            total_bytes += len(att.pdf_bytes)
            total_pages += n_pages
            result.document_blocks.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(att.pdf_bytes).decode("utf-8"),
                },
            })
            log(f"Sending PDF ({len(att.pdf_bytes) // 1024}KB, {n_pages}p) natively to Claude: {att.name}")
        else:
            log(f"PDF unsuitable for native block ({reason}) — falling back to extracted text: {att.name}")
            text = att.fallback_text or extract_pdf_text_safe(att.pdf_bytes)
            if text:
                result.fallback_sections.append(
                    f"=== {att.name} (extracted text; native attach skipped: {reason}) ===\n"
                    f"{text[:fallback_text_limit]}"
                )
            else:
                result.fallback_sections.append(
                    f"=== {att.name}: could not be analysed (native attach skipped: {reason}; "
                    f"no extractable text available) ==="
                )

    return result


def build_single_pdf_content(
    pdf_path,
    user_text: str,
    fallback_text: str = "",
    log: Callable[[str], None] = print,
    text_char_limit: int = 100_000,
    pdf_text_companion_limit: int = 50_000,
) -> Tuple[object, bool]:
    """Convenience wrapper for the common single-PDF case.

    Returns ``(content, used_native_pdf)`` where *content* is either a list
    of Anthropic content blocks (native PDF + companion text) or a plain
    string (full text fallback) ready to pass as ``messages[0]["content"]``.
    """
    pdf_bytes = None
    name = pdf_path.name if pdf_path else "document"
    if pdf_path is not None and pdf_path.exists():
        try:
            pdf_bytes = pdf_path.read_bytes()
        except Exception as exc:
            log(f"Could not read PDF {pdf_path}: {exc}")

    batch = build_pdf_attachments(
        [PdfAttachment(name=name, pdf_bytes=pdf_bytes, fallback_text=fallback_text)],
        log=log,
    )
    if batch.any_native:
        content = list(batch.document_blocks) + [
            {"type": "text", "text": user_text[:pdf_text_companion_limit]}
        ]
        return content, True

    text = fallback_text or user_text
    if batch.fallback_sections:
        text = "\n\n".join(batch.fallback_sections) + f"\n\n{user_text}"
    return text[:text_char_limit], False
