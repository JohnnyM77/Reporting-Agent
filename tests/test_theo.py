#!/usr/bin/env python3
"""Theo's tests. A plain script — run it with `python tests/test_theo.py`.

No pytest dependency on purpose: this runs in the Pages workflow before the
site is built, and the fewer things that have to install first, the fewer ways
the build has to fail for reasons that are not about the theses.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from theo import drift as drift_mod  # noqa: E402
from theo import ledger as ledger_mod  # noqa: E402
from theo import publish as publish_mod  # noqa: E402
from theo import render as render_mod  # noqa: E402
from theo import season as season_mod  # noqa: E402
from theo import signals as signals_mod  # noqa: E402
from theo import thesis as thesis_mod  # noqa: E402

THESES_DIR = REPO_ROOT / "theses"

_failures: list[str] = []
_passes = 0


def check(condition: bool, label: str, detail: str = "") -> None:
    global _passes
    if condition:
        _passes += 1
        print(f"  ok   {label}")
    else:
        _failures.append(f"{label}{f' — {detail}' if detail else ''}")
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))


# --------------------------------------------------------------------------

def test_theses_parse_and_validate(theses):
    print("\nthesis files parse and validate")
    check(bool(theses), "at least one thesis file exists", f"looked in {THESES_DIR}")
    for thesis in theses:
        problems = thesis_mod.validate(thesis)
        check(not problems, f"{thesis.ticker} validates", "; ".join(problems))
        check(bool(thesis.the_bet), f"{thesis.ticker} has a bet")
        check(
            thesis_mod.word_count(thesis.the_bet) <= thesis_mod.MAX_BET_WORDS,
            f"{thesis.ticker} bet is under {thesis_mod.MAX_BET_WORDS} words",
            f"{thesis_mod.word_count(thesis.the_bet)} words",
        )


def test_every_pillar_has_a_kill_condition(theses):
    print("\nevery pillar carries a kill condition")
    for thesis in theses:
        check(bool(thesis.pillars), f"{thesis.ticker} has pillars")
        check(
            len(thesis.pillars) <= thesis_mod.MAX_PILLARS,
            f"{thesis.ticker} has at most {thesis_mod.MAX_PILLARS} pillars",
            f"{len(thesis.pillars)}",
        )
        for pillar in thesis.pillars:
            check(
                bool(pillar.kill_condition.strip()),
                f"{thesis.ticker} {pillar.id} has a kill condition",
            )


def test_validator_rejects_what_it_should():
    print("\nthe validator actually fails the things it promises to fail")
    base = {
        "ticker": "TEST",
        "the_bet": "A short, testable reason to own the thing.",
        "pillars": [{"id": "P1", "claim": "c", "kill_condition": "k"}],
    }

    def problems(**overrides):
        data = dict(base)
        data.update(overrides)
        return thesis_mod.validate(thesis_mod.from_dict(data))

    check(not problems(), "a well-formed thesis passes")
    check(bool(problems(the_bet="")), "empty the_bet fails")
    check(
        bool(problems(the_bet=" ".join(["word"] * (thesis_mod.MAX_BET_WORDS + 1)))),
        "an over-long the_bet fails",
    )
    check(
        bool(
            problems(
                pillars=[
                    {"id": f"P{i}", "claim": "c", "kill_condition": "k"} for i in range(1, 6)
                ]
            )
        ),
        "five pillars fails",
    )
    check(
        bool(problems(pillars=[{"id": "P1", "claim": "c"}])),
        "a pillar with no kill condition fails",
    )
    check(bool(problems(origin="BORROWED")), "borrowed with no sources fails")
    check(
        bool(problems(status="EXITED", exit={"date": "2020-01-01", "reason": "BETTER_USE"})),
        "an exit with no sell thesis fails",
    )


def test_entry_slide_is_blind_to_the_outcome(theses, ledger):
    print("\nthe entry slide does not know how it turned out")
    today = dt.date(2026, 8, 22)
    for thesis in theses:
        entry = render_mod.render_html(thesis, "entry", ledger, today)
        current = render_mod.render_html(thesis, "current", ledger, today)
        check(
            "Value today" not in entry,
            f"{thesis.ticker} entry slide has no 'Value today'",
        )
        check(
            "Value today" in current,
            f"{thesis.ticker} current slide has 'Value today'",
        )
        check(
            'class="drift' not in entry,
            f"{thesis.ticker} entry slide has no drift banner",
        )
        check('class="drift' in current, f"{thesis.ticker} current slide has a drift banner")


def test_hold_thesis_leads_the_current_slide(theses, ledger):
    print("\nthe current slide leads with the hold thesis where one exists")
    today = dt.date(2026, 8, 22)
    for thesis in theses:
        if not thesis.hold_thesis:
            continue
        current = render_mod.render_html(thesis, "current", ledger, today)
        entry = render_mod.render_html(thesis, "entry", ledger, today)
        check(
            "Why it is still held" in current,
            f"{thesis.ticker} current slide is labelled as the hold thesis",
        )
        check(
            "The bet, as written at entry" in entry,
            f"{thesis.ticker} entry slide is labelled as the entry bet",
        )


def test_drift_and_season(theses):
    print("\ndrift and the season window")
    for thesis in theses:
        verdict, findings = season_mod.assess(thesis, dt.date(2026, 8, 22))
        check(
            verdict in ("DRIFTING", "WATCH", "CLEAN"),
            f"{thesis.ticker} gets a drift verdict",
            verdict,
        )
        if not thesis.reviews:
            check(
                any(f.code == "NEVER_REVIEWED" for f in findings),
                f"{thesis.ticker} is flagged as never reviewed",
            )

    check(season_mod.in_prep_window(dt.date(2026, 1, 15)), "January is a prep month")
    check(season_mod.in_prep_window(dt.date(2026, 7, 15)), "July is a prep month")
    check(not season_mod.in_prep_window(dt.date(2026, 8, 15)), "August is not a prep month")

    if theses:
        _, july = season_mod.assess(theses[0], dt.date(2026, 7, 15))
        _, march = season_mod.assess(theses[0], dt.date(2026, 3, 15))
        check(
            any(f.code == "PRE_SEASON_REVIEW_DUE" for f in july),
            "the pre-season finding fires inside the prep window",
        )
        check(
            not any(f.code == "PRE_SEASON_REVIEW_DUE" for f in march),
            "the pre-season finding stays quiet outside it",
        )

    pack = season_mod.build_pack(theses, ledger_mod.EMPTY, dt.date(2026, 7, 15))
    check("PRE-SEASON REVIEW PACK" in pack, "the season pack renders")
    check(
        "what number in this result" in pack.lower(),
        "the pack asks for a number before the result",
    )


def test_signals_open_questions_and_orphans():
    print("\nagent signals become questions Theo will not drop")
    sally = {
        "last_run": "2026-08-23",
        "flagged": [
            {"ticker": "AAA", "sally_verdict": "Trim candidate",
             "alert_tier": "Tier 3: Deep Review", "trailing_pe": 40.0,
             "valuation_percentile": 0.95},
            {"ticker": "ZZZ", "sally_verdict": "Trim candidate", "alert_tier": "Tier 3"},
        ],
    }
    tmp = REPO_ROOT / "site" / "_test_signals"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "sally.json").write_text(json.dumps(sally), encoding="utf-8")

    sigs = signals_mod.load_all(tmp)
    check(len(sigs) == 2, "both flags are read as signals", str(len(sigs)))
    check(all(s.kind == signals_mod.SALLY_TRIM for s in sigs), "trim verdicts map to SALLY_TRIM")

    held = thesis_mod.from_dict({
        "ticker": "AAA",
        "the_bet": "A short testable reason to own the thing.",
        "pillars": [{"id": "P1", "claim": "c", "kill_condition": "k"}],
    })

    questions = signals_mod.open_questions([held], sigs)
    check(len(questions) == 1 and questions[0][0].ticker == "AAA",
          "an unanswered signal is an open question")
    check([s.ticker for s in signals_mod.orphans([held], sigs)] == ["ZZZ"],
          "a signal against a holding with no thesis is an orphan")

    findings = signals_mod.findings_for(held, sigs)
    check(any(f.code == "SIGNAL_UNANSWERED" for f in findings),
          "an unanswered signal is a WARN finding")
    check(all(f.severity == drift_mod.WARN for f in findings if f.code == "SIGNAL_UNANSWERED"),
          "unanswered is a warning, not an alert")

    # Answering it — even by declining — closes the question.
    answered = thesis_mod.from_dict({
        "ticker": "AAA",
        "the_bet": "A short testable reason to own the thing.",
        "pillars": [{"id": "P1", "claim": "c", "kill_condition": "k"}],
        "reviews": [{"date": "2026-08-23", "trigger": "SALLY_TRIM",
                     "decision": "DECLINED", "verdict": "Holding."}],
    })
    check(not signals_mod.open_questions([answered], sigs),
          "a declined-but-recorded signal is answered")
    check(not any(f.code == "SIGNAL_UNANSWERED"
                  for f in signals_mod.findings_for(answered, sigs)),
          "answering clears the unanswered finding")

    # A review dated before the signal does not answer the new one.
    stale = thesis_mod.from_dict({
        "ticker": "AAA",
        "the_bet": "A short testable reason to own the thing.",
        "pillars": [{"id": "P1", "claim": "c", "kill_condition": "k"}],
        "reviews": [{"date": "2025-01-01", "trigger": "SALLY_TRIM", "decision": "DECLINED"}],
    })
    check(len(signals_mod.open_questions([stale], sigs)) == 1,
          "last year's answer does not close this year's signal")

    for f in tmp.glob("*.json"):
        f.unlink()
    tmp.rmdir()


def test_three_refusals_without_a_kill_condition_is_an_alert():
    print("\ndeclining three times without tightening anything is an alert")

    def build(decisions_and_amendments):
        return thesis_mod.from_dict({
            "ticker": "AAA",
            "the_bet": "A short testable reason to own the thing.",
            "pillars": [{"id": "P1", "claim": "c", "kill_condition": "k"}],
            "reviews": [
                {"date": d, "trigger": "SALLY_TRIM", "decision": "DECLINED",
                 "verdict": "Holding.", "amendments": a}
                for d, a in decisions_and_amendments
            ],
        })

    twice = build([("2026-01-01", []), ("2026-02-01", [])])
    codes = [f.code for f in signals_mod.findings_for(twice, [])]
    check("SIGNAL_REFUSED_REPEATEDLY" not in codes, "two refusals is not yet an alert")

    thrice = build([("2026-01-01", []), ("2026-02-01", []), ("2026-03-01", [])])
    findings = signals_mod.findings_for(thrice, [])
    hit = [f for f in findings if f.code == "SIGNAL_REFUSED_REPEATEDLY"]
    check(bool(hit), "three refusals is an alert")
    check(bool(hit) and hit[0].severity == drift_mod.ALERT, "and it is severity ALERT")

    tightened = build([
        ("2026-01-01", []),
        ("2026-02-01", [{"pillar": "P1", "change": "added a price ceiling",
                         "direction": "TIGHTENED"}]),
        ("2026-03-01", []),
    ])
    check(not [f for f in signals_mod.findings_for(tightened, [])
               if f.code == "SIGNAL_REFUSED_REPEATEDLY"],
          "writing the kill condition clears the escalation")


def test_signals_fold_into_the_drift_verdict():
    print("\nsignals reach the drift verdict through season.assess")
    clean = thesis_mod.from_dict({
        "ticker": "AAA",
        "the_bet": "A short testable reason to own the thing.",
        "evidence_grade": "A",
        "pillars": [{"id": "P1", "claim": "c", "kill_condition": "k"}],
        "reviews": [{"date": "2026-08-01", "verdict": "fine"}],
    })
    today = dt.date(2026, 8, 23)
    verdict_without, _ = season_mod.assess(clean, today)
    check(verdict_without == "CLEAN", "the thesis is clean on its own", verdict_without)

    sig = signals_mod.Signal(source="Sally", ticker="AAA",
                             kind=signals_mod.SALLY_TRIM, date=today)
    verdict_with, findings = season_mod.assess(clean, today, [sig])
    check(verdict_with == "WATCH", "an open question drags it to WATCH", verdict_with)
    check(any(f.code == "SIGNAL_UNANSWERED" for f in findings),
          "and the finding is carried through")


def test_missing_dashboard_json_is_not_an_error():
    print("\nno agent JSON is not an error")
    check(signals_mod.load_all(REPO_ROOT / "does" / "not" / "exist") == [],
          "a missing dashboard directory yields no signals")


def test_an_exited_thesis_is_not_drifting():
    print("\na closed position is not drifting")
    base = {
        "ticker": "AAA",
        "the_bet": "A short, testable reason to own the thing.",
        "evidence_grade": "A",
        "pillars": [{"id": "P1", "claim": "c", "kill_condition": "k", "status": "BREACHED"}],
    }
    today = dt.date(2026, 8, 23)

    held = thesis_mod.from_dict(base)
    verdict, findings = season_mod.assess(held, today)
    check(verdict == "DRIFTING", "a breached pillar you still hold is an alert", verdict)
    check(any(f.code == "PILLAR_BREACHED" for f in findings), "and it names the breach")

    exited = thesis_mod.from_dict({
        **base,
        "status": "EXITED",
        "exit": {"date": "2026-05-06", "reason": "THESIS_BROKEN", "pillar_failed": "P1",
                 "sell_thesis": "The pillar broke, so I sold."},
    })
    verdict, findings = season_mod.assess(exited, today)
    check(
        not any(f.code == "PILLAR_BREACHED" for f in findings),
        "the same breach on a closed position is the record, not an alert",
        str([f.code for f in findings]),
    )
    check(verdict == "CLEAN", "so an honestly-closed thesis reads CLEAN", verdict)

    # And a closed position does not go stale or nag for review.
    stale_exit = thesis_mod.from_dict({
        **base,
        "pillars": [{"id": "P1", "claim": "c", "kill_condition": "k"}],
        "status": "EXITED",
        "exit": {"date": "2020-01-01", "reason": "BETTER_USE", "sell_thesis": "Moved on."},
    })
    codes = [f.code for f in season_mod.assess(stale_exit, today)[1]]
    check("NEVER_REVIEWED" not in codes, "a closed position is not nagged for review", str(codes))
    check("STALE" not in codes, "and does not go stale", str(codes))


def test_site_builds(theses, ledger, tmp_dir: Path):
    print("\nthe site builds to one self-contained file")
    out = publish_mod.build(theses, ledger, tmp_dir, dt.date(2026, 8, 22))
    html = out.read_text(encoding="utf-8")
    check(out.is_file(), "site/index.html is written")
    # Parse the payload rather than just looking for the tag: autoescaping
    # once turned every quote in it into `&#34;` and the page still contained
    # the string "theo-data".
    start = html.find('id="theo-data">')
    blob = html[start + len('id="theo-data">') : html.find("</script>", start)]
    try:
        payload = json.loads(blob)
    except ValueError as exc:
        payload = {}
        check(False, "the embedded payload is valid JSON", str(exc))
    else:
        check(True, "the embedded payload is valid JSON")
    check(
        sorted(payload) == sorted(t.ticker for t in theses),
        "every thesis is in the payload",
        f"{sorted(payload)}",
    )
    for ticker, entry in payload.items():
        for slug, label in entry["versions"]:
            check(
                'class="slide"' in entry["slides"].get(slug, ""),
                f"{ticker} {slug} slide is embedded",
            )
    check("http://" not in html.replace("http://www.w3.org", ""), "no external http assets")
    check("cdn." not in html, "no CDN references")
    for thesis in theses:
        check(f'"{thesis.ticker}"' in html, f"{thesis.ticker} is in the payload")


def test_ledger_is_optional():
    print("\nthe ledger is optional")
    empty = ledger_mod.load("does/not/exist.xlsx")
    check(not empty.holdings, "a missing ledger loads as empty, not an error")
    check(ledger_mod.gaps(empty, []) == [], "gaps on an empty ledger is empty")
    flows = [(dt.date(2015, 8, 14), -1000.0), (dt.date(2020, 8, 14), 2000.0)]
    rate = ledger_mod.xirr(flows)
    check(
        rate is not None and abs(rate - 0.1487) < 0.001,
        "xirr solves a known doubling over five years",
        f"got {rate}",
    )
    check(ledger_mod.xirr([(dt.date(2020, 1, 1), -100.0)]) is None, "xirr needs a sign change")


def test_bhp_irr_ladder(ledger):
    print("\nBHP's three decisions form an ascending IRR ladder")
    if not ledger.holdings:
        print("  skip (no ledger present — this is expected until portfolio.xlsx lands)")
        return
    holding = ledger.get("BHP")
    if holding is None:
        print("  skip (ledger has no BHP block)")
        return
    decisions = [d for d in holding.decisions if not d.is_scrip]
    check(len(decisions) == 3, "BHP has three decisions", f"{len(decisions)}")
    irrs = [d.irr for d in decisions]
    check(all(i is not None for i in irrs), "every BHP decision has an IRR", str(irrs))
    if all(i is not None for i in irrs):
        check(
            irrs == sorted(irrs),
            "the IRRs ascend — each buy was made into more fear than the last",
            str([round(i * 100, 1) for i in irrs]),
        )


# --------------------------------------------------------------------------


def main() -> int:
    theses = thesis_mod.load_all(THESES_DIR)
    ledger = ledger_mod.load(as_at=dt.date(2026, 8, 22))
    tmp_dir = REPO_ROOT / "site" / "_test"

    test_theses_parse_and_validate(theses)
    test_every_pillar_has_a_kill_condition(theses)
    test_validator_rejects_what_it_should()
    test_entry_slide_is_blind_to_the_outcome(theses, ledger)
    test_hold_thesis_leads_the_current_slide(theses, ledger)
    test_drift_and_season(theses)
    test_signals_open_questions_and_orphans()
    test_three_refusals_without_a_kill_condition_is_an_alert()
    test_signals_fold_into_the_drift_verdict()
    test_an_exited_thesis_is_not_drifting()
    test_missing_dashboard_json_is_not_an_error()
    test_site_builds(theses, ledger, tmp_dir)
    test_ledger_is_optional()
    test_bhp_irr_ladder(ledger)

    for path in (tmp_dir / "index.html", tmp_dir / ".nojekyll"):
        path.unlink(missing_ok=True)
    if tmp_dir.is_dir():
        tmp_dir.rmdir()

    print()
    if _failures:
        print(f"{len(_failures)} failure(s), {_passes} passed:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print(f"All {_passes} checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
