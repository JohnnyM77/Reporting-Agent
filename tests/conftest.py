# tests/conftest.py
#
# Shared test fixtures for Master Engine and Super Investor Agent tests.

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

# Add repo root to path so imports work from tests/ directory
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture
def sample_event():
    """A minimal valid InvestorEvent for testing."""
    from master_engine.schemas import InvestorEvent, AGENT_BOB, EVENT_TYPE_EARNINGS_RELEASE
    return InvestorEvent(
        ticker="NHC.AX",
        company_name="New Hope Corporation Limited",
        agent=AGENT_BOB,
        event_type=EVENT_TYPE_EARNINGS_RELEASE,
        headline="NHC Half Year Results Released",
        timestamp=dt.datetime.utcnow().isoformat() + "Z",
        summary="Strong half-year results with increased dividend.",
        action="Read full report",
    )


@pytest.fixture
def sample_events():
    """A list of InvestorEvents with mixed types and priorities."""
    from master_engine.schemas import (
        InvestorEvent, AGENT_BOB, AGENT_NED, AGENT_WALLY,
        EVENT_TYPE_EARNINGS_RELEASE, EVENT_TYPE_NEAR_52W_LOW,
        EVENT_TYPE_GENERIC_NEWS, UNIVERSE_PORTFOLIO,
    )
    return [
        InvestorEvent(
            ticker="NHC.AX",
            company_name="New Hope Corporation",
            agent=AGENT_BOB,
            event_type=EVENT_TYPE_EARNINGS_RELEASE,
            headline="NHC Half Year Results",
            timestamp=dt.datetime.utcnow().isoformat() + "Z",
            universe=UNIVERSE_PORTFOLIO,
        ),
        InvestorEvent(
            ticker="POOL",
            company_name="Pool Corporation",
            agent=AGENT_WALLY,
            event_type=EVENT_TYPE_NEAR_52W_LOW,
            headline="POOL Trading at 3.8% above 52 week low",
            timestamp=dt.datetime.utcnow().isoformat() + "Z",
            distance_to_low_pct=3.8,
        ),
        InvestorEvent(
            ticker="CSL.AX",
            company_name="CSL Limited",
            agent=AGENT_NED,
            event_type=EVENT_TYPE_GENERIC_NEWS,
            headline="CSL mentioned in biotech roundup",
            timestamp=dt.datetime.utcnow().isoformat() + "Z",
        ),
    ]
