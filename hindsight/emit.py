"""Summary events in Master Engine's ``InvestorEvent`` shape.

Headline and severity only: no position data, no bias notes. Written to the
private store. There is no Master Engine runner in the repo yet; when one
exists, pass :func:`collect_events` to it as a fourth collector.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from .config import REPO_ROOT

_PRIORITY = {"LOLLAPALOOZA": "CRITICAL", "RED": "HIGH", "AMBER": "MEDIUM", "GREEN": "LOW"}


def to_investor_events(reports: list[dict], today: dt.date) -> list[dict]:
    sys.path.insert(0, str(REPO_ROOT))
    from master_engine.schemas import InvestorEvent, normalise_ticker

    out = []
    for b in reports:
        sev = b["output"].severity if b.get("output") else b["status"]
        t = b.get("ticker") or "PORTFOLIO"
        ev = InvestorEvent(
            ticker=normalise_ticker(t) if t != "PORTFOLIO" else t,
            company_name=t,
            agent="hindsight",
            event_type="hindsight_" + b["report_type"].lower(),
            headline=f"Captain Hindsight {sev}: {t} {b['report_type'].replace('_', ' ').lower()}",
            timestamp=f"{today.isoformat()}T00:00:00Z",
            priority=_PRIORITY.get(sev, "HIGH" if str(sev).startswith("FAILED") else "LOW"),
            action="Read the Captain Hindsight email",
        )
        out.append(ev.to_dict())
    return out


def write_events(store, today: dt.date, reports: list[dict]) -> Path | None:
    if not reports:
        return None
    return store.write_text(f"emit/{today.isoformat()}/hindsight_events.json",
                            json.dumps(to_investor_events(reports, today), indent=2))


def collect_events(store_root: Path | None = None):
    """Zero-arg-friendly collector returning the latest run's InvestorEvents."""
    from master_engine.schemas import InvestorEvent

    root = Path(store_root) if store_root else REPO_ROOT / "hindsight_data"
    files = sorted((root / "emit").glob("*/hindsight_events.json"))
    if not files:
        return []
    return [InvestorEvent.from_dict(d) for d in json.loads(files[-1].read_text(encoding="utf-8"))]
