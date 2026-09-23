"""Markdown/plain-text rendering for reports, case files and the email.

Claim types render visibly ("[Fact]", "[Mgmt]"). Non-OK analyses render as
exactly what they are: SKIPPED, FAILED (API) or FAILED (invalid output),
each with the real reason. The deterministic facts are still shown, because
they are real, but they are never dressed up as an analysis.
"""

from __future__ import annotations

from typing import Any

from .schemas import CLAIM_TAGS, AnalysisOutput, Claim

STATUS_LABEL = {
    "OK": "",
    "SKIPPED_CAP": "SKIPPED: run call cap reached, queued for the next run",
    "FAILED_API": "ANALYSIS FAILED: API error",
    "FAILED_INVALID": "ANALYSIS FAILED: invalid model output",
    "DRY_RUN": "DRY RUN: no model call made",
}

TITLES = {
    "SELL_ALERT": "SELL ALERT",
    "BOB_REVIEW": "EVENT REVIEW",
    "WALLY_REVIEW": "OPPORTUNITY REVIEW",
    "PORTFOLIO_REVIEW": "PORTFOLIO REVIEW",
}


def clean(text: Any) -> str:
    """House style: no em dashes."""
    s = str(text or "")
    return s.replace(" — ", ", ").replace("—", ", ").replace("–", "-")


def claim_line(c: Claim) -> str:
    refs = f" ({', '.join(c.source_refs)})" if c.source_refs else ""
    return f"- {CLAIM_TAGS.get(c.claim_type, '')} {clean(c.text)}{refs}"


def _section(out: AnalysisOutput, key: str) -> list[str]:
    for k, claims in out.sections.items():
        if k.strip().upper() == key:
            return [claim_line(c) for c in claims] or ["- (nothing recorded)"]
    return ["- (section missing)"]


def _powers(out: AnalysisOutput) -> list[str]:
    lines = [clean(out.seven_powers_change)] if out.seven_powers_change else []
    for p in out.seven_powers:
        lines.append(f"- **{p.power}**: {p.status} (confidence {p.confidence}; durability: {clean(p.durability) or 'n/a'})")
        for label, group in (("benefit", p.benefit_evidence), ("barrier", p.barrier_evidence), ("against", p.evidence_against)):
            for c in group:
                lines.append(f"  - {label}: {claim_line(c)[2:]}")
        if p.change_vs_last:
            lines.append(f"  - change vs last: {clean(p.change_vs_last)}")
    return lines or ["- (no assessment)"]


def _munger(out: AnalysisOutput) -> list[str]:
    lines = []
    shown = [t for t in out.munger_scan if t.state not in ("NOT ASSESSABLE", "NOT EVIDENT")]
    for t in shown:
        refs = f" [refs: {', '.join(t.evidence_refs)}]" if t.evidence_refs else ""
        lines.append(f"- **{t.tendency}**: {t.state}, pushing toward {t.direction}{refs}")
        for e in t.evidence_for:
            lines.append(f"  - for: {clean(e)}")
        for e in t.evidence_against:
            lines.append(f"  - against: {clean(e)}")
        if t.potential_consequence:
            lines.append(f"  - consequence: {clean(t.potential_consequence)}")
        if t.antidote_question:
            lines.append(f"  - ask: {clean(t.antidote_question)}")
    quiet = [t.tendency for t in out.munger_scan if t not in shown]
    if quiet:
        lines.append(f"- Not evident or not assessable: {', '.join(quiet)}")
    lines.append("- (A checklist for asking better questions, not a diagnosis.)")
    return lines


def _lolla(out: AnalysisOutput, lolla: Any) -> list[str]:
    if lolla is None:
        return ["- not run"]
    if lolla.fired and out.severity == "LOLLAPALOOZA":
        lines = [f"- **LOLLAPALOOZA ALERT.** {', '.join(lolla.tendencies)} all push toward {lolla.direction}.",
                 f"- Hard triggers: {'; '.join(lolla.hard_triggers)}"]
        if out.lollapalooza_explanation:
            lines.append(f"- {clean(out.lollapalooza_explanation)}")
        return lines
    lines = ["- No Lollapalooza. Gate: " + ("; ".join(lolla.failures) or "passed but not claimed by the analysis")]
    if lolla.hard_triggers:
        lines.append(f"- Hard behavioural triggers present: {'; '.join(lolla.hard_triggers)}")
    return lines


def _fresh(out: AnalysisOutput) -> list[str]:
    f = out.fresh_capital_test
    if not f:
        return ["- (not run)"]
    lines = [f"- Would buy today with zero shares: **{f.would_buy_today}**" + (f", at {clean(f.weight_if_new)}" if f.weight_if_new else ""),
             f"- {clean(f.reasoning)}"]
    if f.holding_held_to_lower_standard:
        lines.append("- Holding is being judged by a lower standard than buying.")
    return lines


