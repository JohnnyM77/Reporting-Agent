# master_engine/prioritizer.py
#
# Scores and ranks InvestorEvent objects using a weighted multi-factor model.
# Scoring logic is delegated to agents/super_investor/scoring.py when
# available so we mirror the same specification here; otherwise a basic
# local fallback scorer is used.

from __future__ import annotations

import logging
from typing import Sequence

from .schemas import (
    InvestorEvent,
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_MEDIUM,
    PRIORITY_LOW,
    PRIORITY_FYI,
    PRIORITY_ORDER,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Priority band thresholds
# ---------------------------------------------------------------------------
THRESHOLD_CRITICAL = 80
THRESHOLD_HIGH = 55
THRESHOLD_MEDIUM = 30
THRESHOLD_LOW = 10


def score_to_priority(score: int) -> str:
    """Convert a numeric score to a named priority band."""
    if score >= THRESHOLD_CRITICAL:
        return PRIORITY_CRITICAL
    if score >= THRESHOLD_HIGH:
        return PRIORITY_HIGH
    if score >= THRESHOLD_MEDIUM:
        return PRIORITY_MEDIUM
    if score >= THRESHOLD_LOW:
        return PRIORITY_LOW
    return PRIORITY_FYI


def prioritize(events: list[InvestorEvent]) -> list[InvestorEvent]:
    """
    Score every event and sort the list descending by score.

    The scoring is done by the Super Investor scoring module so we import
    it here rather than re-implementing the weights.  Falls back to a basic
    local score if the module is unavailable.

    Returns
    -------
    list[InvestorEvent]
        Same events with ``score`` and ``priority`` fields populated,
        sorted descending by score.
    """
    try:
        from agents.super_investor.scoring import score_event  # type: ignore[import]
        _score_fn = score_event
    except ImportError:
        logger.warning(
            "[prioritizer] super_investor.scoring unavailable — using basic scorer"
        )
        _score_fn = _basic_score  # type: ignore[assignment]

    scored: list[InvestorEvent] = []
    for event in events:
        score = _score_fn(event)
        event.score = score
        event.priority = score_to_priority(score)
        logger.debug(
            "[prioritizer] %s | %s → score=%d priority=%s",
            event.ticker,
            event.event_type,
            score,
            event.priority,
        )
        scored.append(event)

    scored.sort(key=lambda e: e.score, reverse=True)
    logger.info(
        "[prioritizer] %d event(s) scored and sorted", len(scored)
    )
    return scored


# ---------------------------------------------------------------------------
# Basic fallback scorer (used when super_investor.scoring is not importable)
# ---------------------------------------------------------------------------
_BASE_SEVERITY: dict[str, int] = {
    "earnings_release": 50,
    "guidance_change": 50,
    "capital_raise": 50,
    "takeover": 50,
    "regulator_action": 50,
    "major_contract": 30,
    "ceo_change": 30,
    "litigation": 30,
    "profit_warning": 30,
    "valuation_trigger": 20,
    "near_52w_low": 10,
    "generic_news": 5,
    "appendix_4d": 50,
    "appendix_4e": 50,
    "acquisition": 50,
}


def _basic_score(event: InvestorEvent) -> int:
    """Minimal fallback scorer that only uses event type severity."""
    return _BASE_SEVERITY.get(event.event_type, 5)
