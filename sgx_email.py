"""
sgx_email.py -- Bob SG daily digest email builder.

Renders the classified SGX announcements into the same visual shape as
Bob's ASX digest: dark panel background, section headers colored by
priority (high impact / material / FYI), individual items as compact
cards with a link back to investors.sgx.com.

Constraints (mirroring agent.py's email conventions):
  - Table-based layout with inline styles only.
  - No `<style>` blocks or `class=` attributes — most email clients strip
    styles and mishandle flexbox/grid.
  - `S$` currency prefix on any figure in the results card, since SGX
    reporters use SGD (a bare `$` would ambiguously read as USD).

Round 2 renders full results cards only when the caller passes a parsed
analysis dict; otherwise every item renders as a compact two-liner. The
LLM/Google-Doc plumbing lives in sgx_agent.py.
"""

from __future__ import annotations

import datetime as dt
import html as htmlmod
import re
from typing import Dict, List, Optional, Tuple


BOB_SG_NAME = "Bob SG"
BOB_SG_VERSION = "V4 R2"

SGT = dt.timezone(dt.timedelta(hours=8))

# Color palette -- mirrors Bob's ASX email colors so both digests read as
# one family. Only differentiator is the header badge that says "SG".
COLOR_BG = "#0B1220"
COLOR_PANEL = "#111B2E"
COLOR_TEXT = "#E5E7EB"
COLOR_HIGH_IMPACT = "#F59E0B"   # amber -- material to your investment thesis
COLOR_MATERIAL = "#3B82F6"      # blue -- worth reading
COLOR_FYI = "#10B981"           # green -- everything else
COLOR_SG_ACCENT = "#EF4444"     # SG badge red

COLOR_RESULTS_HEADER = "#14532D"
COLOR_RESULTS_HEADER_SUB = "#A7F3D0"
COLOR_RESULTS_CARD = "#FFFFFF"
COLOR_RESULTS_ROW_SHADE = "#F1F5F2"
COLOR_RESULTS_LABEL = "#374151"
COLOR_RESULTS_VALUE = "#111827"
COLOR_RESULTS_MUTED = "#6B7280"

# Priority tiers. RESULTS_HY_FY items with a parsed analysis get the full
# card; without analysis they still land in HIGH IMPACT as a two-liner.
_HIGH_IMPACT_BUCKETS = (
    "RESULTS_HY_FY", "ACQUISITION", "CAPITAL_OR_DEBT_RAISE",
    "TRADING_UPDATE",
)
_MATERIAL_BUCKETS = (
    "DIVIDEND", "SHARE_BUYBACK", "CONTRACT_MATERIAL",
)


def _sgt_today_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone(SGT).date().isoformat()


def _esc(s: str) -> str:
    return htmlmod.escape(s or "")


def _linkify_urls(text: str) -> str:
    """Escape then rewrite http(s) URLs into anchors -- mirrors agent.py's
    _linkify_urls so plain-text summaries with a URL render as clickable."""
    escaped = htmlmod.escape(text or "")
    return re.sub(
        r"(https?://[^\s<]+)",
        r"<a href='\1' style='color:#93C5FD; text-decoration:underline;'>\1</a>",
        escaped,
    )


def _plain_block(text: str) -> str:
    """Compact panel for a two-line item."""
    return (
        f"<div style='margin:12px 0; padding:12px; background:{COLOR_PANEL}; "
        f"border-radius:10px; white-space:pre-wrap; line-height:1.5; "
        f"font-size:14px; color:{COLOR_TEXT};'>"
        f"{_linkify_urls(text)}"
        f"</div>"
    )


def _section(title: str, color: str, blocks_html: List[str]) -> str:
    """A tinted header bar + its member blocks. Skips rendering when empty."""
    if not blocks_html:
        return ""
    items = "".join(blocks_html)
    return (
        f"<div style='margin:18px 0;'>"
        f"<div style='padding:10px 12px; background:{color}; color:#0B1220; "
        f"font-weight:800; border-radius:10px; letter-spacing:0.6px;'>"
        f"{_esc(title)}"
        f"</div>{items}</div>"
    )


def _two_liner_for_item(item: Dict) -> str:
    """The default rendering when no LLM analysis is available. Keeps the
    ASX two-line shape (headline + so-what)."""
    ticker = item.get("ticker") or ""
    title = item.get("title") or ""
    category = item.get("category_name") or ""
    when = item.get("date") or ""
    url = item.get("url") or ""
    ref = item.get("ref_id") or ""

    header = f"{ticker}: {title[:180]}"
    meta_bits = [b for b in (category, when, f"ref {ref}" if ref else "") if b]
    meta_line = "  |  ".join(meta_bits)
    link_line = f"Source: {url}" if url else ""

    parts = [header]
    if meta_line:
        parts.append(meta_line)
    if link_line:
        parts.append(link_line)
    return "\n".join(parts)


