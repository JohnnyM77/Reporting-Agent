"""
sgx_agent.py -- Bob SG orchestrator (V4 Round 2).

Ties Round 1's fetch layer (sgx_fetch.py) to the classifier, PDF fetcher,
LLM analysis (via shared/pdf_llm), and email sender. Runs standalone on
the self-hosted Windows runner. Does NOT touch agent.py or the ASX Bob
path -- deliberately duplicates the ~40 lines of LLM-plumbing rather
than coupling to agent.py (which drags weasyprint / pypdf / bs4 imports
the SGX box may not need for anything else).

Pipeline
--------
    1. Fetch (sgx_fetch)                  -- Playwright prime + REST replay
    2. Filter by hours_back window
    3. Dedupe via seen_state              -- state_seen_sgx.json
    4. Classify (sgx_classify)            -- by SGX sub/category, not title
    5. For RESULTS_HY_FY items only:
         a. Download PDFs (sgx_pdf)       -- each carries a source_url
         b. Anthropic streaming call with native PDF blocks + RESULTS_HYFY_PROMPT
         c. Parse structured JSON (metric table + summary + full_analysis)
         d. Render the deep analysis to a standalone PDF via Playwright
    6. Build email (sgx_email)            -- 3 buckets, same shape as ASX Bob;
                                             email body carries metric card,
                                             summary, links to source PDFs
    7. Send via SMTP                      -- same env vars as agent.py; the
                                             analysis PDF(s) are attached so
                                             the user can forward them
    8. Save seen_state

Design note: the source PDFs are LINKED (not attached) so the email
stays lightweight; the analysis is ATTACHED as a PDF because the user
wants a self-contained forwardable file rather than an inline body
block. Rendered via Playwright + installed Chrome (already required by
sgx_fetch, so no new deps).

Env
---
    EMAIL_FROM, EMAIL_TO, EMAIL_APP_PASSWORD   -- SMTP (Gmail app password)
    ANTHROPIC_API_KEY                          -- Claude
    CLAUDE_MODEL                               -- optional, defaults inline
    SGX_HOURS_BACK                             -- window (default 24)
    SEEN_STATE_SGX_PATH                        -- seen-state file path

CLI
---
    python sgx_agent.py [--dry-run]           -- runs the whole pipeline
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import smtplib
import ssl
import sys
import tempfile
import traceback
from email.message import EmailMessage
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from sgx_fetch import fetch_sgx_announcements
from sgx_classify import classify_sgx_announcement
from sgx_pdf import fetch_announcement_pdfs, FetchedPdf
from sgx_email import build_email, build_analysis_pdf_html, BOB_SG_NAME, BOB_SG_VERSION
from shared.pdf_llm import (
    LLM_FAILED,
    LLM_SKIPPED,
    PdfAttachment,
    build_pdf_attachments,
)

try:
    from prompts import RESULTS_HYFY_PROMPT
except Exception:  # pragma: no cover -- if prompts.py imports break, we still email FYI
    RESULTS_HYFY_PROMPT = ""


SGT = dt.timezone(dt.timedelta(hours=8))

CLAUDE_MODEL_DEFAULT = "claude-sonnet-4-5-20250929"
CLAUDE_RESULTS_MAX_TOKENS = int(os.environ.get("CLAUDE_RESULTS_MAX_TOKENS", "50000"))
_STREAMING_MIN_TOKENS = 8192   # matches agent.py -- keep in sync if it changes

SEEN_STATE_PATH = Path(os.environ.get("SEEN_STATE_SGX_PATH", "state_seen_sgx.json"))
SEEN_STATE_RETENTION_HOURS = 72
HOURS_BACK_DEFAULT = int(os.environ.get("SGX_HOURS_BACK", "24"))

# Results-ticker mode: how far back to look for a results release when the
# user asks "get me 5DD's last half" outside the normal 24h window. Mirrors
# ASX Bob's RESULTS_LOOKBACK_DAYS (180 by default). Micro-cap SG names like
# Micro-Mechanics report once a year, so 180 days is usually enough for FY;
# annual reporters that just missed it may need 365.
RESULTS_LOOKBACK_DAYS = int(os.environ.get("SGX_RESULTS_LOOKBACK_DAYS", "365"))

# Cap Round 2 to avoid a runaway reporting-morning bill. When several banks
# report on the same day this will need loosening -- same problem Bob's ASX
# path already solved.
MAX_LLM_CALLS_PER_RUN = int(os.environ.get("SGX_MAX_LLM_CALLS", "10"))
MAX_PDFS_PER_RUN = int(os.environ.get("SGX_MAX_PDFS", "20"))

# Per-announcement PDF cap. SGX results releases commonly bundle 3-5 PDFs
# under one announcement (per the DBS half-year screenshot: performance
# summary + CFO deck + CEO deck + press statement). 10 is enough to catch
# even the wordiest reporters without hitting Claude's 32MB request cap.
MAX_PDFS_PER_ANNOUNCEMENT = int(os.environ.get("SGX_MAX_PDFS_PER_ANNOUNCEMENT", "10"))


# --- utils -------------------------------------------------------------------

def _log(msg: str) -> None:
    print(msg, flush=True)


def _sgt_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(SGT)


def _announcement_key(item: Dict) -> str:
    """Dedup key. SGX's ref_id is a stable per-announcement identifier
    ("SG260909OTHRLOH7"), so use it directly rather than the URL (which
    can churn if SGX regenerates the opaque hash)."""
    ref = (item.get("ref_id") or "").strip()
    if ref:
        return f"SGX:{ref}"
    return f"SGX:{item.get('ticker', '')}:{item.get('url', '')}"


def _load_seen_state(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log(f"[seen] could not load {path}: {exc}")
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _save_seen_state(path: Path, state: Dict[str, str]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _prune_seen_state(state: Dict[str, str], retention_hours: int) -> Dict[str, str]:
    cutoff = _sgt_now() - dt.timedelta(hours=retention_hours)
    out: Dict[str, str] = {}
    for key, seen_iso in state.items():
        try:
            seen_dt = dt.datetime.fromisoformat(seen_iso)
        except Exception:
            continue
        if seen_dt.tzinfo is None:
            seen_dt = seen_dt.replace(tzinfo=SGT)
        if seen_dt >= cutoff:
            out[key] = seen_iso
    return out


def _load_sgx_tickers(tickers_yaml: Path) -> List[str]:
    if not tickers_yaml.exists():
        return []
    data = yaml.safe_load(tickers_yaml.read_text(encoding="utf-8")) or {}
    sgx = data.get("sgx") or {}
    if not isinstance(sgx, dict):
        return []
    return sorted(k for k in sgx.keys() if isinstance(k, str))


def _within_hours(item: Dict, hours: int) -> bool:
    ts_ms = item.get("submission_ts_ms")
    if not ts_ms:
        # Undated -> keep it; the alternative silently drops potentially
        # important items and we've been bitten by that before with BXB.
        return True
    try:
        when = dt.datetime.fromtimestamp(int(ts_ms) / 1000, tz=dt.timezone.utc)
    except Exception:
        return True
    return when >= dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)


# --- LLM -------------------------------------------------------------------

def _parse_analysis_json(text: str) -> Optional[Dict]:
    """Simpler than agent.py's _parse_analysis_json (which does bracket
    repair on truncated JSON). Round 2 tries a straight parse and an
    outermost-{...}-span fallback; anything past that renders as "analysis
    unavailable" and the raw text is stashed on the Doc for inspection."""
    if not text or text in (LLM_SKIPPED, LLM_FAILED):
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group())
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _anthropic_call(
    pdf_paths: List[Path],
    system_prompt: str,
    user_prompt: str,
    counters: Dict,
) -> str:
    """Call Claude with native PDF attachments (via shared/pdf_llm) and
    return the raw text response. Uses streaming for large max_tokens.
    Returns LLM_SKIPPED if the run cap is reached, LLM_FAILED on API
    error -- same sentinels as shared/pdf_llm so downstream branching
    matches."""
    if counters["llm_calls"] >= MAX_LLM_CALLS_PER_RUN:
        _log(f"[llm] cap reached ({MAX_LLM_CALLS_PER_RUN}) -- skipping")
        return LLM_SKIPPED

    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        _log("[llm] ANTHROPIC_API_KEY not set")
        return LLM_FAILED

    # PdfAttachment takes name + pdf_bytes; we read the bytes off disk here
    # so shared/pdf_llm can pick between a native document block and a
    # pypdf-extracted fallback per its own size/page rules.
    attachments: List[PdfAttachment] = []
    for p in pdf_paths:
        try:
            data = p.read_bytes()
        except Exception as exc:
            _log(f"[llm] could not read {p.name}: {exc}")
            continue
        attachments.append(PdfAttachment(name=p.name, pdf_bytes=data))
    batch = build_pdf_attachments(attachments, log=_log)
    if batch.any_native:
        content: object = list(batch.document_blocks) + [
            {"type": "text", "text": user_prompt[:50_000]}
        ]
    else:
        prefix = "\n\n".join(batch.fallback_sections)
        content = ((prefix + "\n\n" + user_prompt) if prefix else user_prompt)[:100_000]

    # `or` -- an EMPTY env var (GH sets missing secrets to "") should fall
    # back to the default, not pass "" to Anthropic which then 400s with
    # "model: String should have at least 1 character".
    model = os.environ.get("CLAUDE_MODEL") or CLAUDE_MODEL_DEFAULT
    max_tokens = CLAUDE_RESULTS_MAX_TOKENS
    counters["llm_calls"] += 1
    counters.pop("last_stop_reason", None)

    client = anthropic.Anthropic(api_key=api_key)
    try:
        if max_tokens >= _STREAMING_MIN_TOKENS:
            parts: List[str] = []
            with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
            ) as stream:
                for chunk in stream.text_stream:
                    parts.append(chunk)
                final = stream.get_final_message()
            text = "".join(parts)
            counters["last_stop_reason"] = str(getattr(final, "stop_reason", "") or "")
        else:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": content}],
            )
            text = resp.content[0].text if resp.content else ""
            counters["last_stop_reason"] = str(getattr(resp, "stop_reason", "") or "")
        if counters.get("last_stop_reason") == "max_tokens":
            _log(f"[llm] WARNING truncated at max_tokens={max_tokens}")
        return (text or "").strip()
    except Exception as exc:
        _log(f"[llm] ERROR {exc.__class__.__name__}: {exc}")
        counters["last_llm_error"] = f"{exc.__class__.__name__}: {exc}"
        return LLM_FAILED


