# wally/emit.py
#
# Emits normalized InvestorEvent objects from Wally's watchlist screening.
#
# This module converts TickerScreenResult objects to InvestorEvent objects
# so they can be ingested by the Master Engine aggregator.

from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from master_engine.schemas import (
    InvestorEvent,
    AGENT_WALLY,
    EVENT_TYPE_NEAR_52W_LOW,
    EVENT_TYPE_VALUATION_TRIGGER,
    UNIVERSE_TII75,
    UNIVERSE_OTHER,
)
from wally.config import STANDARD_WATCHLISTS, TII75_WATCHLIST
from wally.data_fetch import fetch_price_snapshot
from wally.screening import screen_snapshot
from wally.watchlist_loader import load_watchlist


def _watchlist_universe(watchlist_name: str) -> str:
    """Map watchlist name to universe type."""
    lower = watchlist_name.lower()
    if "tii75" in lower:
        return UNIVERSE_TII75
    return UNIVERSE_OTHER


def _screen_result_to_event(
    result,
    watchlist_name: str,
    universe: str,
) -> InvestorEvent:
    """Convert a TickerScreenResult to an InvestorEvent."""
    dist = result.distance_to_low_pct

    if dist <= 2.0:
        event_type = EVENT_TYPE_VALUATION_TRIGGER
        headline = (
            f"{result.ticker} — Trading at {dist:.1f}% above 52-week low "
            f"(STRONG valuation trigger)"
        )
        action = "Urgent: Review valuation, thesis and position sizing"
    elif dist <= 5.0:
        event_type = EVENT_TYPE_NEAR_52W_LOW
        headline = (
            f"{result.ticker} — Trading at {dist:.1f}% above 52-week low"
        )
        action = "Review valuation and thesis"
    else:
        event_type = EVENT_TYPE_NEAR_52W_LOW
        headline = (
            f"{result.ticker} — Trading at {dist:.1f}% above 52-week low"
        )
        action = "Monitor — approaching low territory"

    summary = (
        f"Current price: {result.current_price:.3f} | "
        f"52w low: {result.low_52w:.3f} | "
        f"52w high: {result.high_52w:.3f} | "
        f"Distance to low: {dist:.2f}% | "
        f"Watchlist: {watchlist_name}"
    )

    return InvestorEvent(
        ticker=result.ticker,
        company_name=result.company_name,
        agent=AGENT_WALLY,
        event_type=event_type,
        headline=headline,
        timestamp=dt.datetime.utcnow().isoformat() + "Z",
        summary=summary,
        thesis_impact="Stock near 52-week low — potential entry point",
        action=action,
        universe=universe,
        distance_to_low_pct=dist,
    )


def collect_events(
    include_tii75: bool = False,
    threshold_pct: float = 5.0,
) -> list[InvestorEvent]:
    """
    Collect normalized InvestorEvent objects from Wally's watchlist screening.

    Runs the watchlist screens and returns events only for flagged tickers
    (those within *threshold_pct* of their 52-week low).

    Parameters
    ----------
    include_tii75 : bool
        Whether to also screen the TII75 watchlist (normally fortnightly).
    threshold_pct : float
        Distance-to-low threshold for flagging.  Defaults to 5.0%.

    Returns
    -------
    list[InvestorEvent]
    """
    watchlist_paths = list(STANDARD_WATCHLISTS)
    if include_tii75:
        watchlist_paths.append(TII75_WATCHLIST)

    all_events: list[InvestorEvent] = []

    for watchlist_path in watchlist_paths:
        try:
            wl = load_watchlist(watchlist_path)
        except Exception as exc:
            logger.warning("[wally/emit] Could not load %s: %s", watchlist_path, exc)
            continue

        universe = _watchlist_universe(wl.name)
        logger.info(
            "[wally/emit] Screening watchlist: %s (%d tickers)",
            wl.name, len(wl.tickers),
        )

        for ticker in wl.tickers:
            try:
                snap = fetch_price_snapshot(ticker)
                if not snap:
                    logger.debug("[wally/emit] No data for %s — skipping", ticker)
                    continue
                result = screen_snapshot(snap, threshold_pct=threshold_pct)
                if not result.flagged:
                    continue
                event = _screen_result_to_event(result, wl.name, universe)
                all_events.append(event)
                logger.info(
                    "[wally/emit] Flagged: %s (%.1f%% above 52w low)",
                    ticker, result.distance_to_low_pct,
                )
            except Exception as exc:
                logger.warning(
                    "[wally/emit] Error screening %s: %s", ticker, exc
                )

    logger.info(
        "[wally/emit] %d flagged event(s) across %d watchlist(s)",
        len(all_events), len(watchlist_paths),
    )
    return all_events
