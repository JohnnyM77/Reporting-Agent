"""Parse and validate thesis files.

A thesis lives in ``theses/<TICKER>.md`` as YAML frontmatter followed by a free
prose body. The frontmatter is the structured record Theo reasons over; the
body is whatever narrative is worth keeping and is rendered as-is.

Nothing in here holds a number that belongs to the ledger. Prices, share
counts, cost basis and returns are deliberately absent from the schema: if a
figure can be derived from the transaction record then storing it in the thesis
only creates a second version of the truth that quietly rots.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from pathlib import Path
from typing import Any, Iterable

import yaml

# --------------------------------------------------------------------------
# Controlled vocabularies
# --------------------------------------------------------------------------

ARCHETYPES = (
    "COMPOUNDING_MACHINE",
    "CYCLICAL_TRADE",
    "STRUCTURAL_WINNER",
    "SPECULATIVE_PREPROFIT",
    "VALUE_TRAP",
    "MELTING_ICE_CUBE",
)

STATUSES = ("HELD", "EXITED")

# A: written at the time of the decision. B: reconstructed later, but from
# documents that existed at the time. C: memory only — the grade that exists
# so that a thesis reconstructed from a feeling cannot pass itself off as a
# contemporaneous record.
EVIDENCE_GRADES = ("A", "B", "C")

EVIDENCE_GRADE_LABELS = {
    "A": "contemporaneous",
    "B": "reconstructed with documents",
    "C": "memory only",
}

ORIGINS = ("OWN", "BORROWED", "HYBRID")

ALIGNMENTS = ("ALIGNED", "DIVERGED", "INDEPENDENT")

PILLAR_STATUSES = ("INTACT", "STRAINED", "BREACHED")

AMENDMENT_DIRECTIONS = ("LOOSENED", "TIGHTENED", "NEUTRAL")

EXIT_REASONS = (
    "THESIS_BROKEN",
    "THESIS_PLAYED_OUT",
    "BETTER_USE",
    "RISK_MANAGEMENT",
    "LOST_PATIENCE",
    "NEEDED_CASH",
)

MAX_PILLARS = 4
MAX_BET_WORDS = 60

THESES_DIR = Path("theses")

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _as_date(value: Any) -> dt.date | None:
    """Coerce whatever YAML handed back into a date, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _upper(value: Any) -> str:
    return str(value).strip().upper() if value is not None else ""


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def word_count(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w])


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Source:
    """An outside voice that shaped the thesis, and whether we still agree."""

    name: str = ""
    outlet: str = ""
    url: str = ""
    alignment: str = "INDEPENDENT"
    note: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "Source":
        if isinstance(raw, str):
            return cls(name=raw.strip())
        d = _as_dict(raw)
        return cls(
            name=_text(d.get("name")),
            outlet=_text(d.get("outlet")),
            url=_text(d.get("url")),
            alignment=_upper(d.get("alignment")) or "INDEPENDENT",
            note=_text(d.get("note")),
        )

    @property
    def diverged(self) -> bool:
        return self.alignment == "DIVERGED"


@dataclasses.dataclass
class Pillar:
    """One load-bearing reason to own the thing, and what would break it.

    A pillar without a kill condition is not a pillar, it is a feeling. The
    validator refuses the file rather than letting one through.
    """

    id: str = ""
    claim: str = ""
    evidence: str = ""
    kill_condition: str = ""
    status: str = "INTACT"
    note: str = ""

    @classmethod
    def from_raw(cls, raw: Any, index: int = 0) -> "Pillar":
        d = _as_dict(raw)
        return cls(
            id=_text(d.get("id")) or f"P{index + 1}",
            claim=_text(d.get("claim")),
            evidence=_text(d.get("evidence")),
            kill_condition=_text(d.get("kill_condition")),
            status=_upper(d.get("status")) or "INTACT",
            note=_text(d.get("note")),
        )

    @property
    def symbol(self) -> str:
        return {"INTACT": "●", "STRAINED": "◐", "BREACHED": "○"}.get(self.status, "?")

    @property
    def kill_needs_work(self) -> bool:
        return "NEEDS WORK" in self.kill_condition.upper()


@dataclasses.dataclass
class Amendment:
    """A change to a pillar, and — the point of the record — which way."""

    pillar: str = ""
    change: str = ""
    trigger: str = ""
    direction: str = "NEUTRAL"

    @classmethod
    def from_raw(cls, raw: Any) -> "Amendment":
        d = _as_dict(raw)
        return cls(
            pillar=_text(d.get("pillar")),
            change=_text(d.get("change")),
            trigger=_text(d.get("trigger")),
            direction=_upper(d.get("direction")) or "NEUTRAL",
        )

    @property
    def loosened(self) -> bool:
        return self.direction == "LOOSENED"


