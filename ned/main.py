#!/usr/bin/env python3
# ned/main.py
#
# Ned the News Agent — media scanner for portfolio companies.
#
# Sources:
#   - YouTube channels (via YouTube Data API v3 + youtube-transcript-api)
#   - Google News RSS (one feed per company)
#   - Static RSS feeds (Livewire, etc.)
#   - Yahoo Finance news
#
# Config:
#   ../tickers.yaml        — portfolio companies
#   ../media_sources.yaml  — which YouTube channels + RSS feeds to scan
#
# Required secrets:
#   YOUTUBE_API_KEY        — YouTube Data API v3 key (Google Cloud Console)
#   ANTHROPIC_API_KEY      — for LLM summarisation
#   EMAIL_FROM / EMAIL_TO / EMAIL_APP_PASSWORD — Gmail SMTP

from __future__ import annotations

import argparse
import json
import os
import sys
import datetime as dt
from pathlib import Path

import yaml

# Allow imports from repo root (news_context_fetcher etc.)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ned.youtube_scanner import scan_youtube_channels
from ned.news_scanner import scan_rss_feeds, scan_yahoo_finance
from ned.email_builder import build_email
from ned.importance_scorer import sort_by_importance
from ned.youtube_transcript_fetcher import (
    TranscriptError,
    TranscriptResult,
    extract_video_id,
    fetch_transcript,
    fetch_video_metadata,
)
from ned.podcast_transcript_fetcher import fetch_podcast_transcript
from ned.portfolio_context import load_portfolio_context
from ned.transcript_digest import (
    CHUNK_EXTRACTION_PROMPT,
    DigestResult,
    build_system_prompt,
    build_user_prompt,
    chunk_text,
    parse_digest_response,
)
from shared.email_service import send_email as shared_send_email
from shared.llm import make_client, send as llm_send

# ----------------------------
# Config / paths
# ----------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
TICKERS_PATH = REPO_ROOT / "tickers.yaml"
MEDIA_SOURCES_PATH = REPO_ROOT / "media_sources.yaml"
SEEN_STATE_PATH = Path(os.environ.get("NED_SEEN_STATE_PATH", "ned_seen.json"))
SEEN_STATE_RETENTION_HOURS = 96

MODEL = os.environ.get("MODEL_NAME", "claude-haiku-4-5-20251001")
MAX_LLM_CALLS = 30


