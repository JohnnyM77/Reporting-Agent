# tests/test_scoring.py
#
# Tests for agents/super_investor/scoring.py

from __future__ import annotations

import datetime as dt

import pytest

from master_engine.schemas import (
    InvestorEvent,
    AGENT_BOB, AGENT_WALLY, AGENT_NED,
    EVENT_TYPE_EARNINGS_RELEASE,
    EVENT_TYPE_GENERIC_NEWS,
    EVENT_TYPE_NEAR_52W_LOW,
    EVENT_TYPE_VALUATION_TRIGGER,
    UNIVERSE_PORTFOLIO,
    UNIVERSE_TII75,
    UNIVERSE_OTHER,
)
from agents.super_investor.scoring import (
    score_event,
    score_to_priority,
    _severity_score,
    _universe_score,
    _valuation_score,
    _recency_score,
)


def _event(**kwargs):
    defaults = dict(
        ticker="NHC.AX",
        company_name="New Hope",
        agent=AGENT_BOB,
        event_type=EVENT_TYPE_EARNINGS_RELEASE,
        headline="NHC results",
        timestamp=dt.datetime.utcnow().isoformat() + "Z",
    )
    defaults.update(kwargs)
    return InvestorEvent(**defaults)


class TestSeverityScore:
    def test_earnings_release_gets_50(self):
        ev = _event(event_type=EVENT_TYPE_EARNINGS_RELEASE)
        assert _severity_score(ev) == 50

    def test_generic_news_gets_5(self):
        ev = _event(event_type=EVENT_TYPE_GENERIC_NEWS)
        assert _severity_score(ev) == 5

    def test_unknown_event_type_defaults_to_5(self):
        ev = _event(event_type="future_hook_event")
        assert _severity_score(ev) == 5

    def test_near_52w_low_gets_10(self):
        ev = _event(event_type=EVENT_TYPE_NEAR_52W_LOW)
        assert _severity_score(ev) == 10


class TestUniverseScore:
    def test_portfolio_gets_20(self):
        ev = _event(universe=UNIVERSE_PORTFOLIO)
        assert _universe_score(ev) == 20

    def test_tii75_gets_10(self):
        ev = _event(universe=UNIVERSE_TII75)
        assert _universe_score(ev) == 10

    def test_other_gets_0(self):
        ev = _event(universe=UNIVERSE_OTHER)
        assert _universe_score(ev) == 0


class TestValuationScore:
    def test_within_2pct_gets_25(self):
        ev = _event(event_type=EVENT_TYPE_VALUATION_TRIGGER, distance_to_low_pct=1.5)
        assert _valuation_score(ev) == 25

    def test_within_5pct_gets_15(self):
        ev = _event(event_type=EVENT_TYPE_NEAR_52W_LOW, distance_to_low_pct=3.8)
        assert _valuation_score(ev) == 15

    def test_above_5pct_gets_0(self):
        ev = _event(distance_to_low_pct=10.0)
        assert _valuation_score(ev) == 0

    def test_none_distance_gets_0(self):
        ev = _event(distance_to_low_pct=None)
        assert _valuation_score(ev) == 0


class TestRecencyScore:
    def test_within_2_hours_gets_10(self):
        ts = (dt.datetime.utcnow() - dt.timedelta(hours=1)).isoformat() + "Z"
        ev = _event(timestamp=ts)
        assert _recency_score(ev) == 10

    def test_same_day_gets_5(self):
        ts = (dt.datetime.utcnow() - dt.timedelta(hours=12)).isoformat() + "Z"
        ev = _event(timestamp=ts)
        assert _recency_score(ev) == 5

    def test_old_event_gets_0(self):
        ts = (dt.datetime.utcnow() - dt.timedelta(days=3)).isoformat() + "Z"
        ev = _event(timestamp=ts)
        assert _recency_score(ev) == 0

    def test_empty_timestamp_gets_0(self):
        # Manually override __post_init__ by using a space (not empty — it autofills)
        ev = _event()
        ev.timestamp = "not-a-date"
        assert _recency_score(ev) == 0


class TestScoreEvent:
    def test_portfolio_earnings_recent_scores_high(self):
        ts = (dt.datetime.utcnow() - dt.timedelta(minutes=30)).isoformat() + "Z"
        ev = _event(
            event_type=EVENT_TYPE_EARNINGS_RELEASE,
            universe=UNIVERSE_PORTFOLIO,
            timestamp=ts,
        )
        score = score_event(ev)
        # severity(50) + universe(20) + recency(10) = 80
        assert score >= 80

    def test_generic_other_gets_low_score(self):
        ts = (dt.datetime.utcnow() - dt.timedelta(days=2)).isoformat() + "Z"
        ev = _event(
            event_type=EVENT_TYPE_GENERIC_NEWS,
            universe=UNIVERSE_OTHER,
            timestamp=ts,
        )
        score = score_event(ev)
        assert score <= 10


class TestScoreToPriority:
    def test_critical(self):
        assert score_to_priority(80) == "CRITICAL"

    def test_high(self):
        assert score_to_priority(70) == "HIGH"

    def test_medium(self):
        assert score_to_priority(40) == "MEDIUM"

    def test_low(self):
        assert score_to_priority(15) == "LOW"

    def test_fyi(self):
        assert score_to_priority(5) == "FYI"

    def test_exactly_at_boundary(self):
        assert score_to_priority(55) == "HIGH"
        assert score_to_priority(54) == "MEDIUM"
        assert score_to_priority(30) == "MEDIUM"
        assert score_to_priority(29) == "LOW"
        assert score_to_priority(10) == "LOW"
        assert score_to_priority(9) == "FYI"
