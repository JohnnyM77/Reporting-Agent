"""Orchestration: events in, deterministic gates, capped model calls, one email.

Order of work in a run:
1. infer Johnny's actions from ledger / tickers.yaml changes
2. collect events: queued from last run, Sally sells, Bob high/critical,
   autopsies due, Wally top-N, portfolio review if due
3. JM Watch List triage (NO CHANGE costs nothing)
4. full analyses in priority order until the call cap; the rest are queued
5. render, save to the private store, emit, email
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from pathlib import Path
from typing import Any

from . import behaviour, profile as bias_profile, thesis_change
from .adapters import (
    BobAdapter, PortfolioAdapter, SallyAdapter, TheoAdapter, WallyAdapter, WatchlistAdapter, bare, git_log,
)
from .config import REPO_ROOT
from .gating import bob_events, bob_triggers, file_hash, portfolio_review_due, sort_events, triage_gates, wally_top
from .llm import LLMClient, Transport
from .prices import PriceProvider, YFinancePrices
from .prompts import (
    AUTOPSY_SYSTEM, SYSTEM_PROMPT, TRIAGE_SYSTEM, build_autopsy_prompt, build_triage_prompt, build_user_prompt, data_block,
)
from .email_html import build_email_html, build_pdf_html, pdf_filename, render_pdf
from .render import clean, render_email, render_report
from .schemas import (
    EVIDENCED_BIAS_STATES, AnalysisOutput, Autopsy, AutopsyOutput, BiasRecord, HindsightEvent, HindsightOutcome,
    HindsightQuestion, HindsightReport, JohnnyResponse, SevenPowersRecord, ThesisSnapshot, TriageOutput, WarningRecord,
)
from .store import Store
from .validators import shape_check, validate_analysis

REPORT_TYPE_FOR = {
    "SELL_SIGNAL": "SELL_ALERT",
    "HIGH_IMPACT_EVENT": "BOB_REVIEW",
    "WATCHLIST_DAILY": "BOB_REVIEW",
    "TOP_OPPORTUNITY": "WALLY_REVIEW",
    "PORTFOLIO_REVIEW": "PORTFOLIO_REVIEW",
}


@dataclasses.dataclass
class Run:
    cfg: dict
    store: Store
    llm: LLMClient
    prices: PriceProvider
    today: dt.date
    repo_root: Path = REPO_ROOT
    sally: SallyAdapter | None = None
    bob: BobAdapter | None = None
    wally: WallyAdapter | None = None
    theo: TheoAdapter | None = None
    portfolio: PortfolioAdapter | None = None
    watchlist: WatchlistAdapter | None = None
    dry_run: bool = False
    force: bool = False
    reports: list[dict] = dataclasses.field(default_factory=list)
    triage: list[str] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)
    queued: int = 0
    duplicates: int = 0
    profile: dict = dataclasses.field(default_factory=dict)

    def __post_init__(self):
        root = self.repo_root
        self.sally = self.sally or SallyAdapter(root, self.cfg)
        self.bob = self.bob or BobAdapter(root)
        self.wally = self.wally or WallyAdapter(root)
        self.theo = self.theo or TheoAdapter(root)
        self.portfolio = self.portfolio or PortfolioAdapter(root, self.store.root)
        self.watchlist = self.watchlist or WatchlistAdapter(root, self.wally)
        self.profile = bias_profile.load(self.store, self.cfg)


def make_run(cfg: dict, store: Store, *, transport: Transport | None = None, prices: PriceProvider | None = None,
             today: dt.date | None = None, repo_root: Path = REPO_ROOT, **kw) -> Run:
    return Run(cfg=cfg, store=store, llm=LLMClient(cfg, transport), prices=prices or YFinancePrices(),
               today=today or dt.date.today(), repo_root=repo_root, **kw)


# ---------------------------------------------------------------------------
# Context for one ticker: facts, refs, data blocks, history
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class TickerContext:
    ticker: str
    thesis: Any
    tc: Any
    facts: behaviour.BehaviourFacts
    refs: dict[str, str]
    blocks: list[str]
    hard_triggers: list[str]


def _thesis_block(th: Any) -> dict:
    return {
        "archetype": th.archetype, "status": th.status, "conviction": th.conviction, "horizon": th.horizon,
        "origin": th.origin, "the_bet": th.the_bet, "hold_thesis": th.hold_thesis,
        "pillars": [{"id": p.id, "claim": p.claim, "evidence": p.evidence, "kill_condition": p.kill_condition,
                     "status": p.status} for p in th.pillars],
        "pre_mortem": th.pre_mortem, "valuation_note": th.valuation_note,
        "management_verdict": th.management_verdict,
        "reviews": [{"date": str(r.date), "verdict": r.verdict, "trigger": r.trigger, "decision": r.decision,
                     "amendments": [dataclasses.asdict(a) for a in r.amendments]} for r in th.reviews],
        "resolution_criterion": th.resolution_criterion,
        "notes": th.body[:5000],
    }


def history_block(run: Run, ticker: str, refs: dict[str, str]) -> dict:
    """Prior warnings, open questions, earlier verdicts and Johnny's responses."""
    st = run.store
    warnings = [w for w in st.query("hindsight_warning", WarningRecord, ticker=ticker) if w.status == "OPEN"]
    questions = [q for q in st.query("hindsight_question", HindsightQuestion, ticker=ticker) if q.status == "OPEN"]
    reports = [r for r in st.query("hindsight_report", HindsightReport, ticker=ticker) if r.analysis_status == "OK"][-3:]
    responses = st.query("johnny_response", JohnnyResponse, ticker=ticker)[-5:]
    for w in warnings:
        refs[f"warning:{w.id}"] = f"open warning raised {w.created_at[:10]}: {w.text[:80]}"
    for r in reports:
        refs[f"report:{r.id}"] = f"{r.report_type} {r.created_at[:10]}: {r.severity}"
    for r in responses:
        refs[f"response:{r.id}"] = f"Johnny {r.action} on {r.created_at[:10]}{' (inferred)' if r.inferred else ''}"
    return {
        "open_warnings": [{"id": w.id, "raised": w.created_at[:10], "text": w.text, "severity": w.severity,
                           "predictions": [p.model_dump() for p in w.predictions]} for w in warnings],
        "open_questions": [{"raised": q.created_at[:10], "question": q.question, "for_theo": q.for_theo} for q in questions],
        "earlier_verdicts": [{"id": r.id, "date": r.created_at[:10], "type": r.report_type, "severity": r.severity,
                              "verdict": r.verdict} for r in reports],
        "johnny_responses": [{"date": r.created_at[:10], "action": r.action, "note": r.note, "inferred": r.inferred}
                             for r in responses],
    }


