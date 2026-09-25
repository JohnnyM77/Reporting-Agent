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
LLM plumbing lives in sgx_agent.py.

The email body carries the metric card, a short summary, and links to
the source PDFs on SGX (no PDF attachments — the user wanted lightweight
links, not 4MB of duplicate attachments). The full markdown analysis
renders into a standalone HTML document (build_analysis_pdf_html) that
sgx_agent then prints to PDF via Playwright + Chrome and attaches to
the email as a forwardable, shareable file.
"""

from __future__ import annotations

import datetime as dt
import html as htmlmod
import re
from typing import Dict, List, Optional, Tuple


# Display name for the SGX bot. Renamed from "Bob SG" to "Singapore
# Slinger" -- ASX Bob and Slinger are two distinct personas now, not one
# bot with a regional suffix. The variable name stays BOB_SG_NAME to
# avoid a churny rename across every import; only the string changed.
BOB_SG_NAME = "Singapore Slinger"
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


def _markdown_to_email_html(md: str, *, pdf_mode: bool = False) -> str:
    """Tiny markdown subset -> inline-styled HTML.

    Supports: headings h1/h2/h3, paragraphs, unordered lists (`- `/`* `),
    pipe tables, blockquotes (`> ...` -> styled callout), bold and
    italics inline. Deliberately not a general markdown engine --
    matches what RESULTS_HYFY_PROMPT emits.

    `pdf_mode=True` bumps sizing/spacing/section-header styling for
    print (larger base font, colored + underlined `##` section headers,
    page-break-inside:avoid on tables/blockquotes) so the standalone
    analysis PDF reads like a report, not a form. Left as the default
    `False` for the email card so it stays glance-sized."""
    if not md:
        return ""

    # Sizing knobs -- one place per pdf vs email.
    if pdf_mode:
        base_size, base_line = "13px", "1.6"
        h1_size, h2_size, h3_size = "22px", "16px", "13px"
        p_margin, ul_margin, li_margin = "8px 0", "8px 0", "4px 0"
        table_size = "12px"
        h2_style = (
            "font-weight:700; color:#111827; margin:24px 0 10px 0; "
            "padding-bottom:6px; border-bottom:2px solid #14532D; "
            "letter-spacing:0.02em; text-transform:uppercase;"
        )
        h3_style = (
            "font-weight:700; color:#374151; margin:16px 0 6px 0; "
            "text-transform:uppercase; letter-spacing:0.05em;"
        )
        table_break = "page-break-inside:avoid; break-inside:avoid;"
        quote_break = "page-break-inside:avoid; break-inside:avoid;"
    else:
        base_size, base_line = "13px", "1.5"
        h1_size, h2_size, h3_size = "18px", "16px", "14px"
        p_margin, ul_margin, li_margin = "6px 0", "8px 0", "2px 0"
        table_size = "13px"
        h2_style = "color:#111827; font-weight:700;"
        h3_style = "color:#111827; font-weight:600;"
        table_break = ""
        quote_break = ""

    def esc(s: str) -> str:
        return htmlmod.escape(s or "")

    def inline(s: str) -> str:
        # Bold (**...**) and italics (*...*), applied to already-escaped text.
        out = esc(s)
        out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
        out = re.sub(r"(?<!\*)\*(?!\s)([^*\n]+?)\*(?!\*)", r"<em>\1</em>", out)
        return out

    lines = md.splitlines()
    out: List[str] = []
    i = 0
    in_ul = False

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    while i < len(lines):
        raw = lines[i].rstrip()
        stripped = raw.strip()

        # Pipe table: header row, separator row, body rows.
        if (
            "|" in raw and i + 1 < len(lines)
            and re.match(r"^\s*\|?\s*:?-{3,}", lines[i + 1])
        ):
            close_ul()
            header_cells = [c.strip() for c in raw.strip().strip("|").split("|")]
            i += 2  # skip separator
            rows_html: List[str] = []
            while i < len(lines) and "|" in lines[i]:
                row_cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                cells_html = "".join(
                    f"<td style='padding:6px 10px; border-top:1px solid #E5E7EB; "
                    f"color:{COLOR_RESULTS_VALUE}; font-size:{table_size};'>"
                    f"{inline(c)}</td>"
                    for c in row_cells
                )
                rows_html.append(f"<tr>{cells_html}</tr>")
                i += 1
            header_html = "".join(
                f"<th style='padding:6px 10px; text-align:left; "
                f"background:{COLOR_RESULTS_ROW_SHADE}; "
                f"color:{COLOR_RESULTS_LABEL}; font-size:{table_size};'>"
                f"{inline(c)}</th>"
                for c in header_cells
            )
            out.append(
                f"<table style='width:100%; border-collapse:collapse; "
                f"margin:12px 0; {table_break} font-family:-apple-system, "
                f"Segoe UI, Arial, sans-serif;'>"
                f"<thead><tr>{header_html}</tr></thead>"
                f"<tbody>{''.join(rows_html)}</tbody></table>"
            )
            continue

        # Blockquote block: consecutive lines starting with `> `. Renders
        # as a left-bar callout so the LLM can visually mark the one
        # sentence the reader must not miss ("the bull case is X; the
        # bear case is Y" etc). Bob's ASX card doesn't need this but a
        # forwarded PDF does.
        if stripped.startswith(">"):
            close_ul()
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            body = "<br>".join(inline(x) for x in quote_lines if x)
            out.append(
                f"<div style='margin:12px 0; padding:10px 14px; "
                f"background:#F1F5F2; border-left:4px solid #14532D; "
                f"color:#111827; font-size:{base_size}; "
                f"line-height:{base_line}; {quote_break}'>"
                f"{body}"
                f"</div>"
            )
            continue

        if not stripped:
            close_ul()
            i += 1
            continue

        if stripped.startswith("### "):
            close_ul()
            out.append(
                f"<h3 style='font-size:{h3_size}; {h3_style}'>"
                f"{inline(stripped[4:])}</h3>"
            )
        elif stripped.startswith("## "):
            close_ul()
            out.append(
                f"<h2 style='font-size:{h2_size}; {h2_style}'>"
                f"{inline(stripped[3:])}</h2>"
            )
        elif stripped.startswith("# "):
            close_ul()
            out.append(
                f"<h1 style='font-size:{h1_size}; margin:18px 0 12px; "
                f"color:{COLOR_RESULTS_VALUE}; font-weight:800;'>"
                f"{inline(stripped[2:])}</h1>"
            )
        elif stripped.startswith(("- ", "* ")):
            if not in_ul:
                out.append(
                    f"<ul style='margin:{ul_margin}; padding-left:22px; "
                    f"line-height:{base_line};'>"
                )
                in_ul = True
            out.append(
                f"<li style='margin:{li_margin}; font-size:{base_size}; "
                f"color:{COLOR_RESULTS_VALUE};'>{inline(stripped[2:])}</li>"
            )
        else:
            close_ul()
            out.append(
                f"<p style='margin:{p_margin}; font-size:{base_size}; "
                f"line-height:{base_line}; color:{COLOR_RESULTS_VALUE};'>"
                f"{inline(stripped)}</p>"
            )
        i += 1
    close_ul()
    return "".join(out)


def _metric_rows_html(analysis: Dict, shade_light: str, shade_dark: str) -> str:
    """Render the five metric rows. Shared between the email card and
    the standalone analysis PDF so both stay in lockstep."""
    metrics = analysis.get("metrics") or {}
    # Keys MUST match RESULTS_HYFY_PROMPT's schema:
    # dividend_ordinary + change_pct. An earlier version of this file
    # used ordinary_dividend / change which never populated -> the
    # dividend row rendered "n/a" and the YoY column stayed blank for
    # every SGX report. Do not rename without updating the prompt too.
    metric_order = [
        ("revenue", "Revenue"),
        ("underlying_npat", "Underlying NPAT"),
        ("underlying_eps", "Underlying EPS"),
        ("dividend_ordinary", "Ordinary dividend"),
        ("operating_cash_flow", "Operating cash flow"),
    ]
    rows = ""
    for i, (key, label) in enumerate(metric_order):
        shade = shade_light if i % 2 == 0 else shade_dark
        m = metrics.get(key) or {}
        value = (m.get("value") or "n/a").strip()
        change = (m.get("change_pct") or "").strip()
        basis = (m.get("basis") or "").strip()

        # Colour the YoY change: green for +ve, red for -ve, muted grey
        # for anything else (n/a, n/m, unsigned). Reads instantly on the
        # metric card without breaking the ASCII text-mode fallback.
        change_html = ""
        if change:
            c_norm = change.strip().lstrip("(").rstrip(")")
            if c_norm.startswith("+"):
                change_colour = "#15803D"  # green-700
            elif c_norm.startswith("-"):
                change_colour = "#B91C1C"  # red-700
            else:
                change_colour = COLOR_RESULTS_MUTED
            change_html = (
                f" <span style='color:{change_colour}; font-weight:600;'>"
                f"({_esc(change)})</span>"
            )
        basis_html = ""
        if basis:
            basis_html = (
                f"<div style='color:{COLOR_RESULTS_MUTED}; font-size:11px;'>"
                f"{_esc(basis)}</div>"
            )
        rows += (
            f"<tr>"
            f"<td style='padding:10px 12px; background:{shade}; "
            f"color:{COLOR_RESULTS_LABEL}; font-size:13px; "
            f"border-bottom:1px solid #E5E7EB;'>{_esc(label)}</td>"
            f"<td style='padding:10px 12px; background:{shade}; "
            f"color:{COLOR_RESULTS_VALUE}; font-size:14px; "
            f"font-weight:600; text-align:right; "
            f"border-bottom:1px solid #E5E7EB;'>"
            f"{_esc(value)}{change_html}{basis_html}</td>"
            f"</tr>"
        )
    return rows


def _results_card_html(item: Dict, analysis: Dict) -> str:
    """Full results card in the email body: metric table + short summary
    + links to source PDFs on SGX + link to the announcement landing page.

    The deep analysis (full_analysis markdown) is NOT rendered here — it
    goes into a standalone PDF the caller attaches to the email so the
    user can forward it. `item['_pdf_sources']` is an optional list of
    `(url, name)` tuples for the source PDF links; sgx_agent populates it
    after `fetch_announcement_pdfs`.

    Currency prefix is always `S$` for SGX names (unlike ASX which uses
    a bare `$` because the card context line already says A$)."""
    ticker = item.get("ticker") or ""
    issuer = item.get("issuer_name") or ""
    period = analysis.get("period") or ""
    period_type = analysis.get("period_type") or ""
    summary = (analysis.get("summary") or "").strip()
    pdf_sources = item.get("_pdf_sources") or []

    header_bits = [b for b in (ticker, issuer, period, period_type) if b]
    header = " | ".join(header_bits)

    metric_rows_html = _metric_rows_html(
        analysis, COLOR_RESULTS_ROW_SHADE, COLOR_RESULTS_CARD,
    )

    # Source PDFs -- one link per attachment fetched from SGX. Preferred
    # over attaching the PDFs to the email (which duplicates them and
    # bloats the message) but still gives one-click access to the
    # originals.
    source_pdfs_html = ""
    if pdf_sources:
        items_html = "".join(
            f"<li style='margin:3px 0; font-size:12px;'>"
            f"<a href='{_esc(url)}' "
            f"style='color:#2563EB; text-decoration:underline;'>"
            f"{_esc(name)}</a></li>"
            for url, name in pdf_sources
        )
        source_pdfs_html = (
            f"<div style='margin-top:12px; padding-top:10px; "
            f"border-top:1px solid #E5E7EB;'>"
            f"<div style='font-weight:700; font-size:11px; "
            f"color:{COLOR_RESULTS_LABEL}; text-transform:uppercase; "
            f"letter-spacing:0.5px; margin-bottom:4px;'>Source PDFs</div>"
            f"<ul style='margin:0; padding-left:18px;'>{items_html}</ul>"
            f"</div>"
        )

    # Note the deep analysis is in the attached PDF -- so a reader who
    # only glances at the email body still knows there's more.
    analysis_note_html = (
        f"<div style='margin-top:10px; font-size:12px; "
        f"color:{COLOR_RESULTS_MUTED}; font-style:italic;'>"
        f"Full deep analysis attached as PDF."
        f"</div>"
    )

    source_link_html = ""
    source = item.get("url") or ""
    if source:
        source_link_html = (
            f"<div style='margin-top:6px; font-size:12px;'>"
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
        f"{analysis_note_html}{source_pdfs_html}{source_link_html}"
        f"</div>"
        f"</div>"
    )


def build_analysis_pdf_html(item: Dict, analysis: Dict) -> str:
    """Standalone HTML document for the attached analysis PDF.

    Full-page A4-friendly rendering: ticker + issuer + period header,
    metric table, summary, then the full deep analysis rendered from
    markdown. Emitted as a complete document (<!doctype>, <html>, <head>,
    <body>) with a light theme -- suitable for printing to PDF via
    headless Chrome. Kept in one function so sgx_agent can call it
    directly without knowing anything about page layout."""
    ticker = item.get("ticker") or ""
    issuer = item.get("issuer_name") or ""
    period = analysis.get("period") or ""
    period_type = (analysis.get("period_type") or "").replace("_", " ")
    currency = analysis.get("currency") or "SGD"
    title = item.get("title") or ""
    summary = (analysis.get("summary") or "").strip()
    full_md = (analysis.get("full_analysis") or "").strip()

    generated = dt.datetime.now(dt.timezone.utc).astimezone(SGT)
    generated_str = f"{generated.day} {generated.strftime('%b %Y')} SGT"

    # --- Cover header. Big ticker/issuer, period pill, generated-on
    # date. Filed under "the reader forwards this PDF, so page one has
    # to earn attention".
    period_pill = ""
    if period:
        period_pill = (
            f"<span style='display:inline-block; margin-left:10px; "
            f"padding:3px 10px; background:rgba(255,255,255,0.16); "
            f"color:#FFFFFF; font-size:13px; font-weight:600; "
            f"border-radius:999px; vertical-align:middle;'>"
            f"{_esc(period)}{' &middot; ' + _esc(period_type) if period_type else ''}"
            f"</span>"
        )
    cover = (
        f"<div style='padding:24px 28px 22px; background:{COLOR_RESULTS_HEADER}; "
        f"color:#FFFFFF; page-break-after:avoid;'>"
        f"<div style='font-size:10px; text-transform:uppercase; "
        f"letter-spacing:2px; color:{COLOR_RESULTS_HEADER_SUB}; "
        f"margin-bottom:6px;'>"
        f"Singapore Slinger &middot; SGX Results Analysis"
        f"</div>"
        f"<div style='font-size:28px; font-weight:800; line-height:1.15; "
        f"margin-bottom:4px;'>{_esc(ticker) or 'SGX'}"
        f"{period_pill}</div>"
        f"<div style='font-size:15px; font-weight:400; "
        f"color:{COLOR_RESULTS_HEADER_SUB}; line-height:1.4;'>"
        f"{_esc(issuer) if issuer else ''}"
        f"</div>"
        f"<div style='margin-top:14px; font-size:11px; "
        f"color:{COLOR_RESULTS_HEADER_SUB}; border-top:1px solid "
        f"rgba(255,255,255,0.18); padding-top:10px;'>"
        f"Reported in {_esc(currency)} &middot; figures as stated in the source "
        f"&middot; generated {generated_str}"
        f"</div>"
        f"</div>"
    )

    source_line = (
        f"<div style='margin:14px 0 4px; color:#6B7280; font-size:11px; "
        f"font-style:italic;'>SGX announcement: {_esc(title)}</div>"
    )

    # --- Metric table. Bordered rows, larger figures, section header.
    metric_rows_html = _metric_rows_html(
        analysis,
        shade_light="#F7F9F7",
        shade_dark="#FFFFFF",
    )
    metric_block = (
        f"<h2 style='font-size:16px; font-weight:700; color:#111827; "
        f"margin:18px 0 8px; padding-bottom:6px; "
        f"border-bottom:2px solid {COLOR_RESULTS_HEADER}; "
        f"text-transform:uppercase; letter-spacing:0.05em;'>"
        f"Key metrics</h2>"
        f"<table style='width:100%; border-collapse:collapse; "
        f"border:1px solid #E5E7EB; page-break-inside:avoid; "
        f"break-inside:avoid;'>{metric_rows_html}</table>"
    )

    # --- Summary. Callout-styled so the "verdict first" line the prompt
    # asks for actually reads that way.
    summary_html = (
        f"<h2 style='font-size:16px; font-weight:700; color:#111827; "
        f"margin:22px 0 8px; padding-bottom:6px; "
        f"border-bottom:2px solid {COLOR_RESULTS_HEADER}; "
        f"text-transform:uppercase; letter-spacing:0.05em;'>"
        f"Summary &mdash; the verdict</h2>"
        f"<div style='margin:8px 0 18px; padding:12px 16px; "
        f"background:#F7F9F7; border-left:4px solid {COLOR_RESULTS_HEADER}; "
        f"font-size:14px; line-height:1.6; color:#111827; "
        f"page-break-inside:avoid; break-inside:avoid;'>"
        f"{_esc(summary) if summary else '<em>Summary unavailable.</em>'}"
        f"</div>"
    )

    # --- Full analysis. Renders through the markdown converter in
    # pdf_mode so headings, tables, blockquotes and lists all pick up
    # print-friendly sizing + page-break control.
    if full_md:
        analysis_body_html = _markdown_to_email_html(full_md, pdf_mode=True)
    else:
        analysis_body_html = (
            "<p style='color:#6B7280; font-style:italic;'>"
            "No deep analysis available (LLM returned no content)."
            "</p>"
        )
    analysis_block = (
        f"<h2 style='font-size:16px; font-weight:700; color:#111827; "
        f"margin:26px 0 8px; padding-bottom:6px; "
        f"border-bottom:2px solid {COLOR_RESULTS_HEADER}; "
        f"text-transform:uppercase; letter-spacing:0.05em;'>"
        f"Deep analysis</h2>"
        f"{analysis_body_html}"
    )

    footer = (
        f"<div style='margin-top:34px; padding-top:12px; "
        f"border-top:1px solid #E5E7EB; color:#6B7280; font-size:10px; "
        f"line-height:1.5;'>"
        f"Prepared by Singapore Slinger, an automated SGX results "
        f"analyst. Not investment advice. Verify figures against the "
        f"source filing on links.sgx.com before acting on any number "
        f"in this document."
        f"</div>"
    )

    doc_title = f"{ticker} {period} SGX analysis" if ticker else "SGX analysis"

    return (
        "<!doctype html>"
        "<html lang='en'>"
        "<head>"
        "<meta charset='utf-8'>"
        f"<title>{_esc(doc_title)}</title>"
        # A4 with running footer showing page numbers. Chrome honours
        # @page counter markers for print-to-PDF.
        "<style>"
        "@page { size: A4; margin: 16mm 14mm 18mm; "
        "@bottom-right { content: 'Page ' counter(page) ' of ' counter(pages); "
        "font: 9px/1 -apple-system, Segoe UI, Arial, sans-serif; color:#9CA3AF; } }"
        "body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }"
        "h2 { page-break-after: avoid; break-after: avoid; }"
        "</style>"
        "</head>"
        "<body style='margin:0; padding:0; background:#FFFFFF; "
        "color:#111827; font-family:-apple-system, BlinkMacSystemFont, "
        "Segoe UI, Roboto, Arial, sans-serif;'>"
        f"{cover}"
        "<div style='padding:18px 24px 24px;'>"
        f"{source_line}"
        f"{metric_block}"
        f"{summary_html}"
        f"{analysis_block}"
        f"{footer}"
        "</div>"
        "</body></html>"
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
