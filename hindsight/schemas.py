"""Pydantic models: the event schema, what the model must return, and the
records kept in the private store.

The LLM output models are deliberately strict about shape and lenient about
content. Content rules that need context (is this evidence ref real? does
this warning have a prediction?) live in ``validators.py`` so a single bad
claim is downgraded rather than failing the whole analysis.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

SOURCES = ("BOB", "SALLY", "WALLY", "THEO", "SCHEDULER", "MANUAL")
EVENT_TYPES = (
    "HIGH_IMPACT_EVENT",
    "SELL_SIGNAL",
    "TOP_OPPORTUNITY",
    "WATCHLIST_DAILY",
    "PORTFOLIO_REVIEW",
    "AUTOPSY_DUE",
)
PRIORITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}

ClaimType = Literal["FACT", "INTERPRETATION", "MANAGEMENT_CLAIM", "AGENT_CLAIM", "HINDSIGHT_INFERENCE"]
PowerStatus = Literal["PRESENT", "EMERGING", "ABSENT", "UNCLEAR"]
Confidence = Literal["LOW", "MEDIUM", "HIGH"]
BiasState = Literal[
    "KNOWN VULNERABILITY",
    "OBSERVED EVIDENCE",
    "CURRENTLY TRIGGERED",
    "NOT EVIDENT",
    "UNKNOWN",
    "NOT ASSESSABLE",
]
EVIDENCED_BIAS_STATES = ("OBSERVED EVIDENCE", "CURRENTLY TRIGGERED")
Direction = Literal["HOLD", "ADD", "BUY", "SELL", "IGNORE_EVIDENCE", "NONE"]
Severity = Literal["GREEN", "AMBER", "RED", "LOLLAPALOOZA"]
SEVERITY_RANK = {"GREEN": 0, "AMBER": 1, "RED": 2, "LOLLAPALOOZA": 3}
ThesisClass = Literal["THESIS INTACT", "STRENGTHENED", "WEAKENED", "CHANGED", "BROKEN"]
TriageStatus = Literal["NO CHANGE", "WATCH", "PROVOCATE", "ESCALATE"]
AnalysisStatus = Literal["OK", "SKIPPED_CAP", "FAILED_API", "FAILED_INVALID"]
ReportType = Literal["SELL_ALERT", "BOB_REVIEW", "WALLY_REVIEW", "PORTFOLIO_REVIEW", "AUTOPSY"]

CLAIM_TAGS = {
    "FACT": "[Fact]",
    "INTERPRETATION": "[Interp]",
    "MANAGEMENT_CLAIM": "[Mgmt]",
    "AGENT_CLAIM": "[Agent]",
    "HINDSIGHT_INFERENCE": "[Hindsight]",
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")



# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class HindsightEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: Literal["BOB", "SALLY", "WALLY", "THEO", "SCHEDULER", "MANUAL"]
    event_type: Literal[
        "HIGH_IMPACT_EVENT", "SELL_SIGNAL", "TOP_OPPORTUNITY", "WATCHLIST_DAILY", "PORTFOLIO_REVIEW", "AUTOPSY_DUE"
    ]
    event_subtype: str = ""
    ticker: str = ""
    tickers: list[str] = Field(default_factory=list)
    priority: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] = "LOW"
    timestamp: str = Field(default_factory=_now)
    source_report_ref: str = ""
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    def dedup_key(self) -> str:
        return "|".join([self.source, self.ticker.upper(), self.event_type, self.source_report_ref])


# ---------------------------------------------------------------------------
# What the model returns
# ---------------------------------------------------------------------------


class Claim(BaseModel):
    text: str
    claim_type: ClaimType = "HINDSIGHT_INFERENCE"
    source_refs: list[str] = Field(default_factory=list)

    @field_validator("claim_type", mode="before")
    @classmethod
    def _ct(cls, v):
        return v.strip().upper().replace(" ", "_") if isinstance(v, str) else v


class PowerAssessment(BaseModel):
    power: str
    status: PowerStatus = "UNCLEAR"
    benefit_evidence: list[Claim] = Field(default_factory=list)
    barrier_evidence: list[Claim] = Field(default_factory=list)
    evidence_against: list[Claim] = Field(default_factory=list)
    durability: str = ""
    confidence: Confidence = "LOW"
    source_refs: list[str] = Field(default_factory=list)
    change_vs_last: str = ""

    @field_validator("status", "confidence", mode="before")
    @classmethod
    def _up(cls, v):
        return v.strip().upper() if isinstance(v, str) else v


class TendencyAssessment(BaseModel):
    tendency: str
    state: BiasState = "UNKNOWN"
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)
    direction: Direction = "NONE"
    potential_consequence: str = ""
    antidote_question: str = ""
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("state", mode="before")
    @classmethod
    def _state(cls, v):
        return " ".join(v.replace("_", " ").split()).upper() if isinstance(v, str) else v

    @field_validator("direction", mode="before")
    @classmethod
    def _dir(cls, v):
        return v.strip().upper().replace(" ", "_") if isinstance(v, str) else v


class Prediction(BaseModel):
    metric: str
    direction: str
    threshold: str
    check_date: str  # ISO date, or "next results"

    def due(self, today: dt.date, results_since: Optional[dt.date] = None, raised: Optional[dt.date] = None) -> bool:
        if self.check_date.strip().lower().startswith("next result"):
            return results_since is not None and (raised is None or results_since > raised)
        try:
            return dt.date.fromisoformat(self.check_date[:10]) <= today
        except ValueError:
            return False


class HindsightWarning(BaseModel):
    text: str
    severity: Severity = "AMBER"
    predictions: list[Prediction] = Field(default_factory=list)

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v):
        return v.strip().upper() if isinstance(v, str) else v


class ThesisTest(BaseModel):
    classification: ThesisClass = "THESIS INTACT"
    reason: str = ""
    evidence_changed_or_thesis_changed: str = ""

    @field_validator("classification", mode="before")
    @classmethod
    def _cls(cls, v):
        if not isinstance(v, str):
            return v
        v = " ".join(v.replace("_", " ").split()).upper()
        return "THESIS INTACT" if v in ("INTACT", "THESIS INTACT") else v


class FreshCapitalTest(BaseModel):
    would_buy_today: Literal["YES", "NO", "SMALLER"]
    weight_if_new: str = ""
    reasoning: str
    holding_held_to_lower_standard: bool = False

    @field_validator("would_buy_today", mode="before")
    @classmethod
    def _up(cls, v):
        return v.strip().upper() if isinstance(v, str) else v


class ForcedSaleTest(BaseModel):
    would_rebuy: Literal["YES", "NO", "UNSURE"]
    conviction_or_attachment: str = ""
    reasoning: str

    @field_validator("would_rebuy", mode="before")
    @classmethod
    def _up(cls, v):
        return v.strip().upper() if isinstance(v, str) else v


class Disagreement(BaseModel):
    with_agent: str = Field(alias="with")
    point: str

    model_config = {"populate_by_name": True}


class AnalysisOutput(BaseModel):
    """One full report. ``sections`` carries the report-type headings."""

    sections: dict[str, list[Claim]] = Field(default_factory=dict)
    strongest_opposing_case: str
    distinguishing_evidence: list[str] = Field(default_factory=list)
    thesis_test: Optional[ThesisTest] = None
    seven_powers: list[PowerAssessment] = Field(default_factory=list)
    seven_powers_change: str = ""
    munger_scan: list[TendencyAssessment] = Field(default_factory=list)
    lollapalooza_claimed: bool = False
    lollapalooza_explanation: str = ""
    fresh_capital_test: Optional[FreshCapitalTest] = None
    forced_sale_test: Optional[ForcedSaleTest] = None
    tax_note: str = ""
    breakeven_check: str = ""
    what_would_change_my_mind: list[str] = Field(default_factory=list)
    unanswered_questions: list[str] = Field(default_factory=list)
    questions_for_theo: list[str] = Field(default_factory=list)
    disagreements: list[Disagreement] = Field(default_factory=list)
    warnings: list[HindsightWarning] = Field(default_factory=list)
    severity: Severity
    severity_reason: str
    verdict: str

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v):
        return v.strip().upper() if isinstance(v, str) else v


class TriageOutput(BaseModel):
    status: Literal["WATCH", "PROVOCATE", "ESCALATE"]
    reason: str

    @field_validator("status", mode="before")
    @classmethod
    def _up(cls, v):
        return v.strip().upper() if isinstance(v, str) else v


class AutopsyOutput(BaseModel):
    warning_right: Literal["YES", "NO", "PARTLY", "TOO EARLY"]
    useful: bool
    what_it_missed: str = ""
    bias_read_correct: Literal["YES", "NO", "PARTLY", "UNKNOWN"] = "UNKNOWN"
    johnny_acted: str = ""
    notes: str = ""

    @field_validator("warning_right", "bias_read_correct", mode="before")
    @classmethod
    def _up(cls, v):
        return " ".join(v.replace("_", " ").split()).upper() if isinstance(v, str) else v


# ---------------------------------------------------------------------------
# Stored records. All carry timestamps and source refs.
# ---------------------------------------------------------------------------


class Record(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    ticker: str = ""
    created_at: str = Field(default_factory=_now)
    status: str = ""
    source_refs: list[str] = Field(default_factory=list)


class Company(Record):
    name: str = ""
    exchange: str = "ASX"


class Position(Record):
    avg_cost: Optional[float] = None
    tranches: list[dict[str, Any]] = Field(default_factory=list)
    weight: Optional[float] = None


class ThesisSnapshot(Record):
    """Read from Theo, never authored here."""

    version_ref: str = ""
    classification: str = ""
    red_flags: list[str] = Field(default_factory=list)
    revision_count: int = 0


class SevenPowersRecord(Record):
    assessments: list[PowerAssessment] = Field(default_factory=list)
    report_id: str = ""


class BiasRecord(Record):
    assessments: list[TendencyAssessment] = Field(default_factory=list)
    report_id: str = ""
    downgrades: list[str] = Field(default_factory=list)


class HindsightReport(Record):
    report_type: str = ""
    event_id: str = ""
    analysis_status: str = "OK"
    severity: str = ""
    verdict: str = ""
    error: str = ""
    markdown: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    validation_notes: list[str] = Field(default_factory=list)


class HindsightQuestion(Record):
    question: str = ""
    report_id: str = ""
    for_theo: bool = False
    answered_at: str = ""


class WarningRecord(Record):
    report_id: str = ""
    text: str = ""
    severity: str = "AMBER"
    predictions: list[Prediction] = Field(default_factory=list)
    tendencies: list[str] = Field(default_factory=list)
    lollapalooza: bool = False
    raised_price: Optional[float] = None


class HindsightOutcome(Record):
    warning_id: str = ""
    outcome: str = ""


class Autopsy(Record):
    warning_id: str = ""
    report_id: str = ""
    result: dict[str, Any] = Field(default_factory=dict)


class JohnnyResponse(Record):
    action: Literal["held", "sold", "trimmed", "added", "ignored"]
    note: str = ""
    inferred: bool = False
    report_id: str = ""
