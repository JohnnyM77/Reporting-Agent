"""The pre-season review pack.

The ASX reports in February and August. Theo runs in January and July.

The month matters more than anything else in this file. A thesis reviewed
*after* the result is reviewed with the answer already on the page, and a
review conducted with the answer in front of you is worth nothing — it will
find whatever it needs to find to make the last six months look intended. The
only review that can be scored is the one written before the number lands.

So the pack does not ask "was the thesis right". It asks, per pillar: what
number in this coming result would you need to see to keep calling this
intact? Commit to it now, while it can still be wrong.
"""

from __future__ import annotations

import datetime as dt
import textwrap
from typing import Sequence

from . import drift as drift_mod
from .drift import Finding, WARN
from .ledger import EMPTY, Ledger, gaps
from .thesis import Thesis

# Reporting months, and the month before each one.
RESULTS_MONTHS = (2, 8)
PREP_MONTHS = (1, 7)

WRAP = 76


# --------------------------------------------------------------------------
# The season hook into drift
# --------------------------------------------------------------------------


def in_prep_window(today: dt.date | None = None) -> bool:
    """True during January and July — the month before results land."""
    today = today or dt.date.today()
    return today.month in PREP_MONTHS


def next_results_month(today: dt.date | None = None) -> tuple[int, int]:
    """(year, month) of the next reporting month."""
    today = today or dt.date.today()
    for month in RESULTS_MONTHS:
        if month > today.month:
            return today.year, month
        if month == today.month:
            return today.year, month
    return today.year + 1, RESULTS_MONTHS[0]


def season_label(today: dt.date | None = None) -> str:
    year, month = next_results_month(today)
    return f"{dt.date(year, month, 1):%B %Y}"


def pre_season_finding(thesis: Thesis, today: dt.date | None = None) -> Finding | None:
    """A warn finding while we are inside the prep window and this is still held.

    Lives here rather than in ``drift`` because it is a calendar fact, not a
    property of the thesis. ``assess()`` folds it in for every caller.
    """
    today = today or dt.date.today()
    if not in_prep_window(today) or thesis.is_exited:
        return None
    reviewed_this_window = any(
        r.date and r.date.year == today.year and r.date.month == today.month
        for r in thesis.reviews
    )
    if reviewed_this_window:
        return None
    return Finding(
        code="PRE_SEASON_REVIEW_DUE",
        severity=WARN,
        message=(
            f"{season_label(today)} results land next month — commit to the numbers "
            "now, while the review can still be wrong"
        ),
        ticker=thesis.ticker,
    )


def assess(thesis: Thesis, today: dt.date | None = None) -> tuple[str, list[Finding]]:
    """Drift verdict for a thesis, including the season finding.

    This is the entry point every other module uses; call ``drift.check``
    directly only when the calendar deliberately should not matter.
    """
    extra = [f for f in (pre_season_finding(thesis, today),) if f is not None]
    return drift_mod.check(thesis, today, extra)


# --------------------------------------------------------------------------
# Pack rendering
# --------------------------------------------------------------------------


def _rule(char: str = "-") -> str:
    return char * WRAP


def _wrap(text: str, indent: str = "") -> str:
    if not text:
        return ""
    return textwrap.fill(
        " ".join(text.split()),
        width=WRAP,
        initial_indent=indent,
        subsequent_indent=indent,
    )


def _money(value: float | None, show_amounts: bool) -> str:
    if value is None:
        return "—"
    if not show_amounts:
        return "—"
    return f"${value:,.0f}"


def _pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"


def _order_key(entry: tuple[Thesis, str, list[Finding], float]) -> tuple[int, float]:
    _, verdict, _, capital = entry
    return (drift_mod.VERDICT_ORDER.get(verdict, 9), -capital)


