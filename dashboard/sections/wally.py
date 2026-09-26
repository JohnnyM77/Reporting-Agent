"""Wally the Watcher card.

Pure rendering: takes the agent's docs/data JSON (already loaded) and
returns HTML. Reading and writing docs/data stays in scripts/build_dashboard.py."""

from __future__ import annotations

from dashboard.common import REPO_ROOT, _fmt_date, _pct_bar


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


def _fmt_num(x, pct=False, dp=2):
    if not isinstance(x, (int, float)):
        return "n/a"
    return f"{x*100:.1f}%" if pct else f"{x:.{dp}f}"


def _shortlist_block(data: dict) -> str:
    """Wally's Shortlist — the weekly valuation-aware top picks, at the top of the card."""
    sl = data.get("shortlist")
    if not sl:
        return ""
    picks = sl.get("picks", [])
    note = sl.get("note", "")
    if not picks:
        msg = note or "Nothing compelling this week."
        return (
            "<div style='background:#0b1220;border:1px solid #1e3a5f;border-radius:8px;"
            "padding:14px 16px;margin-bottom:18px'>"
            "<div style='font-size:13px;font-weight:700;color:#38bdf8;letter-spacing:0.3px;"
            "text-transform:uppercase;margin-bottom:6px'>Wally's Shortlist</div>"
            f"<p style='color:#22c55e;font-size:13px;margin:0'>✓ {msg}</p></div>"
        )
    cards = ""
    for p in picks:
        vpe = p.get("forward_pe") or p.get("trailing_pe")
        metrics = " · ".join(filter(None, [
            f"${_fmt_num(p.get('current_price'))}",
            f"PE {_fmt_num(vpe, dp=0)}" if isinstance(vpe, (int, float)) else None,
            f"EV/EBITDA {_fmt_num(p.get('ev_ebitda'), dp=0)}" if isinstance(p.get('ev_ebitda'), (int, float)) else None,
            f"FCF {_fmt_num(p.get('fcf_yield'), pct=True)}" if isinstance(p.get('fcf_yield'), (int, float)) else None,
            f"Yld {_fmt_num(p.get('dividend_yield'), pct=True)}" if isinstance(p.get('dividend_yield'), (int, float)) else None,
        ]))
        cards += (
            "<div style='background:#0f172a;border:1px solid #334155;border-left:3px solid #38bdf8;"
            "border-radius:6px;padding:11px 13px;margin-bottom:8px'>"
            "<div style='display:flex;justify-content:space-between;align-items:baseline;gap:8px'>"
            f"<span style='font-weight:700;color:#e2e8f0;font-size:14px'>{p.get('rank','')}. "
            f"<span style='color:#fbbf24'>{p.get('ticker','')}</span> "
            f"<span style='color:#94a3b8;font-weight:400;font-size:12px'>{str(p.get('company_name',''))[:32]}</span></span>"
            f"<span style='background:#1e3a5f;color:#7dd3fc;font-size:10px;padding:2px 8px;border-radius:10px;"
            f"white-space:nowrap'>{p.get('verdict','')}</span></div>"
            f"<div style='color:#cbd5e1;font-style:italic;font-size:12px;margin:5px 0'>{p.get('one_liner','')}</div>"
            f"<div style='color:#64748b;font-size:11px;margin-bottom:6px'>{metrics} · <span style='color:#475569'>{p.get('watchlist','')}</span></div>"
            f"<div style='color:#cbd5e1;font-size:12px'>{p.get('thesis','')}</div>"
            f"<div style='color:#4ade80;font-size:12px;margin-top:4px'><strong>Why now:</strong> {p.get('why_now','')}</div>"
            f"<div style='color:#fb923c;font-size:12px;margin-top:2px'><strong>Risk:</strong> {p.get('key_risk','')}</div>"
            "</div>"
        )
    return (
        "<div style='background:#0b1220;border:1px solid #1e3a5f;border-radius:8px;"
        "padding:14px 16px;margin-bottom:18px'>"
        "<div style='font-size:13px;font-weight:700;color:#38bdf8;letter-spacing:0.3px;"
        f"text-transform:uppercase;margin-bottom:10px'>Wally's Shortlist &middot; {len(picks)} to look at</div>"
        f"{cards}"
        f"<p style='color:#475569;font-size:11px;margin:6px 0 0'>{note}</p></div>"
    )


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
            target = r.get("target_price")
            near_low = r.get("near_low", True)
            below_target = r.get("below_target", False)

            target_cell = (
                f"${target:.2f}" if isinstance(target, (int, float)) and target > 0 else "—"
            )

            triggers = []
            if near_low:
                triggers.append("52w low")
            if below_target:
                triggers.append("Buy price")
            trigger_txt = " + ".join(triggers) if triggers else "—"
            # Highlight a below-buy-price hit in green (a buying opportunity).
            trigger_colour = "22c55e" if below_target else "94a3b8"

            rows += (
                f"<tr>"
                f"<td><strong style='color:#fbbf24'>{r.get('ticker','')}</strong></td>"
                f"<td style='color:#cbd5e1'>{r.get('company_name','')[:35]}</td>"
                f"<td style='text-align:right;color:#e2e8f0'>${r.get('current_price',0):.2f}</td>"
                f"<td style='text-align:right;color:#94a3b8'>${r.get('low_52w',0):.2f}</td>"
                f"<td style='text-align:right;color:#34d399'>{target_cell}</td>"
                f"<td style='text-align:right'>{_pct_bar(dist)} <span style='font-size:11px;color:#{'22c55e' if dist<=3 else 'f59e0b' if dist<=7 else 'ef4444'}'>{dist:.1f}%</span></td>"
                f"<td style='text-align:right;color:#{trigger_colour};font-size:12px'>{trigger_txt}</td>"
                f"</tr>"
            )

        wl_blocks += f"""
        <div style="margin-bottom:20px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <h4 style="color:#94a3b8;margin:0;font-size:14px">{wl_name}</h4>
            <span style="font-size:12px;color:#{'f59e0b' if flagged_count else '22c55e'}">{flagged_count}/{total} flagged</span>
          </div>
          {"<table style='width:100%;border-collapse:collapse;font-size:13px'><tr style='color:#64748b;font-size:11px'><th style='text-align:left'>Ticker</th><th style='text-align:left'>Name</th><th style='text-align:right'>Price</th><th style='text-align:right'>52W Low</th><th style='text-align:right'>Buy Price</th><th>% Above Low</th><th style='text-align:right'>Trigger</th></tr>" + rows + "</table>" if flagged else "<p style='color:#22c55e;font-size:13px;margin:0'>✓ No stocks near 52-week low or below buy price</p>"}
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
      {_shortlist_block(data)}
      {wl_blocks}
    </div>"""