@dataclasses.dataclass
class Review:
    date: dt.date | None = None
    verdict: str = ""
    pillar_status: dict[str, str] = dataclasses.field(default_factory=dict)
    expected_vs_actual: str = ""
    process_score: Any = None
    outcome_score: Any = None
    amendments: list[Amendment] = dataclasses.field(default_factory=list)
    note: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "Review":
        d = _as_dict(raw)
        return cls(
            date=_as_date(d.get("date")),
            verdict=_text(d.get("verdict")),
            pillar_status={
                _text(k): _upper(v) for k, v in _as_dict(d.get("pillar_status")).items()
            },
            expected_vs_actual=_text(d.get("expected_vs_actual")),
            process_score=d.get("process_score"),
            outcome_score=d.get("outcome_score"),
            amendments=[Amendment.from_raw(a) for a in _as_list(d.get("amendments"))],
            note=_text(d.get("note")),
        )

    @property
    def slug(self) -> str:
        return f"review-{self.date.isoformat()}" if self.date else "review"

    @property
    def label(self) -> str:
        return f"Review {self.date.isoformat()}" if self.date else "Review"


@dataclasses.dataclass
class Exit:
    date: dt.date | None = None
    reason: str = ""
    pillar_failed: str = ""
    kill_condition_fired: Any = None
    sell_thesis: str = ""
    capital_went_to: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "Exit":
        d = _as_dict(raw)
        return cls(
            date=_as_date(d.get("date")),
            reason=_upper(d.get("reason")),
            pillar_failed=_text(d.get("pillar_failed")),
            kill_condition_fired=d.get("kill_condition_fired"),
            sell_thesis=_text(d.get("sell_thesis")),
            capital_went_to=_text(d.get("capital_went_to")),
        )


@dataclasses.dataclass
class Alternative:
    name: str = ""
    why_rejected: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "Alternative | None":
        if raw is None:
            return None
        if isinstance(raw, str):
            return cls(name=raw.strip())
        d = _as_dict(raw)
        if not d:
            return None
        return cls(name=_text(d.get("name")), why_rejected=_text(d.get("why_rejected")))


