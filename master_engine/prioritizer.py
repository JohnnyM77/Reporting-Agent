# master_engine/prioritizer.py
#
# Scores and ranks InvestorEvent objects by event-type severity.
#
# This used to try agents/super_investor/scoring.py first. That module no
# longer exists in the repo, so the import always failed and the basic
# scorer below was the one actually used; the dead import has been removed.

from __future__ import annotations

import logging
from typing import Sequence

from shared.events import (
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

    Scores by event-type severity (``_basic_score``).

    Returns
    -------
    list[InvestorEvent]
        Same events with ``score`` and ``priority`` fields populated,
        sorted descending by score.
    """
    _score_fn = _basic_score

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
# Event-type severity scorer
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
    """Score from event type severity only."""
    return _BASE_SEVERITY.get(event.event_type, 5)