def build_pack(
    theses: Sequence[Thesis],
    ledger: Ledger | None = None,
    today: dt.date | None = None,
    show_amounts: bool = False,
) -> str:
    """The plain-text pack, ready to email."""
    today = today or dt.date.today()
    ledger = ledger or EMPTY
    label = season_label(today)

    entries: list[tuple[Thesis, str, list[Finding], float]] = []
    for thesis in theses:
        if thesis.is_exited:
            continue
        verdict, findings = assess(thesis, today)
        holding = ledger.get(thesis.ticker)
        capital = (holding.value if holding else 0.0) or 0.0
        entries.append((thesis, verdict, findings, capital))
    entries.sort(key=_order_key)

    lines: list[str] = []
    add = lines.append

    add(_rule("="))
    add("THEO — PRE-SEASON REVIEW PACK".center(WRAP))
    add(f"{label} results · prepared {today.isoformat()}".center(WRAP))
    add(_rule("="))
    add("")
    add(
        _wrap(
            "Answer these before the results land. A thesis reviewed after the "
            "number is published is reviewed with the answer in front of you, "
            "which is worthless. Commit to what would change your mind while it "
            "can still be wrong."
        )
    )
    add("")
    if not in_prep_window(today):
        add(
            _wrap(
                "NOTE: today is outside the January/July prep window, so this pack "
                "was built off-cycle."
            )
        )
        add("")

    if not entries:
        add("No open theses to review.")
    else:
        counts = {v: sum(1 for e in entries if e[1] == v) for v in (drift_mod.DRIFTING, drift_mod.WATCH, drift_mod.CLEAN)}
        add(
            f"{len(entries)} open theses — "
            f"{counts[drift_mod.DRIFTING]} drifting, "
            f"{counts[drift_mod.WATCH]} watch, "
            f"{counts[drift_mod.CLEAN]} clean. "
            "Worst first."
        )
        add("")

    for position, (thesis, verdict, findings, capital) in enumerate(entries, start=1):
        holding = ledger.get(thesis.ticker)
        add(_rule("="))
        header = f"{position}. {thesis.ticker} — {thesis.name or thesis.ticker}"
        add(header)
        meta = [thesis.archetype or "?", f"grade {thesis.evidence_grade}", f"drift {verdict}"]
        if thesis.draft:
            meta.append("DRAFT")
        if holding:
            if show_amounts and holding.value:
                meta.append(f"value {_money(holding.value, True)}")
            if holding.irr is not None:
                meta.append(f"IRR {_pct(holding.irr)}")
        add("   " + " · ".join(meta))
        add(_rule("="))
        add("")

        add("   THE BET AS WRITTEN")
        add(_wrap(thesis.the_bet or "(not written)", indent="   "))
        add("")

        if findings:
            add("   DRIFT")
            for finding in findings:
                add(_wrap(f"{finding.badge} {finding.code}: {finding.message}", indent="   "))
            add("")

        add("   PILLARS — commit to a number before the result")
        add("")
        for pillar in thesis.pillars:
            add(f"   {pillar.symbol} {pillar.id} [{pillar.status}] {pillar.claim}")
            add(_wrap(f"kill: {pillar.kill_condition}", indent="      "))
            add("")
            add(
                _wrap(
                    "Q: What number in this result would you need to see to keep "
                    f"calling {pillar.id} {pillar.status.lower() if pillar.status != 'INTACT' else 'intact'}?",
                    indent="      ",
                )
            )
            add("      A: ______________________________________________________")
            add("")
            if pillar.kill_needs_work:
                add(
                    _wrap(
                        "Q: This kill condition is still marked NEEDS WORK. Write a "
                        "testable one now, or say plainly that the pillar is untestable.",
                        indent="      ",
                    )
                )
                add("      A: ______________________________________________________")
                add("")

        open_divergences = thesis.diverged_sources
        if open_divergences:
            add("   OPEN DIVERGENCE")
            for source in open_divergences:
                who = " · ".join(p for p in (source.name, source.outlet) if p) or "source"
                add(_wrap(f"{who} has diverged from this position.", indent="      "))
                if source.note:
                    add(_wrap(source.note, indent="      "))
                add("")
                add(
                    _wrap(
                        "Q: What have they seen that you have not? If the answer is "
                        "'nothing', why is the disagreement still open?",
                        indent="      ",
                    )
                )
                add("      A: ______________________________________________________")
                add("")

        if thesis.hold_thesis and thesis.the_bet and thesis.hold_thesis != thesis.the_bet:
            add("   ENTRY REASON vs HOLD REASON")
            add(_wrap(f"Bought because: {thesis.the_bet}", indent="      "))
            add(_wrap(f"Held because:   {thesis.hold_thesis}", indent="      "))
            add("")
            add(
                _wrap(
                    "Q: The reason you hold this is not the reason you bought it. "
                    "Is that a thesis that matured, or a thesis that was replaced "
                    "after the fact to justify not selling?",
                    indent="      ",
                )
            )
            add("      A: ______________________________________________________")
            add("")
        elif not thesis.hold_thesis:
            add("   ENTRY REASON vs HOLD REASON")
            add(
                _wrap(
                    "Q: No hold thesis is recorded. If the entry case has already "
                    "resolved, what is holding it now — and if it has not resolved, "
                    "what is still outstanding?",
                    indent="      ",
                )
            )
            add("      A: ______________________________________________________")
            add("")

        if thesis.resolution_date:
            add(
                _wrap(
                    f"   Resolution set for {thesis.resolution_date.isoformat()}: "
                    f"{thesis.resolution_criterion or '(no criterion written)'}",
                )
            )
            add("")

    missing = gaps(ledger, [t.ticker for t in theses]) if ledger.holdings else []
    if missing:
        add(_rule("="))
        add("HOLDINGS WITH NO THESIS — ranked by capital")
        add(_rule("="))
        add("")
        add(
            _wrap(
                "Capital is committed here and no reason is written down. That is "
                "the same as not having one."
            )
        )
        add("")
        for holding in missing:
            bits = [f"   {holding.ticker:<6}"]
            if show_amounts:
                bits.append(f"{_money(holding.value, True):>12}")
            if holding.irr is not None:
                bits.append(f"IRR {_pct(holding.irr):>7}")
            add("  ".join(bits))
        add("")

    add(_rule("="))
    add("End of pack.")
    return "\n".join(lines) + "\n"
