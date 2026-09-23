"""
sgx_agent.py -- Bob SG orchestrator (V4 Round 2).

Ties Round 1's fetch layer (sgx_fetch.py) to the classifier, PDF fetcher,
LLM analysis (via shared/pdf_llm), and email sender. Runs standalone on
the self-hosted Windows runner. Does NOT import agent.py or the ASX Bob
path (which drags weasyprint / pypdf / bs4 imports the SGX box may not
need for anything else). The Claude transport and SMTP send come from
shared/llm.py and shared/email_service.py instead.

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
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from sgx_fetch import fetch_sgx_announcements
from sgx_classify import classify_sgx_announcement
from sgx_pdf import fetch_announcement_pdfs, FetchedPdf
from sgx_email import build_email, build_analysis_pdf_html, BOB_SG_NAME, BOB_SG_VERSION
from shared.email_service import send_email as shared_send_email
from shared.llm import STREAMING_MIN_TOKENS, make_client, send as llm_send
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
_STREAMING_MIN_TOKENS = STREAMING_MIN_TOKENS   # shared/llm.py owns the threshold

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

def _sanitise_json_string_bodies(text: str) -> str:
    """Escape raw control chars / invalid escapes inside JSON string
    bodies. Ported from agent.py -- covers the "raw \\n in a markdown
    blob" and "\\%" invalid-escape failure modes that show up on any
    long full_analysis. Does NOT fix unescaped inner double-quotes;
    _extract_full_analysis_raw handles that."""
    out: List[str] = []
    in_string = False
    escaped = False
    control_map = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
    for ch in text:
        if escaped:
            if ch in '"\\/bfnrtu':
                out.append(ch)
            else:
                out.pop()
                out.append(ch)
            escaped = False
            continue
        if in_string and ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch in control_map:
            out.append(control_map[ch])
            continue
        if in_string and ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
            continue
        out.append(ch)
    return "".join(out)


def _extract_full_analysis_raw(text: str) -> Optional[Dict]:
    """Last-ditch parse for the "unescaped inner quotes in full_analysis"
    case. C07 hit this: the model wrote a long markdown blob containing
    a quoted phrase like `"impairment of X, Y and Z."` inside the
    `full_analysis` value without escaping the inner quotes, so
    json.loads bailed on the whole object.

    Strategy: cut `full_analysis` out of the input entirely, parse the
    remaining JSON (which is small, well-behaved metrics + summary),
    then re-attach the raw markdown body as `full_analysis`. Quotes,
    backslashes, whatever the model wrote -- all preserved because
    that value never has to be JSON-escaped."""
    # Locate `, "full_analysis": "` (or just `"full_analysis": "` if
    # it's the first field, though the prompt puts it last).
    key_re = re.compile(
        r'\s*,\s*"full_analysis"\s*:\s*"',
        re.DOTALL,
    )
    m = key_re.search(text)
    if not m:
        # Try without a leading comma -- covers the case where the
        # model reordered fields and full_analysis is first.
        key_re = re.compile(r'"full_analysis"\s*:\s*"', re.DOTALL)
        m = key_re.search(text)
        if not m:
            return None
        # Rare enough that we bail rather than reconstruct.
        return None

    body_start = m.end()
    # The body ends at the last `"` immediately preceding the closing
    # `}` of the outer object (optionally with whitespace between).
    tail_re = re.compile(r'"\s*}\s*\Z', re.DOTALL)
    tail_m = tail_re.search(text[body_start:])
    if not tail_m:
        return None
    markdown_body = text[body_start:body_start + tail_m.start()]

    # Everything before `,\s*"full_analysis":` is the header JSON.
    # Close it with `}` and parse.
    header = text[:m.start()] + "}"
    try:
        parsed = json.loads(header)
    except Exception:
        # Header might still have string-body defects; try sanitising.
        try:
            parsed = json.loads(_sanitise_json_string_bodies(header))
        except Exception:
            return None
    if not isinstance(parsed, dict):
        return None
    parsed["full_analysis"] = markdown_body
    return parsed


def _parse_analysis_json(text: str) -> Optional[Dict]:
    """Parse structured JSON from the LLM response.

    Tries progressively harder, cheapest first, and every step
    preserves the model's content exactly (no bracket-balancing --
    truncation stays a parse_error rather than becoming a
    plausible-looking cutoff card):

      1. straight parse (after stripping code fences)
      2. outermost `{...}` span, ignoring prose either side
      3. that span with string bodies sanitised (control chars, bad
         escapes -- the newline / \\% cases Bob's ASX path hit)
      4. carve `full_analysis` out as raw text and re-parse the header
         (the unescaped-inner-quote case C07 hit)
    """
    if not text or text in (LLM_SKIPPED, LLM_FAILED):
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```\s*$", "", cleaned.strip(), flags=re.MULTILINE).strip()

    def _load(candidate: str) -> Optional[Dict]:
        try:
            result = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            return None
        return result if isinstance(result, dict) else None

    parsed = _load(cleaned)
    if parsed is not None:
        return parsed

    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    span = m.group()

    parsed = _load(span)
    if parsed is not None:
        return parsed

    parsed = _load(_sanitise_json_string_bodies(span))
    if parsed is not None:
        _log("[parse] recovered JSON after sanitising string bodies")
        return parsed

    parsed = _extract_full_analysis_raw(span)
    if parsed is not None:
        _log("[parse] recovered JSON by extracting full_analysis raw "
             "(unescaped inner quotes)")
        return parsed

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

    try:
        resp = llm_send(
            make_client(api_key),
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
            streaming_min_tokens=_STREAMING_MIN_TOKENS,
        )
        counters["last_stop_reason"] = resp.stop_reason or ""
        if resp.truncated:
            _log(f"[llm] WARNING truncated at max_tokens={max_tokens}")
        return resp.text.strip()
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
        # Hint goes at the top so the model reads it before the schema
        # boilerplate and treats the whole analysis through that lens.
        # A hint appended at the end tends to get skimmed after the
        # model has already committed to the shape of the response --
        # which is exactly what "the analysis was crap on C07" was.
        hint_block = (
            "IMPORTANT CONTEXT for this specific company (read first, "
            "weight heavily throughout your analysis -- especially in "
            "the full_analysis section):\n"
            f"{results_hint.strip()}\n\n"
        )

    # Slinger-side instructions layered on top of the shared
    # RESULTS_HYFY_PROMPT. The shared prompt calls the full_analysis
    # section "goes to a Google Doc, not the email" -- that's true for
    # ASX Bob but wrong for Slinger, where full_analysis IS the file
    # the user forwards to peers. Restate the expectation so the model
    # writes to that audience and depth. Also flag SGX-specific shapes
    # (many holding companies / cross-listed stakes) so the model
    # doesn't render a pure-P&L card for a NAV-driven name.
    slinger_notes = (
        "You are writing as the Singapore Slinger -- SGX-focused, "
        "buyside-forensic, sceptical. Additional expectations on top "
        "of the shared schema:\n"
        "- The `full_analysis` markdown is attached to the recipient's "
        "email as a standalone PDF that they forward to sophisticated "
        "peers. Write it to that bar. Substance over template: real "
        "numbers, real segment splits, real balance-sheet lines, not "
        "corporate boilerplate. Every claim carries a figure or "
        "explicit page-reference to the source. Aim for 800-1500 "
        "words of actual analysis, not filler.\n"
        "- SGX has many listed investment / holding companies (JC&C, "
        "Haw Par, Jardine Matheson, F&N, UOL, etc.) where reported "
        "P&L understates the real story -- underlying stake value, "
        "NAV per share vs price, see-through earnings and dividend "
        "flow-through from associates matter more than headline "
        "revenue. If the source or the user-supplied context above "
        "indicates the issuer is a holding company or has material "
        "associate/JV stakes, weight NAV / associate contribution / "
        "sum-of-parts in your analysis and say so explicitly in the "
        "summary and the Bottom line.\n"
        "- The issuer reports in Singapore dollars (SGD) by default. "
        "Set `currency` to what the report actually states (SGD, USD, "
        "IDR are all common on SGX) and prefix values with the "
        "explicit currency: S$, US$, Rp. Never leave a figure with a "
        "bare $ prefix -- readers will misread it as USD.\n"
    )

    user = (
        f"{hint_block}"
        f"Ticker: {ticker}\n"
        f"Issuer: {issuer}\n"
        f"Announcement title: {title}\n\n"
        f"The attached PDF(s) are this issuer's official results "
        f"release, financial statements and (sometimes) press release "
        f"or investor deck for this reporting period.\n\n"
        f"{slinger_notes}\n"
        f"Return your response as strict JSON per the system-prompt "
        f"schema. No preamble, no markdown fences, no text outside the "
        f"JSON object."
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
    """Send the digest with PDFs attached (capped at MAX_ATTACHMENT_TOTAL_BYTES).
    Never raises: a missing EMAIL_* env var or SMTP failure logs and returns False."""
    return shared_send_email(
        subject,
        body_text,
        body_html,
        attachments=attachments,
        force_type=("application", "pdf"),
        raise_on_error=False,
        max_total_bytes=MAX_ATTACHMENT_TOTAL_BYTES,
        log=_log,
        log_prefix="[email]",
    )

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

        # 7a. Dashboard JSON. Written for every real run (portfolio and
        # results-ticker) so a one-off query shows up on the site until
        # the next scheduled portfolio run replaces it. This is a
        # deliberate departure from Bob's "one-off never touches the
        # dashboard" rule -- ASX Bob has a catch-up cron the dashboard
        # write would trip; Slinger doesn't.
        _write_dashboard_json(classified)

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


# --- dashboard JSON ---------------------------------------------------------

# Shape mirrors docs/data/bob.json so scripts/build_dashboard.py can reuse
# the same rendering helpers (_hi_item_card / _mat_item_row / etc). The
# results items carry an `analysis` sub-dict with the same metric-key
# schema (`dividend_ordinary`, `change_pct`), so Bob's `_flatten_metrics`
# just works. `type` on high-impact items follows Bob's _BADGE_COLOURS
# keys.
SLINGER_DASHBOARD_JSON = Path(__file__).resolve().parent / "docs" / "data" / "slinger.json"


_BUCKET_TO_TYPE = {
    "RESULTS_HY_FY": "results",
    "ACQUISITION": "acquisition",
    "CAPITAL_OR_DEBT_RAISE": "capital",
    "TRADING_UPDATE": "trading_update",
}


def _dashboard_item(item: Dict, bucket: str, analysis: Optional[Dict]) -> Dict:
    out: Dict = {
        "ticker": item.get("ticker") or "",
        "title": item.get("title") or "",
        "url": item.get("url") or "",
    }
    if bucket in _BUCKET_TO_TYPE:
        out["type"] = _BUCKET_TO_TYPE[bucket]
    if item.get("issuer_name"):
        out["issuer_name"] = item["issuer_name"]
    pdf_sources = item.get("_pdf_sources") or []
    if pdf_sources:
        out["source_pdfs"] = [{"url": u, "name": n} for u, n in pdf_sources]
    if analysis:
        # Drop the raw model text if any -- it belongs in the attached
        # PDF, not the dashboard JSON.
        clean = {k: v for k, v in analysis.items() if k != "_raw_text"}
        out["analysis"] = clean
    return out


def _write_dashboard_json(
    classified: List[Tuple[Dict, str, Optional[Dict]]],
) -> None:
    """Write docs/data/slinger.json in the same shape as bob.json.

    Called after a successful email send. The Publish site workflow
    picks this up via `workflow_run` and republishes the combined
    dashboard. Silent no-op if we can't write for any reason -- the
    email already went out, no need to fail the run over the dashboard."""
    high_impact: List[Dict] = []
    material: List[Dict] = []
    fyi: List[Dict] = []
    for item, bucket, analysis in classified:
        row = _dashboard_item(item, bucket, analysis)
        if bucket == "RESULTS_HY_FY" or bucket == "ACQUISITION" \
                or bucket == "CAPITAL_OR_DEBT_RAISE" \
                or bucket == "TRADING_UPDATE":
            high_impact.append(row)
        elif bucket in ("DIVIDEND", "SHARE_BUYBACK", "CONTRACT_MATERIAL"):
            material.append(row)
        else:
            fyi.append(row)

    data = {
        "last_run": _sgt_now().date().isoformat(),
        "silence": False,
        "high_impact": high_impact,
        "material": material,
        "fyi": fyi,
    }
    try:
        SLINGER_DASHBOARD_JSON.parent.mkdir(parents=True, exist_ok=True)
        SLINGER_DASHBOARD_JSON.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _log(f"[main] dashboard JSON written -> {SLINGER_DASHBOARD_JSON}")
    except Exception as exc:
        _log(f"[main] dashboard JSON write failed: {exc.__class__.__name__}: {exc}")


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Singapore Slinger daily digest orchestrator.")
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