def _forced(out: AnalysisOutput) -> list[str]:
    f = out.forced_sale_test
    if not f:
        return ["- (not run)"]
    return [f"- Desperate to buy it back after 30 days out: **{f.would_rebuy}**",
            f"- {clean(f.conviction_or_attachment)}", f"- {clean(f.reasoning)}"]


def _thesis(out: AnalysisOutput, tc: Any) -> list[str]:
    lines = []
    if out.thesis_test:
        lines.append(f"- Hindsight read: **{out.thesis_test.classification}**. {clean(out.thesis_test.reason)}")
        if out.thesis_test.evidence_changed_or_thesis_changed:
            lines.append(f"- {clean(out.thesis_test.evidence_changed_or_thesis_changed)}")
    if tc is not None:
        lines += [f"- [Fact] {clean(line)} (thesis_history:{tc.ticker})" for line in tc.lines()]
    return lines or ["- (no thesis test)"]


def _bullets(items: list[str]) -> list[str]:
    return [f"- {clean(i)}" for i in items] or ["- (none)"]


def _verdict(out: AnalysisOutput) -> list[str]:
    lines = [f"**{out.severity}**: {clean(out.severity_reason)}", "", clean(out.verdict)]
    for d in out.disagreements:
        lines.append(f"- Captain Hindsight disagrees with {d.with_agent.title()}: {clean(d.point)}")
    return lines


def _warnings(out: AnalysisOutput) -> list[str]:
    lines = []
    for w in out.warnings:
        lines.append(f"- {w.severity}: {clean(w.text)}")
        for p in w.predictions:
            lines.append(f"  - predict: {clean(p.metric)} {p.direction} {clean(p.threshold)}, check {p.check_date}")
    return lines or ["- (no warnings raised)"]


def _layout(report_type: str) -> list[tuple[str, str]]:
    if report_type == "SELL_ALERT":
        return [("THE ISSUE", "sec"), ("SALLY'S CASE", "sec"), ("THE COUNTERCASE", "counter"), ("THESIS TEST", "thesis"),
                ("7 POWERS (what changed)", "powers"), ("MUNGER SCAN", "munger"), ("LOLLAPALOOZA CHECK", "lolla"),
                ("FRESH CAPITAL TEST", "fresh"), ("FORCED SALE TEST", "forced"), ("TAX NOTE", "tax"),
                ("WHAT WOULD CHANGE MY MIND", "mind"), ("UNANSWERED QUESTIONS", "questions"), ("VERDICT", "verdict")]
    if report_type == "BOB_REVIEW":
        return [("WHAT CHANGED", "sec"), ("WHAT WE EXPECTED", "sec"), ("WHAT HAPPENED", "sec"), ("THESIS IMPACT", "sec+thesis"),
                ("7 POWERS IMPACT", "powers"), ("MANAGEMENT INCENTIVES", "sec"), ("ACCOUNTING AND CASH FLOW", "sec"),
                ("CONTRADICTIONS", "sec"), ("WHAT ARE WE BEING ASKED TO BELIEVE", "believe"), ("MUNGER SCAN", "munger"),
                ("WHAT WOULD PROVE THE THESIS WRONG", "mind"), ("INVESTIGATE NEXT", "sec"), ("VERDICT", "verdict")]
    if report_type == "WALLY_REVIEW":
        return [("WHY WALLY LIKES IT", "sec"), ("WHAT HAS TO BE TRUE", "sec"), ("WHAT COULD MAKE WALLY WRONG", "sec"),
                ("7 POWERS", "powers"), ("VALUE TRAP TEST", "sec"), ("GOOD COMPANY VS GOOD PRICE", "sec"),
                ("WHAT ARE WE BEING SEDUCED BY", "sec"), ("MUNGER SCAN", "munger"), ("MISSING EVIDENCE", "sec"),
                ("WHAT WOULD DISPROVE IT", "mind"), ("VERDICT", "verdict")]
    from .prompts import TASKS

    return [(s, "sec") for s in TASKS["PORTFOLIO_REVIEW"][1]] + [("MUNGER SCAN", "munger"),
                                                                  ("LOLLAPALOOZA CHECK", "lolla"), ("VERDICT", "verdict")]