def _answered_dates(run: Run, ticker: str, thesis: Any) -> list[dt.date]:
    out = []
    if thesis is not None:
        out += [r.date for r in thesis.reviews if r.date and r.trigger.startswith("SALLY")]
    for r in run.store.query("johnny_response", JohnnyResponse, ticker=ticker):
        try:
            out.append(dt.date.fromisoformat(r.created_at[:10]))
        except ValueError:
            pass
    return out


def current_price(run: Run, ticker: str, event: HindsightEvent | None) -> tuple[float | None, str]:
    payload = (event.payload if event else {}) or {}
    for key in ("sally_row", "row"):
        row = payload.get(key) or {}
        if row.get("current_price"):
            src = f"Sally run {payload.get('sally_run')}" if key == "sally_row" else f"Wally run {run.wally.run_ref}"
            return float(row["current_price"]), src
    p = run.prices.current(ticker)
    if p:
        return float(p), f"market close via yfinance, {run.today}"
    return None, ""


def ticker_context(run: Run, ticker: str, event: HindsightEvent | None = None) -> TickerContext:
    t = bare(ticker)
    thesis = run.theo.get(t)
    versions = run.theo.versions(t) if thesis is not None else []
    tc = thesis_change.detect(t, thesis, versions)
    price, price_src = current_price(run, t, event)
    facts = behaviour.compute(
        t, ledger=run.portfolio.ledger(), thesis=thesis, thesis_versions=len(versions) or (1 if thesis else 0),
        current_price=price, price_source=price_src, sally_streak=run.sally.consecutive_flags(t),
        answered_after=_answered_dates(run, t, thesis), positions_row=run.portfolio.positions_yaml().get(t, {}),
        repo_root=run.repo_root, today=run.today, cfg=run.cfg,
    )
    refs: dict[str, str] = {}
    blocks: list[str] = []
    for ref, text in facts.lines():
        refs[ref] = text
    if price:
        refs[f"price:{t}"] = f"${price:.2f} ({price_src})"
    for d in facts.sally_flag_dates:
        refs[f"sally:{d}:{t}"] = f"Sally flagged {t} in her {d} run"
    if thesis is not None:
        refs[f"thesis:{t}"] = f"theses/{t}.md as it stands today"
        refs[f"thesis_history:{t}"] = f"{tc.versions} committed version(s) of theses/{t}.md"
        blocks.append(data_block(f"thesis:{t}", "THEO", _thesis_block(thesis)))
        blocks.append(data_block(f"thesis_history:{t}", "HINDSIGHT (computed from git)", "\n".join(tc.lines())))
    blocks.append(data_block(f"behaviour:{t}", "HINDSIGHT (computed from the decision log)",
                             "\n".join(f"[{r}] {x}" for r, x in facts.lines())))
    row = run.wally.row(t)
    if row:
        refs[f"wally:{t}"] = f"Wally row from {run.wally.run_ref}"
        blocks.append(data_block(f"wally:{t}", "WALLY", row))
    bob_items = [it for _, it in run.bob.items() if bare(it.get("ticker")) == t]
    for it in bob_items[:5]:
        url = str(it.get("url") or f"bob:{t}")
        refs[url] = f"Bob: {' '.join(str(it.get('title', '')).split())[:80]}"
        blocks.append(data_block(url, "BOB", {k: it.get(k) for k in ("title", "url", "type")}))
    last_powers = run.store.query("seven_powers_assessment", SevenPowersRecord, ticker=t)
    if last_powers:
        lp = last_powers[-1]
        blocks.append(data_block(f"report:{lp.report_id}", "HINDSIGHT (last 7 Powers assessment)",
                                 {"date": lp.created_at[:10], "assessments": [a.model_dump() for a in lp.assessments]}))
    hist = history_block(run, t, refs)
    if any(hist.values()):
        blocks.append(data_block(f"hindsight_history:{t}", "HINDSIGHT (case file)", hist))
    return TickerContext(t, thesis, tc, facts, refs, blocks, facts.hard_triggers(run.cfg))


