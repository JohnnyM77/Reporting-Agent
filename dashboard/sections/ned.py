"""Ned news card: Critical / High items, four across.

Pure rendering: takes the agent's docs/data JSON (already loaded) and
returns HTML. Reading and writing docs/data stays in scripts/build_dashboard.py."""

from __future__ import annotations

from dashboard.common import _esc, _fmt_date


# ---------------------------------------------------------------------------
# Ned section — Critical / High news, 4 cards across
# ---------------------------------------------------------------------------

_NED_LEVEL_COLOUR = {"CRITICAL": "#dc2626", "HIGH": "#f59e0b"}


def _ned_card(item: dict) -> str:
    level = str(item.get("level", "")).upper()
    colour = _NED_LEVEL_COLOUR.get(level, "#64748b")
    tickers = ", ".join(item.get("tickers", []) or [])
    title = _esc(str(item.get("title", "")))
    url = _esc(str(item.get("url", "")))
    source = _esc(str(item.get("source", "")))
    summary = _esc(str(item.get("summary", "")))
    title_html = (
        f"<a href='{url}' target='_blank' style='color:#e2e8f0;text-decoration:none'>{title}</a>"
        if url else f"<span style='color:#e2e8f0'>{title}</span>"
    )
    summary_html = (
        f"<div style='color:#94a3b8;font-size:12px;margin-top:6px;line-height:1.45'>{summary}</div>"
        if summary else ""
    )
    ticker_html = (
        f"<span style='color:#fbbf24;font-size:11px;font-weight:700'>{_esc(tickers)}</span>"
        if tickers else ""
    )
    return (
        "<div style='background:#0f172a;border:1px solid #334155;border-left:3px solid "
        f"{colour};border-radius:8px;padding:12px 14px;display:flex;flex-direction:column'>"
        "<div style='display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px'>"
        f"<span style='background:{colour};color:#0b1220;font-size:9px;font-weight:800;"
        f"padding:2px 7px;border-radius:5px;letter-spacing:0.5px'>{_esc(level)}</span>"
        f"{ticker_html}</div>"
        f"<div style='font-size:13px;font-weight:600;line-height:1.4'>{title_html}</div>"
        f"{summary_html}"
        f"<div style='color:#64748b;font-size:11px;margin-top:8px'>{source}</div>"
        "</div>"
    )


def _ned_section(data: dict) -> str:
    run_date = _fmt_date(data.get("last_run"))
    items = data.get("items", []) or []
    total_hits = data.get("total_hits", 0)

    # Number of distinct companies represented in the cards.
    companies = {
        (it.get("tickers") or ["—"])[0] for it in items
    }
    n_companies = len(companies)
    status_dot = "#f59e0b" if items else "#22c55e"

    if items:
        cards = "".join(_ned_card(it) for it in items)
        body = f"<div class='ned-grid'>{cards}</div>"
        headline = f"{len(items)} card(s) &middot; {n_companies} compan{'y' if n_companies == 1 else 'ies'}"
    else:
        body = (
            "<p style='color:#22c55e;font-size:13px;margin:0'>"
            "✓ No Critical or High-impact news this run"
            f"{f' ({total_hits} lower-priority mention(s))' if total_hits else ''}.</p>"
        )
        headline = "0 flagged"

    return f"""
    <div class="agent-card">
      <div class="card-header">
        <div>
          <span class="agent-name">Ned the News Agent</span>
          <span class="agent-role">Media Scanner &middot; Critical &amp; High impact, up to 2 per company (daily)</span>
        </div>
        <div style="text-align:right">
          <div><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{status_dot};margin-right:6px"></span><span style="font-size:13px;color:#e2e8f0">{headline}</span></div>
          <div style="font-size:12px;color:#64748b;margin-top:4px">Last run: {run_date}</div>
        </div>
      </div>
      {body}
    </div>"""