def render_report(b: dict) -> str:
    """``b`` is the bundle built by runner.analyse()."""
    rt, ticker, status = b["report_type"], b.get("ticker") or "PORTFOLIO", b["status"]
    head = f"## {ticker} | {TITLES.get(rt, rt)}"
    if status == "OK":
        head += f" | {b['output'].severity}"
    lines = [head, f"_Trigger: {clean(b.get('reason', ''))}_", ""]
    if status != "OK":
        lines += [f"**{STATUS_LABEL.get(status, status)}**"]
        if b.get("error"):
            lines += ["", f"Error: `{clean(b['error'])[:600]}`"]
        if status == "FAILED_INVALID" and b.get("raw"):
            lines += ["", "Raw model output (unparseable, kept verbatim in the private store)."]
        lines += ["", "Deterministic facts (computed by code, not analysis):"]
        lines += [f"- {clean(t)} ({r})" for r, t in b.get("fact_lines", [])]
        if b.get("tc") is not None:
            lines += [f"- {clean(t)}" for t in b["tc"].lines()]
        if b.get("hard_triggers"):
            lines += [f"- Hard behavioural trigger: {t}" for t in b["hard_triggers"]]
        return "\n".join(lines) + "\n"

    out: AnalysisOutput = b["output"]
    for heading, kind in _layout(rt):
        lines.append(f"### {heading}")
        if kind == "sec":
            lines += _section(out, heading)
        elif kind == "sec+thesis":
            lines += _section(out, heading) + _thesis(out, b.get("tc"))
        elif kind == "counter":
            lines += _section(out, heading)
            lines.append(f"- Strongest opposing case: {clean(out.strongest_opposing_case)}")
            lines += [f"- What would tell them apart: {clean(d)}" for d in out.distinguishing_evidence]
        elif kind == "thesis":
            lines += _thesis(out, b.get("tc"))
        elif kind == "powers":
            lines += _powers(out)
        elif kind == "munger":
            lines += _munger(out)
        elif kind == "lolla":
            lines += _lolla(out, b.get("lolla"))
        elif kind == "fresh":
            lines += _fresh(out)
        elif kind == "forced":
            lines += _forced(out)
        elif kind == "tax":
            lines += [f"- {clean(out.tax_note) or '(none given)'}"]
            if out.breakeven_check:
                lines.append(f"- Breakeven check: {clean(out.breakeven_check)}")
        elif kind == "mind":
            lines += _bullets(out.what_would_change_my_mind)
        elif kind == "questions":
            lines += _bullets(out.unanswered_questions)
            if out.questions_for_theo:
                lines.append("- Questions for Theo:")
                lines += [f"  - {clean(q)}" for q in out.questions_for_theo]
        elif kind == "believe":
            lines.append("- Evidence for:")
            lines += ["  " + x for x in _section(out, "EVIDENCE FOR")]
            lines.append("- Evidence against:")
            lines += ["  " + x for x in _section(out, "EVIDENCE AGAINST")]
            lines.append(f"- Strongest opposing case: {clean(out.strongest_opposing_case)}")
        elif kind == "verdict":
            lines += _verdict(out)
        lines.append("")
    if rt != "SELL_ALERT" and out.questions_for_theo:
        lines += ["### QUESTIONS FOR THEO"] + _bullets(out.questions_for_theo) + [""]
    lines += ["### PREDICTIONS FOR THE AUTOPSY"] + _warnings(out) + [""]
    if b.get("validation_notes"):
        lines += ["### VALIDATOR NOTES"] + [f"- {clean(n)}" for n in b["validation_notes"]] + [""]
    lines += ["### EVIDENCE REFS"] + [f"- {k}: {clean(v)}" for k, v in b.get("refs", {}).items()] + [""]
    return "\n".join(lines)


def render_email(summary: dict) -> tuple[str, str]:
    reports = summary["reports"]
    order = ["GREEN", "AMBER", "RED", "LOLLAPALOOZA"]
    oks = [b["output"].severity for b in reports if b["status"] == "OK"]
    worst = max(oks, key=order.index) if oks else None
    failed = sum(1 for b in reports if b["status"].startswith("FAILED"))
    skipped = sum(1 for b in reports if b["status"] == "SKIPPED_CAP")
    subject = f"Captain Hindsight | {summary['date']} | {len(reports)} report(s)"
    if worst:
        subject += f", worst {worst}"
    if failed:
        subject += f" | {failed} FAILED"

    lines = ["Captain Hindsight here. Let's have this conversation before it's hindsight.", ""]
    llm = summary.get("llm", {})
    lines.append(f"Run: {llm.get('calls', 0)}/{llm.get('cap', 0)} model calls, "
                 f"{llm.get('tokens_in', 0)} in / {llm.get('tokens_out', 0)} out tokens, "
                 f"about US${llm.get('cost_usd_estimate', 0):.2f}.")
    if skipped or summary.get("queued"):
        lines.append(f"Call cap reached: {summary.get('queued', skipped)} item(s) queued for the next run. "
                     "These are skipped, not failed.")
    if failed:
        lines.append(f"{failed} analysis/analyses FAILED. Details below with the real error.")
    for n in summary.get("notes", []):
        lines.append(f"Note: {clean(n)}")
    lines.append("")

    if summary.get("triage"):
        lines += ["# JM WATCH LIST TRIAGE", ""] + summary["triage"] + [""]
    for b in reports:
        lines += [b["markdown"], ""]
    if summary.get("scorecard"):
        lines += ["# QUARTERLY SCORECARD", ""] + summary["scorecard"] + [""]
    return subject, "\n".join(lines)
