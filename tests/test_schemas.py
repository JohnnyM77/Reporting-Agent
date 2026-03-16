# tests/test_schemas.py
#
# Tests for master_engine/schemas.py

from __future__ import annotations

import datetime as dt

from master_engine.schemas import (
    InvestorEvent,
    AGENT_BOB, AGENT_NED, AGENT_WALLY,
    EVENT_TYPE_EARNINGS_RELEASE,
    EVENT_TYPE_GENERIC_NEWS,
    EVENT_TYPE_NEAR_52W_LOW,
    PRIORITY_FYI,
    UNIVERSE_OTHER,
)


def test_investor_event_defaults():
    ev = InvestorEvent(
        ticker="NHC.AX",
        company_name="New Hope Corporation",
        agent=AGENT_BOB,
        event_type=EVENT_TYPE_EARNINGS_RELEASE,
        headline="Results released",
        timestamp="2025-01-01T00:00:00Z",
    )
    assert ev.priority == PRIORITY_FYI
    assert ev.score == 0
    assert ev.universe == UNIVERSE_OTHER
    assert ev.source_links == {}
    assert ev.summary == ""
    assert ev.action == ""


def test_investor_event_to_dict_round_trip():
    ev = InvestorEvent(
        ticker="BHP.AX",
        company_name="BHP Group",
        agent=AGENT_NED,
        event_type=EVENT_TYPE_GENERIC_NEWS,
        headline="BHP news item",
        timestamp="2025-06-01T10:00:00Z",
        summary="Some summary",
        score=42,
        priority="HIGH",
        source_links={"quote_page": "https://finance.yahoo.com/quote/BHP.AX"},
    )
    data = ev.to_dict()
    ev2 = InvestorEvent.from_dict(data)
    assert ev2.ticker == ev.ticker
    assert ev2.score == ev.score
    assert ev2.priority == ev.priority
    assert ev2.source_links == ev.source_links


def test_dedup_key_case_insensitive():
    ev1 = InvestorEvent(
        ticker="nhc.ax",
        company_name="New Hope",
        agent=AGENT_BOB,
        event_type=EVENT_TYPE_EARNINGS_RELEASE,
        headline="Half Year Results  ",
        timestamp="2025-01-01T00:00:00Z",
    )
    ev2 = InvestorEvent(
        ticker="NHC.AX",
        company_name="New Hope Corporation",
        agent=AGENT_NED,
        event_type=EVENT_TYPE_EARNINGS_RELEASE,
        headline="half year results",
        timestamp="2025-01-01T01:00:00Z",
    )
    assert ev1.dedup_key() == ev2.dedup_key()


def test_dedup_key_different_event_types():
    ev1 = InvestorEvent(
        ticker="NHC.AX",
        company_name="New Hope",
        agent=AGENT_BOB,
        event_type=EVENT_TYPE_EARNINGS_RELEASE,
        headline="NHC news",
        timestamp="2025-01-01T00:00:00Z",
    )
    ev2 = InvestorEvent(
        ticker="NHC.AX",
        company_name="New Hope",
        agent=AGENT_WALLY,
        event_type=EVENT_TYPE_NEAR_52W_LOW,
        headline="NHC news",
        timestamp="2025-01-01T00:00:00Z",
    )
    assert ev1.dedup_key() != ev2.dedup_key()


def test_auto_timestamp_on_empty():
    ev = InvestorEvent(
        ticker="X",
        company_name="X Corp",
        agent=AGENT_NED,
        event_type=EVENT_TYPE_GENERIC_NEWS,
        headline="Something",
        timestamp="",
    )
    # __post_init__ should have filled in a non-empty timestamp
    assert ev.timestamp != ""