# ---------------------------------------------------------------------------
# Full analysis
# ---------------------------------------------------------------------------


def _event_block(event: HindsightEvent) -> str:
    payload = dict(event.payload)
    item = payload.get("item")
    if isinstance(item, dict) and isinstance(item.get("analysis"), dict):
        a = item["analysis"]
        payload["item"] = {**item, "analysis": {"summary": a.get("summary"), "metrics": a.get("metrics"),
                                                "full_analysis": str(a.get("full_analysis") or "")[:12000]}}
    return data_block(event.source_report_ref or event.event_id, event.source,
                      {"event_type": event.event_type, "subtype": event.event_subtype, "priority": event.priority,
                       "reason": event.reason, "payload": payload})


def analyse(run: Run, event: HindsightEvent, ctx: TickerContext | None = None,
            extra_blocks: list[str] | None = None, extra_refs: dict | None = None,
            hard_triggers: list[str] | None = None) -> dict:
    rt = REPORT_TYPE_FOR[event.event_type]
    ticker = bare(event.ticker)
    if ctx is None and ticker:
        ctx = ticker_context(run, ticker, event)
    refs = dict(ctx.refs if ctx else {})
    refs.update(extra_refs or {})
    blocks = [_event_block(event)] + (ctx.blocks if ctx else []) + (extra_blocks or [])
    if event.source_report_ref:
        refs.setdefault(event.source_report_ref, f"the triggering {event.source} record")
    triggers = hard_triggers if hard_triggers is not None else (ctx.hard_triggers if ctx else [])
    focus = bias_profile.focus_list(run.profile)
    user = build_user_prompt(rt, ticker, refs, blocks, focus)

    bundle = {"report_type": rt, "ticker": ticker, "reason": event.reason, "event": event, "refs": refs,
              "tc": ctx.tc if ctx else None, "facts": ctx.facts if ctx else None, "fact_lines": ctx.facts.lines() if ctx else [],
              "hard_triggers": triggers, "prompt": user, "status": "DRY_RUN", "error": "", "raw": ""}
    if run.dry_run:
        bundle["markdown"] = render_report(bundle)
        return bundle

    res = run.llm.structured(SYSTEM_PROMPT, user, AnalysisOutput, "full", label=f"{rt}:{ticker}",
                             shape_check=shape_check(rt))
    bundle.update(status=res.status, error=res.error, raw=res.raw)
    if res.status == "OK":
        out, notes, lolla = validate_analysis(res.data, set(refs), triggers, run.cfg)
        bundle.update(output=out, validation_notes=notes, lolla=lolla)
        if notes:  # counts only: Actions logs on a public repo are public
            print(f"[hindsight] validator adjusted {len(notes)} item(s) in {rt}")
    bundle["markdown"] = render_report(bundle)
    persist(run, bundle)
    return bundle