@dataclasses.dataclass
class Thesis:
    ticker: str = ""
    name: str = ""
    archetype: str = ""
    status: str = "HELD"
    evidence_grade: str = "C"
    conviction: str = ""
    horizon: str = ""
    origin: str = "OWN"
    draft: bool = False
    sources: list[Source] = dataclasses.field(default_factory=list)
    the_bet: str = ""
    what_i_know: str = ""
    pillars: list[Pillar] = dataclasses.field(default_factory=list)
    pre_mortem: list[str] = dataclasses.field(default_factory=list)
    pre_mortem_hindsight: bool = False
    alternative_considered: Alternative | None = None
    management_score: Any = None
    management_verdict: str = ""
    valuation_note: str = ""
    hold_thesis: str = ""
    resolution_date: dt.date | None = None
    resolution_criterion: str = ""
    reviews: list[Review] = dataclasses.field(default_factory=list)
    exit: Exit | None = None
    body: str = ""
    path: Path | None = None
    raw: dict[str, Any] = dataclasses.field(default_factory=dict)

    # -- derived ----------------------------------------------------------

    @property
    def is_exited(self) -> bool:
        return self.status == "EXITED" or (self.exit is not None and self.exit.date is not None)

    @property
    def evidence_grade_label(self) -> str:
        return EVIDENCE_GRADE_LABELS.get(self.evidence_grade, "unknown")

    @property
    def borrowed(self) -> bool:
        return self.origin in ("BORROWED", "HYBRID")

    @property
    def diverged_sources(self) -> list[Source]:
        return [s for s in self.sources if s.diverged]

    @property
    def last_review(self) -> Review | None:
        dated = [r for r in self.reviews if r.date]
        return max(dated, key=lambda r: r.date) if dated else None

    @property
    def display_bet(self) -> str:
        """What the slide leads with: the hold thesis once one exists."""
        return self.hold_thesis or self.the_bet

    def pillar(self, pid: str) -> Pillar | None:
        for p in self.pillars:
            if p.id.upper() == str(pid).upper():
                return p
        return None

    def pillar_status_at(self, review: Review | None) -> dict[str, str]:
        """Pillar statuses as of a given review (falls back to current)."""
        out = {p.id: p.status for p in self.pillars}
        if review is None:
            return out
        for pid, st in review.pillar_status.items():
            out[pid] = st
        return out

    @property
    def pillar_symbols(self) -> str:
        return " ".join(p.symbol for p in self.pillars)

    @property
    def all_amendments(self) -> list[Amendment]:
        return [a for r in self.reviews for a in r.amendments]

    def versions(self) -> list[tuple[str, str]]:
        """(slug, label) for every slide version that exists for this thesis."""
        out = [("entry", "Entry")]
        for r in sorted([r for r in self.reviews if r.date], key=lambda r: r.date):
            out.append((r.slug, r.label))
        out.append(("current", "Current"))
        if self.is_exited:
            out.append(("exit", "Exit"))
        return out


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file into (frontmatter dict, body)."""
    match = _FRONTMATTER_RE.match(text.lstrip("﻿"))
    if not match:
        raise ValueError("no YAML frontmatter found (file must start with '---')")
    data = yaml.safe_load(match.group(1)) or {}
    if not isinstance(data, dict):
        raise ValueError("frontmatter did not parse to a mapping")
    return data, match.group(2)


def from_dict(data: dict[str, Any], body: str = "", path: Path | None = None) -> Thesis:
    pillars = [Pillar.from_raw(p, i) for i, p in enumerate(_as_list(data.get("pillars")))]
    reviews = [Review.from_raw(r) for r in _as_list(data.get("reviews"))]
    reviews.sort(key=lambda r: (r.date is None, r.date or dt.date.min))
    exit_raw = data.get("exit")
    return Thesis(
        ticker=_upper(data.get("ticker")),
        name=_text(data.get("name")),
        archetype=_upper(data.get("archetype")),
        status=_upper(data.get("status")) or "HELD",
        evidence_grade=_upper(data.get("evidence_grade")) or "C",
        conviction=_upper(data.get("conviction")),
        horizon=_text(data.get("horizon")),
        origin=_upper(data.get("origin")) or "OWN",
        draft=bool(data.get("draft", False)),
        sources=[Source.from_raw(s) for s in _as_list(data.get("sources"))],
        the_bet=_text(data.get("the_bet")),
        what_i_know=_text(data.get("what_i_know")),
        pillars=pillars,
        pre_mortem=[_text(p) for p in _as_list(data.get("pre_mortem"))],
        pre_mortem_hindsight=bool(data.get("pre_mortem_hindsight", False)),
        alternative_considered=Alternative.from_raw(data.get("alternative_considered")),
        management_score=data.get("management_score"),
        management_verdict=_upper(data.get("management_verdict")),
        valuation_note=_text(data.get("valuation_note")),
        hold_thesis=_text(data.get("hold_thesis")),
        resolution_date=_as_date(data.get("resolution_date")),
        resolution_criterion=_text(data.get("resolution_criterion")),
        reviews=reviews,
        exit=Exit.from_raw(exit_raw) if isinstance(exit_raw, dict) and exit_raw else None,
        body=body.strip(),
        path=path,
        raw=data,
    )


def load_thesis(path: str | Path) -> Thesis:
    path = Path(path)
    data, body = split_frontmatter(path.read_text(encoding="utf-8"))
    thesis = from_dict(data, body, path)
    if not thesis.ticker:
        thesis.ticker = path.stem.upper()
    return thesis


def load_all(directory: str | Path = THESES_DIR) -> list[Thesis]:
    """Load every thesis in a directory, sorted by ticker."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    out: list[Thesis] = []
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("_"):
            continue
        out.append(load_thesis(path))
    out.sort(key=lambda t: t.ticker)
    return out


