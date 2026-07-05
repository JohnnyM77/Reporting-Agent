# results_pack_agent/claude_runner.py
# Run multiple long-form analysis prompts against a ResultPack using Claude.
# Passes the full PDF pack directly to Claude via base64 document blocks,
# falling back to pypdf-extracted text (per-document) only when a document
# can't be attached natively — never silently dropping it.

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from shared.pdf_llm import LLM_FAILED, PdfAttachment, build_pdf_attachments

from .config import CLAUDE_DEFAULT_MODEL, CLAUDE_MAX_TOKENS
from .models import Announcement, ResultPack
from .prompts import ARTIFACT_SUFFIX, PROMPT_REGISTRY
from .utils import log

NO_CONTENT = "__NO_CONTENT__"  # no PDFs attachable AND no fallback text extractable


# ── Low-level Claude call ──────────────────────────────────────────────────────

def _call_claude(
    system_prompt: str,
    text_context: str,
    pdf_items: List[Announcement],
    model: str = CLAUDE_DEFAULT_MODEL,
    max_retries: int = 1,
) -> str:
    """Send *system_prompt* + *text_context* + PDFs to Claude.

    Each announcement's PDF is attached natively when possible; any that are
    missing, malformed, or too large (individually or in combination) are
    gracefully downgraded to extracted text rather than dropped, so a single
    bad document doesn't erase itself from the analysis.

    Returns the response text, or a sentinel on failure:
    - ``LLM_FAILED``  — API error (after one retry)
    - ``NO_CONTENT``  — nothing usable could be attached or extracted
    """
    import anthropic

    attachments = [
        PdfAttachment(name=ann.title[:200] or "document", pdf_bytes=ann.pdf_bytes)
        for ann in pdf_items
    ]
    batch = build_pdf_attachments(attachments, log=lambda m: log(f"[claude_runner] {m}"))

    content: List[Dict] = list(batch.document_blocks)
    text_parts = list(batch.fallback_sections)
    text_parts.append(text_context[:30_000])
    content.append({"type": "text", "text": "\n\n".join(text_parts)[:60_000]})

    if not batch.document_blocks and not batch.fallback_sections:
        log("[claude_runner] No PDFs could be attached or extracted — cannot send to Claude.")
        return NO_CONTENT

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log("[claude_runner] ERROR: ANTHROPIC_API_KEY not set.")
        return LLM_FAILED

    client = anthropic.Anthropic(api_key=api_key)
    last_err: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=CLAUDE_MAX_TOKENS,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
            )
            return (resp.content[0].text or "").strip()
        except Exception as exc:
            last_err = exc
            if attempt < max_retries:
                log(f"[claude_runner] WARNING: Claude API call failed (attempt {attempt + 1}/{max_retries + 1}, model={model}): {exc} — retrying once")
                time.sleep(2)
                continue
            log(f"[claude_runner] ERROR: Claude API call failed after {attempt + 1} attempt(s) (model={model}): {exc}")
    return LLM_FAILED


# ── Pack text context builder ──────────────────────────────────────────────────

def _build_text_context(pack: ResultPack) -> str:
    """Build the plain-text context sent alongside the PDF documents."""
    titles = "\n".join(f"  - {a.title}" for a in pack.announcements)
    urls = "\n".join(
        f"  - {a.title[:80]}: {a.pdf_url or a.url}" for a in pack.announcements
    )
    attached = pack.pdfs_downloaded
    return (
        f"Ticker: {pack.ticker}\n"
        f"Company: {pack.company_name}\n"
        f"Announcement date: {pack.result_date}\n"
        f"Result type: {pack.result_type}\n"
        f"Number of documents in pack: {len(pack.announcements)} "
        f"({attached} PDFs attached)\n\n"
        f"Document titles:\n{titles}\n\n"
        f"Document URLs:\n{urls}\n"
    )


# ── Multi-prompt runner ────────────────────────────────────────────────────────

def run_prompts(
    pack: ResultPack,
    output_folder: Path,
    prompts_to_run: Optional[List[str]] = None,
    include_strawman: bool = False,
    dry_run: bool = False,
    model: str = CLAUDE_DEFAULT_MODEL,
) -> Dict[str, object]:
    """Run one or more analysis prompts against *pack* and save output files.

    *prompts_to_run* is a list of keys from ``PROMPT_REGISTRY``.  If omitted,
    the management report and equity report are run by default.  The Strawman
    post is only included when *include_strawman* is True (or when it appears
    in *prompts_to_run* explicitly).

    Returns a dict mapping ``{prompt_key: local_file_path}`` for each output
    artifact saved. If one or more prompts failed to produce a real analysis
    (Claude API error, or no usable PDF/text content), the dict also carries
    an ``"_llm_failures"`` key listing the failed prompt keys — callers
    (``main.run()``) must check this and mark the run as failed rather than
    reporting success with placeholder files on disk.
    """
    if prompts_to_run is None:
        prompts_to_run = ["management_report", "equity_report"]
        if include_strawman:
            prompts_to_run.append("strawman_post")

    text_context = _build_text_context(pack)
    artifacts: Dict[str, str] = {}
    failed_prompts: List[str] = []

    for prompt_key in prompts_to_run:
        system_prompt = PROMPT_REGISTRY.get(prompt_key)
        if system_prompt is None:
            log(f"[claude_runner] Unknown prompt key: {prompt_key} — skipping.")
            continue

        suffix = ARTIFACT_SUFFIX.get(prompt_key, f"{prompt_key}.md")
        out_file = output_folder / f"{pack.file_prefix}-{suffix}"

        if dry_run:
            log(f"[claude_runner] [DRY-RUN] Would run prompt '{prompt_key}' → {out_file.name}")
            artifacts[prompt_key] = str(out_file)
            continue

        log(f"[claude_runner] Running prompt '{prompt_key}' for {pack.ticker} …")
        response = _call_claude(system_prompt, text_context, pack.announcements, model=model)

        if response in (LLM_FAILED, NO_CONTENT):
            log(f"[claude_runner] ERROR: prompt '{prompt_key}' did not produce a real analysis — sentinel: {response}")
            failed_prompts.append(prompt_key)
            # Write a placeholder file, clearly flagged, so the run folder is
            # complete but nobody mistakes this for a finished analysis.
            placeholder = (
                f"# {suffix.replace('.md', '')}\n\n"
                f"⚠️ Analysis failed — manual review needed.\n\n"
                f"Claude did not produce a real analysis for this prompt "
                f"(sentinel: {response}). Re-run once the underlying issue "
                f"(API credit/billing, rate limit, or missing PDFs) is resolved.\n"
            )
            out_file.write_text(placeholder, encoding="utf-8")
        else:
            out_file.write_text(response, encoding="utf-8")
            log(f"[claude_runner] Saved '{prompt_key}' → {out_file.name}")

        artifacts[prompt_key] = str(out_file)

    if failed_prompts:
        # Machine-readable marker main.run() checks to avoid reporting a
        # successful RunSummary when the actual Claude analysis failed.
        artifacts["_llm_failures"] = failed_prompts

    # Also save the raw Claude context as JSON for debugging
    raw_json_path = output_folder / f"{pack.file_prefix}-Claude-Context.json"
    raw_json_path.write_text(
        json.dumps(
            {
                "ticker": pack.ticker,
                "result_date": pack.result_date,
                "result_type": pack.result_type,
                "text_context": text_context,
                "prompts_run": prompts_to_run,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return artifacts