# --- analysis PDF rendering -------------------------------------------------

def _render_analysis_pdf(html: str, out_path: Path) -> bool:
    """Print `html` to `out_path` using Playwright + installed Chrome.

    Chrome is already provisioned on the self-hosted Windows runner for
    sgx_fetch's token-prime step, so this adds no deps. `page.pdf` only
    works with Chromium-family browsers, which channel='chrome' is."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _log(f"[analysis-pdf] playwright not available: {exc}")
        return False
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page()
                # `load` waits for external resources -- fine here because
                # we author self-contained HTML (no <img>/<script>/CSS refs).
                page.set_content(html, wait_until="load")
                page.pdf(
                    path=str(out_path),
                    format="A4",
                    print_background=True,
                    margin={"top": "14mm", "right": "14mm",
                            "bottom": "18mm", "left": "14mm"},
                )
            finally:
                browser.close()
        _log(f"[analysis-pdf] wrote {out_path.name} ({out_path.stat().st_size}B)")
        return True
    except Exception as exc:
        _log(f"[analysis-pdf] render failed: {exc.__class__.__name__}: {exc}")
        return False


# --- deep results analysis --------------------------------------------------

def _run_results_analysis(
    item: Dict,
    pdf_paths: List[Path],
    counters: Dict,
    results_hint: str = "",
) -> Optional[Dict]:
    """Run one results-item deep analysis. Returns the parsed analysis
    dict (metrics + summary + full_analysis markdown), or None if
    analysis couldn't be produced.

    `results_hint` is a free-form paragraph appended to the user prompt
    -- use it to give the model context Bob doesn't otherwise know (e.g.
    "Haw Par's main asset is its holding in UOB, not the Tiger Balm
    business, so the analysis should focus on the mark-to-market bank
    stake and dividend flow-through, not consumer product revenue").
    Passed through unchanged; the prompt is otherwise ticker-agnostic."""
    ticker = item.get("ticker") or ""
    title = item.get("title") or ""
    issuer = item.get("issuer_name") or ""

    if not RESULTS_HYFY_PROMPT:
        _log("[results] RESULTS_HYFY_PROMPT unavailable -- skipping deep analysis")
        return None
    if not pdf_paths:
        _log(f"[results] {ticker}: no PDFs -- skipping deep analysis")
        return None

    hint_block = ""
    if results_hint.strip():
        hint_block = (
            f"\n\nUser-supplied context (weight this in your analysis, "
            f"especially where it points to what matters vs. what's noise):\n"
            f"{results_hint.strip()}"
        )

    user = (
        f"Ticker: {ticker}\n"
        f"Issuer: {issuer}\n"
        f"Title: {title}\n\n"
        f"The attached PDF(s) contain the full results release / statements. "
        f"Return your response as strict JSON per the system prompt schema. "
        f"Note this issuer reports in Singapore dollars (SGD) — reflect that "
        f"in every metric's currency and prefix figures with S$."
        f"{hint_block}"
    )
    text = _anthropic_call(pdf_paths, RESULTS_HYFY_PROMPT, user, counters)
    if text in (LLM_SKIPPED, LLM_FAILED):
        _log(f"[results] {ticker}: LLM returned {text}")
        return None

    parsed = _parse_analysis_json(text)
    if not parsed:
        _log(f"[results] {ticker}: JSON parse failed")
        return {
            "summary": "Analysis failed: could not parse model output. "
                       "Raw text preserved below.",
            "period": "",
            "period_type": "",
            "metrics": {},
            "full_analysis": (
                "## Raw model output (unparseable)\n\n"
                f"{text}"
            ),
            "_raw_text": text,
        }
    return parsed


# --- main pipeline ----------------------------------------------------------

# Gmail rejects attachments totalling ~25MB. Cap conservatively and let
# oversized PDFs go missing rather than fail the send outright — the
# email still carries the source-announcement link.
MAX_ATTACHMENT_TOTAL_BYTES = 20 * 1024 * 1024


def _send_email(
    subject: str,
    body_text: str,
    body_html: str,
    attachments: Optional[List[Path]] = None,
) -> bool:
    email_from = os.environ.get("EMAIL_FROM", "").strip()
    email_to = os.environ.get("EMAIL_TO", "").strip()
    app_pw = os.environ.get("EMAIL_APP_PASSWORD", "").strip()
    if not (email_from and email_to and app_pw):
        _log("[email] EMAIL_* env not set -- skipping send")
        return False
    msg = EmailMessage()
    msg["From"] = email_from
    msg["To"] = email_to
    msg["Subject"] = subject
    msg.set_content(body_text)
    msg.add_alternative(body_html, subtype="html")

    total = 0
    for pdf in attachments or []:
        try:
            data = pdf.read_bytes()
        except Exception as exc:
            _log(f"[email] could not read attachment {pdf.name}: {exc}")
            continue
        if total + len(data) > MAX_ATTACHMENT_TOTAL_BYTES:
            _log(f"[email] attachment cap reached ({MAX_ATTACHMENT_TOTAL_BYTES}B) "
                 f"-- skipping {pdf.name} ({len(data)}B)")
            continue
        msg.add_attachment(
            data,
            maintype="application",
            subtype="pdf",
            filename=pdf.name,
        )
        total += len(data)
        _log(f"[email] attached {pdf.name} ({len(data)}B)")

    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(email_from, app_pw)
            server.send_message(msg)
        _log(f"[email] sent -> {email_to}")
        return True
    except Exception as exc:
        _log(f"[email] send failed: {exc}")
        return False


def _select_items_default_mode(
    fetched: Dict[str, List[Dict]],
    tickers: List[str],
    hours_back: int,
    seen: Dict[str, str],
) -> List[Dict]:
    """Portfolio mode: everything in the last N hours that we haven't
    already surfaced."""
    out: List[Dict] = []
    for ticker in tickers:
        for item in fetched.get(ticker, []):
            if not _within_hours(item, hours_back):
                continue
            if _announcement_key(item) in seen:
                continue
            out.append(item)
    return out


def _select_items_results_mode(
    fetched: Dict[str, List[Dict]],
    tickers: List[str],
) -> List[Dict]:
    """Results-ticker mode: the ONE most recent RESULTS_HY_FY item per
    ticker, ignoring the 24h window (fetch was widened to
    RESULTS_LOOKBACK_DAYS instead). Ignores seen_state — this is the
    "someone at the pub asked about 5DD's last FY" path and should always
    surface a result even if it's already been emailed before."""
    out: List[Dict] = []
    for ticker in tickers:
        results = [
            it for it in fetched.get(ticker, [])
            if classify_sgx_announcement(it) == "RESULTS_HY_FY"
        ]
        if not results:
            _log(f"[results-mode] {ticker}: no results in last "
                 f"{RESULTS_LOOKBACK_DAYS} days")
            continue
        # Newest first by submission timestamp.
        results.sort(
            key=lambda it: it.get("submission_ts_ms") or 0, reverse=True,
        )
        top = results[0]
        _log(f"[results-mode] {ticker}: picked {top.get('ref_id')} "
             f"({top.get('date')} — {top.get('title')[:80]})")
        out.append(top)
    return out


