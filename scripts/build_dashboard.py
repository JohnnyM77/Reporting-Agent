#!/usr/bin/env python3
"""
build_dashboard.py — generates docs/index.html from the three agent data JSON files.
Run from repo root: python scripts/build_dashboard.py
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
DATA_DIR = DOCS_DIR / "data"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(name: str) -> dict:
    path = DATA_DIR / name
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "Never"
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return d.strftime("%-d %b %Y")
    except Exception:
        return iso


def _pct_bar(pct: float, max_pct: float = 10.0, good_direction: str = "low") -> str:
    """Mini HTML bar showing how far a stock is from its low/high."""
    clamped = min(max(pct, 0), max_pct)
    width = int((clamped / max_pct) * 100)
    colour = "#22c55e" if good_direction == "low" and pct < 3 else "#f59e0b" if pct < 7 else "#ef4444"
    return (
        f"<div style='background:#1e293b;border-radius:3px;height:8px;width:80px;display:inline-block;vertical-align:middle'>"
        f"<div style='background:{colour};height:8px;border-radius:3px;width:{width}%'></div></div>"
    )


def _tier_badge(tier: str) -> str:
    colours = {
        "Tier 1: Watch":        "#3b82f6",
        "Tier 2: Review":       "#f59e0b",
        "Tier 3: Deep Review":  "#ef4444",
    }
    bg = colours.get(tier, "#64748b")
    short = tier.replace("Tier 1: ", "T1 ").replace("Tier 2: ", "T2 ").replace("Tier 3: ", "T3 ")
    return f"<span style='background:{bg};color:#fff;padding:2px 6px;border-radius:4px;font-size:11px'>{short}</span>"


# ---------------------------------------------------------------------------
# Bob section — analysis card helpers
# ---------------------------------------------------------------------------

_BADGE_COLOURS = {
    "results":        "#f59e0b",
    "acquisition":    "#8b5cf6",
    "capital":        "#3b82f6",
    "trading_update": "#06b6d4",
    "price_sensitive":"#ef4444",
}

def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _section_cell(label: str, value) -> str:
    if not value:
        return ""
    if isinstance(value, list):
        items = "".join(f"<li style='margin:2px 0'>{_esc(str(v))}</li>" for v in value if v)
        body = f"<ul style='margin:4px 0 0 14px;padding:0'>{items}</ul>"
    elif isinstance(value, dict):
        row_parts = []
        for k, v in value.items():
            if not v:
                continue
            label = _esc(k.replace("_", " ").title())
            if isinstance(v, list):
                items_html = "".join(f"<li style='margin:1px 0'>{_esc(str(i))}</li>" for i in v if i)
                row_parts.append(
                    f"<div style='margin:2px 0'><span style='color:#64748b'>{label}:</span>"
                    f"<ul style='margin:2px 0 0 14px;padding:0'>{items_html}</ul></div>"
                )
            else:
                row_parts.append(
                    f"<div style='margin:2px 0'><span style='color:#64748b'>{label}:</span> {_esc(str(v))}</div>"
                )
        body = f"<div style='margin-top:4px'>{''.join(row_parts)}</div>"
    else:
        body = f"<div style='margin-top:4px'>{_esc(str(value))}</div>"
    return (
        f"<div style='background:#0f172a;border-radius:6px;padding:10px 12px;min-width:0'>"
        f"<div style='color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:2px'>{_esc(label)}</div>"
        f"<div style='color:#e2e8f0;font-size:12px;line-height:1.5'>{body}</div>"
        f"</div>"
    )


# Results cards carry the current five-metric schema first; the long-form
# fields below it are kept so historical bob.json entries still render.
_RESULTS_FIELDS = [
    ("summary",           "Summary"),
    ("metrics",           "Key Metrics"),
    ("executive_summary", "Executive Summary"),
    ("key_numbers",       "Key Numbers"),
    ("quality_of_earnings","Quality of Earnings"),
    ("management_framing","Management Framing"),
    ("positives",         "Positives"),
    ("negatives",         "Negatives / Red Flags"),
    ("bottom_line",       "Bottom Line"),
]

_METRIC_LABELS = [
    ("revenue",             "Revenue"),
    ("underlying_npat",     "Underlying NPAT"),
    ("underlying_eps",      "Underlying EPS"),
    ("dividend_ordinary",   "Ordinary dividend"),
    ("operating_cash_flow", "Operating cash flow"),
]


def _flatten_metrics(metrics: dict) -> dict:
    """Collapse {"revenue": {"value": .., "change_pct": ..}} into a flat
    label -> "value  change" map that _section_cell can render."""
    flat = {}
    for key, label in _METRIC_LABELS:
        entry = metrics.get(key)
        if isinstance(entry, dict):
            value = str(entry.get("value", "") or "n/a")
            change = str(entry.get("change_pct", entry.get("change", "")) or "")
            flat[label] = f"{value}  {change}".strip()
        elif entry:
            flat[label] = str(entry)
    return flat
_ACQUISITION_FIELDS = [
    ("deal_summary",        "Deal Summary"),
    ("what_they_bought",    "What They Bought"),
    ("price_check",         "Price Check"),
    ("strategic_fit",       "Strategic Fit"),
    ("integration_risk",    "Integration Risk"),
    ("balance_sheet_impact","Balance Sheet"),
    ("red_flags",           "Red Flags"),
    ("bottom_line",         "Bottom Line"),
]
_CAPITAL_FIELDS = [
    ("what_happened",       "What Happened"),
    ("fairness_signaling",  "Fairness & Signaling"),
    ("balance_sheet_impact","Balance Sheet Impact"),
    ("why_now",             "Why Now"),
    ("dilution_math",       "Dilution Math"),
    ("disclosure_quality",  "Disclosure Quality"),
    ("bottom_line",         "Bottom Line"),
    ("key_questions",       "Key Questions"),
]
_TRADING_FIELDS = [
    ("what_they_said",    "What They Said"),
    ("vs_prior_guidance", "vs Prior Guidance"),
    ("the_numbers",       "The Numbers"),
    ("why_happening",     "Why Happening"),
    ("balance_sheet",     "Balance Sheet"),
    ("red_flags",         "Red Flags"),
    ("bottom_line",       "Bottom Line"),
    ("key_questions",     "Key Questions"),
]
_PRICE_SENSITIVE_FIELDS = [
    ("what_happened",       "What Happened"),
    ("why_price_sensitive", "Why Price Sensitive"),
    ("numbers_materiality", "Numbers & Materiality"),
    ("impact_on_thesis",    "Impact on Thesis"),
    ("risks_questions",     "Risks / Questions"),
    ("bottom_line",         "Bottom Line"),
]
_DEFAULT_FIELDS = [
    ("what_happened", "What Happened"),
    ("so_what",       "So What"),
]

_FIELD_MAP = {
    "results":        _RESULTS_FIELDS,
    "acquisition":    _ACQUISITION_FIELDS,
    "capital":        _CAPITAL_FIELDS,
    "trading_update": _TRADING_FIELDS,
    "price_sensitive":_PRICE_SENSITIVE_FIELDS,
}


def _render_analysis_sections(analysis: dict, kind: str) -> str:
    fields = _FIELD_MAP.get(kind, _DEFAULT_FIELDS)

    def _value(key):
        raw = analysis.get(key)
        if key == "metrics" and isinstance(raw, dict):
            return _flatten_metrics(raw)
        return raw

    cells = "".join(_section_cell(label, _value(key)) for key, label in fields)
    return (
        f"<div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));"
        f"gap:8px;padding:12px 14px 14px'>{cells}</div>"
    )


def _hi_item_card(item: dict) -> str:
    ticker = _esc(item.get("ticker", ""))
    title  = _esc(item.get("title", "")[:120])
    url    = item.get("url", "")
    itype  = item.get("type", "")
    badge_bg = _BADGE_COLOURS.get(itype, "#64748b")
    type_label = itype.replace("_", " ").upper() if itype else "HIGH IMPACT"
    analysis = item.get("analysis")

    header = (
        f"<div style='background:#1a2540;padding:10px 14px;display:flex;"
        f"justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px'>"
        f"<div style='display:flex;align-items:center;gap:8px;flex-wrap:wrap'>"
        f"<strong style='color:#fbbf24;font-size:14px'>{ticker}</strong>"
        f"<span style='background:{badge_bg};color:#fff;padding:1px 7px;border-radius:3px;"
        f"font-size:10px;white-space:nowrap'>{type_label}</span>"
        f"<span style='color:#cbd5e1;font-size:12px'>{title}</span>"
        f"</div>"
        f"<div style='display:flex;align-items:center;gap:10px;white-space:nowrap'>"
        + (
            f"<a href='{item.get('doc_link')}' target='_blank' style='color:#34d399;"
            f"font-size:12px'>Full analysis ↗</a>" if item.get("doc_link") else ""
        )
        + f"<a href='{url}' target='_blank' style='color:#60a5fa;font-size:12px'>Open ↗</a>"
        f"</div>"
        f"</div>"
    )

    if analysis:
        sections_html = _render_analysis_sections(analysis, itype)
        body = (
            f"<details open style='border-top:1px solid #334155'>"
            f"<summary style='cursor:pointer;padding:7px 14px;color:#94a3b8;font-size:11px;"
            f"list-style:none;user-select:none'>▸ Analysis</summary>"
            f"{sections_html}"
            f"</details>"
        )
    else:
        body = ""

    return (
        f"<div style='border:1px solid #334155;border-radius:8px;overflow:hidden;margin:10px 0'>"
        f"{header}{body}"
        f"</div>"
    )


def _mat_item_row(item: dict) -> str:
    ticker = _esc(item.get("ticker", ""))
    title  = _esc(item.get("title", "")[:120])
    url    = item.get("url", "")
    analysis = item.get("analysis", {})
    what = _esc(str(analysis.get("what_happened", title)))
    so_what = _esc(str(analysis.get("so_what", "")))
    detail = (
        f"<div style='color:#e2e8f0'>{what}</div>"
        f"<div style='color:#94a3b8;font-size:11px;margin-top:3px'>→ {so_what}</div>"
        if so_what else f"<div style='color:#e2e8f0'>{what}</div>"
    )
    return (
        f"<tr>"
        f"<td style='white-space:nowrap'><strong style='color:#60a5fa'>{ticker}</strong></td>"
        f"<td>{detail}</td>"
        f"<td style='white-space:nowrap'>"
        f"<a href='{url}' target='_blank' style='color:#60a5fa;font-size:11px'>Open ↗</a>"
        f"</td>"
        f"</tr>"
    )


# ---------------------------------------------------------------------------
# Bob section
# ---------------------------------------------------------------------------

def _bob_section(data: dict) -> str:
    run_date = _fmt_date(data.get("last_run"))
    hi = data.get("high_impact", [])
    mat = data.get("material", [])
    fyi = data.get("fyi", [])
    silence = data.get("silence", False)

    status_dot = "#ef4444" if hi else "#22c55e"
    status_text = f"{len(hi)} HIGH IMPACT" if hi else ("SILENCE" if silence else "All clear")

    hi_cards = "".join(_hi_item_card(item) for item in hi)

    mat_rows = "".join(_mat_item_row(item) for item in mat[:10])
    if len(mat) > 10:
        mat_rows += (
            f"<tr><td colspan='3' style='color:#64748b;font-size:11px;padding:6px 0'>"
            f"… and {len(mat)-10} more material items</td></tr>"
        )

    fyi_rows = "".join(
        f"<tr>"
        f"<td><strong style='color:#94a3b8'>{_esc(item.get('ticker',''))}</strong></td>"
        f"<td style='color:#94a3b8;font-size:12px'>{_esc(item.get('title','')[:100])}</td>"
        f"<td><a href='{item.get('url','')}' target='_blank' style='color:#60a5fa;font-size:11px'>Open</a></td>"
        f"</tr>"
        for item in fyi[:15]
    )
    if len(fyi) > 15:
        fyi_rows += (
            f"<tr><td colspan='3' style='color:#64748b;font-size:11px'>"
            f"… and {len(fyi)-15} more FYI items</td></tr>"
        )

    _no_hi   = "<p style='color:#64748b;font-size:13px'>No high-impact announcements</p>"
    _no_mat  = "<tr><td style='color:#64748b;padding:6px 0'>No material announcements</td></tr>"
    _no_fyi  = "<tr><td style='color:#64748b;padding:6px 0'>No announcements today</td></tr>"

    hi_block = (
        f"<h4 style='color:#fbbf24;margin:16px 0 6px'>⚡ HIGH IMPACT ({len(hi)})</h4>"
        + (hi_cards if hi_cards else _no_hi)
    ) if hi else ""

    mat_block = (
        f"<h4 style='color:#3b82f6;margin:16px 0 6px'>📌 MATERIAL ({len(mat)})</h4>"
        f"<table style='width:100%;border-collapse:collapse;font-size:13px'>"
        + (mat_rows if mat_rows else _no_mat)
        + "</table>"
    ) if mat else ""

    fyi_block = (
        f"<h4 style='color:#10b981;margin:16px 0 6px'>📋 FYI — ALL ANNOUNCEMENTS ({len(fyi)})</h4>"
        f"<table style='width:100%;border-collapse:collapse;font-size:13px'>"
        + (fyi_rows if fyi_rows else _no_fyi)
        + "</table>"
    )

    return f"""
    <div class="agent-card">
      <div class="card-header">
        <div>
          <span class="agent-name">Bob the Bot</span>
          <span class="agent-role">Daily ASX Digest</span>
        </div>
        <div style="text-align:right">
          <div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{status_dot};margin-right:6px"></span><span style="font-size:13px;color:#e2e8f0">{status_text}</span></div>
          <div style="font-size:12px;color:#64748b;margin-top:4px">Last run: {run_date}</div>
        </div>
      </div>
      {hi_block}
      {mat_block}
      {fyi_block}
    </div>"""


# ---------------------------------------------------------------------------
# Wally section
# ---------------------------------------------------------------------------

def _live_watchlist_names() -> set[str] | None:
    """Names declared by the YAML files that currently exist.

    Wally merges each run into wally.json keyed by watchlist name and never
    prunes, so deleting a watchlist YAML leaves its last result in the JSON
    forever — a "Test" watchlist outlived its file and kept rendering. Filter
    on the source of truth instead. Returns None if the directory cannot be
    read, in which case nothing is filtered.
    """
    directory = REPO_ROOT / "watchlists"
    if not directory.is_dir():
        return None
    names: set[str] = set()
    for path in directory.glob("*.yaml"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                names.add(line.split(":", 1)[1].strip().strip("'\""))
                break
    return names or None


def _wally_section(data: dict) -> str:
    run_date = _fmt_date(data.get("last_run"))
    watchlists = data.get("watchlists", {})

    live = _live_watchlist_names()
    if live is not None:
        watchlists = {k: v for k, v in watchlists.items() if k in live}

    total_flagged = sum(wl.get("flagged_count", 0) for wl in watchlists.values())
    status_dot = "#f59e0b" if total_flagged > 0 else "#22c55e"

    wl_blocks = ""
    for wl_name, wl_data in watchlists.items():
        flagged = wl_data.get("flagged", [])
        total = wl_data.get("total", 0)
        flagged_count = wl_data.get("flagged_count", len(flagged))
        wl_run = _fmt_date(wl_data.get("run_timestamp"))

        rows = ""
        for r in flagged:
            dist = r.get("distance_to_low_pct", 0)
            below = r.get("below_high_pct", 0)
            rows += (
                f"<tr>"
                f"<td><strong style='color:#fbbf24'>{r.get('ticker','')}</strong></td>"
                f"<td style='color:#cbd5e1'>{r.get('company_name','')[:35]}</td>"
                f"<td style='text-align:right;color:#e2e8f0'>${r.get('current_price',0):.2f}</td>"
                f"<td style='text-align:right;color:#94a3b8'>${r.get('low_52w',0):.2f}</td>"
                f"<td style='text-align:right'>{_pct_bar(dist)} <span style='font-size:11px;color:#{'22c55e' if dist<=3 else 'f59e0b' if dist<=7 else 'ef4444'}'>{dist:.1f}%</span></td>"
                f"<td style='text-align:right;color:#64748b;font-size:12px'>{below:.1f}%↓high</td>"
                f"</tr>"
            )

        wl_blocks += f"""
        <div style="margin-bottom:20px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <h4 style="color:#94a3b8;margin:0;font-size:14px">{wl_name}</h4>
            <span style="font-size:12px;color:#{'f59e0b' if flagged_count else '22c55e'}">{flagged_count}/{total} flagged</span>
          </div>
          {"<table style='width:100%;border-collapse:collapse;font-size:13px'><tr style='color:#64748b;font-size:11px'><th style='text-align:left'>Ticker</th><th style='text-align:left'>Name</th><th style='text-align:right'>Price</th><th style='text-align:right'>52W Low</th><th>% Above Low</th><th style='text-align:right'>vs High</th></tr>" + rows + "</table>" if flagged else "<p style='color:#22c55e;font-size:13px;margin:0'>✓ No stocks near 52-week low</p>"}
        </div>"""

    if not watchlists:
        wl_blocks = "<p style='color:#64748b'>No watchlist data yet</p>"

    return f"""
    <div class="agent-card">
      <div class="card-header">
        <div>
          <span class="agent-name">Wally the Watcher</span>
          <span class="agent-role">Watchlist Low-Screen (Tue/Fri)</span>
        </div>
        <div style="text-align:right">
          <div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{status_dot};margin-right:6px"></span><span style="font-size:13px;color:#e2e8f0">{total_flagged} ticker(s) flagged</span></div>
          <div style="font-size:12px;color:#64748b;margin-top:4px">Last run: {run_date}</div>
        </div>
      </div>
      {wl_blocks}
    </div>"""


# ---------------------------------------------------------------------------
# Sally section
# ---------------------------------------------------------------------------

def _sally_section(data: dict) -> str:
    run_date = _fmt_date(data.get("last_run"))
    flagged = data.get("flagged", [])
    flagged_count = data.get("flagged_count", len(flagged))
    portfolio_size = data.get("portfolio_size", 0)

    status_dot = "#ef4444" if flagged_count >= 3 else "#f59e0b" if flagged_count > 0 else "#22c55e"

    rows = ""
    for r in flagged:
        dist = r.get("distance_to_high_pct", 0)
        pe = r.get("trailing_pe")
        pe_str = f"{pe:.1f}x" if pe else "—"
        fwd_pe = r.get("forward_pe")
        fwd_pe_str = f"{fwd_pe:.1f}x" if fwd_pe else "—"
        div = r.get("dividend_yield")
        div_str = f"{div:.1f}%" if div else "—"
        pct = r.get("valuation_percentile")
        pct_str = f"{pct*100:.0f}th pct" if pct else "—"
        rows += (
            f"<tr>"
            f"<td><strong style='color:#fbbf24'>{r.get('ticker','')}</strong></td>"
            f"<td style='color:#cbd5e1;font-size:12px'>{r.get('company_name','')[:30]}</td>"
            f"<td style='text-align:right;color:#e2e8f0'>${r.get('current_price',0):.2f}</td>"
            f"<td style='text-align:right;color:#94a3b8;font-size:12px'>{dist:.1f}% ↓</td>"
            f"<td style='text-align:right;color:#94a3b8;font-size:12px'>{pe_str} / {fwd_pe_str}</td>"
            f"<td style='text-align:right;color:#94a3b8;font-size:12px'>{div_str}</td>"
            f"<td style='text-align:right;font-size:12px;color:#94a3b8'>{pct_str}</td>"
            f"<td>{_tier_badge(r.get('alert_tier',''))}</td>"
            f"<td style='color:#f59e0b;font-size:12px'>{r.get('sally_verdict','')}</td>"
            f"</tr>"
        )

    table = ""
    if flagged:
        table = f"""
        <table style='width:100%;border-collapse:collapse;font-size:13px;margin-top:12px'>
          <tr style='color:#64748b;font-size:11px'>
            <th style='text-align:left'>Ticker</th><th style='text-align:left'>Name</th>
            <th style='text-align:right'>Price</th><th style='text-align:right'>↓ 52W High</th>
            <th style='text-align:right'>PE TTM/Fwd</th><th style='text-align:right'>Div Yield</th>
            <th style='text-align:right'>Val Pct</th><th>Alert</th><th>Verdict</th>
          </tr>
          {rows}
        </table>"""
    else:
        table = "<p style='color:#22c55e;font-size:13px;margin-top:12px'>✓ No valuation stretch alerts this week</p>"

    return f"""
    <div class="agent-card">
      <div class="card-header">
        <div>
          <span class="agent-name">Selling Sally</span>
          <span class="agent-role">Weekly Valuation Review (Sunday)</span>
        </div>
        <div style="text-align:right">
          <div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{status_dot};margin-right:6px"></span><span style="font-size:13px;color:#e2e8f0">{flagged_count}/{portfolio_size} flagged</span></div>
          <div style="font-size:12px;color:#64748b;margin-top:4px">Last run: {run_date}</div>
        </div>
      </div>
      {table}
    </div>"""


# ---------------------------------------------------------------------------
# Theo section — thesis accountability
# ---------------------------------------------------------------------------

_VERDICT_COLOUR = {"DRIFTING": "#ef4444", "WATCH": "#f59e0b", "CLEAN": "#22c55e"}
_PILLAR_COLOUR = {"INTACT": "#22c55e", "STRAINED": "#f59e0b", "BREACHED": "#ef4444"}


def _theo_section(data: dict) -> str:
    """Theo on the dashboard is a glance, not the analysis.

    Drift verdicts, unanswered questions from the other agents, and holdings
    with capital and no written reason. The slides live at /theo/ — a page
    each, which is not a dashboard thing.
    """
    if not data:
        return ""

    run_date = _fmt_date(data.get("last_run"))
    rows_in = data.get("theses", [])
    questions = data.get("open_questions", [])
    no_thesis = data.get("no_thesis", [])
    drifting = data.get("drifting", 0)

    status_dot = (
        "#ef4444" if drifting or questions else "#f59e0b" if data.get("watch") else "#22c55e"
    )

    # --- open questions come first: they are the part that needs an answer
    questions_html = ""
    if questions:
        items = ""
        for q in questions:
            items += (
                f"<div style='border-left:3px solid #f59e0b;padding:8px 12px;margin-top:8px;background:#0f172a'>"
                f"<div><strong style='color:#fbbf24'>{_esc(q.get('ticker',''))}</strong>"
                f"<span style='color:#64748b;font-size:12px'> &middot; {_esc(q.get('source',''))}"
                f" &middot; {_esc(q.get('date') or '')}</span></div>"
                f"<div style='color:#94a3b8;font-size:12px;margin-top:3px'>{_esc(q.get('detail',''))}</div>"
                f"<div style='color:#e2e8f0;font-size:13px;margin-top:5px'>{_esc(q.get('question',''))}</div>"
                f"</div>"
            )
        questions_html = (
            "<div style='margin-top:12px'>"
            "<div style='color:#f59e0b;font-size:11px;font-weight:700;letter-spacing:0.08em;"
            "text-transform:uppercase'>Unanswered &mdash; silence is recorded</div>"
            f"{items}</div>"
        )

    # --- the thesis table
    table = ""
    if rows_in:
        rows = ""
        for r in rows_in:
            pills = "".join(
                f"<span style=\"color:{_PILLAR_COLOUR.get(p.get('status',''), '#64748b')}\" "
                f"title=\"{_esc(p.get('id',''))}: {_esc(p.get('status',''))}\">"
                f"{p.get('symbol','?')}</span>"
                for p in r.get("pillars", [])
            )
            irr = r.get("irr")
            irr_str = f"{irr * 100:.1f}%" if isinstance(irr, (int, float)) else "&mdash;"
            mult = r.get("multiple")
            mult_str = f"{mult:.2f}x" if isinstance(mult, (int, float)) else "&mdash;"
            verdict = r.get("verdict", "")
            draft = (
                " <span style='color:#64748b;font-size:10px;border:1px solid #475569;"
                "padding:1px 4px;border-radius:3px'>DRAFT</span>"
                if r.get("draft")
                else ""
            )
            rows += (
                f"<tr>"
                f"<td><strong style='color:#fbbf24'>{_esc(r.get('ticker',''))}</strong>{draft}</td>"
                f"<td style='color:#cbd5e1;font-size:12px'>"
                f"{_esc(r.get('archetype','').replace('_',' ').title())}</td>"
                f"<td style='letter-spacing:2px;font-size:14px'>{pills}</td>"
                f"<td style='text-align:center;color:#94a3b8;font-size:12px'>{_esc(r.get('grade',''))}</td>"
                f"<td style='text-align:right;color:#e2e8f0;font-size:12px'>{irr_str}</td>"
                f"<td style='text-align:right;color:#94a3b8;font-size:12px'>{mult_str}</td>"
                f"<td><span style='color:{_VERDICT_COLOUR.get(verdict, '#64748b')};font-size:11px;"
                f"font-weight:700;letter-spacing:0.06em'>{_esc(verdict)}</span></td>"
                f"</tr>"
            )
        table = f"""
        <table style='width:100%;border-collapse:collapse;font-size:13px;margin-top:12px'>
          <tr style='color:#64748b;font-size:11px'>
            <th style='text-align:left'>Ticker</th><th style='text-align:left'>Archetype</th>
            <th style='text-align:left'>Pillars</th><th style='text-align:center'>Grade</th>
            <th style='text-align:right'>IRR</th><th style='text-align:right'>Multiple</th>
            <th style='text-align:left'>Drift</th>
          </tr>
          {rows}
        </table>"""
    else:
        table = (
            "<p style='color:#64748b;font-size:13px;margin-top:12px'>"
            "No theses written yet.</p>"
        )

    gaps_html = ""
    if no_thesis:
        names = ", ".join(_esc(g.get("ticker", "")) for g in no_thesis)
        gaps_html = (
            "<div style='margin-top:14px;padding:8px 12px;background:#0f172a;"
            "border-left:3px solid #ef4444'>"
            "<span style='color:#f87171;font-size:12px;font-weight:600'>Flagged with no thesis: </span>"
            f"<span style='color:#e2e8f0;font-size:12px'>{names}</span>"
            "<div style='color:#64748b;font-size:11px;margin-top:3px'>"
            "Capital committed, a signal raised, and no written reason to own it.</div>"
            "</div>"
        )

    return f"""
    <div class="agent-card">
      <div class="card-header">
        <div>
          <span class="agent-name">Theo</span>
          <span class="agent-role">Thesis Accountability &middot; <a href="theo/" style="color:#3b82f6;text-decoration:none">slides &rarr;</a></span>
        </div>
        <div style="text-align:right">
          <div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{status_dot};margin-right:6px"></span><span style="font-size:13px;color:#e2e8f0">{len(questions)} open &middot; {drifting} drifting</span></div>
          <div style="font-size:12px;color:#64748b;margin-top:4px">Last run: {run_date}</div>
        </div>
      </div>
      {questions_html}
      {table}
      {gaps_html}
    </div>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_dashboard() -> None:
    bob = _load("bob.json")
    wally = _load("wally.json")
    sally = _load("sally.json")
    theo = _load("theo.json")

    generated_at = datetime.utcnow().strftime("%-d %b %Y %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Reporting Agent Dashboard</title>
  <link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🤖</text></svg>">
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0f172a;
      color: #e2e8f0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
      min-height: 100vh;
    }}
    header {{
      background: #1e293b;
      border-bottom: 1px solid #334155;
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 100;
    }}
    .logo {{
      font-size: 20px;
      font-weight: 700;
      color: #f1f5f9;
      letter-spacing: -0.3px;
    }}
    .logo span {{ color: #3b82f6; }}
    .meta {{
      font-size: 12px;
      color: #64748b;
    }}
    main {{
      max-width: 1100px;
      margin: 32px auto;
      padding: 0 20px;
      display: flex;
      flex-direction: column;
      gap: 24px;
    }}
    .agent-card {{
      background: #1e293b;
      border: 1px solid #334155;
      border-radius: 10px;
      padding: 20px 24px;
    }}
    .card-header {{
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      padding-bottom: 14px;
      border-bottom: 1px solid #334155;
      margin-bottom: 4px;
    }}
    .agent-name {{
      display: block;
      font-size: 18px;
      font-weight: 700;
      color: #f1f5f9;
    }}
    .agent-role {{
      display: block;
      font-size: 12px;
      color: #64748b;
      margin-top: 2px;
    }}
    table td, table th {{
      padding: 6px 8px;
      border-bottom: 1px solid #1e293b;
      vertical-align: middle;
    }}
    a {{ text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    footer {{
      text-align: center;
      color: #334155;
      font-size: 11px;
      padding: 32px 20px;
    }}
    @media (max-width: 700px) {{
      .card-header {{ flex-direction: column; gap: 8px; }}
      table {{ font-size: 11px; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="logo">🤖 <span>Reporting</span> Agent</div>
    <div class="meta">Auto-updated by GitHub Actions &nbsp;·&nbsp; Generated: {generated_at}</div>
  </header>
  <main>
    {_bob_section(bob)}
    {_wally_section(wally)}
    {_sally_section(sally)}
    {_theo_section(theo)}
  </main>
  <footer>
    Bob the Bot · Wally the Watcher · Selling Sally &nbsp;·&nbsp; JohnnyM77/Reporting-Agent
  </footer>
</body>
</html>"""

    out = DOCS_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"[dashboard] Written → {out}")


if __name__ == "__main__":
    build_dashboard()
