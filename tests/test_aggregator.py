# tests/test_aggregator.py
#
# Tests for master_engine/aggregator.py

from __future__ import annotations

import datetime as dt

from master_engine.aggregator import aggregate, deduplicate
from master_engine.schemas import (
    InvestorEvent,
    AGENT_BOB, AGENT_NED, AGENT_WALLY,
    EVENT_TYPE_EARNINGS_RELEASE,
    EVENT_TYPE_NEAR_52W_LOW,
    EVENT_TYPE_GENERIC_NEWS,
)


def _make_event(ticker, agent, event_type, headline):
    return InvestorEvent(
        ticker=ticker,
        company_name=ticker,
        agent=agent,
        event_type=event_type,
        headline=headline,
        timestamp=dt.datetime.utcnow().isoformat() + "Z",
    )


def test_aggregate_all_agents():
    def ned(): return [_make_event("CSL.AX", AGENT_NED, EVENT_TYPE_GENERIC_NEWS, "CSL news")]
    def wally(): return [_make_event("BHP.AX", AGENT_WALLY, EVENT_TYPE_NEAR_52W_LOW, "BHP near low")]
    def bob(): return [_make_event("NHC.AX", AGENT_BOB, EVENT_TYPE_EARNINGS_RELEASE, "NHC results")]

    events = aggregate(ned, wally, bob)
    assert len(events) == 3
    tickers = {e.ticker for e in events}
    assert tickers == {"CSL.AX", "BHP.AX", "NHC.AX"}


def test_aggregate_with_none_collector():
    def ned(): return [_make_event("CSL.AX", AGENT_NED, EVENT_TYPE_GENERIC_NEWS, "CSL news")]

    events = aggregate(ned_collector=ned, wally_collector=None, bob_collector=None)
    assert len(events) == 1
    assert events[0].ticker == "CSL.AX"


def test_aggregate_deduplication():
    # Same ticker + event_type + headline from two agents → deduplicated to 1
    dup = _make_event("NHC.AX", AGENT_BOB, EVENT_TYPE_EARNINGS_RELEASE, "NHC Half Year Results")
    dup2 = _make_event("NHC.AX", AGENT_NED, EVENT_TYPE_EARNINGS_RELEASE, "nhc half year results")

    def ned(): return [dup2]
    def bob(): return [dup]

    events = aggregate(ned_collector=ned, bob_collector=bob)
    assert len(events) == 1


def test_aggregate_collector_error_does_not_crash():
    def bad_ned():
        raise RuntimeError("Ned is broken")

    def bob(): return [_make_event("NHC.AX", AGENT_BOB, EVENT_TYPE_EARNINGS_RELEASE, "NHC results")]

    events = aggregate(ned_collector=bad_ned, bob_collector=bob)
    assert len(events) == 1


def test_deduplicate_removes_duplicates():
    ev1 = _make_event("NHC.AX", AGENT_BOB, EVENT_TYPE_EARNINGS_RELEASE, "NHC Half Year Results")
    ev2 = _make_event("NHC.AX", AGENT_NED, EVENT_TYPE_EARNINGS_RELEASE, "nhc half year results")
    ev3 = _make_event("BHP.AX", AGENT_NED, EVENT_TYPE_GENERIC_NEWS, "BHP news")

    unique = deduplicate([ev1, ev2, ev3])
    assert len(unique) == 2


def test_deduplicate_preserves_order():
    ev1 = _make_event("A.AX", AGENT_BOB, EVENT_TYPE_EARNINGS_RELEASE, "A results")
    ev2 = _make_event("B.AX", AGENT_NED, EVENT_TYPE_GENERIC_NEWS, "B news")

    unique = deduplicate([ev1, ev2])
    assert unique[0].ticker == "A.AX"
    assert unique[1].ticker == "B.AX"
