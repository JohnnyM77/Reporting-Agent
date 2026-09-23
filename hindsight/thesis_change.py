"""Thesis change detector.

Did the thesis change because the evidence changed, or was it changed to
accommodate the evidence? Code can't answer that fully, but it can surface
the structural red flags that make the question worth asking: a kill
condition rewritten while its pillar was under pressure, a pillar that
quietly disappeared, a holding rationale that switched category.

Reads Theo's files and git history. Never writes to them.
"""

from __future__ import annotations

import dataclasses
from typing import Any

PRESSURE = ("STRAINED", "BREACHED")


@dataclasses.dataclass
class ThesisChange:
    ticker: str
    classification: str = "THESIS INTACT"
    reasons: list[str] = dataclasses.field(default_factory=list)
    red_flags: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    revision_count: int = 0
    versions: int = 0
    first_date: str = ""
    adjustments_to_fit: int = 0

    def lines(self) -> list[str]:
        out = [f"Deterministic classification: {self.classification}" + (f" ({'; '.join(self.reasons)})" if self.reasons else "")]
        out.append(f"{self.versions} committed version(s) since {self.first_date or 'unknown'}; revision count {self.revision_count}")
        out += [f"RED FLAG: {r}" for r in self.red_flags]
        out += [f"Note: {n}" for n in self.notes]
        if self.adjustments_to_fit >= 3:
            out.append(f"This is the {_ordinal(self.adjustments_to_fit)} time the thesis has been adjusted in a way that fits the facts.")
        return out


def _ordinal(n: int) -> str:
    return {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}.get(n, f"{n}th")


def detect(ticker: str, current: Any, versions: list[dict]) -> ThesisChange:
    """``versions`` is ``[{sha, date, thesis}]`` oldest first (may be empty)."""
    tc = ThesisChange(ticker=ticker)
    if current is None:
        tc.classification = "CHANGED"
        tc.reasons.append("no Theo thesis on file")
        tc.red_flags.append("Held or reviewed with no written thesis. There is nothing to be wrong against.")
        return tc

    original = versions[0]["thesis"] if versions else current
    tc.versions = len(versions) or 1
    tc.first_date = versions[0]["date"] if versions else ""
    amendments = current.all_amendments
    tc.revision_count = max(len(versions) - 1, len(amendments), 0)

    # Walk the history pairwise to catch rewrites made while under pressure.
    chain = [v["thesis"] for v in versions] or [current]
    if chain[-1] is not current:
        chain.append(current)
    for prev, nxt in zip(chain, chain[1:]):
        prev_ids = {p.id: p for p in prev.pillars}
        nxt_ids = {p.id: p for p in nxt.pillars}
        for pid, p in prev_ids.items():
            if pid not in nxt_ids:
                msg = f"pillar {pid} removed" + (f" while {p.status}" if p.status in PRESSURE else "")
                tc.red_flags.append(msg + f": '{p.claim[:90]}'")
                if p.status in PRESSURE:
                    tc.adjustments_to_fit += 1
        for pid, p in prev_ids.items():
            q = nxt_ids.get(pid)
            if q is None or " ".join(p.kill_condition.split()) == " ".join(q.kill_condition.split()):
                continue
            if p.kill_needs_work:
                tc.notes.append(f"{pid} kill condition defined for the first time (was a placeholder)")
            elif p.status in PRESSURE or q.status in PRESSURE:
                tc.red_flags.append(f"{pid} kill condition rewritten while the pillar was {p.status if p.status in PRESSURE else q.status}")
                tc.adjustments_to_fit += 1
            else:
                tc.notes.append(f"{pid} kill condition reworded")
        if prev.archetype and nxt.archetype and prev.archetype != nxt.archetype:
            tc.red_flags.append(f"holding rationale switched category: {prev.archetype} to {nxt.archetype}")
            tc.adjustments_to_fit += 1

    for a in amendments:
        if a.loosened:
            tc.red_flags.append(f"{a.pillar} amendment labelled LOOSENED by Theo: {a.change[:100]}")
            tc.adjustments_to_fit += 1

    if current.hold_thesis and current.hold_thesis.strip() != current.the_bet.strip():
        tc.notes.append("the reason for holding now is written separately from the reason for buying (a second iteration)")

    # Classification, worst first.
    breached = [p.id for p in current.pillars if p.status == "BREACHED"]
    strained = [p.id for p in current.pillars if p.status == "STRAINED"]
    loosened = [a for a in amendments if a.loosened]
    tightened = [a for a in amendments if a.direction == "TIGHTENED"]
    structural = [r for r in tc.red_flags if "removed" in r or "switched category" in r]
    if breached:
        tc.classification = "BROKEN"
        tc.reasons.append(f"pillar(s) breached: {', '.join(breached)}")
    elif strained or loosened or tc.adjustments_to_fit:
        tc.classification = "WEAKENED"
        if strained:
            tc.reasons.append(f"pillar(s) strained: {', '.join(strained)}")
        if loosened or tc.adjustments_to_fit:
            tc.reasons.append("kill conditions loosened or rewritten under pressure")
    elif structural:
        tc.classification = "CHANGED"
        tc.reasons.append("pillars or rationale changed without a new entry decision")
    elif tightened:
        tc.classification = "STRENGTHENED"
        tc.reasons.append(f"{len(tightened)} amendment(s) tightened a kill condition")
    else:
        tc.classification = "THESIS INTACT"
    if original is not current and original.pillars and len(current.pillars) > len(original.pillars):
        tc.notes.append(f"pillars grew from {len(original.pillars)} to {len(current.pillars)}")
    return tc