def _results_card_html(item: Dict, analysis: Dict) -> str:
    """Full results card: metric table + short summary + link. `analysis`
    is the parsed JSON from the LLM (see sgx_agent's run_deep_results_analysis).
    Currency prefix is always `S$` for SGX names (unlike ASX which uses a
    bare `$` because the card context line already says A$)."""
    ticker = item.get("ticker") or ""
    issuer = item.get("issuer_name") or ""
    period = analysis.get("period") or ""
    period_type = analysis.get("period_type") or ""
    metrics = analysis.get("metrics") or {}
    summary = (analysis.get("summary") or "").strip()
    doc_url = analysis.get("doc_url") or ""

    header_bits = [b for b in (ticker, issuer, period, period_type) if b]
    header = " | ".join(header_bits)

    metric_rows_html = ""
    metric_order = [
        ("revenue", "Revenue"),
        ("underlying_npat", "Underlying NPAT"),
        ("underlying_eps", "Underlying EPS"),
        ("ordinary_dividend", "Ordinary dividend"),
        ("operating_cash_flow", "Operating cash flow"),
    ]
    for i, (key, label) in enumerate(metric_order):
        shade = COLOR_RESULTS_ROW_SHADE if i % 2 == 0 else COLOR_RESULTS_CARD
        m = metrics.get(key) or {}
        value = (m.get("value") or "n/a").strip()
        change = (m.get("change") or "").strip()
        basis = (m.get("basis") or "").strip()
        change_html = ""
        if change:
            change_html = f" <span style='color:{COLOR_RESULTS_MUTED};'>({_esc(change)})</span>"
        basis_html = ""
        if basis:
            basis_html = (
                f"<div style='color:{COLOR_RESULTS_MUTED}; font-size:11px;'>"
                f"{_esc(basis)}</div>"
            )
        metric_rows_html += (
            f"<tr>"
            f"<td style='padding:8px 10px; background:{shade}; "
            f"color:{COLOR_RESULTS_LABEL}; font-size:13px;'>{_esc(label)}</td>"
            f"<td style='padding:8px 10px; background:{shade}; "
            f"color:{COLOR_RESULTS_VALUE}; font-size:13px; text-align:right;'>"
            f"{_esc(value)}{change_html}{basis_html}</td>"
            f"</tr>"
        )

    doc_link_html = ""
    if doc_url:
        doc_link_html = (
            f"<div style='margin-top:8px; font-size:12px;'>"
            f"<a href='{_esc(doc_url)}' "
            f"style='color:#2563EB; text-decoration:underline;'>"
            f"Full analysis (Google Doc)</a></div>"
        )
    source_link_html = ""
    source = item.get("url") or ""
    if source:
        source_link_html = (
            f"<div style='margin-top:4px; font-size:12px;'>"
            f"<a href='{_esc(source)}' "
            f"style='color:#2563EB; text-decoration:underline;'>"
            f"Source announcement (SGX)</a></div>"
        )

    return (
        f"<div style='margin:12px 0; padding:0; background:{COLOR_RESULTS_CARD}; "
        f"border-radius:10px; overflow:hidden; "
        f"box-shadow:0 1px 3px rgba(0,0,0,0.2);'>"
        f"<div style='padding:10px 12px; background:{COLOR_RESULTS_HEADER}; "
        f"color:#FFFFFF; font-weight:700;'>"
        f"{_esc(header)}"
        f"<span style='display:block; color:{COLOR_RESULTS_HEADER_SUB}; "
        f"font-weight:400; font-size:11px; margin-top:2px;'>"
        f"reported in S$ (SGD) — figures as stated in the source"
        f"</span>"
        f"</div>"
        f"<table style='width:100%; border-collapse:collapse; "
        f"font-family:-apple-system, Segoe UI, Arial, sans-serif;'>"
        f"{metric_rows_html}"
        f"</table>"
        f"<div style='padding:12px; color:{COLOR_RESULTS_VALUE}; "
        f"font-size:13px; line-height:1.5;'>"
        f"{_esc(summary) if summary else '<em>Summary unavailable.</em>'}"
        f"{doc_link_html}{source_link_html}"
        f"</div>"
        f"</div>"
    )