def _env_int(name: str, default: int) -> int:
    """int(env) with a fallback for unset, blank or malformed values (a
    workflow `env:` line with an unset repo variable passes "")."""
    raw = (os.environ.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        print(f"[ned] {name}={raw!r} is not an integer; using {default}")
        return default


# Where single-episode transcripts are written. Kept out of the repo (the CI
# workflow uploads this directory as a downloadable artifact instead).
TRANSCRIPTS_DIR = Path(os.environ.get("NED_TRANSCRIPTS_DIR", str(REPO_ROOT / "transcripts")))
# The single-episode digest reasons about JM's portfolio, theses and
# watchlists, which is beyond Haiku; the daily scan keeps MODEL above.
TRANSCRIPT_MODEL = (os.environ.get("NED_TRANSCRIPT_MODEL") or "").strip() or "claude-sonnet-4-6"
TRANSCRIPT_MAX_TOKENS = _env_int("NED_TRANSCRIPT_MAX_TOKENS", 6000)
# Transcript characters sent whole. An hour of speech is ~55k chars, so 400k
# covers a ~7 hour episode. Anything longer is never clipped: it is split
# into TRANSCRIPT_CHUNK_CHARS parts, each part is reduced to extraction
# notes, and the digest runs on the notes.
TRANSCRIPT_LLM_MAX_CHARS = _env_int("NED_TRANSCRIPT_LLM_MAX_CHARS", 400_000)
TRANSCRIPT_CHUNK_CHARS = _env_int("NED_TRANSCRIPT_CHUNK_CHARS", 150_000)
TRANSCRIPT_CHUNK_MAX_TOKENS = 4000


# ----------------------------
# Seen-state helpers
# ----------------------------
def load_seen(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
        cutoff = (dt.datetime.utcnow() - dt.timedelta(hours=SEEN_STATE_RETENTION_HOURS)).isoformat()
        return {k for k, ts in data.items() if ts > cutoff}
    except Exception:
        return set()


def save_seen(path: Path, seen: set[str]) -> None:
    now = dt.datetime.utcnow().isoformat()
    data = {k: now for k in seen}
    path.write_text(json.dumps(data, indent=2))


# ----------------------------
# LLM summarisation
# ----------------------------
def llm_summarise(hit: dict, llm_calls: list[int]) -> str | None:
    """One-line summary of a hit. Returns None if cap reached or call fails."""
    if llm_calls[0] >= MAX_LLM_CALLS:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    llm_calls[0] += 1
    tickers = ", ".join(hit["tickers"])
    source_text = hit.get("transcript_snippet") or hit.get("description") or hit["title"]
    prompt = (
        f"Portfolio tickers: {tickers}\n"
        f"Source: {hit['source']}\n"
        f"Title: {hit['title']}\n"
        f"Content snippet: {source_text[:3000]}\n\n"
        "Write one punchy sentence: what happened and why it matters to a shareholder. "
        "If the content is vague or unrelated to these companies, write: [not material]"
    )
    try:
        resp = llm_send(
            make_client(api_key),
            model=MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.text.strip()
    except Exception as exc:
        print(f"[ned/llm] LLM failed: {exc}")
        return None


def llm_transcript_digest(
    source_url: str,
    transcript: str,
    llm_calls: list[int],
    *,
    kind: str = "youtube",
    title: str = "",
    channel: str = "",
    portfolio_context: str = "",
) -> DigestResult:
    """Analyse one episode transcript against JM's portfolio.

    One structured-JSON call on TRANSCRIPT_MODEL, with the portfolio context
    block in the system prompt. A transcript longer than
    TRANSCRIPT_LLM_MAX_CHARS is first reduced part by part to extraction
    notes (one call per part), and the digest runs on those notes.

    Never raises. On any failure the returned DigestResult carries `error`,
    which the email and PDF print verbatim ("Digest failed: ...").
    """
    result = DigestResult(model=TRANSCRIPT_MODEL)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        result.error = "ANTHROPIC_API_KEY is not set"
        return result
    if llm_calls[0] >= MAX_LLM_CALLS:
        result.error = f"LLM call cap reached ({MAX_LLM_CALLS})"
        return result

    try:
        client = make_client(api_key)
    except Exception as exc:
        result.error = f"could not create Anthropic client: {type(exc).__name__}: {exc}"
        return result

    body = transcript
    notes_parts = 0
    if len(transcript) > TRANSCRIPT_LLM_MAX_CHARS:
        chunks = chunk_text(transcript, TRANSCRIPT_CHUNK_CHARS)
        n = len(chunks)
        result.parts = n
        print(
            f"[ned/llm] Transcript is {len(transcript):,} chars (> {TRANSCRIPT_LLM_MAX_CHARS:,}); "
            f"extracting notes from {n} parts first"
        )
        notes: list[str] = []
        for i, chunk in enumerate(chunks, 1):
            if llm_calls[0] >= MAX_LLM_CALLS:
                result.error = f"LLM call cap reached ({MAX_LLM_CALLS}) while extracting part {i} of {n}"
                return result
            llm_calls[0] += 1
            prompt = CHUNK_EXTRACTION_PROMPT.format(
                part=i, total=n, kind=kind, title=title or source_url, text=chunk,
            )
            try:
                resp = llm_send(
                    client,
                    model=TRANSCRIPT_MODEL,
                    max_tokens=TRANSCRIPT_CHUNK_MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                )
                notes.append(f"## Part {i} of {n}\n{resp.text.strip()}")
                print(f"[ned/llm] Part {i}/{n}: {resp.input_tokens} in / {resp.output_tokens} out tokens")
            except Exception as exc:
                result.parts_failed += 1
                notes.append(f"## Part {i} of {n}\n[Notes for this part are missing: {type(exc).__name__}]")
                print(f"[ned/llm] Part {i}/{n} extraction failed: {exc}")
        if result.parts_failed == n:
            result.error = f"all {n} transcript parts failed to extract"
            return result
        body = "\n\n".join(notes)
        notes_parts = n

    if llm_calls[0] >= MAX_LLM_CALLS:
        result.error = f"LLM call cap reached ({MAX_LLM_CALLS})"
        return result
    llm_calls[0] += 1
    system = build_system_prompt(portfolio_context)
    user = build_user_prompt(
        kind=kind, title=title, channel=channel, source_url=source_url,
        body=body, from_chunk_notes=notes_parts,
    )
    try:
        resp = llm_send(
            client,
            model=TRANSCRIPT_MODEL,
            max_tokens=TRANSCRIPT_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        result.error = f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "") + f": {exc}"
        print(f"[ned/llm] Transcript digest LLM failed: {result.error}")
        return result

    print(
        f"[ned/llm] Digest ({TRANSCRIPT_MODEL}): {resp.input_tokens} in / "
        f"{resp.output_tokens} out tokens, stop={resp.stop_reason}"
    )
    digest_json, markdown = parse_digest_response(resp.text)
    result.digest_json = digest_json
    result.markdown = markdown
    if digest_json is None:
        if resp.truncated:
            result.error = (
                f"the reply was cut off at max_tokens ({TRANSCRIPT_MAX_TOKENS}); "
                "raise NED_TRANSCRIPT_MAX_TOKENS"
            )
        elif not markdown.strip():
            result.error = "the model returned an empty reply"
        else:
            print("[ned/llm] Reply was not valid JSON; keeping it as markdown")
    return result


# ----------------------------
# Single-episode transcript pipeline
# ----------------------------
def _save_transcripts(result: TranscriptResult) -> tuple[Path, Path]:
    """Write the plain and timestamped transcripts to disk. Returns their paths."""
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    base = f"{result.video_id}_{stamp}"
    plain_path = TRANSCRIPTS_DIR / f"{base}.txt"
    ts_path = TRANSCRIPTS_DIR / f"{base}.timestamped.txt"
    plain_path.write_text(result.plain_text, encoding="utf-8")
    ts_path.write_text(result.timestamped_text, encoding="utf-8")
    return plain_path, ts_path


# Email palette: the PDF's navy + gold, light background so it reads the same
# as the attachment it points to.
_E_NAVY = "#0B1F3A"
_E_GOLD = "#C9A227"
_E_INK = "#1A2233"
_E_SLATE = "#5B6577"
_E_HAIR = "#E3E6EB"
_E_TINT = "#F4F6FA"
_EFFECT_COLOURS = {
    "Kill condition at risk": "#B42318",
    "Challenges": "#B7791F",
    "Supports": "#2E7D5B",
    "Neutral": "#8A94A6",
}
_RELEVANCE_COLOURS = {  # (background, text)
    "High": (_E_NAVY, _E_GOLD),
    "Medium": (_E_GOLD, _E_NAVY),
    "Low": (_E_HAIR, _E_SLATE),
    "None": ("#FFFFFF", _E_SLATE),
    "Unrated": (_E_HAIR, _E_SLATE),
    "Failed": ("#B42318", "#FFFFFF"),
}


def _kind_label(kind: str) -> str:
    return "YouTube" if kind == "youtube" else "Podcast"


def _display_title(result: TranscriptResult) -> str:
    return (result.title or "").strip() or result.video_id


def _digest_subject(kind: str, title: str, digest: DigestResult) -> str:
    rel = digest.relevance
    tag = "Digest failed" if rel == "Failed" else rel
    return f"Ned [{tag}] {_kind_label(kind)}: {title}"


def _provenance(kind: str, result: TranscriptResult) -> str:
    if kind == "youtube":
        return f"YouTube {result.caption_kind} captions"
    return "Whisper transcription" if result.is_generated else "Published show transcript"


def _digest_email(
    *,
    kind: str,
    source_url: str,
    result: TranscriptResult,
    digest: DigestResult,
) -> tuple[str, str]:
    """(plain, html) for a transcript digest email. Short on purpose: the
    attached PDF is the product. Tables + inline styles only, so it renders
    the same in Gmail and Outlook."""
    import html as htmlmod

    esc = htmlmod.escape
    title = _display_title(result)
    kind_lbl = _kind_label(kind)
    d = digest.digest_json or {}
    rel = digest.relevance
    impact = [i for i in d.get("portfolio_impact", []) if i.get("effect") != "Neutral"]
    pdf_line = "The full analysis and the transcript are in the attached PDF."

    # ---- plain text ----
    lines = [f"Ned | {kind_lbl}: {title}"]
    if result.channel:
        lines.append(result.channel)
    lines += [f"Source: {source_url}", ""]
    if digest.error:
        lines += [f"Digest failed: {digest.error}", ""]
        if digest.markdown:
            lines += ["Raw model output:", digest.markdown, ""]
    elif d:
        reason = d.get("relevance_reason", "")
        lines += [f"Portfolio relevance: {rel}" + (f". {reason}" if reason else ""), ""]
        if d.get("tldr"):
            lines += ["TL;DR", d["tldr"], ""]
        lines.append("What it means for my portfolio")
        if impact:
            for it in impact:
                pillar = f" {it['pillar']}" if it.get("pillar") else ""
                lines.append(
                    f"- {it['ticker']} ({it['type']}): {it['effect']}{pillar}. "
                    f"{it['explanation']} Action: {it['action']}."
                )
        else:
            lines.append("No material impact on current holdings or watchlists.")
        lines.append("")
        if d.get("so_what"):
            lines += ["So what", d["so_what"], ""]
    else:
        lines += [digest.markdown, ""]
    lines.append(pdf_line)
    plain = "\n".join(lines)

    # ---- HTML ----
    font = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
    serif = "Georgia,'Times New Roman',serif"
    label_css = (
        f"font-size:11px;letter-spacing:1.5px;text-transform:uppercase;"
        f"color:{_E_GOLD};font-weight:700;"
    )
    rows: list[str] = []

    def row(inner: str, pad: str = "18px 28px") -> None:
        rows.append(f'<tr><td style="padding:{pad};">{inner}</td></tr>')

    sub = f'<div style="font-size:13px;color:#AEB7C6;margin-top:6px;">{esc(result.channel)}</div>' if result.channel else ""
    rows.append(
        f'<tr><td style="background:{_E_NAVY};padding:26px 28px;">'
        f'<div style="{label_css}">Ned &middot; {esc(kind_lbl)}</div>'
        f'<div style="font-family:{serif};font-size:22px;line-height:1.3;color:#FFFFFF;margin-top:8px;">'
        f'{esc(title)}</div>{sub}</td></tr>'
    )

    if digest.error:
        row(
            '<div style="background:#FDECEA;border-left:3px solid #B42318;padding:12px 14px;'
            f'color:#B42318;font-size:14px;"><b>Digest failed:</b> {esc(digest.error)}</div>'
        )
        if digest.markdown:
            row(
                f'<div style="{label_css}">Raw model output</div>'
                f'<div style="font-size:13px;color:{_E_INK};white-space:pre-wrap;margin-top:6px;">'
                f'{esc(digest.markdown[:4000])}</div>'
            )
    elif d:
        bg, fg = _RELEVANCE_COLOURS.get(rel, _RELEVANCE_COLOURS["Unrated"])
        reason = d.get("relevance_reason", "")
        row(
            f'<span style="display:inline-block;background:{bg};color:{fg};border:1px solid {_E_HAIR};'
            f'font-size:12px;font-weight:700;letter-spacing:1px;text-transform:uppercase;'
            f'padding:5px 10px;border-radius:3px;">{esc(rel)} relevance</span>'
            + (f'<div style="font-size:14px;color:{_E_SLATE};margin-top:8px;">{esc(reason)}</div>' if reason else ""),
            pad="22px 28px 6px 28px",
        )
        if d.get("tldr"):
            row(
                f'<div style="border-left:2px solid {_E_GOLD};padding:2px 0 2px 14px;'
                f'font-family:{serif};font-size:16px;line-height:1.5;color:{_E_INK};">'
                f'{esc(d["tldr"])}</div>'
            )
        items = []
        for it in impact:
            colour = _EFFECT_COLOURS.get(it["effect"], _EFFECT_COLOURS["Neutral"])
            pillar = f' &middot; {esc(it["pillar"])}' if it.get("pillar") else ""
            items.append(
                f'<tr><td style="padding:10px 0;border-top:1px solid {_E_HAIR};vertical-align:top;width:72px;">'
                f'<div style="font-family:{serif};font-size:15px;font-weight:700;color:{_E_NAVY};">{esc(it["ticker"])}</div>'
                f'<div style="font-size:11px;color:{_E_SLATE};">{esc(it["type"])}</div></td>'
                f'<td style="padding:10px 0 10px 12px;border-top:1px solid {_E_HAIR};vertical-align:top;">'
                f'<span style="display:inline-block;background:{colour};color:#FFFFFF;font-size:10px;'
                f'font-weight:700;letter-spacing:0.5px;text-transform:uppercase;padding:2px 7px;border-radius:9px;">'
                f'{esc(it["effect"])}</span><span style="font-size:11px;color:{_E_SLATE};">{pillar}</span>'
                f'<div style="font-size:13px;line-height:1.5;color:{_E_INK};margin-top:5px;">{esc(it["explanation"])}</div>'
                f'<div style="font-size:10px;letter-spacing:1px;text-transform:uppercase;color:{_E_SLATE};margin-top:4px;">'
                f'Action: {esc(it["action"])}</div></td></tr>'
            )
        body = (
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(items)}</table>'
            if items else
            f'<div style="font-size:14px;color:{_E_SLATE};">No material impact on current holdings or watchlists.</div>'
        )
        row(f'<div style="{label_css}margin-bottom:8px;">What it means for my portfolio</div>{body}')
        if d.get("so_what"):
            row(
                f'<div style="background:{_E_TINT};border-left:3px solid {_E_GOLD};padding:12px 14px;">'
                f'<div style="{label_css}">So what</div>'
                f'<div style="font-size:14px;line-height:1.55;color:{_E_INK};margin-top:4px;">{esc(d["so_what"])}</div>'
                '</div>'
            )
    else:
        row(_digest_markdown_to_html(digest.markdown, colour=_E_INK))

    row(
        f'<div style="font-size:13px;color:{_E_SLATE};border-top:1px solid {_E_HAIR};padding-top:14px;">'
        f'{esc(pdf_line)}<br><a href="{esc(source_url)}" style="color:{_E_NAVY};">{esc(source_url)}</a></div>',
        pad="10px 28px 26px 28px",
    )
    html = (
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{_E_TINT};font-family:{font};"><tr><td align="center" style="padding:20px 8px;">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:640px;background:#FFFFFF;border:1px solid {_E_HAIR};">'
        + "".join(rows)
        + "</table></td></tr></table>"
    )
    return plain, html


def _transcript_email(source_url: str, result: TranscriptResult, digest: DigestResult) -> tuple[str, str]:
    """(plain, html) for a YouTube transcript digest."""
    return _digest_email(kind="youtube", source_url=source_url, result=result, digest=digest)


def _digest_markdown_to_html(md: str, colour: str = "#E5E7EB") -> str:
    """Very small markdown renderer for the digest: bold, bullets, paragraphs."""
    import html as htmlmod
    import re as _re

    def inline(text: str) -> str:
        text = htmlmod.escape(text)
        return _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)

    html_parts: list[str] = []
    in_list = False
    for raw in md.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        is_bullet = stripped.startswith(("- ", "* ", "• "))
        if is_bullet:
            if not in_list:
                html_parts.append('<ul style="margin:6px 0 10px 18px;padding:0;">')
                in_list = True
            item = stripped[2:].strip()
            html_parts.append(
                f'<li style="font-size:13px;color:{colour};margin:3px 0;">{inline(item)}</li>'
            )
        else:
            if in_list:
                html_parts.append("</ul>")
                in_list = False
            if stripped:
                html_parts.append(
                    f'<div style="font-size:13px;color:{colour};margin:6px 0;">{inline(stripped)}</div>'
                )
    if in_list:
        html_parts.append("</ul>")
    return "".join(html_parts)


# The transcript history file lives alongside Bob/Ned/Wally/Sally's data
# under docs/data/. The dashboard build reads it and renders a section; each
# workflow run commits+pushes it, and the Pages workflow re-deploys on
# workflow_run (see .github/workflows/theo-pages.yml).
_TRANSCRIPTS_HISTORY_PATH = REPO_ROOT / "docs" / "data" / "transcripts.json"
# Keep the last N entries. Enough to browse a few weeks of listens; each
# entry carries its digest_json (~5-10 KB), so the file stays well under 1 MB.
_TRANSCRIPTS_HISTORY_MAX = 40

# Rendered PDFs for each transcript run land here and get committed to the
# public site. Anyone with the link can download them, so we prune anything
# older than the retention window on each run — "for a short time" per the
# original ask. The file gone from HEAD stops working from Pages; git
# history is unavoidable, but the source podcast/YouTube is public anyway.
_TRANSCRIPT_PDFS_DIR = REPO_ROOT / "docs" / "transcripts"
_TRANSCRIPT_PDFS_RETENTION_DAYS = int(os.environ.get("NED_TRANSCRIPT_PDF_RETENTION_DAYS", "30"))


def _save_transcript_pdf(
    *,
    kind: str,
    result: TranscriptResult,
    source_url: str,
    title: str,
    digest: DigestResult,
) -> Path | None:
    """Render a PDF of {digest + full transcript}, save to docs/transcripts/,
    prune older ones, and return the path — or None on any failure.

    Failures are printed and swallowed: the email + dashboard row must still
    land even if PDF generation goes sideways. The dashboard card falls back
    to "no PDF available" when the field is missing.
    """
    from ned.transcript_pdf import build_transcript_pdf, TranscriptPdfError

    try:
        _TRANSCRIPT_PDFS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        pdf_path = _TRANSCRIPT_PDFS_DIR / f"{result.video_id}_{stamp}.pdf"
        build_transcript_pdf(
            kind=kind,
            title=title,
            source_url=source_url,
            digest_markdown=digest.markdown or "",
            transcript_text=result.plain_text,
            output_path=pdf_path,
            digest_json=digest.digest_json,
            digest_error=digest.error,
            channel=result.channel,
            provenance=_provenance(kind, result),
            segments=result.segments,
            parts=digest.parts,
            parts_failed=digest.parts_failed,
        )
        size_kb = pdf_path.stat().st_size / 1024
        print(f"[ned/transcript] Wrote PDF -> {pdf_path.relative_to(REPO_ROOT)} ({size_kb:.0f} KB)")
        _prune_old_transcript_pdfs()
        return pdf_path
    except TranscriptPdfError as exc:
        print(f"[ned/transcript] PDF generation failed (no backend usable): {exc}")
    except Exception as exc:
        print(f"[ned/transcript] PDF generation failed: {exc}")
    return None


def _prune_old_transcript_pdfs() -> int:
    """Delete PDFs older than the retention window. Returns count removed.

    Uses mtime so it doesn't depend on the filename format holding the
    stamp — a filename convention we might change later shouldn't leave
    old files pinned. Never raises; a broken prune must not fail the run.
    """
    if not _TRANSCRIPT_PDFS_DIR.exists():
        return 0
    cutoff = dt.datetime.utcnow().timestamp() - _TRANSCRIPT_PDFS_RETENTION_DAYS * 86400
    removed = 0
    for p in _TRANSCRIPT_PDFS_DIR.glob("*.pdf"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except Exception as exc:
            print(f"[ned/transcript] Could not prune {p.name}: {exc}")
    if removed:
        print(
            f"[ned/transcript] Pruned {removed} PDF(s) older than "
            f"{_TRANSCRIPT_PDFS_RETENTION_DAYS}d from {_TRANSCRIPT_PDFS_DIR.relative_to(REPO_ROOT)}"
        )
    return removed


def _append_transcript_history(entry: dict) -> None:
    """Append one transcript entry to docs/data/transcripts.json.

    Ordered newest-first (matches every other agent's JSON), capped at
    _TRANSCRIPTS_HISTORY_MAX. Never raises — a failure here must not
    kill the run, since the email is what the user actually asked for
    and the dashboard update is a bonus.
    """
    try:
        _TRANSCRIPTS_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing: list[dict] = []
        if _TRANSCRIPTS_HISTORY_PATH.exists():
            try:
                existing = json.loads(_TRANSCRIPTS_HISTORY_PATH.read_text(encoding="utf-8") or "[]")
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
        # De-dupe on (kind, source_url) so re-running the same episode
        # updates the entry in place rather than piling up rows.
        key = (entry.get("kind"), entry.get("source_url"))
        existing = [
            e for e in existing
            if (e.get("kind"), e.get("source_url")) != key
        ]
        existing.insert(0, entry)
        existing = existing[:_TRANSCRIPTS_HISTORY_MAX]
        _TRANSCRIPTS_HISTORY_PATH.write_text(
            json.dumps(existing, indent=2), encoding="utf-8"
        )
        print(
            f"[ned/transcript] Wrote transcript entry to "
            f"{_TRANSCRIPTS_HISTORY_PATH.relative_to(REPO_ROOT)} "
            f"({len(existing)} entries total)"
        )
    except Exception as exc:
        print(f"[ned/transcript] Could not update transcripts history: {exc}")


def _digest_and_deliver(kind: str, url: str, result: TranscriptResult) -> int:
    """Shared tail of the YouTube and podcast runs: save the transcript,
    analyse it against the portfolio, render the PDF, email, and record the
    run on the dashboard. Returns the process exit code."""
    tag = "[ned/transcript]" if kind == "youtube" else "[ned/podcast]"
    title = _display_title(result)

    plain_path, ts_path = _save_transcripts(result)
    print(f"{tag} Saved plain transcript      -> {plain_path}")
    print(f"{tag} Saved timestamped transcript -> {ts_path}")

    try:
        context = load_portfolio_context(REPO_ROOT)
    except Exception as exc:  # load_portfolio_context already fails soft
        print(f"{tag} Portfolio context unavailable: {exc}")
        context = ""

    llm_calls = [0]
    digest = llm_transcript_digest(
        url, result.plain_text, llm_calls,
        kind=kind, title=title, channel=result.channel, portfolio_context=context,
    )
    if digest.error:
        print(f"{tag} Digest failed: {digest.error}")
    elif digest.digest_json:
        print(f"{tag} LLM digest produced (relevance: {digest.relevance}).")
    else:
        print(f"{tag} LLM digest produced as markdown (reply was not JSON).")

    pdf_path = _save_transcript_pdf(
        kind=kind, result=result, source_url=url, title=title, digest=digest,
    )

    plain, html = _digest_email(kind=kind, source_url=url, result=result, digest=digest)
    _maybe_email(
        subject=_digest_subject(kind, title, digest),
        plain=plain,
        html=html,
        attachments=[pdf_path] if pdf_path else None,
    )

    entry = {
        "timestamp": dt.datetime.utcnow().isoformat() + "Z",
        "kind": kind,
        "source_url": url,
        "title": title,
        "channel": result.channel,
        "video_id": result.video_id,
        "portfolio_relevance": digest.relevance,
        "digest_markdown": digest.markdown or "",
        "digest_json": digest.digest_json,
        "digest_error": digest.error,
        "model": digest.model,
        "parts": digest.parts,
        "chars": len(result.plain_text),
        # Path relative to docs/ so the dashboard can build the Pages URL as
        # <site>/<pdf_path>. Empty when PDF generation failed — the card
        # then omits the download link and the row still renders.
        "pdf_path": (str(pdf_path.relative_to(REPO_ROOT / "docs")).replace("\\", "/") if pdf_path else ""),
    }
    if kind == "youtube":
        entry["caption_kind"] = result.caption_kind
    else:
        # is_generated is False when a published RSS transcript was used,
        # True when we fell back to Whisper.
        entry["used_whisper"] = bool(result.is_generated)
    _append_transcript_history(entry)
    return 0


def run_transcript_digest(url: str) -> int:
    """Fetch a single YouTube episode transcript, save it, analyse it against
    JM's portfolio, and email the digest with the PDF attached.

    Returns a process exit code (0 on success, 1 on a handled failure).
    """
    print(f"[ned/transcript] Requested URL: {url}")
    try:
        video_id = extract_video_id(url)
    except TranscriptError as exc:
        print(f"[ned/transcript] {exc}")
        return 1
    print(f"[ned/transcript] Video ID: {video_id}")

    try:
        result = fetch_transcript(video_id)
    except TranscriptError as exc:
        # Captions disabled / none found / network — a clear message, no crash.
        print(f"[ned/transcript] {exc}")
        _maybe_email(
            subject=f"Ned — transcript fetch failed — {video_id}",
            plain=f"Could not fetch transcript for {url}\n\n{exc}",
            html=(
                '<div style="padding:18px;background:#0B1220;color:#E5E7EB;">'
                f'<b>Transcript fetch failed for</b> {video_id}<br>{exc}</div>'
            ),
        )
        return 1

    print(
        f"[ned/transcript] Got {len(result.segments)} segments "
        f"({result.caption_kind} captions, {result.language_code or '?'}); "
        f"{len(result.plain_text)} chars of plain text"
    )

    meta = fetch_video_metadata(video_id)
    result.title = meta.get("title") or ""
    result.channel = meta.get("channel") or ""
    print(f"[ned/transcript] Title: {_display_title(result)!r}"
          + (f" ({result.channel})" if result.channel else ""))

    return _digest_and_deliver("youtube", url, result)


def run_podcast_digest(url: str) -> int:
    """Fetch a single podcast episode (published transcript, else Whisper),
    save it, analyse it against JM's portfolio, and email the digest with the
    PDF attached.

    Returns a process exit code (0 on success, 1 on a handled failure).
    """
    print(f"[ned/podcast] Requested URL: {url}")
    try:
        result = fetch_podcast_transcript(url)
    except TranscriptError as exc:
        print(f"[ned/podcast] {exc}")
        _maybe_email(
            subject="Ned — podcast transcript fetch failed",
            plain=f"Could not fetch podcast transcript for {url}\n\n{exc}",
            html=(
                '<div style="padding:18px;background:#0B1220;color:#E5E7EB;">'
                f'<b>Podcast transcript fetch failed for</b> {htmlmod_escape(url)}'
                f'<br>{htmlmod_escape(str(exc))}</div>'
            ),
        )
        return 1

    print(
        f"[ned/podcast] Got {len(result.segments)} segments; "
        f"{len(result.plain_text)} chars of plain text"
    )
    return _digest_and_deliver("podcast", url, result)


def htmlmod_escape(s: str) -> str:
    """Local shim so run_podcast_digest doesn't need to import at module top."""
    import html as _h
    return _h.escape(s)


def _podcast_email(source_url: str, result: TranscriptResult, digest: DigestResult) -> tuple[str, str]:
    """(plain, html) for a podcast transcript digest."""
    return _digest_email(kind="podcast", source_url=source_url, result=result, digest=digest)


def _maybe_email(
    subject: str,
    plain: str,
    html: str,
    attachments: list[Path] | None = None,
) -> None:
    """Send via Ned's SMTP path when email env is configured; else print.

    `attachments` is a list of file paths (usually one PDF). A path that
    doesn't exist is skipped with a log line rather than raising, so a
    failed PDF render never blocks the email.
    """
    valid_attachments: list[Path] = []
    for p in (attachments or []):
        if p and p.exists():
            valid_attachments.append(p)
        elif p:
            print(f"[ned/transcript] Attachment missing, skipping: {p}")

    if all(os.environ.get(k) for k in ("EMAIL_FROM", "EMAIL_TO", "EMAIL_APP_PASSWORD")):
        try:
            send_email(subject, plain, html, attachments=valid_attachments)
            print(
                f"[ned/transcript] Email sent"
                + (f" with {len(valid_attachments)} attachment(s)" if valid_attachments else "")
                + "."
            )
            return
        except Exception as exc:
            print(f"[ned/transcript] Email send failed: {exc}")
    print("[ned/transcript] Email not configured — digest below:\n")
    print(plain)


# ----------------------------
# Email
# ----------------------------
def send_email(
    subject: str,
    plain: str,
    html: str,
    attachments: list[Path] | None = None,
) -> None:
    """Send Ned's digest. Raises if the email env is missing or SMTP fails."""
    shared_send_email(
        subject, plain, html,
        attachments=attachments or None,
        raise_on_error=True,
        log_prefix="[ned/email]",
    )


def today_sgt() -> str:
    sgt = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime.now(sgt).date().isoformat()


# ----------------------------
# Dashboard JSON
# ----------------------------
# Only these importance levels are surfaced on the public dashboard.
_DASHBOARD_LEVELS = ("CRITICAL", "HIGH")
# At most this many cards per company — the dashboard shows each company's
# highest-priority items, not a flat global top-N. A company with lots of news
# still only takes two cards, so twenty companies is forty cards, not two
# hundred.
_DASHBOARD_MAX_PER_COMPANY = 2


def _dashboard_items(all_hits: list[dict], summaries: dict[str, str]) -> list[dict]:
    """Pick the Critical + High hits for the dashboard: each company's two
    highest-priority items, companies ordered by their most important item.

    A hit is attributed to its first ticker (news hits carry exactly one), and
    the same headline never appears twice.
    """
    picked = [h for h in all_hits if h.get("importance_level") in _DASHBOARD_LEVELS]
    # Most important first; title breaks ties for a stable order.
    picked.sort(key=lambda h: (-h.get("importance_score", 0), h.get("title", "")))

    per_company: dict[str, list[dict]] = {}
    seen_keys: set[str] = set()
    for h in picked:
        key = h.get("seen_key", "")
        if key and key in seen_keys:
            continue
        tickers = h.get("tickers") or []
        company = tickers[0] if tickers else "—"
        bucket = per_company.setdefault(company, [])
        if len(bucket) >= _DASHBOARD_MAX_PER_COMPANY:
            continue
        if key:
            seen_keys.add(key)
        bucket.append({
            "tickers": tickers,
            "title": h.get("title", ""),
            "url": h.get("url", ""),
            "source": h.get("source", ""),
            "level": h.get("importance_level", ""),
            "summary": summaries.get(key, ""),
            "published": h.get("published", ""),
            "_score": h.get("importance_score", 0),
        })

    # Order companies by their single most-important item, keep each company's
    # cards together, then drop the private sort key.
    ordered_companies = sorted(
        per_company.values(),
        key=lambda cards: -max(c["_score"] for c in cards),
    )
    items: list[dict] = []
    for cards in ordered_companies:
        for c in cards:
            c.pop("_score", None)
            items.append(c)
    return items


def write_dashboard_json(
    youtube_hits: list[dict],
    news_hits: list[dict],
    summaries: dict[str, str] | None = None,
) -> None:
    out_path = REPO_ROOT / "docs" / "data" / "ned.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    total = len(youtube_hits) + len(news_hits)
    all_hits = youtube_hits + news_hits
    items = _dashboard_items(all_hits, summaries or {})
    critical_high_total = sum(
        1 for h in all_hits if h.get("importance_level") in _DASHBOARD_LEVELS
    )
    out_path.write_text(json.dumps({
        "agent": "Ned",
        "last_run": dt.datetime.utcnow().isoformat() + "Z",
        "youtube_hits": len(youtube_hits),
        "news_hits": len(news_hits),
        "total_hits": total,
        "critical_high_count": critical_high_total,
        "status": "ok" if total else "silence",
        "items": items,
    }, indent=2))


# ----------------------------
# Main
# ----------------------------
def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        prog="ned",
        description="Ned the News Agent — media scanner and transcript fetcher.",
    )
    parser.add_argument(
        "--transcript",
        metavar="YOUTUBE_URL",
        default=None,
        help=(
            "Fetch a single YouTube episode's transcript, save it, summarise it, "
            "and email the digest. If omitted, Ned runs its normal channel/feed "
            "scan. Falls back to the NED_TRANSCRIPT_URL environment variable."
        ),
    )
    parser.add_argument(
        "--podcast",
        metavar="PODCAST_URL",
        default=None,
        help=(
            "Fetch a single podcast episode (direct .mp3, Apple Podcasts link, "
            "or RSS feed URL), transcribe it via the OpenAI Whisper API, save "
            "it, summarise it and email the digest. Falls back to the "
            "NED_PODCAST_URL environment variable."
        ),
    )
    args = parser.parse_args(argv)

    # A pasted link (CLI flag or workflow input via env) switches Ned into
    # single-episode transcript / podcast mode instead of the daily scan.
    transcript_url = args.transcript or os.environ.get("NED_TRANSCRIPT_URL", "").strip()
    if transcript_url:
        return run_transcript_digest(transcript_url)

    podcast_url = args.podcast or os.environ.get("NED_PODCAST_URL", "").strip()
    if podcast_url:
        return run_podcast_digest(podcast_url)

    return run_scan()


def run_scan():
    # Load portfolio
    with open(TICKERS_PATH) as f:
        ticker_data = yaml.safe_load(f) or {}

    asx = ticker_data.get("asx", {})
    lse = ticker_data.get("lse", {})
    skip = set(ticker_data.get("etf_tickers", []))
    # Build {TICKER: "Company Name"} excluding ETFs
    companies: dict[str, str] = {}
    if isinstance(asx, dict):
        companies.update({k: v for k, v in asx.items() if k not in skip})
    if isinstance(lse, dict):
        companies.update(lse)

    # Load media sources
    with open(MEDIA_SOURCES_PATH) as f:
        media = yaml.safe_load(f) or {}

    lookback_hours: int = media.get("lookback_hours", 48)
    yt_channels: list[dict] = media.get("youtube_channels", [])
    rss_feeds: list[dict] = media.get("rss_feeds", [])
    skip_news = set(media.get("skip_news_tickers", []))
    news_companies = {k: v for k, v in companies.items() if k not in skip_news}

    # Load seen state
    seen = load_seen(SEEN_STATE_PATH)
    print(f"[ned] {len(seen)} items in seen state")

    # Scan YouTube
    print(f"[ned] Scanning {len(yt_channels)} YouTube channel(s)…")
    youtube_hits = scan_youtube_channels(yt_channels, companies, lookback_hours, seen)
    print(f"[ned] {len(youtube_hits)} YouTube hit(s)")

    # Scan RSS + Yahoo Finance
    print(f"[ned] Scanning {len(rss_feeds)} RSS feed(s) + Yahoo Finance…")
    rss_hits = scan_rss_feeds(rss_feeds, news_companies, lookback_hours, seen)
    yf_hits = scan_yahoo_finance(news_companies, lookback_hours, seen)
    news_hits = rss_hits + yf_hits
    print(f"[ned] {len(news_hits)} news hit(s)")

    # LLM summaries
    llm_calls = [0]
    summaries: dict[str, str] = {}
    all_hits = youtube_hits + news_hits
    for hit in all_hits:
        summary = llm_summarise(hit, llm_calls)
        if summary and summary != "[not material]":
            summaries[hit["seen_key"]] = summary

    # Filter out "[not material]" hits where LLM flagged them
    youtube_hits = [h for h in youtube_hits if summaries.get(h["seen_key"]) != "[not material]"]
    news_hits = [h for h in news_hits if summaries.get(h["seen_key"]) != "[not material]"]

    # Sort news by importance (adds importance_score and importance_level to each hit)
    youtube_hits = sort_by_importance(youtube_hits)
    news_hits = sort_by_importance(news_hits)

    # Build + send email
    run_date = today_sgt()
    plain, html = build_email(youtube_hits, news_hits, summaries, lookback_hours, run_date)
    subject = f"Ned the News Agent — Media Digest — {run_date} (SGT)"

    print(f"[ned] Sending email: {len(youtube_hits)} YT + {len(news_hits)} news")
    send_email(subject, plain, html)
    print("[ned] Email sent.")

    # Update seen state
    for hit in all_hits:
        seen.add(hit["seen_key"])
    save_seen(SEEN_STATE_PATH, seen)

    # Write dashboard JSON
    write_dashboard_json(youtube_hits, news_hits, summaries)


if __name__ == "__main__":
    sys.exit(main() or 0)
