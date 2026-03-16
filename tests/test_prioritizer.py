# tests/test_prioritizer.py
#
# Tests for master_engine/prioritizer.py

from __future__ import annotations

import datetime as dt

from master_engine.prioritizer import prioritize, score_to_priority
from master_engine.schemas import (
    InvestorEvent,
    AGENT_BOB, AGENT_WALLY, AGENT_NED,
    EVENT_TYPE_EARNINGS_RELEASE,
    EVENT_TYPE_GENERIC_NEWS,
    EVENT_TYPE_NEAR_52W_LOW,
    UNIVERSE_PORTFOLIO,
    PRIORITY_FYI, PRIORITY_LOW, PRIORITY_MEDIUM, PRIORITY_HIGH, PRIORITY_CRITICAL,
)


def _event(ticker, event_type, universe=None, distance_to_low_pct=None, timestamp=None):
    ev = InvestorEvent(
        ticker=ticker,
        company_name=ticker,
        agent=AGENT_BOB,
        event_type=event_type,
        headline=f"{ticker} event",
        timestamp=timestamp or (dt.datetime.utcnow().isoformat() + "Z"),
    )
    if universe:
        ev.universe = universe
    if distance_to_low_pct is not None:
        ev.distance_to_low_pct = distance_to_low_pct
    return ev


class TestScoreToPriority:
    def test_critical_threshold(self):
        assert score_to_priority(80) == PRIORITY_CRITICAL
        assert score_to_priority(100) == PRIORITY_CRITICAL

    def test_high_threshold(self):
        assert score_to_priority(55) == PRIORITY_HIGH
        assert score_to_priority(79) == PRIORITY_HIGH

    def test_medium_threshold(self):
        assert score_to_priority(30) == PRIORITY_MEDIUM
        assert score_to_priority(54) == PRIORITY_MEDIUM

    def test_low_threshold(self):
        assert score_to_priority(10) == PRIORITY_LOW
        assert score_to_priority(29) == PRIORITY_LOW

    def test_fyi_threshold(self):
        assert score_to_priority(0) == PRIORITY_FYI
        assert score_to_priority(9) == PRIORITY_FYI


class TestPrioritize:
    def test_returns_sorted_descending(self):
        events = [
            _event("A.AX", EVENT_TYPE_GENERIC_NEWS),
            _event("B.AX", EVENT_TYPE_EARNINGS_RELEASE, universe=UNIVERSE_PORTFOLIO),
        ]
        ranked = prioritize(events)
        assert len(ranked) == 2
        # Earnings + portfolio should score higher than generic news
        assert ranked[0].score >= ranked[1].score

    def test_sets_score_and_priority_on_each_event(self):
        events = [
            _event("A.AX", EVENT_TYPE_EARNINGS_RELEASE),
            _event("B.AX", EVENT_TYPE_GENERIC_NEWS),
        ]
        ranked = prioritize(events)
        for ev in ranked:
            assert ev.score >= 0
            assert ev.priority in (
                PRIORITY_CRITICAL, PRIORITY_HIGH, PRIORITY_MEDIUM,
                PRIORITY_LOW, PRIORITY_FYI,
            )

    def test_empty_list(self):
        ranked = prioritize([])
        assert ranked == []
