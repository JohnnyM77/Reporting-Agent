"""Slide rendering.

Three kinds of slide, and the difference between them is the whole design:

``entry``
    What was thought at the time of the buy — and deliberately nothing else.
    An entry slide must not show what the position is worth now. A record that
    knows the outcome cannot score the decision; it will always read as either
    obvious foresight or obvious stupidity, and neither is what happened.

``review``
    One per logged review. Pillar statuses as recorded on that date, and the
    amendments made — which is the part worth being able to look back at.

``current``
    Live pillar status, the drift check, today's numbers, and the *hold*
    thesis rather than the entry bet where one exists. The reason to own
    something in year ten is rarely the reason it was bought in year one, and
    pretending otherwise is how a thesis quietly becomes unfalsifiable.

``exit``
    Why the capital moved, and what it moved to.

Numbers only ever come from the ledger. When there is no ledger, the slide
renders the judgement and puts dashes where the figures go.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import season as season_mod
from .drift import Finding, ALERT, INFO, WARN
from .ledger import EMPTY, Decision, Ledger
from .thesis import Review, Thesis

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

KIND_ENTRY = "entry"
KIND_REVIEW = "review"
KIND_CURRENT = "current"
KIND_EXIT = "exit"

DEVICE_SCALE_FACTOR = 3
SLIDE_WIDTH = 1123
SLIDE_HEIGHT = 794

# Escape hatch for environments where Playwright's own browser download does
# not match the installed package (CI images that pin chromium elsewhere).
CHROMIUM_PATH_ENV = "THEO_CHROMIUM_PATH"

_ENV: Environment | None = None


def env() -> Environment:
    global _ENV
    if _ENV is None:
        _ENV = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
    return _ENV


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

DASH = "—"


def fmt_money(value: float | None, show_amounts: bool, places: int = 0) -> str:
    if value is None or not show_amounts:
        return DASH
    return f"${value:,.{places}f}"


def fmt_price(value: float | None) -> str:
    """Prices are always shown: a historical share price discloses nothing."""
    if value is None:
        return DASH
    return f"${value:,.2f}"


def fmt_pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else DASH


def fmt_mult(value: float | None) -> str:
    return f"{value:.2f}x" if value is not None else DASH


def fmt_years(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else DASH


def fmt_shares(value: float | None, show_amounts: bool) -> str:
    if value is None or not show_amounts:
        return DASH
    return f"{value:,.0f}"


def fmt_date(value: dt.date | None) -> str:
    return value.isoformat() if value else DASH


# --------------------------------------------------------------------------
# Context building
# --------------------------------------------------------------------------


LONG_METRIC = 16


def _metric(label: str, value: str, note: str = "") -> dict[str, Any]:
    return {
        "label": label,
        "value": value,
        "note": note,
        "muted": value == DASH,
        "long": len(value) > LONG_METRIC,
    }


def _pillar_rows(thesis: Thesis, statuses: dict[str, str]) -> list[dict[str, Any]]:
    symbols = {"INTACT": "●", "STRAINED": "◐", "BREACHED": "○"}
    rows = []
    for pillar in thesis.pillars:
        status = statuses.get(pillar.id, pillar.status)
        rows.append(
            {
                "id": pillar.id,
                "claim": pillar.claim,
                "evidence": pillar.evidence,
                "kill_condition": pillar.kill_condition,
                "status": status,
                "symbol": symbols.get(status, "?"),
            }
        )
    return rows


def _decision_rows(
    decisions: Sequence[Decision],
    kind: str,
    show_amounts: bool,
    cutoff: dt.date | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    if cutoff:
        decisions = [d for d in decisions if d.date and d.date <= cutoff]

    if kind == KIND_ENTRY:
        headers = ["Shares", "Price", "Cost"]
    elif kind == KIND_EXIT:
        headers = ["Price", "Yrs", "Proceeds", "IRR"]
    else:
        headers = ["Price", "Yrs", "Value", "IRR"]

    rows = []
    for decision in decisions:
        if kind == KIND_ENTRY:
            cells = [
                fmt_shares(decision.shares, show_amounts),
                fmt_price(decision.price),
                fmt_money(decision.cost, show_amounts),
            ]
        elif kind == KIND_EXIT:
            cells = [
                fmt_price(decision.price),
                fmt_years(decision.years),
                fmt_money(decision.proceeds + decision.dividends, show_amounts),
                "scrip" if decision.is_scrip else fmt_pct(decision.irr),
            ]
        else:
            cells = [
                fmt_price(decision.price),
                fmt_years(decision.years),
                fmt_money(decision.value_now, show_amounts),
                "scrip" if decision.is_scrip else fmt_pct(decision.irr),
            ]
        rows.append(
            {
                "index": decision.index,
                "date": fmt_date(decision.date),
                "cells": cells,
                "scrip": decision.is_scrip,
            }
        )
    return headers, rows


def _review_findings(review: Review) -> list[Finding]:
    out: list[Finding] = []
    for amendment in review.amendments:
        severity = ALERT if amendment.loosened else (WARN if amendment.direction == "TIGHTENED" else INFO)
        out.append(
            Finding(
                code=f"AMENDED_{amendment.direction}",
                severity=severity,
                message=f"{amendment.pillar}: {amendment.change}"
                + (f" (trigger: {amendment.trigger})" if amendment.trigger else ""),
            )
        )
    if review.expected_vs_actual:
        out.append(Finding(code="EXPECTED_VS_ACTUAL", severity=INFO, message=review.expected_vs_actual))
    return out


def _provenance(thesis: Thesis) -> str:
    bits = [f"Origin {thesis.origin.title()}"]
    for source in thesis.sources:
        who = " · ".join(p for p in (source.name, source.outlet) if p)
        bits.append(f"{who} ({source.alignment.title()})" if who else source.alignment.title())
    bits.append(f"Grade {thesis.evidence_grade} — {thesis.evidence_grade_label}")
    if thesis.pre_mortem_hindsight:
        bits.append("Pre-mortem written with hindsight")
    return " · ".join(bits)


def slide_context(
    thesis: Thesis,
    version: str = KIND_CURRENT,
    ledger: Ledger | None = None,
    today: dt.date | None = None,
    show_amounts: bool = False,
) -> dict[str, Any]:
    """Everything ``_slide.html`` needs, and nothing it does not."""
    today = today or dt.date.today()
    ledger = ledger or EMPTY
    holding = ledger.get(thesis.ticker)
    decisions = holding.decisions if holding else []

    review: Review | None = None
    if version.startswith("review-"):
        wanted = version[len("review-") :]
        review = next((r for r in thesis.reviews if r.date and r.date.isoformat() == wanted), None)
        kind = KIND_REVIEW if review else KIND_CURRENT
    elif version in (KIND_ENTRY, KIND_EXIT):
        kind = version
    else:
        kind = KIND_CURRENT

    # --- the bet ---------------------------------------------------------
    if kind == KIND_ENTRY:
        bet, bet_label = thesis.the_bet, "The bet, as written at entry"
    elif kind == KIND_EXIT and thesis.exit and thesis.exit.sell_thesis:
        bet, bet_label = thesis.exit.sell_thesis, "The sell thesis"
    elif thesis.hold_thesis:
        bet, bet_label = thesis.hold_thesis, "Why it is still held"
    else:
        bet, bet_label = thesis.the_bet, "The bet"

    # --- pillars ---------------------------------------------------------
    if kind == KIND_ENTRY:
        # At entry every pillar was intact by construction — you do not buy
        # into a breach. Showing today's status here would leak the answer.
        statuses = {p.id: "INTACT" for p in thesis.pillars}
    elif kind == KIND_REVIEW and review:
        statuses = thesis.pillar_status_at(review)
    else:
        statuses = {p.id: p.status for p in thesis.pillars}

    # --- drift -----------------------------------------------------------
    show_drift = kind != KIND_ENTRY
    if kind == KIND_REVIEW and review:
        findings = _review_findings(review)
        if any(a.loosened for a in review.amendments):
            verdict = "DRIFTING"
        elif review.amendments:
            verdict = "WATCH"
        else:
            verdict = "CLEAN"
    elif show_drift:
        verdict, findings = season_mod.assess(thesis, today)
    else:
        verdict, findings = "CLEAN", []

    # --- decisions -------------------------------------------------------
    cutoff = review.date if (kind == KIND_REVIEW and review) else None
    headers, rows = _decision_rows(decisions, kind, show_amounts, cutoff)
    no_ledger_note = (
        "No transaction ledger loaded — drop data/portfolio.xlsx in and every "
        "figure on this slide fills itself in."
        if not ledger.holdings
        else f"No transactions recorded for {thesis.ticker}."
    )

    # --- metric strip ----------------------------------------------------
    if kind == KIND_ENTRY:
        first = min((d.date for d in decisions if d.date), default=None)
        metrics = [
            _metric("Decisions", str(len(rows)) if rows else DASH),
            _metric("First bought", fmt_date(first)),
            _metric("Shares bought", fmt_shares(sum(d.shares for d in decisions) or None, show_amounts)),
            _metric("Cost basis", fmt_money(sum(d.cost for d in decisions) or None, show_amounts)),
            _metric("Horizon", thesis.horizon or DASH),
        ]
    elif kind == KIND_REVIEW and review:
        intact = sum(1 for pid, st in statuses.items() if st == "INTACT")
        metrics = [
            _metric("Reviewed", fmt_date(review.date)),
            _metric("Verdict", review.verdict or DASH),
            _metric("Process", str(review.process_score) if review.process_score is not None else DASH),
            _metric("Outcome", str(review.outcome_score) if review.outcome_score is not None else DASH),
            _metric("Pillars intact", f"{intact}/{len(thesis.pillars)}" if thesis.pillars else DASH),
        ]
    elif kind == KIND_EXIT:
        metrics = [
            _metric("Decisions", str(len(rows)) if rows else DASH),
            _metric("Held for", (fmt_years(holding.years) + " yrs") if holding and holding.years else DASH),
            _metric(
                "Realised",
                fmt_money(
                    sum(d.proceeds + d.dividends for d in decisions) or None, show_amounts
                ),
            ),
            _metric("IRR", fmt_pct(holding.irr) if holding else DASH),
            _metric("Multiple", fmt_mult(holding.multiple) if holding else DASH),
        ]
    else:
        metrics = [
            _metric("Decisions", str(len(rows)) if rows else DASH),
            _metric("Held for", (fmt_years(holding.years) + " yrs") if holding and holding.years else DASH),
            # The one label the entry slide must never carry.
            _metric("Value today", fmt_money(holding.value if holding else None, show_amounts)),
            _metric("IRR", fmt_pct(holding.irr) if holding else DASH),
            _metric("Multiple", fmt_mult(holding.multiple) if holding else DASH),
        ]

    alternative = DASH
    if thesis.alternative_considered:
        alternative = thesis.alternative_considered.name
        if thesis.alternative_considered.why_rejected:
            alternative += f" — {thesis.alternative_considered.why_rejected}"

    management = DASH
    if thesis.management_score is not None or thesis.management_verdict:
        score = thesis.management_score
        management = " · ".join(
            p
            for p in (
                f"{score}/10" if score is not None else "",
                thesis.management_verdict.title() if thesis.management_verdict else "",
            )
            if p
        )

    kind_labels = {
        KIND_ENTRY: "Entry — what I thought when I bought",
        KIND_REVIEW: f"Review {fmt_date(review.date) if review else ''}".strip(),
        KIND_CURRENT: "Current",
        KIND_EXIT: "Exit",
    }

    record_note = (
        f"Generated {today.isoformat()} from {thesis.path.name if thesis.path else 'thesis'}. "
        "Figures come from the ledger at render time; the thesis file holds no numbers."
    )
    if kind == KIND_ENTRY:
        record_note = (
            "Entry record — deliberately blind to the outcome. "
            "A slide that knows how it turned out cannot score the decision."
        )

    return {
        "ticker": thesis.ticker,
        "name": thesis.name or thesis.ticker,
        "version": version,
        "kind": kind,
        "kind_label": kind_labels.get(kind, kind.title()),
        "archetype": thesis.archetype,
        "conviction": thesis.conviction,
        "horizon": thesis.horizon,
        "evidence_grade": thesis.evidence_grade,
        "evidence_grade_label": thesis.evidence_grade_label,
        "draft": thesis.draft,
        "exited": thesis.is_exited,
        "bet": bet,
        "bet_label": bet_label,
        "show_drift": show_drift,
        "drift_verdict": verdict,
        "drift_findings": findings,
        "pillars": _pillar_rows(thesis, statuses),
        "decision_headers": headers,
        "decisions": rows,
        "no_ledger_note": no_ledger_note,
        "valuation_note": thesis.valuation_note,
        "what_i_know": thesis.what_i_know,
        "resolution_date": fmt_date(thesis.resolution_date) if thesis.resolution_date else "",
        "resolution_criterion": thesis.resolution_criterion,
        "exit_block": (
            {
                "date": fmt_date(thesis.exit.date),
                "reason": thesis.exit.reason or "EXIT",
                "sell_thesis": thesis.exit.sell_thesis,
                "pillar_failed": thesis.exit.pillar_failed,
                "capital_went_to": thesis.exit.capital_went_to,
            }
            if kind == KIND_EXIT and thesis.exit
            else None
        ),
        "metrics": metrics,
        "provenance": _provenance(thesis),
        "alternative": alternative,
        "management": management,
        "record_note": record_note,
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_fragment(context: dict[str, Any]) -> str:
    """Just the ``.slide`` div — what the site embeds."""
    return env().get_template("_slide.html").render(s=context)


def render_styles() -> str:
    return env().get_template("_styles.html").render()


def render_html(
    thesis: Thesis,
    version: str = KIND_CURRENT,
    ledger: Ledger | None = None,
    today: dt.date | None = None,
    show_amounts: bool = False,
) -> str:
    context = slide_context(thesis, version, ledger, today, show_amounts)
    return env().get_template("slide.html").render(s=context)


class RenderError(RuntimeError):
    pass


def render_image(html: str, out_path: str | Path, fmt: str = "png") -> Path:
    """Screenshot (or print) the ``.slide`` element with Playwright.

    device_scale_factor=3 so the PNG is sharp enough to print or drop into a
    document without looking like a screenshot.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RenderError(
            "playwright is not installed — use --format html, or "
            "pip install playwright && playwright install chromium"
        ) from exc

    tmp = out_path.with_suffix(".src.html")
    tmp.write_text(html, encoding="utf-8")
    try:
        with sync_playwright() as play:
            executable = os.environ.get(CHROMIUM_PATH_ENV) or None
            browser = play.chromium.launch(executable_path=executable)
            page = browser.new_page(
                viewport={"width": SLIDE_WIDTH, "height": SLIDE_HEIGHT},
                device_scale_factor=DEVICE_SCALE_FACTOR,
            )
            page.goto(tmp.resolve().as_uri())
            page.wait_for_selector(".slide")
            if fmt == "pdf":
                page.pdf(
                    path=str(out_path),
                    width=f"{SLIDE_WIDTH}px",
                    height=f"{SLIDE_HEIGHT}px",
                    print_background=True,
                    margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                )
            else:
                page.locator(".slide").screenshot(path=str(out_path))
            browser.close()
    finally:
        tmp.unlink(missing_ok=True)
    return out_path


def render_slide(
    thesis: Thesis,
    version: str = KIND_CURRENT,
    ledger: Ledger | None = None,
    today: dt.date | None = None,
    show_amounts: bool = False,
    fmt: str = "html",
    out_path: str | Path | None = None,
) -> Path:
    html = render_html(thesis, version, ledger, today, show_amounts)
    out_path = Path(out_path or f"slides/{thesis.ticker}-{version}.{fmt}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "html":
        out_path.write_text(html, encoding="utf-8")
        return out_path
    return render_image(html, out_path, fmt)
