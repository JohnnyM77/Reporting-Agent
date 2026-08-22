"""Drift detection — the honesty mechanism.

Drift is not the share price going the wrong way. Drift is the *reasoning*
moving: a kill condition quietly widened after the fact, a pillar that has been
"strained" at three reviews running without anyone calling it, a resolution
date that came and went unanswered, a thesis borrowed from someone who has
since publicly changed their mind.

The failure mode this exists to catch is the comfortable one — the thesis that
survives by being edited. Every finding here is something a person defending a
position would rather not be asked about.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Iterable

from .thesis import Thesis

ALERT = "ALERT"
WARN = "WARN"
INFO = "INFO"

SEVERITY_ORDER = {ALERT: 0, WARN: 1, INFO: 2}

DRIFTING = "DRIFTING"
WATCH = "WATCH"
CLEAN = "CLEAN"

VERDICT_ORDER = {DRIFTING: 0, WATCH: 1, CLEAN: 2}

# Thresholds, named so they can be argued with.
STALE_MONTHS = 12
LOOSENED_LIMIT = 2  # two loosened kill conditions is a pattern, not an update
PERSISTENT_STRAIN_REVIEWS = 3
AMENDMENT_RATE_LIMIT = 1.0  # amendments per review


@dataclasses.dataclass
class Finding:
    code: str
    severity: str
    message: str
    ticker: str = ""
    detail: str = ""

    @property
    def badge(self) -> str:
        return {ALERT: "▲", WARN: "▲", INFO: "•"}.get(self.severity, "•")

    def __str__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.message}"


def _months_between(earlier: dt.date, later: dt.date) -> float:
    return (later - earlier).days / 30.4375


def analyse(thesis: Thesis, today: dt.date | None = None) -> list[Finding]:
    """Every drift finding for one thesis."""
    today = today or dt.date.today()
    out: list[Finding] = []

    def add(code: str, severity: str, message: str, detail: str = "") -> None:
        out.append(Finding(code, severity, message, thesis.ticker, detail))

    # --- alerts ----------------------------------------------------------

    for pillar in thesis.pillars:
        if pillar.status == "BREACHED":
            add(
                "PILLAR_BREACHED",
                ALERT,
                f"{pillar.id} is breached and the position is still open",
                pillar.claim,
            )

    loosened = [a for a in thesis.all_amendments if a.loosened]
    if len(loosened) >= LOOSENED_LIMIT:
        add(
            "KILL_CONDITIONS_LOOSENED",
            ALERT,
            f"{len(loosened)} kill conditions have been loosened after the fact",
            "; ".join(f"{a.pillar}: {a.change}" for a in loosened if a.change)[:400],
        )

    dated_reviews = sorted([r for r in thesis.reviews if r.date], key=lambda r: r.date)
    for pillar in thesis.pillars:
        run = 0
        longest = 0
        for review in dated_reviews:
            status = review.pillar_status.get(pillar.id, "")
            if status == "STRAINED":
                run += 1
                longest = max(longest, run)
            elif status:
                run = 0
        # The current status counts as the most recent observation.
        if pillar.status == "STRAINED":
            run += 1
            longest = max(longest, run)
        if longest >= PERSISTENT_STRAIN_REVIEWS:
            add(
                "PILLAR_PERSISTENTLY_STRAINED",
                ALERT,
                f"{pillar.id} has been strained at {longest} consecutive reviews "
                "without being called either way",
                pillar.claim,
            )

    # --- warnings --------------------------------------------------------

    if thesis.resolution_date and not thesis.is_exited and thesis.resolution_date < today:
        overdue = (today - thesis.resolution_date).days
        add(
            "RESOLUTION_OVERDUE",
            WARN,
            f"resolution date passed {overdue} days ago and nothing was written down",
            thesis.resolution_criterion,
        )

    last = thesis.last_review
    if not thesis.reviews:
        add("NEVER_REVIEWED", WARN, "never reviewed since it was written")
    elif last and last.date and _months_between(last.date, today) >= STALE_MONTHS:
        months = int(_months_between(last.date, today))
        add(
            "STALE",
            WARN,
            f"last reviewed {months} months ago ({last.date.isoformat()})",
        )

    if dated_reviews:
        rate = len(thesis.all_amendments) / len(dated_reviews)
        if rate > AMENDMENT_RATE_LIMIT:
            add(
                "AMENDMENT_RATE",
                WARN,
                f"{len(thesis.all_amendments)} amendments across {len(dated_reviews)} "
                f"reviews ({rate:.1f} per review) — the thesis is being edited to survive",
            )

    # --- info ------------------------------------------------------------

    for source in thesis.diverged_sources:
        if thesis.is_exited:
            continue
        who = source.name or source.outlet or "the source"
        add(
            "DIVERGENCE_OPEN",
            INFO,
            f"held against {who}, who has diverged — the disagreement is still open",
            source.note,
        )

    if thesis.evidence_grade == "C":
        add(
            "GRADE_C",
            INFO,
            "grade C — reconstructed from memory, so treat the confident bits with suspicion",
        )

    out.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.code))
    return out


def verdict(findings: Iterable[Finding]) -> str:
    severities = {f.severity for f in findings}
    if ALERT in severities:
        return DRIFTING
    if WARN in severities:
        return WATCH
    return CLEAN


def check(thesis: Thesis, today: dt.date | None = None, extra: Iterable[Finding] = ()) -> tuple[str, list[Finding]]:
    """(verdict, findings) for one thesis, with any externally-supplied findings folded in."""
    findings = analyse(thesis, today) + list(extra)
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.code))
    return verdict(findings), findings


def summarise(findings: Iterable[Finding]) -> str:
    findings = list(findings)
    if not findings:
        return "No drift detected."
    counts = {ALERT: 0, WARN: 0, INFO: 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    parts = [f"{counts[s]} {s.lower()}" for s in (ALERT, WARN, INFO) if counts.get(s)]
    return ", ".join(parts)
