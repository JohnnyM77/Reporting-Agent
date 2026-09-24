"""Singapore Slinger (SGX) card.

Pure rendering: takes the agent's docs/data JSON (already loaded) and
returns HTML. Reading and writing docs/data stays in scripts/build_dashboard.py."""

from __future__ import annotations

from dashboard.common import _esc, _fmt_date
from dashboard.sections.bob import _BADGE_COLOURS, _render_analysis_sections


# ---------------------------------------------------------------------------
# Singapore Slinger section (SGX)
# ---------------------------------------------------------------------------

def _slinger_hi_item_card(item: dict) -> str:
    """Same shape as Bob's HI card, but with the SGX-specific extras --
    source PDF link list (Slinger doesn't attach them to the email) and
    an issuer_name subtitle. Currency labels come out as S$ because
    Slinger's LLM prompt tells the model to prefix figures that way,
    so no per-currency handling is needed here."""
    ticker = _esc(item.get("ticker", ""))
    title  = _esc(item.get("title", "")[:120])
    url    = item.get("url", "")
    itype  = item.get("type", "")
    badge_bg = _BADGE_COLOURS.get(itype, "#64748b")
    type_label = itype.replace("_", " ").upper() if itype else "HIGH IMPACT"
    analysis = item.get("analysis")
    issuer = _esc(item.get("issuer_name", ""))

    subtitle = (
        f"<div style='color:#94a3b8;font-size:11px;margin-top:2px'>{issuer}</div>"
        if issuer else ""
    )

    header = (
        f"<div style='background:#1a2540;padding:10px 14px;display:flex;"
        f"justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px'>"
        f"<div style='display:flex;flex-direction:column;min-width:0'>"
        f"<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap'>"
        f"<strong style='color:#fbbf24;font-size:14px'>{ticker}</strong>"
        f"<span style='background:{badge_bg};color:#fff;padding:1px 7px;border-radius:3px;"
        f"font-size:10px;white-space:nowrap'>{type_label}</span>"
        f"<span style='color:#cbd5e1;font-size:12px'>{title}</span>"
        f"</div>"
        f"{subtitle}"
        f"</div>"
        f"<div style='display:flex;align-items:center;gap:10px;white-space:nowrap'>"
        f"<a href='{url}' target='_blank' style='color:#60a5fa;font-size:12px'>Open ↗</a>"
        f"</div>"
        f"</div>"
    )

    source_pdfs = item.get("source_pdfs") or []
    pdf_links_html = ""
    if source_pdfs:
        pdf_items = "".join(
            f"<li style='margin:2px 0'>"
            f"<a href='{_esc(p.get('url',''))}' target='_blank' "
            f"style='color:#60a5fa;font-size:12px'>{_esc(p.get('name',''))} ↗</a>"
            f"</li>"
            for p in source_pdfs
        )
        pdf_links_html = (
            f"<div style='padding:8px 14px 0;border-top:1px solid #334155'>"
            f"<div style='color:#64748b;font-size:10px;text-transform:uppercase;"
            f"letter-spacing:0.5px;margin-bottom:4px'>Source PDFs (SGX)</div>"
            f"<ul style='margin:0 0 0 16px;padding:0;color:#cbd5e1;font-size:12px'>"
            f"{pdf_items}</ul></div>"
        )

    if analysis:
        sections_html = _render_analysis_sections(analysis, itype)
        analysis_block = (
            f"<details open style='border-top:1px solid #334155'>"
            f"<summary style='cursor:pointer;padding:7px 14px;color:#94a3b8;font-size:11px;"
            f"list-style:none;user-select:none'>▸ Analysis</summary>"
            f"{sections_html}"
            f"</details>"
        )
    else:
        analysis_block = ""

    return (
        f"<div style='border:1px solid #334155;border-radius:8px;overflow:hidden;margin:10px 0'>"
        f"{header}{analysis_block}{pdf_links_html}"
        f"</div>"
    )


def _slinger_plain_row(item: dict) -> str:
    """Compact material/fyi row for Slinger. Its items carry only
    ticker+title+url (no analysis.what_happened / so_what like Bob's),
    so the shape is deliberately plainer than Bob's `_mat_item_row`."""
    return (
        f"<tr>"
        f"<td><strong style='color:#60a5fa'>{_esc(item.get('ticker',''))}</strong></td>"
        f"<td style='color:#e2e8f0;font-size:12px'>{_esc(item.get('title','')[:120])}</td>"
        f"<td><a href='{item.get('url','')}' target='_blank' "
        f"style='color:#60a5fa;font-size:11px'>Open ↗</a></td>"
        f"</tr>"
    )


