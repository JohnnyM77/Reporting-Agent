# master_engine/schemas.py
#
# Common event schema used by Ned, Wally, and Bob to emit normalized
# investor events into the Master Engine.

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# Priority levels (ordered highest to lowest)
# ---------------------------------------------------------------------------
PRIORITY_CRITICAL = "CRITICAL"
PRIORITY_HIGH = "HIGH"
PRIORITY_MEDIUM = "MEDIUM"
PRIORITY_LOW = "LOW"
PRIORITY_FYI = "FYI"

PRIORITY_ORDER = [
    PRIORITY_CRITICAL,
    PRIORITY_HIGH,
    PRIORITY_MEDIUM,
    PRIORITY_LOW,
    PRIORITY_FYI,
]

# ---------------------------------------------------------------------------
# Canonical event types
# ---------------------------------------------------------------------------
EVENT_TYPE_EARNINGS_RELEASE = "earnings_release"
EVENT_TYPE_GUIDANCE_CHANGE = "guidance_change"
EVENT_TYPE_CAPITAL_RAISE = "capital_raise"
EVENT_TYPE_TAKEOVER = "takeover"
EVENT_TYPE_REGULATOR_ACTION = "regulator_action"
EVENT_TYPE_MAJOR_CONTRACT = "major_contract"
EVENT_TYPE_CEO_CHANGE = "ceo_change"
EVENT_TYPE_LITIGATION = "litigation"
EVENT_TYPE_PROFIT_WARNING = "profit_warning"
EVENT_TYPE_VALUATION_TRIGGER = "valuation_trigger"
EVENT_TYPE_NEAR_52W_LOW = "near_52w_low"
EVENT_TYPE_GENERIC_NEWS = "generic_news"
EVENT_TYPE_APPENDIX_4D = "appendix_4d"
EVENT_TYPE_APPENDIX_4E = "appendix_4e"
EVENT_TYPE_ACQUISITION = "acquisition"

# ---------------------------------------------------------------------------
# Agent identifiers
# ---------------------------------------------------------------------------
AGENT_BOB = "bob"
AGENT_NED = "ned"
AGENT_WALLY = "wally"

# ---------------------------------------------------------------------------
# Universe / watchlist types
# ---------------------------------------------------------------------------
UNIVERSE_PORTFOLIO = "portfolio"
UNIVERSE_HIGH_CONVICTION = "high_conviction"
UNIVERSE_TII75 = "TII75"
UNIVERSE_OTHER = "other"


@dataclass
class InvestorEvent:
    """
    Normalized investor event emitted by any agent and consumed by the
    Master Engine / Super Investor Agent.

    Required fields
    ---------------
    ticker         : ASX/exchange ticker symbol (e.g. "NHC.AX", "POOL")
    company_name   : Human-readable company name
    agent          : Originating agent: "bob", "ned", or "wally"
    event_type     : Canonical event type string (see EVENT_TYPE_* constants)
    headline       : Short one-line description of the event
    timestamp      : ISO-8601 timestamp of when the event occurred / was detected

    Optional fields
    ---------------
    priority       : CRITICAL / HIGH / MEDIUM / LOW / FYI (set by scoring)
    score          : Numeric score (set by prioritizer)
    summary        : One-paragraph context / analysis
    thesis_impact  : How this event affects the investment thesis
    action         : Recommended investor action
    source_links   : Dict of labelled URLs (see linker.py)
    universe       : portfolio / high_conviction / TII75 / other
    distance_to_low_pct : Distance from current price to 52w low (Wally events)
    drive_report_link   : Google Drive link for Bob-generated PDF reports
    asx_url             : Direct ASX announcement URL
    """

    ticker: str
    company_name: str
    agent: str
    event_type: str
    headline: str
    timestamp: str

    # Scoring / priority — populated by the prioritizer
    priority: str = PRIORITY_FYI
    score: int = 0

    # Narrative fields
    summary: str = ""
    thesis_impact: str = ""
    action: str = ""

    # Link metadata
    source_links: dict = field(default_factory=dict)

    # Classification metadata
    universe: str = UNIVERSE_OTHER

    # Wally-specific
    distance_to_low_pct: Optional[float] = None

    # Bob-specific
    drive_report_link: Optional[str] = None
    asx_url: Optional[str] = None

    def to_dict(self) -> dict:
        """Return a plain dict representation (JSON-serializable)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "InvestorEvent":
        """Reconstruct an InvestorEvent from a plain dict."""
        # Only pass known fields to avoid TypeError on extra keys
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    def dedup_key(self) -> str:
        """
        Key used for de-duplication: same ticker + event_type + headline
        (case-insensitive, whitespace-normalised).
        """
        return "|".join([
            self.ticker.upper(),
            self.event_type.lower(),
            " ".join(self.headline.lower().split()),
        ])

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = dt.datetime.utcnow().isoformat() + "Z"


def normalise_ticker(ticker: str, exchange: str = "AX") -> str:
    """
    Normalise a bare ticker to ``TICKER.{exchange}`` format.

    If the ticker already contains a dot (exchange suffix), it is returned
    unchanged.  The most common case is ASX tickers: ``"NHC"`` → ``"NHC.AX"``.

    Parameters
    ----------
    ticker : str
        Ticker symbol (e.g. ``"NHC"``, ``"NHC.AX"``, ``"POOL"``, ``"RR.L"``).
    exchange : str
        Suffix to append when no suffix is present.  Defaults to ``"AX"``.
    """
    if "." in ticker:
        return ticker
    return f"{ticker}.{exchange}"
