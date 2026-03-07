from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AlertClassification:
    triggered: bool
    tier: str
    reasons: list[str]


def classify_alert(
    distance_to_high: float,
    threshold: float,
    pe_ratio_3y: float | None,
    pe_ratio_5y: float | None,
    pe_ratio_10y: float | None,
    valuation_percentile: float | None,
    evidence_strength: float,
    review_ratio: float = 1.15,
    deep_ratio: float = 1.35,
) -> AlertClassification:
    reasons: list[str] = []
    near_high = distance_to_high <= threshold
    if not near_high:
        return AlertClassification(triggered=False, tier="none", reasons=[])

    reasons.append(f"within {(threshold*100):.1f}% of 52-week high")
    stretch_signals = 0
    for name, ratio in (("PE vs 3y", pe_ratio_3y), ("PE vs 5y", pe_ratio_5y), ("PE vs 10y", pe_ratio_10y)):
        if ratio and ratio >= review_ratio:
            stretch_signals += 1
            reasons.append(f"{name} elevated ({ratio:.2f}x)")

    if valuation_percentile is not None and valuation_percentile >= 0.8:
        stretch_signals += 1
        reasons.append(f"valuation percentile high ({valuation_percentile:.0%})")

    if stretch_signals == 0:
        return AlertClassification(triggered=True, tier="Tier 1: Watch", reasons=reasons)

    if stretch_signals >= 2 and any((r and r >= deep_ratio) for r in (pe_ratio_3y, pe_ratio_5y, pe_ratio_10y)) and evidence_strength < 0.5:
        reasons.append("weak fundamental support for rerating")
        return AlertClassification(triggered=True, tier="Tier 3: Deep Review", reasons=reasons)

    return AlertClassification(triggered=True, tier="Tier 2: Review", reasons=reasons)
