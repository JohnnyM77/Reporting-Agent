"""
us_agent.py -- Bob USA orchestrator (V1).

Mirrors sgx_agent.py but sources filings from SEC EDGAR instead of
SGX. Never imports agent.py, so a change to Bob's ASX path can't
accidentally break this one. The Claude transport and SMTP send come
from shared/llm.py and shared/email_service.py.

Pipeline
--------
    1. Resolve ticker -> CIK (us_fetch)
    2. Fetch recent submissions from data.sec.gov (us_fetch)
    3. Select the "last results" set: newest 10-K/10-Q + matching
       earnings 8-K (item 2.02), if present (us_fetch.select_last_results_set)
    4. Download every relevant document and render to PDF (us_docs)
    5. Anthropic streaming call with native PDF blocks + RESULTS_HYFY_PROMPT_US
    6. Parse structured JSON (metrics + summary + full_analysis)
    7. Render deep analysis to a standalone PDF (reuse sgx_email.build_analysis_pdf_html
       via a thin wrapper -- see build_us_analysis_pdf_html)
    8. Build email body + send via SMTP with the analysis PDF attached
    9. Write docs/data/us.json for the dashboard

CLI
---
    python us_agent.py --ticker MSFT [--dry-run]
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

from us_fetch import fetch_us_filings, select_last_results_set
from us_classify import classify_us_filing, period_type_hint
from us_docs import fetch_filing_documents
from sgx_pdf import FetchedPdf   # shape shared across exchanges
from shared.email_service import send_email as shared_send_email
from shared.llm import STREAMING_MIN_TOKENS, make_client, send as llm_send
from shared.pdf_llm import (
    LLM_FAILED,
    LLM_SKIPPED,
    PdfAttachment,
    build_pdf_attachments,
)

try:
    from prompts import RESULTS_HYFY_PROMPT_US
except Exception:  # pragma: no cover -- prompts.py import break is the only recoverable case
    RESULTS_HYFY_PROMPT_US = ""


# --- constants -------------------------------------------------------------

BOB_US_NAME = "Bob USA"
BOB_US_VERSION = "V1"

# US market timezone -- used for the "generated" timestamp on the report
# and for the daily-run date on the dashboard JSON.
NY = dt.timezone(dt.timedelta(hours=-5))   # EST; DST close enough for date labels

CLAUDE_MODEL_DEFAULT = "claude-sonnet-4-5-20250929"
CLAUDE_RESULTS_MAX_TOKENS = int(os.environ.get("CLAUDE_RESULTS_MAX_TOKENS", "50000"))
_STREAMING_MIN_TOKENS = STREAMING_MIN_TOKENS   # shared/llm.py owns the threshold

# EDGAR window. 400 days = safely more than a full year, so a company that
# reported 11 months ago is still visible even if we haven't been
# analysing them regularly.
DEFAULT_LOOKBACK_DAYS = int(os.environ.get("US_LOOKBACK_DAYS", "400"))

# Whole-run call cap; EDGAR-driven dispatch is one ticker per run so this
# is really a belt-and-braces guard against a runaway retry loop.
MAX_LLM_CALLS_PER_RUN = int(os.environ.get("US_MAX_LLM_CALLS", "5"))
MAX_PDFS_PER_RUN = int(os.environ.get("US_MAX_PDFS", "6"))

# Gmail rejects attachments totalling ~25MB. Same cap the SGX path uses.
MAX_ATTACHMENT_TOTAL_BYTES = 20 * 1024 * 1024

# Where the dashboard reads Bob USA's data from.
US_DASHBOARD_JSON = Path(__file__).resolve().parent / "docs" / "data" / "us.json"

# Seen-state cache: accession -> ISO timestamp. Same pattern as Bob's
# state_seen.json / Slinger's state_seen_sgx.json — dedups tickers whose
# most-recent filing hasn't changed since the last run so the daily
# morning schedule doesn't re-email the same 10-K each morning.
US_SEEN_STATE_PATH = Path(
    os.environ.get("US_SEEN_STATE_PATH", "state_seen_us.json")
)
# 400 days matches DEFAULT_LOOKBACK_DAYS below: a filing older than that
# won't come back through fetch_us_filings anyway, so pruning at the
# same horizon keeps the state file small without ever losing something
# the fetch could rediscover.
US_SEEN_STATE_RETENTION_DAYS = 400

# tickers.yaml lives next to this file — same working-directory assumption
# the rest of the repo makes.
TICKERS_YAML_PATH = Path(__file__).resolve().parent / "tickers.yaml"


# --- utils -----------------------------------------------------------------

def _log(msg: str) -> None:
    print(msg, flush=True)


def _ny_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(NY)


# --- LLM -------------------------------------------------------------------
# The parser is a direct port of sgx_agent's _parse_analysis_json, chosen
# for the same reasons: a truncated / control-char / unescaped-inner-quote
# response would otherwise fail the whole run, and each fallback preserves
# the model's content byte-for-byte. Kept here rather than shared because
# a shared "parse-Claude-JSON" utility is a churn magnet -- the moment
# it lives in shared/ every prompt evolution risks breaking another
# caller. Copy is cheap.

def _sanitise_json_string_bodies(text: str) -> str:
    """Escape raw control chars and invalid escapes inside JSON string
    bodies -- covers the raw-\\n-in-markdown-blob failure mode Bob's ASX
    path first hit. Ported unchanged from sgx_agent.py."""
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
    """Last-ditch parse: carve `full_analysis` out as raw text so
    unescaped inner quotes in the markdown body never break json.loads.
    Ported from sgx_agent.py; same rationale."""
    key_re = re.compile(r'\s*,\s*"full_analysis"\s*:\s*"', re.DOTALL)
    m = key_re.search(text)
    if not m:
        key_re = re.compile(r'"full_analysis"\s*:\s*"', re.DOTALL)
        m = key_re.search(text)
        if not m:
            return None
        return None
    body_start = m.end()
    tail_re = re.compile(r'"\s*}\s*\Z', re.DOTALL)
    tail_m = tail_re.search(text[body_start:])
    if not tail_m:
        return None
    markdown_body = text[body_start:body_start + tail_m.start()]
    header = text[:m.start()] + "}"
    try:
        parsed = json.loads(header)
    except Exception:
        try:
            parsed = json.loads(_sanitise_json_string_bodies(header))
        except Exception:
            return None
    if not isinstance(parsed, dict):
        return None
    parsed["full_analysis"] = markdown_body
    return parsed


def _parse_analysis_json(text: str) -> Optional[Dict]:
    """Progressive-fallback parse -- straight parse, then span, then
    sanitised span, then raw-markdown carve. No bracket-closing fallback
    (truncation stays a parse_error, same rule Bob's ASX path enforces)."""
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
    """Call Claude with native PDF attachments and return the raw text
    response. The API call itself (client, streaming for large max_tokens,
    text/stop_reason extraction) goes through shared/llm.py; the call cap,
    content assembly and sentinels are Bob USA's own."""
    if counters["llm_calls"] >= MAX_LLM_CALLS_PER_RUN:
        _log(f"[llm] cap reached ({MAX_LLM_CALLS_PER_RUN}) -- skipping")
        return LLM_SKIPPED

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        _log("[llm] ANTHROPIC_API_KEY not set")
        return LLM_FAILED

    attachments: List[PdfAttachment] = []
    for p in pdf_paths:
        try:
            data = p.read_bytes()
        except Exception as exc:
            _log(f"[llm] could not read {p.name}: {exc}")
            continue
        attachments.append(PdfAttachment(name=p.name, pdf_bytes=data))
    # A 10-K / 20-F body routinely blows past Claude's 100-page native
    # limit and falls back to pypdf-extracted text. Bump the per-doc text
    # cap way above the shared 60k default -- Claude Sonnet's 200k context
    # window comfortably fits ~250k chars of extracted body alongside the
    # smaller native exhibits, and it is the only way the model sees the
    # income statement / balance sheet / MD&A on a long annual report.
    batch = build_pdf_attachments(
        attachments, log=_log, fallback_text_limit=250_000,
    )
    # Compose the user message. THIS BLOCK MUST ALWAYS INCLUDE THE
    # FALLBACK TEXT WHEN IT EXISTS -- the previous version dropped
    # `batch.fallback_sections` whenever any_native was True, which is
    # exactly the RMD FY26 failure: the 215-page 10-K body fell back to
    # text and then that text was silently discarded because the tiny
    # cert exhibits attached natively. Only the certs reached the model,
    # so the analysis had zero financial content to work with.
    content_parts: List[object] = []
    if batch.any_native:
        content_parts.extend(batch.document_blocks)
    if batch.fallback_sections:
        # Keep the extracted-text bodies together in one text block so
        # the model sees them as a coherent source rather than
        # interleaved with the schema instructions. 250k char ceiling
        # matches the per-doc text cap above and stays well inside
        # Sonnet's 200k-token window.
        fallback_blob = "\n\n".join(batch.fallback_sections)
        content_parts.append({"type": "text", "text": fallback_blob[:250_000]})
    if content_parts:
        content_parts.append({"type": "text", "text": user_prompt[:50_000]})
        content: object = content_parts
    else:
        # No usable attachments at all -- straight text prompt.
        content = user_prompt[:100_000]

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


# --- analysis PDF rendering ------------------------------------------------

def _render_analysis_pdf(html: str, out_path: Path) -> bool:
    """Print `html` to `out_path` via Playwright's bundled Chromium.
    ubuntu-latest already has this available -- unlike the SGX path which
    needs installed Chrome (`channel='chrome'`) for its SGX-fingerprint
    reasons, EDGAR imposes no fingerprint constraint on rendering."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _log(f"[analysis-pdf] playwright not available: {exc}")
        return False
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
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


def build_us_analysis_pdf_html(item: Dict, analysis: Dict) -> str:
    """Thin wrapper over sgx_email.build_analysis_pdf_html.

    That function's page skeleton (cover header, metric table, summary
    callout, deep-analysis markdown, footer) is exactly what a US
    forwardable report needs -- the only piece that mentions SGX is the
    small "Singapore Slinger" line in the cover header, which we can
    swap post-hoc rather than fork the whole renderer.

    Passes through unchanged: metrics, summary, full_analysis, currency
    (Bob USA reports USD; the sgx renderer reads currency from
    analysis['currency'] and does the right thing already)."""
    from sgx_email import build_analysis_pdf_html

    html = build_analysis_pdf_html(item, analysis)
    # Replace SGX-specific header lettering with the US bot label.
    html = html.replace(
        "Singapore Slinger &middot; SGX Results Analysis",
        f"{BOB_US_NAME} &middot; SEC EDGAR Results Analysis",
    )
    # Replace the SGX footer note about links.sgx.com.
    html = html.replace(
        "Prepared by Singapore Slinger, an automated SGX results "
        "analyst. Not investment advice. Verify figures against the "
        "source filing on links.sgx.com before acting on any number "
        "in this document.",
        f"Prepared by {BOB_US_NAME}, an automated SEC EDGAR results "
        "analyst. Not investment advice. Verify figures against the "
        "source filing on sec.gov before acting on any number in this "
        "document.",
    )
    # Also normalise the "reported in S$ (SGD)" caption if it appears
    # (build_analysis_pdf_html hardcodes it in the cover subline). It's
    # only used when currency defaults to SGD -- for US filings the
    # currency label is read from analysis, so this is defensive.
    return html


# --- deep results analysis -------------------------------------------------

def _run_results_analysis(
    item: Dict,
    pdf_paths: List[Path],
    counters: Dict,
    anchor_form: Optional[str],
    report_date: Optional[str],
) -> Optional[Dict]:
    """Run the deep analysis for the selected filing set. Returns the
    parsed JSON dict, or None if no analysis could be produced. On a
    parse failure we still return a dict so the caller renders a
    "raw output preserved" card rather than silently dropping it."""
    ticker = item.get("ticker") or ""
    issuer = item.get("issuer_name") or ""
    title = item.get("title") or ""
    form = anchor_form or item.get("form") or ""

    if not RESULTS_HYFY_PROMPT_US:
        _log("[results] RESULTS_HYFY_PROMPT_US unavailable -- skipping deep analysis")
        return None
    if not pdf_paths:
        _log(f"[results] {ticker}: no PDFs -- skipping deep analysis")
        return None

    # Bob USA-specific instructions layered on top of the shared US
    # prompt. Same pattern sgx_agent uses for Slinger -- the shared
    # RESULTS_HYFY_PROMPT_US calls full_analysis "attached to the email
    # as a standalone PDF the user forwards", so we restate the
    # expectation and give the model the fiscal-year hint up front.
    us_notes = (
        "You are writing as Bob USA -- a US-listing-focused, "
        "buyside-forensic analyst. The `full_analysis` markdown is "
        "attached to the recipient's email as a standalone PDF that "
        "they forward to sophisticated peers. Substance over template. "
        "Real numbers, real segment splits, real balance-sheet lines, "
        "not corporate boilerplate. Every claim carries a figure. Aim "
        "for 800-1500 words of actual analysis.\n"
        "- Fiscal year: US filers do not all use Dec 31. Read the "
        "filing's period-end date and the fiscal year label; don't "
        "assume calendar year. Microsoft ends June 30; Apple late "
        "September; Sea Ltd Dec 31.\n"
        "- GAAP vs non-GAAP: use the company's non-GAAP figure for "
        "NPAT and EPS when it is disclosed and defensible. Never pass "
        "GAAP off as adjusted, and if the non-GAAP adjustments are "
        "aggressive (adding back SBC that materially exceeds R&D "
        "spending, one-off exclusions that recur every quarter), say "
        "so plainly in the Non-GAAP framing section.\n"
        "- Currency: default USD. Some US-listed foreign issuers "
        "report in the underlying currency (Sea Ltd = USD, but many "
        "Chinese ADRs = RMB, European ADRs = EUR). Set `currency` to "
        "what the report actually states and prefix values "
        "accordingly (US$, RMB, EUR).\n"
    )

    user = (
        f"Ticker: {ticker}\n"
        f"Issuer: {issuer}\n"
        f"Filing form: {form}\n"
        f"Report period end date: {report_date or 'as stated in filing'}\n"
        f"Filing title: {title}\n\n"
        f"The attached PDF(s) are the SEC EDGAR filing(s) for this "
        f"issuer's most recently reported period -- typically the "
        f"10-K, 10-Q or 20-F body plus the paired earnings 8-K "
        f"press-release exhibit (Exhibit 99.1) where present.\n\n"
        f"{us_notes}\n"
        f"Return your response as strict JSON per the system-prompt "
        f"schema. No preamble, no markdown fences, no text outside the "
        f"JSON object."
    )
    text = _anthropic_call(pdf_paths, RESULTS_HYFY_PROMPT_US, user, counters)
    if text in (LLM_SKIPPED, LLM_FAILED):
        _log(f"[results] {ticker}: LLM returned {text}")
        return None

    parsed = _parse_analysis_json(text)
    if not parsed:
        _log(f"[results] {ticker}: JSON parse failed")
        # Same "raw model output preserved" pattern the SGX path uses.
        return {
            "summary": "Analysis failed: could not parse model output. "
                       "Raw text preserved below.",
            "period": "",
            "period_type": period_type_hint(item),
            "currency": "USD",
            "metrics": {},
            "full_analysis": (
                "## Raw model output (unparseable)\n\n"
                f"{text}"
            ),
            "_raw_text": text,
        }
    # Seed period_type if the model left it blank.
    if not parsed.get("period_type"):
        parsed["period_type"] = period_type_hint(item)
    return parsed


# --- email -----------------------------------------------------------------

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

def _build_email_body(
    item: Dict,
    analysis: Optional[Dict],
    source_pdfs: List[FetchedPdf],
) -> Tuple[str, str, str]:
    """Build the Bob USA email (subject, text body, html body).

    We reuse sgx_email's rendering primitives -- results card, metric
    rows, plain block -- so the visual language matches Slinger's and
    Bob's. The card mentions "US$"/"USD" and links to sec.gov docs
    (source_pdfs) so the recipient can open the originals."""
    from sgx_email import (
        _results_card_html,
        _plain_block,
        _two_liner_for_item,
        _section,
        _esc,
        BOB_SG_NAME,   # for the layout constants only; not user-visible
        COLOR_BG,
        COLOR_TEXT,
        COLOR_HIGH_IMPACT,
    )

    ticker = item.get("ticker") or ""
    today = _ny_now().date().isoformat()

    subject_bits = [BOB_US_NAME, BOB_US_VERSION, "SEC Results", ticker, today]
    subject = " -- ".join(b for b in subject_bits if b)

    # The card renderer reads item['_pdf_sources'] -- populate it with the
    # SEC docs before calling. Same convention Slinger uses.
    item_for_card = dict(item)
    item_for_card["_pdf_sources"] = [(f.source_url, f.name) for f in source_pdfs]

    if analysis:
        card_html = _results_card_html(item_for_card, analysis)
    else:
        card_html = _plain_block(_two_liner_for_item(item_for_card))

    # Text body -- plain fallback.
    lines: List[str] = []
    lines.append(f"{BOB_US_NAME} {BOB_US_VERSION}")
    lines.append("=" * len(BOB_US_NAME))
    lines.append(f"SEC Results Digest -- {ticker} -- {today}")
    lines.append("")
    lines.append(f"{ticker} -- {item.get('title') or ''}")
    lines.append(f"Form: {item.get('form') or ''}")
    lines.append(f"Accession: {item.get('accession') or ''}")
    lines.append(f"Filed: {item.get('date') or ''}")
    lines.append(f"Source: {item.get('url') or ''}")
    lines.append("")
    if analysis:
        lines.append(f"Summary: {(analysis.get('summary') or '').strip()}")
    if source_pdfs:
        lines.append("")
        lines.append("Source documents (sec.gov):")
        for f in source_pdfs:
            lines.append(f"  - {f.name}: {f.source_url}")
    lines.append("")
    lines.append("Full deep analysis is attached as a PDF.")
    body_text = "\n".join(lines)

    # HTML header -- same visual family as Slinger.
    header_html = (
        f"<div style='padding:18px; background:{COLOR_BG}; color:{COLOR_TEXT}; "
        f"font-family:-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, "
        f"Arial, sans-serif;'>"
        f"<div style='font-size:22px; font-weight:900; margin-bottom:6px;'>"
        f"{_esc(BOB_US_NAME)} "
        f"<span style='display:inline-block; margin-left:6px; padding:2px 8px; "
        f"background:{COLOR_HIGH_IMPACT}; color:#0B1220; font-size:12px; "
        f"border-radius:6px; vertical-align:middle;'>US</span> "
        f"<span style='opacity:0.7; font-weight:400; font-size:14px;'>"
        f"{_esc(BOB_US_VERSION)}</span>"
        f"</div>"
        f"<div style='opacity:0.9; font-size:14px; margin-bottom:10px;'>"
        f"SEC EDGAR Results Digest -- {_esc(ticker)} -- {_esc(today)}"
        f"</div>"
        f"</div>"
    )
    section_html = _section("HIGH IMPACT -- RESULTS", COLOR_HIGH_IMPACT, [card_html])
    body_html = header_html + section_html + "</div>"
    return subject, body_text, body_html


# --- dashboard JSON --------------------------------------------------------

def _dashboard_item(item: Dict, analysis: Optional[Dict],
                    source_pdfs: List[FetchedPdf]) -> Dict:
    """Shape mirrors slinger.json's high_impact entries so
    scripts/build_dashboard.py can reuse `_render_analysis_sections` and
    the results card rendering with no branching."""
    out: Dict = {
        "ticker": item.get("ticker") or "",
        "title": item.get("title") or "",
        "url": item.get("url") or "",
        "type": "results",
    }
    if item.get("issuer_name"):
        out["issuer_name"] = item["issuer_name"]
    if item.get("form"):
        out["form"] = item["form"]
    if item.get("accession"):
        out["accession"] = item["accession"]
    if source_pdfs:
        out["source_pdfs"] = [
            {"url": f.source_url, "name": f.name} for f in source_pdfs
        ]
    if analysis:
        clean = {k: v for k, v in analysis.items() if k != "_raw_text"}
        out["analysis"] = clean
    return out


def _write_dashboard_json(
    item: Dict,
    analysis: Optional[Dict],
    source_pdfs: List[FetchedPdf],
) -> None:
    """Write docs/data/us.json in the same shape as slinger.json/bob.json.

    In portfolio mode several tickers run back-to-back in the same
    morning: if we blindly overwrote us.json for each, only the last
    ticker would ever appear on the dashboard. So the write is now a
    MERGE — if the file already exists AND its ``last_run`` matches
    today (NY), we append this ticker's row to the same document
    instead. A same-ticker re-run replaces the earlier entry rather
    than duplicating it.

    Silent no-op on error -- the email already went out; don't fail
    the run over a dashboard write.
    """
    row = _dashboard_item(item, analysis, source_pdfs)
    today_iso = _ny_now().date().isoformat()
    ticker = row.get("ticker") or ""

    existing: Dict = {}
    try:
        raw = US_DASHBOARD_JSON.read_text(encoding="utf-8")
        existing = json.loads(raw) if raw.strip() else {}
    except FileNotFoundError:
        existing = {}
    except Exception as exc:
        _log(f"[main] existing us.json unreadable "
             f"({exc.__class__.__name__}: {exc}) -- starting fresh")
        existing = {}

    if not isinstance(existing, dict) or str(existing.get("last_run") or "") != today_iso:
        # A stale (yesterday's) file, or an empty/corrupt one: reset.
        high_impact: List[Dict] = []
        fyi: List[Dict] = []
        material: List[Dict] = []
    else:
        high_impact = [
            x for x in (existing.get("high_impact") or [])
            if isinstance(x, dict) and x.get("ticker") != ticker
        ]
        fyi = [
            x for x in (existing.get("fyi") or [])
            if isinstance(x, dict) and x.get("ticker") != ticker
        ]
        material = list(existing.get("material") or [])

    if analysis:
        high_impact.append(row)
    else:
        fyi.append(row)

    data = {
        "last_run": today_iso,
        "silence": not (high_impact or material or fyi),
        "high_impact": high_impact,
        "material": material,
        "fyi": fyi,
    }
    try:
        US_DASHBOARD_JSON.parent.mkdir(parents=True, exist_ok=True)
        US_DASHBOARD_JSON.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _log(f"[main] dashboard JSON written -> {US_DASHBOARD_JSON}")
    except Exception as exc:
        _log(f"[main] dashboard JSON write failed: "
             f"{exc.__class__.__name__}: {exc}")


# --- seen-state ------------------------------------------------------------
#
# Portfolio mode runs every morning. Without dedup, MSFT (whose most
# recent 10-K might be four months old) would be re-analysed and re-
# emailed every day, sending the same PDF over and over. State is a
# per-ticker accession set — an anchor filing whose accession we've
# already handled gets skipped.

def _load_us_seen_state(path: Path = US_SEEN_STATE_PATH) -> Dict[str, str]:
    """Return {accession_key: seen_iso}. Robust to legacy shapes
    (a plain list becomes {accession: today})."""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        _log(f"[state] could not read {path}: {exc}")
        return {}
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        _log(f"[state] could not parse {path}: {exc}")
        return {}
    now_iso = _ny_now().isoformat(timespec="seconds")
    if isinstance(data, list):
        return {k: now_iso for k in data if isinstance(k, str)}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and isinstance(v, str)
    }


def _prune_us_seen_state(
    state: Dict[str, str], retention_days: int = US_SEEN_STATE_RETENTION_DAYS,
) -> Dict[str, str]:
    cutoff = _ny_now() - dt.timedelta(days=retention_days)
    out: Dict[str, str] = {}
    for key, seen_iso in state.items():
        try:
            seen_dt = dt.datetime.fromisoformat(seen_iso)
        except Exception:
            continue
        if seen_dt.tzinfo is None:
            seen_dt = seen_dt.replace(tzinfo=NY)
        if seen_dt >= cutoff:
            out[key] = seen_iso
    return out


def _save_us_seen_state(
    state: Dict[str, str], path: Path = US_SEEN_STATE_PATH,
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as exc:
        _log(f"[state] could not write {path}: {exc}")


def _seen_key(ticker: str, accession: str) -> str:
    """Accession numbers can technically collide across issuers on
    EDGAR (they don't in practice but the CIK is the real key), so
    the seen key embeds the ticker too."""
    return f"{ticker}|{accession}"


# --- portfolio-mode helpers ------------------------------------------------

def _load_us_portfolio(path: Path = TICKERS_YAML_PATH) -> List[str]:
    """Return the sorted list of tickers under ``us:`` in tickers.yaml,
    or [] if the file/section is missing. Same helper us_fetch.py has
    inline; duplicated here so a portfolio run never has to import
    from us_fetch (which pulls in Playwright)."""
    try:
        import yaml  # PyYAML is already a transitive dep
    except Exception as exc:
        _log(f"[portfolio] PyYAML unavailable: {exc}")
        return []
    if not path.exists():
        _log(f"[portfolio] no tickers.yaml at {path}")
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        _log(f"[portfolio] could not parse {path}: {exc}")
        return []
    us = data.get("us") or {}
    if not isinstance(us, dict):
        return []
    return sorted(k for k in us.keys() if isinstance(k, str))


# --- main pipeline ---------------------------------------------------------

def run(
    ticker: str,
    dry_run: bool = False,
    seen_state: Optional[Dict[str, str]] = None,
    respect_seen: bool = True,
) -> int:
    """End-to-end pipeline for one US ticker. Returns 0 on success, non-
    zero on any hard failure (unresolvable ticker, no filings, etc.).

    *seen_state* is a mutable ``{accession_key: iso_seen}`` dict. When
    supplied, the anchor filing's accession is checked against it — an
    already-seen filing is skipped and the state is left unchanged. On
    a fresh anchor the ticker gets a full analysis + email + dashboard
    write, and this accession is added to the state so the next
    portfolio pass won't re-emit it. A single-ticker `--ticker` dispatch
    passes ``respect_seen=False`` so a manual re-fire always runs.
    """
    ticker = ticker.strip().upper()
    _log(f"{BOB_US_NAME} {BOB_US_VERSION} -- ticker={ticker} dry_run={dry_run}")

    # 1-2. Fetch filings.
    fetched = fetch_us_filings([ticker],
                                lookback_days=DEFAULT_LOOKBACK_DAYS,
                                log=_log)
    rows = fetched.get(ticker) or []
    if not rows:
        _log(f"[main] {ticker}: no filings in last {DEFAULT_LOOKBACK_DAYS} days")
        return 1

    # 3. Select the last-results set.
    selected, anchor_form, report_date = select_last_results_set(rows, log=_log)
    if not selected:
        _log(f"[main] {ticker}: no 10-K/10-Q/8-K item 2.02 found -- nothing to analyse")
        return 1

    anchor = selected[0]
    _log(f"[main] {ticker}: anchor {anchor.get('form')} "
         f"{anchor.get('accession')} filed {anchor.get('date')}")

    # Same-morning dedup: the daily portfolio run picks up the LATEST
    # filing per ticker; without this guard we'd re-analyse and re-email
    # the same 10-K every morning until a new one landed. Manual
    # single-ticker dispatches bypass this so a re-fire always works.
    accession = str(anchor.get("accession") or "")
    if respect_seen and seen_state is not None and accession:
        key = _seen_key(ticker, accession)
        if key in seen_state:
            _log(f"[main] {ticker}: anchor {accession} already emitted "
                 f"(seen {seen_state[key]}) -- skipping")
            return 0

    # Sanity-check the anchor really classifies as results.
    bucket = classify_us_filing(anchor)
    if bucket != "RESULTS_HY_FY":
        _log(f"[main] anchor classified as {bucket}, expected RESULTS_HY_FY -- aborting")
        return 1

    counters = {"llm_calls": 0, "pdfs_downloaded": 0}

    with tempfile.TemporaryDirectory(prefix="us_pdfs_") as tmp:
        pdf_root = Path(tmp)

        # 4. Download + render.
        source_pdfs = fetch_filing_documents(
            selected, pdf_root, max_pdfs=MAX_PDFS_PER_RUN, log=_log,
        )
        counters["pdfs_downloaded"] = len(source_pdfs)
        if not source_pdfs:
            _log(f"[main] {ticker}: no documents rendered -- aborting")
            return 1

        # 5. Anthropic call.
        analysis = _run_results_analysis(
            anchor,
            [f.path for f in source_pdfs],
            counters,
            anchor_form=anchor_form,
            report_date=report_date,
        )
        _log(f"[main] LLM calls: {counters['llm_calls']}/{MAX_LLM_CALLS_PER_RUN}, "
             f"source PDFs: {counters['pdfs_downloaded']}/{MAX_PDFS_PER_RUN}")

        # 6-7. Render deep-analysis PDF.
        analysis_pdf_path: Optional[Path] = None
        if analysis:
            pdf_html = build_us_analysis_pdf_html(anchor, analysis)
            period = (analysis.get("period") or "results").replace(" ", "")
            safe_period = re.sub(r"[^A-Za-z0-9._-]+", "", period) or "results"
            analysis_pdf_path = pdf_root / f"{ticker}_{safe_period}_analysis.pdf"
            if not _render_analysis_pdf(pdf_html, analysis_pdf_path):
                analysis_pdf_path = None

        # 8. Email.
        subject, body_text, body_html = _build_email_body(
            anchor, analysis, source_pdfs,
        )
        if dry_run:
            out_dir = Path("outputs/us")
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "digest_preview.html").write_text(body_html, encoding="utf-8")
            (out_dir / "digest_preview.txt").write_text(body_text, encoding="utf-8")
            if analysis:
                (out_dir / f"{ticker}_analysis.json").write_text(
                    json.dumps(analysis, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            if analysis_pdf_path and analysis_pdf_path.exists():
                # Copy the rendered PDF out of the tempdir so a dry-run
                # preview leaves something inspectable on disk.
                dest = out_dir / analysis_pdf_path.name
                dest.write_bytes(analysis_pdf_path.read_bytes())
                _log(f"[main] DRY RUN -- copied analysis PDF -> {dest}")
            _log(f"[main] DRY RUN -- wrote preview to {out_dir}/digest_preview.*")
            # Write dashboard JSON on dry run too, so the pipeline is
            # exercised end-to-end. The workflow itself decides whether
            # to commit + publish it.
            _write_dashboard_json(anchor, analysis, source_pdfs)
            return 0

        # Real run: send + record.
        attachments = [analysis_pdf_path] if analysis_pdf_path else []
        _send_email(subject, body_text, body_html, attachments=attachments)

        # 9. Dashboard JSON.
        _write_dashboard_json(anchor, analysis, source_pdfs)

    # Record the accession as seen so tomorrow's portfolio run doesn't
    # re-emit the same filing. Only the caller of ``run_portfolio``
    # persists the state; here we just mutate the in-memory dict.
    if seen_state is not None and accession:
        seen_state[_seen_key(ticker, accession)] = _ny_now().isoformat(timespec="seconds")

    return 0


def run_portfolio(dry_run: bool = False) -> int:
    """Iterate every ticker under ``us:`` in tickers.yaml, dedup by
    filing accession via ``state_seen_us.json``, and return the number
    of tickers that produced a fresh analysis (0 on a quiet morning, or
    N on the first run when every ticker's latest 10-K is new).

    A per-ticker exception is logged and skipped rather than failing
    the whole portfolio — a single company's SEC hiccup shouldn't stop
    the rest of the morning.
    """
    tickers = _load_us_portfolio()
    if not tickers:
        _log("[portfolio] no tickers under us: in tickers.yaml -- nothing to do")
        return 0

    _log(f"[portfolio] iterating {len(tickers)} US ticker(s): {', '.join(tickers)}")
    seen_state = _prune_us_seen_state(_load_us_seen_state())
    fresh = 0
    for t in tickers:
        try:
            before = dict(seen_state)
            rc = run(ticker=t, dry_run=dry_run, seen_state=seen_state)
            # A fresh analysis is the only thing that grows the state
            # dict. rc == 0 and no growth means "already seen" (dedup)
            # or "no filings" (empty portfolio for that ticker) — both
            # of which are not-fresh outcomes.
            if rc == 0 and len(seen_state) > len(before):
                fresh += 1
            elif rc != 0:
                _log(f"[portfolio] {t}: run returned {rc}")
        except Exception as exc:
            _log(f"[portfolio] {t}: exception {exc.__class__.__name__}: {exc}")
            _log(traceback.format_exc())

    if not dry_run:
        _save_us_seen_state(seen_state)
        _log(f"[portfolio] state saved -> {US_SEEN_STATE_PATH} "
             f"({len(seen_state)} entries)")
    _log(f"[portfolio] {fresh} fresh analysis(es) this run")
    return 0


# --- CLI -------------------------------------------------------------------

def _cli() -> int:
    parser = argparse.ArgumentParser(description="Bob USA -- SEC EDGAR results digest.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ticker",
                       help="US symbol (e.g. MSFT). Single-ticker dispatch.")
    group.add_argument("--portfolio", action="store_true",
                       help="Iterate every ticker under us: in tickers.yaml, "
                            "with per-accession seen-state dedup. Used by "
                            "the morning schedule.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Build digest + preview but don't send email.")
    args = parser.parse_args()
    try:
        if args.portfolio:
            return run_portfolio(dry_run=args.dry_run)
        # ``respect_seen=False`` on a single-ticker dispatch — a manual
        # re-fire must always run, even when the same filing was
        # already emitted earlier that day.
        return run(ticker=args.ticker, dry_run=args.dry_run, respect_seen=False)
    except Exception as exc:
        _log(f"[main] FATAL {exc.__class__.__name__}: {exc}")
        _log(traceback.format_exc())
        return 2


if __name__ == "__main__":
    sys.exit(_cli())
