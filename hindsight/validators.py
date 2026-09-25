"""Rules enforced in code after the model answers.

The model proposes; these functions decide what survives:

- a FACT without a source ref becomes an INTERPRETATION;
- a bias state of OBSERVED EVIDENCE or CURRENTLY TRIGGERED without a ref to
  a real record becomes UNKNOWN (this is how we stop it inventing psychology);
- tendencies that portfolio data can't speak to stay NOT ASSESSABLE unless
  properly evidenced;
- a warning without a falsifiable prediction is rejected;
- LOLLAPALOOZA only survives if the three-part gate passes.

Severity is never raised by code, only confirmed or lowered.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from typing import Any

from .config import munger_tendencies
from .schemas import EVIDENCED_BIAS_STATES, AnalysisOutput, Claim, TendencyAssessment

REQUIRED_SECTIONS = {
    "SELL_ALERT": ["THE ISSUE", "SALLY'S CASE", "THE COUNTERCASE"],
    "BOB_REVIEW": [
        "WHAT CHANGED", "WHAT WE EXPECTED", "WHAT HAPPENED", "THESIS IMPACT", "MANAGEMENT INCENTIVES",
        "ACCOUNTING AND CASH FLOW", "CONTRADICTIONS", "EVIDENCE FOR", "EVIDENCE AGAINST", "INVESTIGATE NEXT",
    ],
    "WALLY_REVIEW": [
        "WHY WALLY LIKES IT", "WHAT HAS TO BE TRUE", "WHAT COULD MAKE WALLY WRONG", "VALUE TRAP TEST",
        "GOOD COMPANY VS GOOD PRICE", "WHAT ARE WE BEING SEDUCED BY", "MISSING EVIDENCE",
    ],
    "PORTFOLIO_REVIEW": [
        "CONCENTRATION", "STRONGEST ATTACHMENT SIGNALS", "LOSERS HARDEST TO SELL", "WINNERS HARDEST TO TRIM",
        "THESIS DRIFT", "REPEATED MACRO ASSUMPTIONS", "WEAKEST 7 POWERS", "HIGHEST LOLLAPALOOZA RISK",
        "MOST UNRESOLVED WARNINGS", "RESEARCH EFFORT BIAS",
    ],
}


def shape_check(report_type: str):
    """A callable for ``LLMClient.structured``: raises ValueError on a missing
    required part, which triggers the one retry with the error attached."""

    def _check(out: AnalysisOutput) -> None:
        keys = {k.strip().upper() for k in out.sections}
        missing = [s for s in REQUIRED_SECTIONS.get(report_type, []) if s not in keys]
        if missing:
            raise ValueError(f"sections missing required keys: {missing}")
        if report_type == "SELL_ALERT":
            if out.fresh_capital_test is None or out.forced_sale_test is None:
                raise ValueError("SELL_ALERT requires both fresh_capital_test and forced_sale_test")
            if out.thesis_test is None:
                raise ValueError("SELL_ALERT requires thesis_test")
        if not out.strongest_opposing_case.strip():
            raise ValueError("strongest_opposing_case must not be empty")

    return _check


def _valid_ref(ref: str, registry: set[str]) -> bool:
    ref = (ref or "").strip()
    return ref in registry or ref.startswith(("http://", "https://"))


def _fix_claim(claim: Claim, registry: set[str], where: str, notes: list[str]) -> None:
    claim.source_refs = [r for r in claim.source_refs if r and r.strip()]
    if claim.claim_type == "FACT" and not any(_valid_ref(r, registry) for r in claim.source_refs):
        claim.claim_type = "INTERPRETATION"
        notes.append(f"FACT downgraded to INTERPRETATION (no valid source ref) in {where}: {claim.text[:70]}")


def _not_assessable_names() -> set[str]:
    return {t["name"] for t in munger_tendencies().get("tendencies", []) if t.get("default_state") == "NOT ASSESSABLE"}


def fix_tendency(t: TendencyAssessment, registry: set[str], notes: list[str]) -> None:
    valid = [r for r in t.evidence_refs if _valid_ref(r, registry) and not r.startswith("http")]
    dropped = [r for r in t.evidence_refs if r not in valid]
    t.evidence_refs = valid
    if t.state in EVIDENCED_BIAS_STATES and not valid:
        notes.append(f"Bias '{t.tendency}' downgraded {t.state} to UNKNOWN: no evidence ref to a real record"
                     + (f" (rejected refs: {dropped})" if dropped else ""))
        t.state = "UNKNOWN"
    if t.tendency in _not_assessable_names() and t.state not in EVIDENCED_BIAS_STATES and t.state != "NOT ASSESSABLE":
        t.state = "NOT ASSESSABLE"


@dataclasses.dataclass
class LollaResult:
    fired: bool
    tendencies: list[str]
    direction: str
    hard_triggers: list[str]
    failures: list[str]


def lollapalooza_gate(tendencies: list[TendencyAssessment], hard_triggers: list[str], cfg: dict) -> LollaResult:
    lcfg = cfg.get("lollapalooza", {})
    min_t = int(lcfg.get("min_tendencies", 3))
    min_dir = int(lcfg.get("min_same_direction", 3))
    evidenced = [t for t in tendencies if t.state in EVIDENCED_BIAS_STATES and t.evidence_refs]
    dirs = Counter(t.direction for t in evidenced if t.direction != "NONE")
    top_dir, top_n = (dirs.most_common(1)[0] if dirs else ("NONE", 0))
    failures = []
    if len(evidenced) < min_t:
        failures.append(f"only {len(evidenced)} evidenced tendencies (need {min_t})")
    if top_n < min_dir:
        failures.append(f"only {top_n} share a direction (need {min_dir})")
    if not hard_triggers:
        failures.append("no hard behavioural trigger present")
    names = [t.tendency for t in evidenced if t.direction == top_dir]
    return LollaResult(not failures, names, top_dir, list(hard_triggers), failures)


def validate_analysis(
    out: AnalysisOutput, registry: set[str], hard_triggers: list[str], cfg: dict
) -> tuple[AnalysisOutput, list[str], LollaResult]:
    notes: list[str] = []
    for key, claims in out.sections.items():
        for c in claims:
            _fix_claim(c, registry, key, notes)
    for p in out.seven_powers:
        for group in (p.benefit_evidence, p.barrier_evidence, p.evidence_against):
            for c in group:
                _fix_claim(c, registry, f"7 Powers/{p.power}", notes)
        # A Power needs both halves. Management language alone is not evidence.
        if p.status == "PRESENT":
            real = lambda cs: [c for c in cs if c.claim_type not in ("MANAGEMENT_CLAIM",)]  # noqa: E731
            if not real(p.benefit_evidence) or not real(p.barrier_evidence):
                notes.append(f"7 Powers/{p.power} PRESENT downgraded to UNCLEAR: benefit and barrier both need non-management evidence")
                p.status = "UNCLEAR"

    for t in out.munger_scan:
        fix_tendency(t, registry, notes)

    kept = []
    for w in out.warnings:
        if not w.predictions:
            notes.append(f"Warning rejected, no falsifiable prediction: {w.text[:80]}")
            continue
        kept.append(w)
    out.warnings = kept

    lolla = lollapalooza_gate(out.munger_scan, hard_triggers, cfg)
    if out.severity == "LOLLAPALOOZA" or out.lollapalooza_claimed:
        if lolla.fired:
            out.severity = "LOLLAPALOOZA"
        else:
            notes.append("LOLLAPALOOZA claimed by the model but the gate failed: " + "; ".join(lolla.failures))
            out.lollapalooza_claimed = False
            if out.severity == "LOLLAPALOOZA":
                out.severity = "RED"
    return out, notes, lolla


def summarise_refs(registry: dict[str, Any]) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in registry.items())
