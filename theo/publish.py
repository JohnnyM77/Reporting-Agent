"""Build the static site — one self-contained ``site/index.html``.

Every slide for every thesis is rendered at build time and embedded as JSON.
Switching company or version is an innerHTML swap; there is no framework, no
fetch and no CDN, so the page works from a file:// URL, from GitHub Pages, or
out of an email attachment, and it cannot break because something upstream
moved.

This deploys publicly, so dollar amounts are suppressed by default. IRRs,
multiples and historical share prices stay — they say how the decisions went
without saying how much money is involved.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Sequence

from . import render as render_mod
from . import season as season_mod
from .ledger import EMPTY, Ledger, gaps as ledger_gaps
from .render import DASH, fmt_mult, fmt_pct, fmt_years, fmt_money
from .thesis import Thesis

DEFAULT_OUT = Path("site")


def _index_row(thesis: Thesis, ledger: Ledger, today: dt.date) -> dict[str, Any]:
    verdict, _ = season_mod.assess(thesis, today)
    holding = ledger.get(thesis.ticker)
    return {
        "ticker": thesis.ticker,
        "name": thesis.name or thesis.ticker,
        "archetype": thesis.archetype or "—",
        "draft": thesis.draft,
        "grade": thesis.evidence_grade,
        "verdict": verdict,
        "irr": fmt_pct(holding.irr) if holding else DASH,
        "multiple": fmt_mult(holding.multiple) if holding else DASH,
        "years": fmt_years(holding.years) if holding else DASH,
        "pillars": [
            {"id": p.id, "status": p.status, "symbol": p.symbol} for p in thesis.pillars
        ],
    }


def _sort_key(row: dict[str, Any]) -> tuple[int, str]:
    order = {"DRIFTING": 0, "WATCH": 1, "CLEAN": 2}
    return (order.get(row["verdict"], 9), row["ticker"])


def build_payload(
    theses: Sequence[Thesis],
    ledger: Ledger,
    today: dt.date,
    show_amounts: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for thesis in theses:
        slides: dict[str, str] = {}
        versions: list[list[str]] = []
        for slug, label in thesis.versions():
            context = render_mod.slide_context(thesis, slug, ledger, today, show_amounts)
            slides[slug] = render_mod.render_fragment(context)
            versions.append([slug, label])
        payload[thesis.ticker] = {
            "name": thesis.name or thesis.ticker,
            "versions": versions,
            "slides": slides,
        }
    return payload


def build(
    theses: Sequence[Thesis],
    ledger: Ledger | None = None,
    out_dir: str | Path = DEFAULT_OUT,
    today: dt.date | None = None,
    show_amounts: bool = False,
) -> Path:
    today = today or dt.date.today()
    ledger = ledger or EMPTY
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = sorted((_index_row(t, ledger, today) for t in theses), key=_sort_key)

    gap_rows = []
    for holding in ledger_gaps(ledger, [t.ticker for t in theses]):
        gap_rows.append(
            {
                "ticker": holding.ticker,
                "value": fmt_money(holding.value, show_amounts),
                "irr": fmt_pct(holding.irr),
                "multiple": fmt_mult(holding.multiple),
                "years": fmt_years(holding.years),
            }
        )

    payload = build_payload(theses, ledger, today, show_amounts)
    # `</script>` inside a JSON string would close the block early.
    payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")

    html = render_mod.env().get_template("site.html").render(
        rows=rows,
        gaps=gap_rows,
        has_ledger=bool(ledger.holdings),
        payload=payload_json,
        built=today.isoformat(),
        favicon=render_mod.favicon_data_uri(),
        show_amounts=show_amounts,
    )

    out_path = out_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    # Pages would otherwise hand the file to Jekyll, which eats anything
    # starting with an underscore and has no business here.
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    return out_path
