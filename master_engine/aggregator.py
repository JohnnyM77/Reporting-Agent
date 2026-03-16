# master_engine/aggregator.py
#
# Collects normalized InvestorEvent objects from all three agents (Ned, Wally,
# Bob) and returns a de-duplicated, consolidated list ready for scoring.

from __future__ import annotations

import logging
from typing import Callable

from .schemas import InvestorEvent

logger = logging.getLogger(__name__)


def _collect_from_agent(
    agent_name: str,
    collector_fn: Callable[[], list[InvestorEvent]],
) -> list[InvestorEvent]:
    """
    Call a single agent's event-collector function, catch any errors, and
    return the (possibly empty) list of events.
    """
    try:
        events = collector_fn()
        logger.info("[aggregator] %s → %d event(s) collected", agent_name, len(events))
        return events
    except Exception as exc:
        logger.error("[aggregator] %s collector failed: %s", agent_name, exc)
        return []


def deduplicate(events: list[InvestorEvent]) -> list[InvestorEvent]:
    """
    Remove duplicate events based on their dedup_key().

    When duplicates exist the first occurrence is kept (collectors should
    return events in relevance order so the best one comes first).
    """
    seen: set[str] = set()
    unique: list[InvestorEvent] = []
    for event in events:
        key = event.dedup_key()
        if key in seen:
            logger.debug("[aggregator] duplicate skipped: %s", key)
            continue
        seen.add(key)
        unique.append(event)
    removed = len(events) - len(unique)
    if removed:
        logger.info("[aggregator] de-duplication removed %d duplicate(s)", removed)
    return unique


def aggregate(
    ned_collector: Callable[[], list[InvestorEvent]] | None = None,
    wally_collector: Callable[[], list[InvestorEvent]] | None = None,
    bob_collector: Callable[[], list[InvestorEvent]] | None = None,
) -> list[InvestorEvent]:
    """
    Aggregate events from all available agent collectors.

    Each collector is a zero-argument callable that returns a list of
    InvestorEvent objects.  Passing ``None`` for a collector skips that agent.

    Returns
    -------
    list[InvestorEvent]
        De-duplicated list of all events from all agents.
    """
    all_events: list[InvestorEvent] = []

    agents = [
        ("ned", ned_collector),
        ("wally", wally_collector),
        ("bob", bob_collector),
    ]

    for agent_name, collector in agents:
        if collector is None:
            logger.info("[aggregator] %s collector not provided — skipping", agent_name)
            continue
        events = _collect_from_agent(agent_name, collector)
        all_events.extend(events)

    logger.info(
        "[aggregator] total before de-duplication: %d event(s)", len(all_events)
    )
    unique = deduplicate(all_events)
    logger.info(
        "[aggregator] total after de-duplication: %d event(s)", len(unique)
    )
    return unique
