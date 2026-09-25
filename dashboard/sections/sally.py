"""Selling Sally card.

Pure rendering: takes the agent's docs/data JSON (already loaded) and
returns HTML. Reading and writing docs/data stays in scripts/build_dashboard.py."""

from __future__ import annotations

from dashboard.common import _fmt_date, _tier_badge


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