def persist(run: Run, b: dict) -> None:
    st, ev, t = run.store, b["event"], b.get("ticker") or ""
    out: AnalysisOutput | None = b.get("output")
    rep = HindsightReport(
        ticker=t, report_type=b["report_type"], event_id=ev.event_id, analysis_status=b["status"],
        severity=out.severity if out else "", verdict=out.verdict if out else "", error=b.get("error", ""),
        markdown=b["markdown"], output=out.model_dump(mode="json", by_alias=True) if out else {},
        validation_notes=b.get("validation_notes", []), source_refs=[ev.source_report_ref], status=b["status"],
    )
    b["report_id"] = rep.id
    st.put("hindsight_report", rep)
    stamp = run.today.isoformat()
    st.write_text(f"reports/{stamp}/{t or 'PORTFOLIO'}_{b['report_type'].lower()}_{rep.id}.md", b["markdown"])
    if b["status"] == "FAILED_INVALID" and b.get("raw"):
        st.write_text(f"reports/{stamp}/{t or 'PORTFOLIO'}_{rep.id}_raw_model_output.txt",
                      "Raw model output (unparseable)\n\n" + b["raw"])

    if b["status"] == "SKIPPED_CAP":
        ev_q = ev.model_copy()
        st.put("hindsight_event", ev_q)
        st.set_status("hindsight_event", ev_q.event_id, "QUEUED")
        run.queued += 1
    if out is None:
        _case_file(run, b)
        return

    st.mark_seen(ev.dedup_key(), t, rep.id, stamp)
    st.set_status("hindsight_event", ev.event_id, "DONE")
    if t:
        st.put("seven_powers_assessment", SevenPowersRecord(ticker=t, assessments=out.seven_powers, report_id=rep.id))
        st.put("bias_assessment", BiasRecord(ticker=t, assessments=out.munger_scan, report_id=rep.id,
                                             downgrades=[n for n in b.get("validation_notes", []) if n.startswith("Bias")]))
        if b.get("tc") is not None:
            tc = b["tc"]
            st.put("thesis_snapshot", ThesisSnapshot(ticker=t, version_ref=f"thesis_history:{t}",
                                                     classification=tc.classification, red_flags=tc.red_flags,
                                                     revision_count=tc.revision_count))
    evidenced = [x.tendency for x in out.munger_scan if x.state in EVIDENCED_BIAS_STATES]
    bias_profile.record_flags(run.profile, evidenced)
    price = None
    for r, text in b.get("fact_lines", []):
        if r.startswith("behaviour:") and "current price $" in text:
            try:
                price = float(text.split("current price $", 1)[1].split()[0])
            except (ValueError, IndexError):
                price = None
    for w in out.warnings:
        st.put("hindsight_warning", WarningRecord(
            ticker=t, report_id=rep.id, text=w.text, severity=w.severity, predictions=w.predictions,
            tendencies=evidenced, lollapalooza=out.severity == "LOLLAPALOOZA", raised_price=price, status="OPEN",
            source_refs=[f"report:{rep.id}"]))
    for q in out.unanswered_questions:
        st.put("hindsight_question", HindsightQuestion(ticker=t, question=q, report_id=rep.id, status="OPEN"))
    for q in out.questions_for_theo:
        st.put("hindsight_question", HindsightQuestion(ticker=t, question=q, report_id=rep.id, for_theo=True, status="OPEN"))
    if t and price:
        base = st.kv_get(f"triage:{t}", {}) or {}
        base["review_price"] = price
        st.kv_set(f"triage:{t}", base)
    _case_file(run, b)


def _case_file(run: Run, b: dict) -> None:
    t = b.get("ticker") or "PORTFOLIO"
    path = run.store.root / "case_files" / f"{t}.md"
    header = "" if path.exists() else (
        f"# {t}: Harry Hindsight case file\n\n"
        "Every entry is dated and traces back to its evidence refs. Newest at the bottom.\n\n")
    out: AnalysisOutput | None = b.get("output")
    entry = [f"## {run.today} | {b['report_type']} | {out.severity if out else b['status']}",
             f"Trigger: {clean(b.get('reason', ''))}", f"Report id: {b.get('report_id', '')}"]
    if out:
        entry.append(f"Verdict: {clean(out.verdict)}")
        for w in out.warnings:
            entry.append(f"- Warning ({w.severity}): {clean(w.text)}")
            for p in w.predictions:
                entry.append(f"  - predict {clean(p.metric)} {p.direction} {clean(p.threshold)} by {p.check_date}")
        for q in out.unanswered_questions:
            entry.append(f"- Open question: {clean(q)}")
        for q in out.questions_for_theo:
            entry.append(f"- Question for Theo: {clean(q)}")
    else:
        entry.append(f"Status: {b['status']}. {clean(b.get('error', ''))}")
    run.store.append_text(f"case_files/{t}.md", header + "\n".join(entry) + "\n\n")


# ---------------------------------------------------------------------------
# JM Watch List triage
# ---------------------------------------------------------------------------


