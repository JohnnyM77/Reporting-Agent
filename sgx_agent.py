"""
sgx_agent.py -- Bob SG orchestrator (V4 Round 2).

Ties Round 1's fetch layer (sgx_fetch.py) to the classifier, PDF fetcher,
LLM analysis (via shared/pdf_llm), Google Doc creation, and email sender.
Runs standalone on the self-hosted Windows runner. Does NOT touch
agent.py or the ASX Bob path -- deliberately duplicates the ~40 lines of
LLM-plumbing and ~30 lines of Drive-plumbing rather than coupling to
agent.py (which drags weasyprint / pypdf / bs4 imports the SGX box may
not need for anything else).

Pipeline
--------
    1. Fetch (sgx_fetch)                  -- Playwright prime + REST replay
    2. Filter by hours_back window
    3. Dedupe via seen_state              -- state_seen_sgx.json
    4. Classify (sgx_classify)            -- by SGX sub/category, not title
    5. For RESULTS_HY_FY items only:
         a. Download PDFs (sgx_pdf)
         b. Anthropic streaming call with native PDF blocks + RESULTS_HYFY_PROMPT
         c. Parse structured JSON
         d. Create Google Doc from full_analysis field (optional)
    6. Build email (sgx_email)            -- 3 buckets, same shape as ASX Bob
    7. Send via SMTP                      -- same env vars as agent.py
    8. Save seen_state

Env
---
    EMAIL_FROM, EMAIL_TO, EMAIL_APP_PASSWORD   -- SMTP (Gmail app password)
    ANTHROPIC_API_KEY                          -- Claude
    CLAUDE_MODEL                               -- optional, defaults inline
    GDRIVE_CLIENT_ID / _SECRET / _REFRESH_TOKEN -- OAuth for Drive
    GDRIVE_SGX_ANALYSIS_FOLDER_ID              -- Drive folder for analysis Docs
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
from sgx_pdf import fetch_announcement_pdfs
from sgx_email import build_email, BOB_SG_NAME, BOB_SG_VERSION
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

    model = os.environ.get("CLAUDE_MODEL", CLAUDE_MODEL_DEFAULT)
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


# --- Google Doc creation (optional) ----------------------------------------

def _drive_service():
    """OAuth-first, service-account fallback. Ported from agent.py's
    drive_service() shape but self-contained. Returns None when no
    credentials are configured -- callers treat Doc creation as
    best-effort."""
    client_id = os.environ.get("GDRIVE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN", "").strip()

    try:
        from googleapiclient.discovery import build
    except Exception:
        _log("[drive] google-api-python-client not installed -- skipping Doc")
        return None

    if client_id and client_secret and refresh_token:
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        try:
            return build("drive", "v3", credentials=creds, cache_discovery=False)
        except Exception as exc:
            _log(f"[drive] OAuth build failed: {exc}")
            return None
    _log("[drive] no OAuth creds set -- skipping Google Doc creation")
    return None


def _markdown_to_html(md: str) -> str:
    """Tiny markdown subset -- headings, paragraphs, lists. Enough for
    what RESULTS_HYFY_PROMPT emits. Deliberately NOT a general engine."""
    if not md:
        return "<p><em>No analysis text.</em></p>"
    out: List[str] = []
    in_ul = False
    for raw in md.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append("")
            continue
        if line.startswith("### "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h3>{line[4:].strip()}</h3>")
        elif line.startswith("## "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h2>{line[3:].strip()}</h2>")
        elif line.startswith("# "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<h1>{line[2:].strip()}</h1>")
        elif line.lstrip().startswith(("- ", "* ")):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{line.lstrip()[2:].strip()}</li>")
        else:
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f"<p>{line}</p>")
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def _create_analysis_doc(
    ticker: str, period: str, body_html: str, folder_id: str,
) -> str:
    """Create a native Google Doc for the deep analysis. Returns the
    webViewLink, or "" on any failure (including Drive not configured)."""
    if not folder_id:
        return ""
    service = _drive_service()
    if service is None:
        return ""
    try:
        from googleapiclient.http import MediaInMemoryUpload
        today = _sgt_now().date().isoformat()
        name = f"{ticker} {period or 'results'} SGX analysis {today}"
        metadata = {
            "name": name,
            "parents": [folder_id],
            "mimeType": "application/vnd.google-apps.document",
        }
        media = MediaInMemoryUpload(
            body_html.encode("utf-8"), mimetype="text/html", resumable=False,
        )
        created = service.files().create(
            body=metadata, media_body=media,
            fields="id,webViewLink", supportsAllDrives=True,
        ).execute()
        link = created.get("webViewLink", "")
        _log(f"[drive] Doc created for {ticker}: {link}")
        return link
    except Exception as exc:
        _log(f"[drive] Doc create failed for {ticker}: {exc}")
        return ""


# --- deep results analysis --------------------------------------------------

def _run_results_analysis(
    item: Dict,
    pdf_paths: List[Path],
    counters: Dict,
    drive_folder: str,
) -> Optional[Dict]:
    """Run one results-item deep analysis. Returns the parsed analysis
    dict (with a `doc_url` key added on success), or None if analysis
    couldn't be produced."""
    ticker = item.get("ticker") or ""
    title = item.get("title") or ""
    issuer = item.get("issuer_name") or ""

    if not RESULTS_HYFY_PROMPT:
        _log("[results] RESULTS_HYFY_PROMPT unavailable -- skipping deep analysis")
        return None
    if not pdf_paths:
        _log(f"[results] {ticker}: no PDFs -- skipping deep analysis")
        return None

    user = (
        f"Ticker: {ticker}\n"
        f"Issuer: {issuer}\n"
        f"Title: {title}\n\n"
        f"The attached PDF(s) contain the full results release / statements. "
        f"Return your response as strict JSON per the system prompt schema. "
        f"Note this issuer reports in Singapore dollars (SGD) — reflect that "
        f"in every metric's currency and prefix figures with S$."
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
                       "See Google Doc for raw text.",
            "period": "",
            "period_type": "",
            "metrics": {},
            "_raw_text": text,
        }

    # Attempt to create a Doc from the full_analysis markdown.
    period = parsed.get("period") or ""
    full_md = parsed.get("full_analysis") or ""
    if full_md or parsed.get("_raw_text"):
        header = (
            f"<h1>{ticker} — {issuer} — {period}</h1>"
            f"<p><em>SGX announcement: {title}</em></p>"
        )
        if full_md:
            body_html = header + _markdown_to_html(full_md)
        else:
            body_html = (
                header + "<h2>Raw model output (unparseable)</h2>"
                f"<pre>{parsed.get('_raw_text', '')}</pre>"
            )
        parsed["doc_url"] = _create_analysis_doc(
            ticker, period, body_html, drive_folder,
        )
    return parsed


