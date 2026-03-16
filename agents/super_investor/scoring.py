# agents/super_investor/scoring.py
#
# Weighted scoring logic for the Super Investor Agent.
#
# Score components
# ----------------
# 1. Base event severity      (event_type)
# 2. Universe relevance       (portfolio / high_conviction / TII75 / other)
# 3. Valuation/opportunity    (distance to 52w low, DCF, target band)
# 4. Recency                  (within 2 hours / same day)
# 5. Future hooks             (placeholders — all return 0 until implemented)

from __future__ import annotations

import datetime as dt
import logging

from master_engine.schemas import InvestorEvent
from .config import (
    BASE_SEVERITY,
    UNIVERSE_BONUS,
    VALUATION_BONUS_WITHIN_2PCT_LOW,
    VALUATION_BONUS_WITHIN_5PCT_LOW,
    VALUATION_BONUS_REVERSE_DCF_ATTRACTIVE,
    VALUATION_BONUS_BELOW_TARGET_BUY_BAND,
    RECENCY_BONUS_WITHIN_2H,
    RECENCY_BONUS_SAME_DAY,
    THRESHOLD_CRITICAL,
    THRESHOLD_HIGH,
    THRESHOLD_MEDIUM,
    THRESHOLD_LOW,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Priority label assignment
# ---------------------------------------------------------------------------

def score_to_priority(score: int) -> str:
    if score >= THRESHOLD_CRITICAL:
        return "CRITICAL"
    if score >= THRESHOLD_HIGH:
        return "HIGH"
    if score >= THRESHOLD_MEDIUM:
        return "MEDIUM"
    if score >= THRESHOLD_LOW:
        return "LOW"
    return "FYI"


# ---------------------------------------------------------------------------
# Individual score components
# ---------------------------------------------------------------------------

def _severity_score(event: InvestorEvent) -> int:
    """Base severity from event type."""
    return BASE_SEVERITY.get(event.event_type, 5)


def _universe_score(event: InvestorEvent) -> int:
    """Universe relevance bonus."""
    return UNIVERSE_BONUS.get(event.universe, 0)


def _valuation_score(event: InvestorEvent) -> int:
    """
    Opportunity bonus based on distance to 52-week low.

    The ``distance_to_low_pct`` field is set by Wally for valuation-trigger
    events.  A smaller distance means a better opportunity.
    """
    dist = event.distance_to_low_pct
    if dist is None:
        return 0
    if dist <= 2.0:
        return VALUATION_BONUS_WITHIN_2PCT_LOW
    if dist <= 5.0:
        return VALUATION_BONUS_WITHIN_5PCT_LOW
    return 0


def _recency_score(event: InvestorEvent) -> int:
    """Recency bonus based on how fresh the event is."""
    if not event.timestamp:
        return 0
    try:
        # Parse ISO-8601 timestamp (with or without trailing Z)
        ts_str = event.timestamp.rstrip("Z")
        event_dt = dt.datetime.fromisoformat(ts_str)
        # Make timezone-aware if naive (assume UTC)
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=dt.timezone.utc)
        now = dt.datetime.now(dt.timezone.utc)
        age_hours = (now - event_dt).total_seconds() / 3600.0
        if age_hours <= 2.0:
            return RECENCY_BONUS_WITHIN_2H
        if age_hours <= 24.0:
            return RECENCY_BONUS_SAME_DAY
    except Exception as exc:
        logger.debug("[scoring] recency parse failed for %r: %s", event.timestamp, exc)
    return 0


# ---------------------------------------------------------------------------
# Future-ready hook placeholders
# ---------------------------------------------------------------------------

def _insider_trading_score(event: InvestorEvent) -> int:
    """Placeholder: insider trading signal bonus (not yet implemented)."""
    return 0


def _broker_target_score(event: InvestorEvent) -> int:
    """Placeholder: broker target price change bonus (not yet implemented)."""
    return 0


def _short_interest_score(event: InvestorEvent) -> int:
    """Placeholder: short interest signal bonus (not yet implemented)."""
    return 0


def _reverse_dcf_score(event: InvestorEvent) -> int:
    """
    Placeholder: reverse DCF attractiveness bonus.

    When enabled, this will return VALUATION_BONUS_REVERSE_DCF_ATTRACTIVE
    for events where the implied growth rate is conservative.
    """
    return 0


def _conviction_score(event: InvestorEvent) -> int:
    """Placeholder: conviction scoring bonus (not yet implemented)."""
    return 0


# ---------------------------------------------------------------------------
# Composite scorer
# ---------------------------------------------------------------------------

def score_event(event: InvestorEvent) -> int:
    """
    Compute a composite score for a single InvestorEvent.

    The score is the sum of all active components.

    Parameters
    ----------
    event : InvestorEvent

    Returns
    -------
    int
        Composite score (≥ 0).
    """
    severity = _severity_score(event)
    universe = _universe_score(event)
    valuation = _valuation_score(event)
    recency = _recency_score(event)

    # Future hooks (all return 0 until implemented)
    insider = _insider_trading_score(event)
    broker = _broker_target_score(event)
    short = _short_interest_score(event)
    dcf = _reverse_dcf_score(event)
    conviction = _conviction_score(event)

    total = severity + universe + valuation + recency + insider + broker + short + dcf + conviction

    logger.debug(
        "[scoring] %s | %s → severity=%d universe=%d valuation=%d recency=%d "
        "insider=%d broker=%d short=%d dcf=%d conviction=%d → total=%d",
        event.ticker, event.event_type,
        severity, universe, valuation, recency,
        insider, broker, short, dcf, conviction, total,
    )
    return max(0, total)