def run_triage(run: Run, bob_today: list[HindsightEvent]) -> list[HindsightEvent]:
    tickers = run.watchlist.tickers()
    prices = run.prices.recent(tickers)
    escalate: list[HindsightEvent] = []
    lines = []
    for t in tickers:
        thesis = run.theo.get(t)
        log = git_log(f"theses/{t}.md", run.repo_root) if thesis is not None else []
        thesis_ref = log[0][0] if log else (file_hash(run.repo_root / "theses" / f"{t}.md") if thesis else None)
        baseline = run.store.kv_get(f"triage:{t}", {}) or {}
        reasons, new_base = triage_gates(
            t, cfg=run.cfg, prices=prices.get(t), baseline=baseline, bob_today=bob_today,
            wally_row=run.wally.row(t), thesis=thesis, thesis_ref=thesis_ref,
            valuation_hash=file_hash(run.repo_root / "valuations" / f"{t.lower()}_ax.yaml"),
        )
        if not run.dry_run:
            run.store.kv_set(f"triage:{t}", new_base)
        if not reasons:
            lines.append(f"{t:<5} | NO CHANGE | Nothing today changes the thesis.")
            continue
        if run.dry_run:
            lines.append(f"{t:<5} | GATE      | {'; '.join(reasons)} (dry run, no triage call)")
            continue
        blocks = [data_block(f"gates:{t}", "HINDSIGHT (deterministic gates)", reasons)]
        if prices.get(t):
            blocks.append(data_block(f"price:{t}", "MARKET", prices[t]))
        if thesis is not None:
            blocks.append(data_block(f"thesis:{t}", "THEO", {"the_bet": thesis.the_bet,
                                     "pillars": [{"id": p.id, "claim": p.claim, "kill_condition": p.kill_condition,
                                                  "status": p.status} for p in thesis.pillars]}))
        for ev in bob_today:
            if ev.ticker == t:
                blocks.append(data_block(ev.source_report_ref, "BOB", {"reason": ev.reason, "priority": ev.priority}))
        res = run.llm.structured(TRIAGE_SYSTEM, build_triage_prompt(t, reasons, blocks), TriageOutput, "triage",
                                 label=f"TRIAGE:{t}")
        if res.status == "OK":
            lines.append(f"{t:<5} | {res.data.status:<9} | {clean(res.data.reason)}")
            if res.data.status == "ESCALATE":
                escalate.append(HindsightEvent(
                    source="SCHEDULER", event_type="WATCHLIST_DAILY", ticker=t, priority="HIGH",
                    source_report_ref=f"triage:{run.today}:{t}", reason=f"JM triage escalated: {res.data.reason}",
                    payload={"gates": reasons}))
        elif res.status == "SKIPPED_CAP":
            lines.append(f"{t:<5} | SKIPPED   | gate fired ({'; '.join(reasons)}); triage skipped, call cap reached")
        else:
            lines.append(f"{t:<5} | FAILED    | gate fired ({'; '.join(reasons)}); triage {res.status}: {res.error[:120]}")
    run.triage = lines
    return escalate


# ---------------------------------------------------------------------------
# Autopsy
# ---------------------------------------------------------------------------


def autopsies_due(run: Run, bob_today: list[HindsightEvent]) -> list[WarningRecord]:
    results_today = {e.ticker for e in bob_today if "RESULT" in (e.event_subtype + str(e.payload.get("item", {}).get("type", ""))).upper()}
    due = []
    for w in run.store.query("hindsight_warning", WarningRecord, status="OPEN"):
        raised = dt.date.fromisoformat(w.created_at[:10])
        results_since = run.today if w.ticker in results_today else None
        if any(p.due(run.today, results_since, raised) for p in w.predictions):
            due.append(w)
    return due