def run(
    hours_back: int,
    dry_run: bool = False,
    results_tickers: Optional[List[str]] = None,
    results_hint: str = "",
) -> int:
    """Main pipeline. Two modes:

      1. Default (portfolio) — fetches the last `hours_back` hours for
         every ticker in tickers.yaml sgx:, dedupes via seen_state,
         classifies, deep-analyses results items, emails.
      2. Results-ticker (`results_tickers` non-empty) — fetches the last
         RESULTS_LOOKBACK_DAYS for those tickers, picks the ONE most
         recent RESULTS_HY_FY item per ticker, runs deep analysis, emails.
         Ignores seen_state entirely (this is the "get me 5DD's last FY"
         path — must always surface a result).

    `results_hint` is passed to every deep-analysis LLM call for this
    run. Use it to tell Bob what to weight when the report itself doesn't
    make the shape of the business obvious -- e.g. "Haw Par's main asset
    is its UOB stake, not Tiger Balm trading". Empty string = no hint."""
    is_results_mode = bool(results_tickers)
    _log(f"{BOB_SG_NAME} {BOB_SG_VERSION} -- "
         f"mode={'results-ticker' if is_results_mode else 'portfolio'} "
         f"hours_back={hours_back} dry_run={dry_run} "
         f"hint={'yes' if results_hint.strip() else 'no'}")

    if is_results_mode:
        tickers = [t.strip().upper() for t in results_tickers if t and t.strip()]
        lookback_days = RESULTS_LOOKBACK_DAYS
        _log(f"[main] results-ticker mode: {len(tickers)} ticker(s): "
             f"{', '.join(tickers)} — lookback {lookback_days} days")
    else:
        tickers = _load_sgx_tickers(Path("tickers.yaml"))
        if not tickers:
            _log("[main] no SGX tickers in tickers.yaml -- exiting")
            return 1
        _log(f"[main] portfolio mode: {len(tickers)} ticker(s): "
             f"{', '.join(tickers)}")

    # 1. Fetch — wider window in results-ticker mode.
    if is_results_mode:
        from_date = (_sgt_now() - dt.timedelta(days=RESULTS_LOOKBACK_DAYS)).date()
    else:
        from_date = (_sgt_now() - dt.timedelta(hours=hours_back)).date()
    fetched: Dict[str, List[Dict]] = fetch_sgx_announcements(
        tickers, from_date=from_date, log=_log,
    )

    # 2+3. Select the items to process (mode-specific).
    seen = (
        {} if is_results_mode
        else _prune_seen_state(_load_seen_state(SEEN_STATE_PATH),
                               SEEN_STATE_RETENTION_HOURS)
    )
    now_iso = _sgt_now().isoformat(timespec="seconds")
    if is_results_mode:
        items_to_process = _select_items_results_mode(fetched, tickers)
    else:
        items_to_process = _select_items_default_mode(
            fetched, tickers, hours_back, seen,
        )
    _log(f"[main] {len(items_to_process)} item(s) to process")

    # 4. Classify + 5. Deep-analyse results items.
    # The temp dir must outlive the email send -- attachments are read
    # from disk right before send. Manage it with an ExitStack so the
    # dry-run path can bail early without leaking, and the send path
    # keeps files alive until SMTP hands off.
    import contextlib
    classified: List[Tuple[Dict, str, Optional[Dict]]] = []
    counters = {"llm_calls": 0, "pdfs_downloaded": 0}
    # Attachments to the outbound email -- the analysis PDF(s) we render
    # ourselves. Source PDFs from SGX are LINKED in the body (via
    # item['_pdf_sources']), not attached, to keep the email lightweight.
    email_pdfs: List[Path] = []

    with contextlib.ExitStack() as stack:
        pdf_root_path = Path(stack.enter_context(
            tempfile.TemporaryDirectory(prefix="sgx_pdfs_")
        ))
        for item in items_to_process:
            bucket = classify_sgx_announcement(item)
            analysis: Optional[Dict] = None

            if bucket == "RESULTS_HY_FY":
                ticker = item.get("ticker") or "UNK"
                item_dir = pdf_root_path / (item.get("ref_id") or ticker)
                # SGX bundles all report + presentation + press release
                # PDFs under one landing page. Grab them all (up to the
                # per-announcement cap) and pass them together to Claude.
                fetched: List[FetchedPdf] = fetch_announcement_pdfs(
                    item.get("url") or "",
                    item_dir,
                    max_pdfs=MAX_PDFS_PER_ANNOUNCEMENT,
                    log=_log,
                )
                counters["pdfs_downloaded"] += len(fetched)
                _log(f"[main] {ticker}: got {len(fetched)} PDF(s) from landing page")
                # Stash (source_url, name) tuples on the item so the
                # email card renders them as clickable "Source PDFs".
                item["_pdf_sources"] = [(f.source_url, f.name) for f in fetched]

                if counters["pdfs_downloaded"] > MAX_PDFS_PER_RUN:
                    _log(f"[main] PDF cap reached -- skipping analysis for {ticker}")
                elif fetched:
                    analysis = _run_results_analysis(
                        item, [f.path for f in fetched], counters,
                        results_hint=results_hint,
                    )

                # Render the standalone analysis PDF and attach it. Only
                # when we have a parsed analysis dict -- a token-cap or
                # API-fail result would render an empty page.
                if analysis:
                    pdf_html = build_analysis_pdf_html(item, analysis)
                    period = (analysis.get("period") or "results").replace(" ", "")
                    safe_period = re.sub(r"[^A-Za-z0-9._-]+", "", period) or "results"
                    analysis_pdf = (
                        pdf_root_path / f"{ticker}_{safe_period}_analysis.pdf"
                    )
                    if _render_analysis_pdf(pdf_html, analysis_pdf):
                        email_pdfs.append(analysis_pdf)

            classified.append((item, bucket, analysis))

        # 6. Build email.
        model_label = os.environ.get("CLAUDE_MODEL") or CLAUDE_MODEL_DEFAULT
        subject, body_text, body_html = build_email(
            classified,
            hours_back=hours_back if not is_results_mode else RESULTS_LOOKBACK_DAYS * 24,
            model_label=model_label,
        )
        # In results-ticker mode, prefix the subject so it's obvious this
        # isn't the scheduled portfolio digest.
        if is_results_mode:
            subject = f"[RESULTS] {subject} — {','.join(tickers)}"

        _log(f"[main] classified: "
             f"{sum(1 for _, b, _ in classified if b == 'RESULTS_HY_FY')} results, "
             f"{sum(1 for _, b, _ in classified if b != 'OTHER' and b != 'RESULTS_HY_FY')} material, "
             f"{sum(1 for _, b, _ in classified if b == 'OTHER')} fyi")
        _log(f"[main] LLM calls: {counters['llm_calls']}/{MAX_LLM_CALLS_PER_RUN}, "
             f"source PDFs downloaded: {counters['pdfs_downloaded']}/{MAX_PDFS_PER_RUN}, "
             f"analysis PDFs attached: {len(email_pdfs)}")

        if dry_run:
            out_dir = Path("outputs/sgx")
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "digest_preview.html").write_text(body_html, encoding="utf-8")
            (out_dir / "digest_preview.txt").write_text(body_text, encoding="utf-8")
            _log(f"[main] DRY RUN -- wrote preview to {out_dir}/digest_preview.*")
            return 0

        # Results-ticker mode with zero items is a failure (usually a token-
        # prime flake). Don't send an empty "no results" digest -- the user
        # asked for a specific report; if we couldn't find it, they should
        # see the failure in the workflow log instead of a misleading empty
        # email that looks like the report doesn't exist.
        if is_results_mode and not classified:
            _log("[main] results-ticker mode with 0 items -- NOT sending email "
                 "(likely a token-prime failure or the ticker has no results "
                 "in the lookback window). Re-run the dispatch to retry.")
            return 3

        _send_email(subject, body_text, body_html, attachments=email_pdfs)

    # 7. Save seen state — portfolio mode only. Results-ticker mode is
    # explicitly one-off (mirrors ASX Bob's RESULTS_TICKER contract).
    if not is_results_mode:
        for item in items_to_process:
            seen[_announcement_key(item)] = now_iso
        _save_seen_state(SEEN_STATE_PATH, seen)
        _log(f"[main] seen_state updated ({len(seen)} entries)")
    else:
        _log("[main] results-ticker mode: seen_state NOT touched (one-off)")
    return 0


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Bob SG daily digest orchestrator.")
    parser.add_argument(
        "--hours-back", type=int, default=HOURS_BACK_DEFAULT,
        help=f"Portfolio-mode window in hours (default {HOURS_BACK_DEFAULT}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Build the digest but don't send email or update seen state.",
    )
    parser.add_argument(
        "--results-ticker", default=None,
        help="Comma-separated SGX codes to pull the LAST HY/FY results for "
             f"(looks back {RESULTS_LOOKBACK_DAYS} days). Overrides the "
             "portfolio; skips seen_state. Example: --results-ticker 5DD",
    )
    parser.add_argument(
        "--results-hint", default="",
        help="Free-form context appended to every deep-analysis LLM call "
             "for this run. Use it to steer the model on what matters vs. "
             "what's noise (e.g. \"Haw Par's main asset is its UOB stake, "
             "not Tiger Balm trading\").",
    )
    args = parser.parse_args()

    results_tickers: Optional[List[str]] = None
    if args.results_ticker:
        results_tickers = [
            t.strip().upper() for t in args.results_ticker.split(",")
            if t.strip()
        ]

    try:
        return run(
            hours_back=args.hours_back,
            dry_run=args.dry_run,
            results_tickers=results_tickers,
            results_hint=args.results_hint or "",
        )
    except Exception as exc:
        _log(f"[main] FATAL {exc.__class__.__name__}: {exc}")
        _log(traceback.format_exc())
        return 2


if __name__ == "__main__":
    sys.exit(_cli())