# --- main pipeline ----------------------------------------------------------

def _send_email(subject: str, body_text: str, body_html: str) -> bool:
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
    """
    is_results_mode = bool(results_tickers)
    _log(f"{BOB_SG_NAME} {BOB_SG_VERSION} -- "
         f"mode={'results-ticker' if is_results_mode else 'portfolio'} "
         f"hours_back={hours_back} dry_run={dry_run}")

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
    classified: List[Tuple[Dict, str, Optional[Dict]]] = []
    counters = {"llm_calls": 0, "pdfs_downloaded": 0}
    drive_folder = os.environ.get("GDRIVE_SGX_ANALYSIS_FOLDER_ID", "").strip()

    with tempfile.TemporaryDirectory(prefix="sgx_pdfs_") as pdf_root:
        pdf_root_path = Path(pdf_root)
        for item in items_to_process:
            bucket = classify_sgx_announcement(item)
            analysis: Optional[Dict] = None

            if bucket == "RESULTS_HY_FY":
                ticker = item.get("ticker") or "UNK"
                item_dir = pdf_root_path / (item.get("ref_id") or ticker)
                # SGX bundles all report + presentation + press release
                # PDFs under one landing page. Grab them all (up to the
                # per-announcement cap) and pass them together to Claude.
                pdfs = fetch_announcement_pdfs(
                    item.get("url") or "",
                    item_dir,
                    max_pdfs=MAX_PDFS_PER_ANNOUNCEMENT,
                    log=_log,
                )
                counters["pdfs_downloaded"] += len(pdfs)
                _log(f"[main] {ticker}: got {len(pdfs)} PDF(s) from landing page")
                if counters["pdfs_downloaded"] > MAX_PDFS_PER_RUN:
                    _log(f"[main] PDF cap reached -- skipping analysis for {ticker}")
                elif pdfs:
                    analysis = _run_results_analysis(
                        item, pdfs, counters, drive_folder,
                    )

            classified.append((item, bucket, analysis))

    # 6. Build email.
    model_label = os.environ.get("CLAUDE_MODEL", CLAUDE_MODEL_DEFAULT)
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
         f"PDFs: {counters['pdfs_downloaded']}/{MAX_PDFS_PER_RUN}")

    if dry_run:
        out_dir = Path("outputs/sgx")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "digest_preview.html").write_text(body_html, encoding="utf-8")
        (out_dir / "digest_preview.txt").write_text(body_text, encoding="utf-8")
        _log(f"[main] DRY RUN -- wrote preview to {out_dir}/digest_preview.*")
        return 0

    _send_email(subject, body_text, body_html)

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
        )
    except Exception as exc:
        _log(f"[main] FATAL {exc.__class__.__name__}: {exc}")
        _log(traceback.format_exc())
        return 2


if __name__ == "__main__":
    sys.exit(_cli())
