# results_pack_agent/claude_runner.py
# Run multiple long-form analysis prompts against a ResultPack using Claude.
# Passes the full PDF pack directly to Claude via base64 document blocks.

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Dict, List, Optional

from .config import CLAUDE_DEFAULT_MODEL, CLAUDE_MAX_TOKENS, MAX_PDF_BYTES
from .models import Announcement, ResultPack
from .prompts import ARTIFACT_SUFFIX, PROMPT_REGISTRY
from .utils import log


# ── Low-level Claude call ──────────────────────────────────────────────────────

def _call_claude(
    system_prompt: str,
    text_context: str,
    pdf_items: List[Announcement],
    model: str = CLAUDE_DEFAULT_MODEL,
) -> str:
    """Send *system_prompt* + *text_context* + PDFs to Claude.

    Returns the response text, or a sentinel on failure:
    - ``"__LLM_FAILED__"``  — API error
    - ``"__NO_PDFS__"``     — no PDFs could be attached
    """
    import anthropic

    content: List[Dict] = []

    # Attach each PDF as a base64 document block
    attached = 0
    for ann in pdf_items:
        raw = ann.pdf_bytes
        if not raw:
            continue
        if len(raw) > MAX_PDF_BYTES:
            log(f"[claude_runner] Skipping oversized PDF ({len(raw)//1024} KB): {ann.title[:60]}")
            continue
        content.append(
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(raw).decode("utf-8"),
                },
                "title": ann.title[:200],
            }
        )
        attached += 1

    if attached == 0:
        log("[claude_runner] No PDFs could be attached — cannot send to Claude.")
        return "__NO_PDFS__"

    # Append text context as final user message
    content.append({"type": "text", "text": text_context[:30_000]})

    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log("[claude_runner] ANTHROPIC_API_KEY not set.")
            return "__LLM_FAILED__"

        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=CLAUDE_MAX_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )
        return (resp.content[0].text or "").strip()
    except Exception as exc:
        log(f"[claude_runner] Claude API call failed: {exc}")
        return "__LLM_FAILED__"


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
) -> Dict[str, str]:
    """Run one or more analysis prompts against *pack* and save output files.

    *prompts_to_run* is a list of keys from ``PROMPT_REGISTRY``.  If omitted,
    the management report and equity report are run by default.  The Strawman
    post is only included when *include_strawman* is True (or when it appears
    in *prompts_to_run* explicitly).

    Returns a dict mapping ``{prompt_key: local_file_path}`` for each output
    artifact saved.
    """
    if prompts_to_run is None:
        prompts_to_run = ["management_report", "equity_report"]
        if include_strawman:
            prompts_to_run.append("strawman_post")

    text_context = _build_text_context(pack)
    artifacts: Dict[str, str] = {}

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

        if response in ("__LLM_FAILED__", "__NO_PDFS__"):
            log(f"[claude_runner] Prompt '{prompt_key}' failed — sentinel: {response}")
            # Write a placeholder file so the run folder is complete
            placeholder = (
                f"# {suffix.replace('.md','')}\n\n"
                f"Claude analysis could not run for this prompt.\n"
                f"Sentinel: {response}\n"
            )
            out_file.write_text(placeholder, encoding="utf-8")
        else:
            out_file.write_text(response, encoding="utf-8")
            log(f"[claude_runner] Saved '{prompt_key}' → {out_file.name}")

        artifacts[prompt_key] = str(out_file)

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