def load_map(directory: str | Path = THESES_DIR) -> dict[str, Thesis]:
    return {t.ticker: t for t in load_all(directory)}


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def validate(thesis: Thesis) -> list[str]:
    """Return a list of problems. Empty list means the file is acceptable.

    The five hard failures are deliberate:

    * an empty or rambling ``the_bet`` means the reason was never actually
      articulated — sixty words is generous;
    * more than four pillars means none of them is load bearing;
    * a pillar without a kill condition is untestable, and an untestable
      pillar is a feeling;
    * a borrowed thesis with no source hides where the idea came from, which
      is exactly the thing worth knowing later;
    * an exit with no sell thesis throws away the only record of why the
      capital moved.
    """
    problems: list[str] = []
    tag = thesis.ticker or (thesis.path.name if thesis.path else "?")

    def bad(msg: str) -> None:
        problems.append(f"{tag}: {msg}")

    # --- hard failures ---------------------------------------------------
    if not thesis.the_bet:
        bad("the_bet is empty — write the one-sentence reason you own this")
    else:
        words = word_count(thesis.the_bet)
        if words > MAX_BET_WORDS:
            bad(f"the_bet is {words} words (max {MAX_BET_WORDS}) — it is an essay, not a bet")

    if len(thesis.pillars) > MAX_PILLARS:
        bad(
            f"{len(thesis.pillars)} pillars (max {MAX_PILLARS}) — "
            "six reasons to own something is none"
        )

    if not thesis.pillars:
        bad("no pillars — a thesis with nothing to break cannot be wrong")

    for pillar in thesis.pillars:
        if not pillar.kill_condition:
            bad(f"pillar {pillar.id} has no kill_condition — untestable pillars are feelings")

    if thesis.borrowed and not thesis.sources:
        bad(f"origin is {thesis.origin} but no sources listed — say whose idea this was")

    if thesis.exit is not None and not thesis.exit.sell_thesis:
        bad("exit block has no sell_thesis — record why the capital moved")

    # --- vocabulary / shape ---------------------------------------------
    if not thesis.ticker:
        bad("no ticker")
    if thesis.archetype and thesis.archetype not in ARCHETYPES:
        bad(f"archetype '{thesis.archetype}' not one of {', '.join(ARCHETYPES)}")
    if thesis.status not in STATUSES:
        bad(f"status '{thesis.status}' not one of {', '.join(STATUSES)}")
    if thesis.evidence_grade not in EVIDENCE_GRADES:
        bad(f"evidence_grade '{thesis.evidence_grade}' not one of A, B, C")
    if thesis.origin not in ORIGINS:
        bad(f"origin '{thesis.origin}' not one of {', '.join(ORIGINS)}")

    seen_ids: set[str] = set()
    for pillar in thesis.pillars:
        if pillar.status not in PILLAR_STATUSES:
            bad(f"pillar {pillar.id} status '{pillar.status}' not INTACT/STRAINED/BREACHED")
        if not pillar.claim:
            bad(f"pillar {pillar.id} has no claim")
        if pillar.id in seen_ids:
            bad(f"duplicate pillar id '{pillar.id}'")
        seen_ids.add(pillar.id)

    for source in thesis.sources:
        if source.alignment not in ALIGNMENTS:
            bad(f"source '{source.name}' alignment '{source.alignment}' not ALIGNED/DIVERGED/INDEPENDENT")

    for review in thesis.reviews:
        if review.date is None:
            bad("a review has no date")
        for pid in review.pillar_status:
            if thesis.pillar(pid) is None:
                bad(f"review references unknown pillar '{pid}'")
        for amendment in review.amendments:
            if amendment.direction not in AMENDMENT_DIRECTIONS:
                bad(
                    f"amendment on {amendment.pillar or '?'} direction "
                    f"'{amendment.direction}' not LOOSENED/TIGHTENED/NEUTRAL"
                )

    if thesis.exit is not None:
        if thesis.exit.reason and thesis.exit.reason not in EXIT_REASONS:
            bad(f"exit reason '{thesis.exit.reason}' not one of {', '.join(EXIT_REASONS)}")
        if thesis.status != "EXITED":
            bad("has an exit block but status is not EXITED")

    return problems


def validate_all(theses: Iterable[Thesis]) -> list[str]:
    problems: list[str] = []
    for thesis in theses:
        problems.extend(validate(thesis))
    return problems


# --------------------------------------------------------------------------
# Scaffolding
# --------------------------------------------------------------------------

SCAFFOLD = """---
ticker: {ticker}
name: {name}
archetype: {archetype}          # COMPOUNDING_MACHINE | CYCLICAL_TRADE | STRUCTURAL_WINNER | SPECULATIVE_PREPROFIT | VALUE_TRAP | MELTING_ICE_CUBE
status: HELD
evidence_grade: {grade}         # A contemporaneous | B reconstructed with documents | C memory only
conviction: {conviction}
horizon: {horizon}
origin: {origin}                # OWN | BORROWED | HYBRID
draft: true

sources: []
  # - name: ""
  #   outlet: ""
  #   url: ""
  #   alignment: INDEPENDENT    # ALIGNED | DIVERGED | INDEPENDENT

the_bet: >-
  {bet}

what_i_know: >-
  What do you actually know here that the person on the other side of the
  trade does not?

pillars:
  - id: P1
    claim: ""
    evidence: ""
    kill_condition: "NEEDS WORK"
    status: INTACT

pre_mortem:
  - "It is three years out and this was a mistake. What happened?"
pre_mortem_hindsight: false

alternative_considered:
  name: ""
  why_rejected: ""

management_score:
management_verdict: ""

valuation_note: >-
  What price was being paid, and against what.

resolution_date:
resolution_criterion: >-
  By that date, what will tell you this worked?

reviews: []
---

## Why this, why now

(Prose. No prices, share counts or returns — those come from the ledger.)
"""


def scaffold(
    ticker: str,
    name: str = "",
    archetype: str = "COMPOUNDING_MACHINE",
    grade: str = "C",
    conviction: str = "MEDIUM",
    horizon: str = "",
    origin: str = "OWN",
    bet: str = "",
) -> str:
    return SCAFFOLD.format(
        ticker=ticker.upper(),
        name=name or ticker.upper(),
        archetype=archetype.upper(),
        grade=grade.upper(),
        conviction=conviction.upper(),
        horizon=horizon or '""',
        origin=origin.upper(),
        bet=bet or "In one sentence, under sixty words: why do you own this?",
    )
