# agents/super_investor/agent.py
#
# Top-level orchestration for the Super Investor workflow.
#
# The Super Investor Agent:
#   1. Ingests normalized InvestorEvent objects from the Master Engine aggregator
#   2. Loads watchlist/universe membership to enrich ``universe`` fields
#   3. De-duplicates repeated items for the same ticker/headline/event_type
#   4. Scores each event via scoring.py
#   5. Assigns priority (CRITICAL / HIGH / MEDIUM / LOW / FYI)
#   6. Sorts descending by score
#   7. Generates a final digest via digest.py

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Optional

from master_engine.schemas import (
    InvestorEvent,
    UNIVERSE_PORTFOLIO,
    UNIVERSE_HIGH_CONVICTION,
    UNIVERSE_TII75,
    UNIVERSE_OTHER,
)
from master_engine.aggregator import aggregate
from master_engine.linker import attach_links
from master_engine.prioritizer import prioritize

from .digest import generate_digest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Watchlist membership loader
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_WATCHLISTS_YAML = _REPO_ROOT / "config" / "watchlists.yaml"


def _load_universe_membership() -> dict[str, str]:
    """
    Return a mapping of ticker → universe type from config/watchlists.yaml.

    Falls back to an empty dict if the file is missing or malformed.
    """
    if not _WATCHLISTS_YAML.exists():
        return {}
    try:
        import yaml
        with open(_WATCHLISTS_YAML) as fh:
            data = yaml.safe_load(fh) or {}

        membership: dict[str, str] = {}
        for ticker in data.get("portfolio", []):
            membership[str(ticker).upper()] = UNIVERSE_PORTFOLIO
        for ticker in data.get("high_conviction", []):
            key = str(ticker).upper()
            if key not in membership:
                membership[key] = UNIVERSE_HIGH_CONVICTION
        for ticker in data.get("tii75", []):
            key = str(ticker).upper()
            if key not in membership:
                membership[key] = UNIVERSE_TII75

        logger.debug(
            "[super_investor] Loaded %d universe memberships", len(membership)
        )
        return membership
    except Exception as exc:
        logger.warning(
            "[super_investor] Could not load watchlists.yaml: %s", exc
        )
        return {}


def _enrich_universe(
    events: list[InvestorEvent],
    membership: dict[str, str],
) -> list[InvestorEvent]:
    """
    Set the ``universe`` field on each event based on ticker membership.
    Events already tagged with a non-default universe are left unchanged.
    """
    for event in events:
        if event.universe == UNIVERSE_OTHER:
            event.universe = membership.get(
                event.ticker.upper(), UNIVERSE_OTHER
            )
    return events


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

def run(
    ned_collector: Optional[Callable[[], list[InvestorEvent]]] = None,
    wally_collector: Optional[Callable[[], list[InvestorEvent]]] = None,
    bob_collector: Optional[Callable[[], list[InvestorEvent]]] = None,
    output_dir: Optional[Path] = None,
    run_date: Optional[str] = None,
    send_email: bool = True,
) -> dict[str, object]:
    """
    Run the full Super Investor workflow.

    Parameters
    ----------
    ned_collector, wally_collector, bob_collector
        Zero-argument callables returning lists of InvestorEvent.
        Pass None to skip that agent.
    output_dir : Path, optional
        Directory to write digest files.  Defaults to ``outputs/<run_date>/``.
    run_date : str, optional
        ISO date string (defaults to today UTC).
    send_email : bool
        Whether to send the digest email.

    Returns
    -------
    dict
        Summary dict with keys: ``events``, ``total``, ``digest``,
        ``email_sent``.
    """
    import datetime as dt

    if run_date is None:
        run_date = dt.datetime.utcnow().date().isoformat()

    if output_dir is None:
        output_dir = _REPO_ROOT / "outputs" / run_date

    logger.info("[super_investor] Starting run for %s", run_date)

    # Step 1: Aggregate from all agents
    events = aggregate(
        ned_collector=ned_collector,
        wally_collector=wally_collector,
        bob_collector=bob_collector,
    )
    logger.info("[super_investor] %d event(s) after aggregation", len(events))

    # Step 2: Enrich universe membership
    membership = _load_universe_membership()
    events = _enrich_universe(events, membership)

    # Step 3: Attach source links
    events = attach_links(events)

    # Step 5: Score and rank
    events = prioritize(events)
    logger.info("[super_investor] %d event(s) scored and ranked", len(events))

    # Step 6: Generate digest
    digest = generate_digest(events, output_dir, run_date)

    # Step 7: Notify
    from master_engine.notifier import send_email as _send_email
    email_sent = False
    if send_email:
        subject = f"Johnny Master Investor Alert — {run_date} ({len(events)} alert(s))"
        email_sent = _send_email(
            subject=subject,
            plain_text=str(digest["plain_text"]),
            html_body=str(digest["html"]),
        )

    logger.info(
        "[super_investor] Run complete — %d event(s), email_sent=%s",
        len(events), email_sent,
    )

    return {
        "events": events,
        "total": len(events),
        "digest": digest,
        "email_sent": email_sent,
        "run_date": run_date,
        "output_dir": output_dir,
    }
