from src.alert_engine import classify_alert


def test_tier1_watch_when_near_high_only():
    out = classify_alert(
        distance_to_high=0.03,
        threshold=0.05,
        pe_ratio_3y=1.0,
        pe_ratio_5y=1.0,
        pe_ratio_10y=1.0,
        valuation_percentile=0.4,
        evidence_strength=0.8,
    )
    assert out.triggered
    assert out.tier == "Tier 1: Watch"


def test_tier3_when_stretched_and_weak_evidence():
    out = classify_alert(
        distance_to_high=0.01,
        threshold=0.05,
        pe_ratio_3y=1.5,
        pe_ratio_5y=1.45,
        pe_ratio_10y=1.4,
        valuation_percentile=0.95,
        evidence_strength=0.2,
    )
    assert out.triggered
    assert out.tier == "Tier 3: Deep Review"
