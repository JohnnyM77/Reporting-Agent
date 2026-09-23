"""Harry on the public website: docs/data/harry.json.

The site and the repo are public, so this writes a verdict-level summary per
report and nothing else by default: severity, verdict, thesis read, the fresh
capital and forced sale answers, what would change the call. The bias read
(Munger tendencies) and position details (weights, average cost) stay in the
private store unless ``web.include_bias`` / ``web.include_position`` are set.

The file keeps the newest ``web.max_entries`` reports, so a throwaway store on
the runner still gives the site a running history.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .render import clean

REL = Path("docs") / "data" / "harry.json"


def entry(b: dict, date: str, cfg: dict) -> dict[str, Any] | None:
    if b.get("status") != "OK":
        return None
    wcfg = cfg.get("web", {})
    out = b["output"]
    e: dict[str, Any] = {
        "date": date,
        "ticker": b.get("ticker") or "PORTFOLIO",
        "report_type": b["report_type"],
        "severity": out.severity,
        "verdict": clean(out.verdict),
        "severity_reason": clean(out.severity_reason),
        "thesis": out.thesis_test.classification if out.thesis_test else None,
        "would_buy_today": (out.fresh_capital_test.would_buy_today if out.fresh_capital_test else None),
        "weight_if_new": clean(out.fresh_capital_test.weight_if_new) if out.fresh_capital_test else None,
        "would_rebuy": out.forced_sale_test.would_rebuy if out.forced_sale_test else None,
        "lollapalooza": bool(b.get("lolla") and b["lolla"].fired and out.severity == "LOLLAPALOOZA"),
        "what_would_change_my_mind": [clean(x) for x in out.what_would_change_my_mind[:5]],
        "unanswered_questions": [clean(x) for x in out.unanswered_questions[:5]],
        "disagreements": [{"with": d.with_agent, "point": clean(d.point)} for d in out.disagreements],
    }
    if wcfg.get("include_bias"):
        e["biases"] = [t.tendency for t in out.munger_scan if t.state in ("OBSERVED EVIDENCE", "CURRENTLY TRIGGERED")]
    if wcfg.get("include_position") and b.get("facts") is not None:
        f = b["facts"]
        e["position"] = {"unrealised_pct": f.unrealised_pct, "avg_cost": f.avg_cost, "value_weight": f.value_weight}
    return e


def write(repo_root: Path, reports: list[dict], date: str, cfg: dict) -> Path | None:
    wcfg = cfg.get("web", {})
    if not wcfg.get("publish", True):
        return None
    new = [e for e in (entry(b, date, cfg) for b in reports) if e]
    if not new:
        return None
    path = repo_root / REL
    try:
        existing = json.loads(path.read_text(encoding="utf-8")).get("reports", [])
    except (OSError, ValueError):
        existing = []
    keys = {(e["date"], e["ticker"], e["report_type"]) for e in new}
    kept = [e for e in existing if (e.get("date"), e.get("ticker"), e.get("report_type")) not in keys]
    reports_out = (new + kept)[: int(wcfg.get("max_entries", 20))]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"last_run": date, "reports": reports_out}, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    return path