def run_autopsy(run: Run, w: WarningRecord) -> str:
    price = run.prices.current(w.ticker) if w.ticker else None
    responses = [r for r in run.store.query("johnny_response", JohnnyResponse, ticker=w.ticker) if r.created_at >= w.created_at]
    blocks = [
        data_block(f"warning:{w.id}", "HINDSIGHT (past warning)", w.model_dump(mode="json")),
        data_block(f"price:{w.ticker}", "MARKET", {"price_when_raised": w.raised_price, "price_now": price}),
        data_block(f"responses:{w.ticker}", "JOHNNY", [r.model_dump(mode="json") for r in responses]),
    ]
    thesis = run.theo.get(w.ticker)
    if thesis is not None:
        blocks.append(data_block(f"thesis:{w.ticker}", "THEO", {"pillars": [{"id": p.id, "status": p.status} for p in thesis.pillars]}))
    res = run.llm.structured(AUTOPSY_SYSTEM, build_autopsy_prompt(w.ticker, blocks), AutopsyOutput, "full",
                             label=f"AUTOPSY:{w.ticker}")
    if res.status != "OK":
        return f"{w.ticker:<5} | AUTOPSY {res.status} | {res.error[:120]}"
    a: AutopsyOutput = res.data
    run.store.put("autopsy", Autopsy(ticker=w.ticker, warning_id=w.id, report_id=w.report_id, result=a.model_dump(),
                                     status=a.warning_right, source_refs=[f"warning:{w.id}"]))
    run.store.put("hindsight_outcome", HindsightOutcome(ticker=w.ticker, warning_id=w.id, outcome=a.warning_right,
                                                        status="useful" if a.useful else "not useful"))
    if a.warning_right != "TOO EARLY":
        run.store.set_status("hindsight_warning", w.id, "CLOSED")
    bias_profile.record_usefulness(run.profile, w.tendencies, a.useful, run.cfg)
    run.store.append_text(f"case_files/{w.ticker}.md",
                          f"## {run.today} | AUTOPSY of warning {w.id}\n- Right: {a.warning_right}; useful: {a.useful}\n"
                          f"- Missed: {clean(a.what_it_missed)}\n- Bias read: {a.bias_read_correct}\n"
                          f"- Johnny acted: {clean(a.johnny_acted)}\n\n")
    return f"{w.ticker:<5} | AUTOPSY {a.warning_right} | useful={a.useful}; {clean(a.notes)[:120]}"


def scorecard(run: Run) -> list[str]:
    autopsies = run.store.query("autopsy", Autopsy)
    warnings = {w.id: w for w in run.store.query("hindsight_warning", WarningRecord)}
    if not autopsies:
        return ["No autopsies yet. Hindsight has not been held to account on anything, so no hit rate to report."]
    by_sev: dict[str, list[str]] = {}
    lolla = []
    for a in autopsies:
        w = warnings.get(a.warning_id)
        sev = w.severity if w else "?"
        by_sev.setdefault(sev, []).append(a.result.get("warning_right", ""))
        if w and w.lollapalooza:
            lolla.append(a.result.get("warning_right", ""))
    lines = []
    for sev, results in sorted(by_sev.items()):
        judged = [r for r in results if r != "TOO EARLY"]
        hits = sum(1 for r in judged if r in ("YES", "PARTLY"))
        misses = sum(1 for r in judged if r == "NO")
        n = len(judged) or 1
        lines.append(f"{sev}: {len(judged)} judged, hit rate {hits / n:.0%}, false alarm rate {misses / n:.0%}")
    if lolla:
        judged = [r for r in lolla if r != "TOO EARLY"]
        lines.append(f"Lollapalooza precision: {sum(1 for r in judged if r == 'YES')}/{len(judged)}")
    for name, t in sorted(run.profile.get("tendencies", {}).items()):
        if t.get("autopsied"):
            flag = " (deprioritised)" if t.get("deprioritised") else ""
            lines.append(f"{name}: flagged {t.get('flags', 0)}, useful {t.get('useful', 0)}/{t['autopsied']}{flag}")
    return lines


# ---------------------------------------------------------------------------
# Johnny's actions, inferred from the files he already keeps
# ---------------------------------------------------------------------------


def infer_responses(run: Run) -> None:
    ledger = run.portfolio.ledger()
    now = {t: len(h.decisions) for t, h in ledger.holdings.items()} if ledger else {}
    held = set(run.portfolio.holdings())
    snap = run.store.kv_get("holdings_snapshot")
    reported = {r.ticker for r in run.store.query("hindsight_report", HindsightReport)}
    if snap and not run.dry_run:
        for t in reported:
            if now.get(t, 0) > snap.get("decisions", {}).get(t, 0):
                run.store.put("johnny_response", JohnnyResponse(ticker=t, action="added", inferred=True,
                                                                note="new buy decision in the ledger"))
            if t in snap.get("held", []) and t not in held:
                run.store.put("johnny_response", JohnnyResponse(ticker=t, action="sold", inferred=True,
                                                                note="removed from tickers.yaml"))
    if not run.dry_run:
        run.store.kv_set("holdings_snapshot", {"decisions": now, "held": sorted(held)})


# ---------------------------------------------------------------------------
# Portfolio review
# ---------------------------------------------------------------------------


def portfolio_event(run: Run, key: str) -> HindsightEvent:
    return HindsightEvent(source="SCHEDULER", event_type="PORTFOLIO_REVIEW", priority="MEDIUM",
                          source_report_ref=f"portfolio:{key}", reason=f"Scheduled portfolio review ({key})")