def _slinger_run_blocks(data: dict) -> str:
    """The high-impact / material / FYI blocks for one Slinger run.

    Pulled out of ``_slinger_section`` so the same rendering serves
    both the current digest and the retained previous one (mirrors
    Bob's ``_bob_run_blocks``)."""
    hi = data.get("high_impact", [])
    mat = data.get("material", [])
    fyi = data.get("fyi", [])

    hi_cards = "".join(_slinger_hi_item_card(item) for item in hi)

    _no_hi   = "<p style='color:#64748b;font-size:13px'>No high-impact announcements</p>"
    _no_mat  = "<tr><td style='color:#64748b;padding:6px 0'>No material announcements</td></tr>"
    _no_fyi  = "<tr><td style='color:#64748b;padding:6px 0'>No announcements today</td></tr>"

    hi_block = (
        f"<h4 style='color:#fbbf24;margin:16px 0 6px'>⚡ HIGH IMPACT ({len(hi)})</h4>"
        + (hi_cards if hi_cards else _no_hi)
    ) if hi else ""

    mat_rows = "".join(_slinger_plain_row(m) for m in mat[:10])
    if len(mat) > 10:
        mat_rows += (
            f"<tr><td colspan='3' style='color:#64748b;font-size:11px;padding:6px 0'>"
            f"… and {len(mat)-10} more material items</td></tr>"
        )
    mat_block = (
        f"<h4 style='color:#3b82f6;margin:16px 0 6px'>📌 MATERIAL ({len(mat)})</h4>"
        f"<table style='width:100%;border-collapse:collapse;font-size:13px'>"
        + (mat_rows if mat_rows else _no_mat)
        + "</table>"
    ) if mat else ""

    fyi_rows = "".join(_slinger_plain_row(f) for f in fyi[:15])
    if len(fyi) > 15:
        fyi_rows += (
            f"<tr><td colspan='3' style='color:#64748b;font-size:11px'>"
            f"… and {len(fyi)-15} more FYI items</td></tr>"
        )
    fyi_block = (
        f"<h4 style='color:#10b981;margin:16px 0 6px'>📋 FYI — ALL ANNOUNCEMENTS ({len(fyi)})</h4>"
        f"<table style='width:100%;border-collapse:collapse;font-size:13px'>"
        + (fyi_rows if fyi_rows else _no_fyi)
        + "</table>"
    )

    return hi_block + mat_block + fyi_block


def _slinger_section(data: dict, history: list[dict] | None = None) -> str:
    """Slinger's card: the current digest, plus the previous run kept
    collapsed below it (mirrors ``_bob_section``). ``history`` is the
    rolling list written by ``_update_slinger_history`` -- newest
    first, current run at index 0, at most two entries.

    SGX badge in the header so it does not get mistaken for Bob's ASX
    card at a glance."""
    if not data:
        return ""

    run_date = _fmt_date(data.get("last_run"))
    hi = data.get("high_impact", [])
    silence = data.get("silence", False)

    status_dot = "#ef4444" if hi else "#22c55e"
    status_text = f"{len(hi)} HIGH IMPACT" if hi else ("SILENCE" if silence else "All clear")

    current_blocks = _slinger_run_blocks(data)

    previous_html = ""
    prior = (history or [])[1:2]
    if prior:
        prev = prior[0]
        prev_date = _fmt_date(prev.get("last_run"))
        prev_hi = len(prev.get("high_impact", []))
        prev_mat = len(prev.get("material", []))
        prev_fyi = len(prev.get("fyi", []))
        previous_html = (
            "<details style='margin-top:20px;border-top:1px solid #334155;padding-top:12px'>"
            "<summary style='cursor:pointer;color:#94a3b8;font-size:13px;font-weight:600'>"
            f"◷ Previous run — {prev_date} "
            f"<span style='color:#64748b;font-weight:400'>"
            f"({prev_hi} high-impact · {prev_mat} material · {prev_fyi} FYI)</span>"
            "</summary>"
            "<div style='margin-top:10px;opacity:0.85'>"
            + _slinger_run_blocks(prev)
            + "</div></details>"
        )

    return f"""
    <div class="agent-card">
      <div class="card-header">
        <div>
          <span class="agent-name">Singapore Slinger
            <span style="display:inline-block;margin-left:6px;padding:2px 8px;background:#ef4444;color:#fff;font-size:11px;border-radius:4px;vertical-align:middle;letter-spacing:0.5px">SGX</span>
          </span>
          <span class="agent-role">Daily SGX Digest &middot; last two runs kept</span>
        </div>
        <div style="text-align:right">
          <div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{status_dot};margin-right:6px"></span><span style="font-size:13px;color:#e2e8f0">{status_text}</span></div>
          <div style="font-size:12px;color:#64748b;margin-top:4px">Last run: {run_date}</div>
        </div>
      </div>
      {current_blocks}
      {previous_html}
    </div>"""