def _bucketise(
    classified: List[Tuple[Dict, str, Optional[Dict]]],
) -> Tuple[List[str], List[str], List[str], List[str], List[str], List[str]]:
    """Sort classified items into (high_html, material_html, fyi_html,
    high_text, material_text, fyi_text). Each entry in `classified` is
    (item_dict, bucket_string, optional_analysis_dict)."""
    high_html: List[str] = []
    material_html: List[str] = []
    fyi_html: List[str] = []
    high_text: List[str] = []
    material_text: List[str] = []
    fyi_text: List[str] = []

    for item, bucket, analysis in classified:
        two_liner = _two_liner_for_item(item)
        if bucket == "RESULTS_HY_FY" and analysis:
            html = _results_card_html(item, analysis)
        else:
            html = _plain_block(two_liner)
        text = two_liner

        if bucket in _HIGH_IMPACT_BUCKETS:
            high_html.append(html)
            high_text.append(text)
        elif bucket in _MATERIAL_BUCKETS:
            material_html.append(html)
            material_text.append(text)
        else:
            fyi_html.append(html)
            fyi_text.append(text)

    return high_html, material_html, fyi_html, high_text, material_text, fyi_text


def build_email(
    classified: List[Tuple[Dict, str, Optional[Dict]]],
    hours_back: int = 24,
    model_label: str = "",
    now_sgt: Optional[dt.datetime] = None,
) -> Tuple[str, str, str]:
    """Render the SGX digest as (subject, plain_text_body, html_body).

    `classified` is a list of (item, bucket, analysis|None) triples. `item`
    is an sgx_fetch row; `bucket` is one of sgx_classify's outputs;
    `analysis` is the parsed LLM JSON for a results item (or None). Passing
    an empty list produces a "no announcements" digest."""
    if now_sgt is None:
        now_sgt = dt.datetime.now(dt.timezone.utc).astimezone(SGT)
    today = now_sgt.date().isoformat()

    subject = f"{BOB_SG_NAME} {BOB_SG_VERSION} -- SGX Announcements Digest -- {today} (SGT)"

    high_html, mat_html, fyi_html, high_text, mat_text, fyi_text = _bucketise(classified)

    # --- plain text ---------------------------------------------------------
    text_lines: List[str] = []
    text_lines.append(f"{BOB_SG_NAME} {BOB_SG_VERSION}")
    text_lines.append("=" * len(BOB_SG_NAME))
    text_lines.append(
        f"SGX Announcements Digest -- last {hours_back} hours -- {today} (SGT)"
    )
    text_lines.append("")

    if not classified:
        text_lines.append(f"No SGX announcements found in the last {hours_back} hours.")
    else:
        if high_text:
            text_lines.append("HIGH IMPACT")
            text_lines.append("-" * 60)
            text_lines.extend(high_text)
            text_lines.append("")
        if mat_text:
            text_lines.append("MATERIAL")
            text_lines.append("-" * 60)
            text_lines.extend(mat_text)
            text_lines.append("")
        if fyi_text:
            text_lines.append("FYI (OTHER ANNOUNCEMENTS)")
            text_lines.append("-" * 60)
            text_lines.extend(fyi_text)
            text_lines.append("")
    body_text = "\n".join(text_lines)

    # --- html ---------------------------------------------------------------
    header_html = (
        f"<div style='padding:18px; background:{COLOR_BG}; color:{COLOR_TEXT}; "
        f"font-family:-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, "
        f"Arial, sans-serif;'>"
        f"<div style='font-size:22px; font-weight:900; margin-bottom:6px;'>"
        f"{_esc(BOB_SG_NAME)} "
        f"<span style='display:inline-block; margin-left:6px; padding:2px 8px; "
        f"background:{COLOR_SG_ACCENT}; color:#FFFFFF; font-size:12px; "
        f"border-radius:6px; vertical-align:middle;'>SGX</span> "
        f"<span style='opacity:0.7; font-weight:400; font-size:14px;'>"
        f"{_esc(BOB_SG_VERSION)}</span>"
        f"</div>"
        f"<div style='opacity:0.9; font-size:14px; margin-bottom:10px;'>"
        f"SGX Announcements Digest — last {hours_back} hours — {_esc(today)} (SGT)"
        f"</div>"
        f"<div style='opacity:0.75; font-size:12px; margin-bottom:18px;'>"
        f"{_esc('AI: ' + model_label) if model_label else '&nbsp;'}"
        f"</div>"
    )

    sections_html = ""
    if not classified:
        sections_html += (
            f"<div style='margin:18px 0; padding:12px; background:{COLOR_PANEL}; "
            f"border-radius:10px; color:{COLOR_TEXT};'>"
            f"No SGX announcements found in the last {hours_back} hours."
            f"</div>"
        )
    else:
        sections_html += _section("HIGH IMPACT", COLOR_HIGH_IMPACT, high_html)
        sections_html += _section("MATERIAL", COLOR_MATERIAL, mat_html)
        sections_html += _section("FYI (OTHER ANNOUNCEMENTS)", COLOR_FYI, fyi_html)

    body_html = header_html + sections_html + "</div>"
    return subject, body_text, body_html