def portfolio_context(run: Run) -> tuple[list[str], dict[str, str], list[str]]:
    rows, refs, triggers = [], {}, []
    holdings = run.portfolio.holdings()
    ledger = run.portfolio.ledger()
    tickers = sorted(set(holdings) | set(ledger.holdings if ledger else {}))
    prices = run.prices.recent(tickers)
    for t in tickers:
        thesis = run.theo.get(t)
        tc = thesis_change.detect(t, thesis, [])
        f = behaviour.compute(t, ledger=ledger, thesis=thesis, thesis_versions=1 if thesis else 0,
                              current_price=(prices.get(t) or {}).get("last"), price_source="yfinance",
                              sally_streak=run.sally.consecutive_flags(t), answered_after=_answered_dates(run, t, thesis),
                              positions_row=run.portfolio.positions_yaml().get(t, {}), repo_root=run.repo_root,
                              today=run.today, cfg=run.cfg)
        hard = f.hard_triggers(run.cfg)
        triggers += [f"{t}: {h}" for h in hard]
        open_w = [w for w in run.store.query("hindsight_warning", WarningRecord, ticker=t) if w.status == "OPEN"]
        last = [r for r in run.store.query("hindsight_report", HindsightReport, ticker=t) if r.analysis_status == "OK"]
        refs[f"behaviour:{t}"] = "; ".join(x for r, x in f.lines() if r.startswith("behaviour:"))[:300]
        rows.append({
            "ticker": t, "held": t in holdings, "archetype": getattr(thesis, "archetype", None),
            "thesis_classification": tc.classification, "thesis_red_flags": tc.red_flags,
            "avg_cost": f.avg_cost, "price": f.current_price,
            "unrealised_pct": round(f.unrealised_pct, 1) if f.unrealised_pct is not None else None,
            "averaging_down_tranches": f.averaging_down_tranches, "cost_weight": f.cost_weight,
            "value_weight": f.value_weight, "hard_triggers": hard, "open_warnings": len(open_w),
            "last_severity": last[-1].severity if last else None,
            "the_bet": getattr(thesis, "the_bet", None),
        })
    blocks = [data_block("portfolio:book", "HINDSIGHT (computed)", rows)]
    return blocks, refs, triggers


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def collect_events(run: Run, mode: str, ticker: str | None) -> tuple[list[HindsightEvent], list[HindsightEvent]]:
    """``(events_to_analyse, bob_today)``."""
    holdings = set(run.portfolio.holdings())
    jm = set(run.watchlist.tickers())
    bob_all = bob_events(run.bob, run.cfg)
    if mode == "sell":
        return [run.sally.manual_event(ticker)], bob_all
    if mode == "portfolio":
        return [portfolio_event(run, f"manual:{run.today}")], bob_all

    events: list[HindsightEvent] = []
    for q in run.store.query("hindsight_event", HindsightEvent, status="QUEUED"):
        events.append(q)
    events += run.sally.events(holdings)
    events += [e for e in bob_all if bob_triggers(e) and (e.ticker in holdings or e.ticker in jm)]
    events += wally_top(run.wally, run.cfg, run.repo_root)
    done = run.store.kv_get("portfolio_reviews_done", []) or []
    key = portfolio_review_due(run.today, run.cfg, done)
    if key:
        events.append(portfolio_event(run, key))
    return events, bob_all


def execute(run: Run, mode: str = "daily", ticker: str | None = None, send: bool = True) -> dict:
    infer_responses(run)
    events, bob_today = collect_events(run, mode, ticker)

    autopsy_lines = []
    if mode in ("daily", "autopsy"):
        for w in autopsies_due(run, bob_today):
            if run.dry_run:
                autopsy_lines.append(f"{w.ticker:<5} | AUTOPSY DUE | {w.text[:80]}")
            else:
                autopsy_lines.append(run_autopsy(run, w))

    # Full analyses first for Sally sells and Bob criticals, then triage, then the rest.
    ordered = sort_events(events)
    urgent = [e for e in ordered if e.event_type in ("SELL_SIGNAL",) or e.priority == "CRITICAL"]
    rest = [e for e in ordered if e not in urgent]

    def _do(evs: list[HindsightEvent]) -> None:
        seen_keys = set()
        for ev in evs:
            key = ev.dedup_key()
            if key in seen_keys or (run.store.seen(key) and not run.force):
                run.duplicates += 1
                continue
            seen_keys.add(key)
            if ev.event_type == "PORTFOLIO_REVIEW":
                blocks, refs, triggers = portfolio_context(run)
                b = analyse(run, ev, None, blocks, refs, triggers)
                if b["status"] == "OK":
                    done = run.store.kv_get("portfolio_reviews_done", []) or []
                    run.store.kv_set("portfolio_reviews_done", done + [ev.source_report_ref.split(":", 1)[1]])
            else:
                b = analyse(run, ev)
            run.reports.append(b)

    _do(urgent)
    if mode == "daily":
        # Bob, Wally and Sally each trigger a run (and Bob has a catch-up cron),
        # so the JM triage runs at most once a day; later runs only pick up
        # new events.
        if run.store.kv_get("triage_done") == run.today.isoformat() and not run.force:
            run.notes.append("JM Watch List already triaged today; this run only reviews new events.")
        else:
            rest = sort_events(rest + run_triage(run, bob_today))
            if not run.dry_run:
                run.store.kv_set("triage_done", run.today.isoformat())
    _do(rest)
    if autopsy_lines:
        run.triage += ["", "Autopsies:"] + autopsy_lines

    if not run.dry_run:
        bias_profile.save(run.store, run.profile)
    quarter = f"{run.today.year}-Q{(run.today.month - 1) // 3 + 1}"
    card = None
    if mode == "scorecard" or (mode == "daily" and run.store.kv_get("scorecard_quarter") != quarter):
        card = scorecard(run)
        if not run.dry_run:
            run.store.kv_set("scorecard_quarter", quarter)

    if run.duplicates:
        run.notes.append(f"{run.duplicates} event(s) already reviewed on an earlier run; not repeated.")
    summary = {"date": run.today.isoformat(), "mode": mode, "reports": run.reports, "triage": run.triage,
               "queued": run.queued, "notes": run.notes, "scorecard": card, "llm": run.llm.summary()}
    subject, body = render_email(summary)
    summary["subject"], summary["body"] = subject, body

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run.store.write_text(f"runs/{stamp}_email.md", f"# {subject}\n\n{body}")
    run.store.write_text(f"runs/{stamp}.json", json.dumps({
        "date": summary["date"], "mode": mode, "dry_run": run.dry_run, "llm": summary["llm"],
        "reports": [{"ticker": b.get("ticker"), "type": b["report_type"], "status": b["status"],
                     "severity": b["output"].severity if b.get("output") else None} for b in run.reports],
        "queued": run.queued, "duplicates": run.duplicates, "triage_lines": len(run.triage),
    }, indent=2))
    if run.dry_run:
        for b in run.reports:
            run.store.write_text(f"runs/{stamp}_prompt_{b.get('ticker') or 'PORTFOLIO'}.txt", b["prompt"])

    if not run.dry_run:
        from .emit import write_events

        write_events(run.store, run.today, run.reports)
    pdfs, pdf_errors = build_pdfs(run)
    summary["pdfs"] = pdfs
    summary["html"] = build_email_html(summary, pdfs, pdf_errors)
    sent = False
    if send and not run.dry_run and (run.reports or any("NO CHANGE" not in line for line in run.triage if line)):
        sent = _send_email(subject, body, summary["html"], list(pdfs.values()))
    summary["sent"] = sent
    # Logs are public on a public repo: counts and statuses only, never content.
    statuses: dict[str, int] = {}
    for b in run.reports:
        statuses[b["status"]] = statuses.get(b["status"], 0) + 1
    print(f"[hindsight] mode={mode} reports={len(run.reports)} statuses={statuses} queued={run.queued} "
          f"duplicates={run.duplicates} triage={len(run.triage)} calls={run.llm.calls}/{run.llm.max_calls} "
          f"cost~US${run.llm.cost_usd:.2f} email_sent={sent}")
    return summary


def build_pdfs(run: Run) -> tuple[dict[str, Path], dict[str, str]]:
    """One forwardable PDF per OK report, saved in the private store.

    A PDF that fails to render is reported on its card, never silently dropped.
    """
    pdfs: dict[str, Path] = {}
    errors: dict[str, str] = {}
    if run.dry_run:
        return pdfs, errors
    date = run.today.isoformat()
    for b in run.reports:
        if b["status"] != "OK":
            continue
        key = b.get("report_id", "")
        html = build_pdf_html(b, date)
        path = run.store.root / "reports" / date / pdf_filename(b, date)
        try:
            pdfs[key] = render_pdf(html, path)
        except ImportError:
            errors[key] = "weasyprint is not installed on this runner, so the PDF could not be rendered."
        except Exception as exc:  # the real error, on the card
            errors[key] = f"PDF render failed: {type(exc).__name__}: {exc}"
        run.store.write_text(f"reports/{date}/{pdf_filename(b, date)[:-4]}.html", html)
    return pdfs, errors


def _send_email(subject: str, body: str, html: str | None = None, attachments: list[Path] | None = None) -> bool:
    import sys

    sys.path.insert(0, str(REPO_ROOT))
    from email_sender import send_summary_email

    return send_summary_email(subject, body, attachments=attachments, body_html=html)
