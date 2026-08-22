"""Theo's command line.

    python -m theo.cli check          # CI gate — non-zero if a thesis is malformed
    python -m theo.cli list
    python -m theo.cli drift --all
    python -m theo.cli show BHP --current --format png
    python -m theo.cli season --today 2026-07-01
    python -m theo.cli publish
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Sequence

from . import drift as drift_mod
from . import ledger as ledger_mod
from . import publish as publish_mod
from . import render as render_mod
from . import season as season_mod
from . import thesis as thesis_mod
from .render import DASH, fmt_money, fmt_mult, fmt_pct, fmt_price, fmt_years

EXIT_OK = 0
EXIT_PROBLEMS = 1


# --------------------------------------------------------------------------
# Shared plumbing
# --------------------------------------------------------------------------


def _as_at(args: argparse.Namespace) -> dt.date:
    if getattr(args, "as_at", None):
        return dt.date.fromisoformat(args.as_at)
    return dt.date.today()


def _load(args: argparse.Namespace) -> tuple[list[thesis_mod.Thesis], ledger_mod.Ledger, dt.date]:
    today = _as_at(args)
    theses = thesis_mod.load_all(args.theses)
    ledger = ledger_mod.load(args.ledger, as_at=today)
    for warning in ledger.warnings:
        print(f"  ! {warning}", file=sys.stderr)
    return theses, ledger, today


def _pick(theses: Sequence[thesis_mod.Thesis], ticker: str) -> thesis_mod.Thesis | None:
    wanted = ticker.upper()
    return next((t for t in theses if t.ticker == wanted), None)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    theses = thesis_mod.load_all(args.theses)
    if not theses:
        print(f"No thesis files found in {args.theses}/")
        return EXIT_OK

    problems: list[str] = []
    for thesis in theses:
        found = thesis_mod.validate(thesis)
        mark = "FAIL" if found else "ok"
        print(f"  [{mark:>4}] {thesis.ticker:<6} {thesis.path.name if thesis.path else ''}")
        for problem in found:
            print(f"         - {problem}")
        problems.extend(found)

    print()
    if problems:
        print(f"{len(problems)} problem(s) across {len(theses)} thesis file(s).")
        return EXIT_PROBLEMS
    print(f"{len(theses)} thesis file(s), all valid.")
    return EXIT_OK


def cmd_list(args: argparse.Namespace) -> int:
    theses, ledger, today = _load(args)
    if not theses:
        print("No theses written yet. Start one with: python -m theo.cli new TICKER")
        return EXIT_OK

    header = f"{'TICKER':<7}{'ARCHETYPE':<24}{'PILLARS':<10}{'GR':<4}{'IRR':>8}{'MULT':>8}{'YRS':>6}  DRIFT"
    print(header)
    print("-" * len(header))
    for thesis in sorted(theses, key=lambda t: t.ticker):
        verdict, _ = season_mod.assess(thesis, today)
        holding = ledger.get(thesis.ticker)
        print(
            f"{thesis.ticker:<7}"
            f"{(thesis.archetype or '-').replace('_', ' ').title():<24}"
            f"{thesis.pillar_symbols:<10}"
            f"{thesis.evidence_grade:<4}"
            f"{(fmt_pct(holding.irr) if holding else DASH):>8}"
            f"{(fmt_mult(holding.multiple) if holding else DASH):>8}"
            f"{(fmt_years(holding.years) if holding else DASH):>6}"
            f"  {verdict}"
            + ("  DRAFT" if thesis.draft else "")
            + ("  EXITED" if thesis.is_exited else "")
        )
    return EXIT_OK


def cmd_drift(args: argparse.Namespace) -> int:
    theses, _, today = _load(args)
    shown = 0
    for thesis in sorted(theses, key=lambda t: t.ticker):
        verdict, findings = season_mod.assess(thesis, today)
        if verdict == drift_mod.CLEAN and not args.all:
            continue
        shown += 1
        print(f"\n{thesis.ticker} — {thesis.name or ''}  [{verdict}]")
        if not findings:
            print("  no findings")
        for finding in findings:
            print(f"  {finding.badge} {finding.severity:<5} {finding.code}: {finding.message}")
            if finding.detail:
                print(f"        {finding.detail[:200]}")
    if not shown:
        print("Nothing drifting." if not args.all else "No theses.")
    return EXIT_OK


def cmd_gaps(args: argparse.Namespace) -> int:
    theses, ledger, _ = _load(args)
    if not ledger.holdings:
        print(
            "No ledger loaded, so Theo cannot tell which holdings lack a thesis.\n"
            f"Expected a spreadsheet at {args.ledger or ledger_mod.DEFAULT_LEDGER_PATH}."
        )
        return EXIT_OK
    missing = ledger_mod.gaps(ledger, [t.ticker for t in theses])
    if not missing:
        print("Every holding has a thesis.")
        return EXIT_OK
    print(f"{len(missing)} holding(s) with capital committed and no thesis written:\n")
    print(f"{'TICKER':<8}{'VALUE':>14}{'IRR':>9}{'MULT':>8}")
    for holding in missing:
        print(
            f"{holding.ticker:<8}"
            f"{fmt_money(holding.value, args.show_amounts):>14}"
            f"{fmt_pct(holding.irr):>9}"
            f"{fmt_mult(holding.multiple):>8}"
        )
    return EXIT_OK


def cmd_irr(args: argparse.Namespace) -> int:
    theses, ledger, _ = _load(args)
    if not ledger.holdings:
        print(
            "No ledger loaded. IRR needs the transaction spreadsheet at "
            f"{args.ledger or ledger_mod.DEFAULT_LEDGER_PATH}."
        )
        return EXIT_OK

    rows = []
    for holding in ledger.holdings.values():
        for decision in holding.decisions:
            rows.append(decision)
    rows.sort(key=lambda d: (d.irr is None, -(d.irr or 0)))
    if args.top:
        rows = rows[: args.top]

    print(f"{'DECISION':<12}{'DATE':<12}{'PRICE':>10}{'YRS':>6}{'IRR':>9}{'MULT':>8}  NOTE")
    for decision in rows:
        note = "demerger scrip — no IRR" if decision.is_scrip else ("open" if decision.open else "closed")
        print(
            f"{decision.label:<12}"
            f"{(decision.date.isoformat() if decision.date else DASH):<12}"
            f"{fmt_price(decision.price):>10}"
            f"{fmt_years(decision.years):>6}"
            f"{fmt_pct(decision.irr):>9}"
            f"{fmt_mult(decision.multiple):>8}  {note}"
        )
    return EXIT_OK


def cmd_show(args: argparse.Namespace) -> int:
    theses, ledger, today = _load(args)
    thesis = _pick(theses, args.ticker)
    if thesis is None:
        print(f"No thesis for {args.ticker.upper()}.", file=sys.stderr)
        return EXIT_PROBLEMS

    if args.version:
        version = args.version
    elif args.exit:
        version = render_mod.KIND_EXIT
    elif args.current:
        version = render_mod.KIND_CURRENT
    else:
        version = render_mod.KIND_ENTRY

    out = args.out or f"slides/{thesis.ticker}-{version}.{args.format}"
    try:
        path = render_mod.render_slide(
            thesis,
            version=version,
            ledger=ledger,
            today=today,
            show_amounts=args.show_amounts,
            fmt=args.format,
            out_path=out,
        )
    except render_mod.RenderError as exc:
        print(f"Render failed: {exc}", file=sys.stderr)
        return EXIT_PROBLEMS
    print(f"Wrote {path}")
    return EXIT_OK


def cmd_season(args: argparse.Namespace) -> int:
    theses, ledger, today = _load(args)
    pack = season_mod.build_pack(theses, ledger, today, show_amounts=args.show_amounts)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(pack, encoding="utf-8")
        print(f"Wrote {out}")
    else:
        print(pack)
    return EXIT_OK


def cmd_publish(args: argparse.Namespace) -> int:
    theses, ledger, today = _load(args)
    problems = thesis_mod.validate_all(theses)
    if problems:
        print("Refusing to publish — fix these first:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_PROBLEMS
    path = publish_mod.build(theses, ledger, args.out, today, show_amounts=args.show_amounts)
    size = path.stat().st_size
    print(
        f"Wrote {path} ({size / 1024:.0f} KB) — {len(theses)} thesis file(s)"
        + (", amounts suppressed" if not args.show_amounts else ", amounts shown")
    )
    return EXIT_OK


def cmd_new(args: argparse.Namespace) -> int:
    ticker = args.ticker.upper()
    directory = Path(args.theses)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ticker}.md"
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite).", file=sys.stderr)
        return EXIT_PROBLEMS

    def ask(prompt: str, default: str = "") -> str:
        if not sys.stdin.isatty():
            return default
        try:
            answer = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
        except EOFError:
            return default
        return answer or default

    print(f"Scaffolding {path}. Blank answers are fine — the file is a draft.\n")
    name = ask("Company name", ticker)
    archetype = ask(
        "Archetype (COMPOUNDING_MACHINE/CYCLICAL_TRADE/STRUCTURAL_WINNER/"
        "SPECULATIVE_PREPROFIT/VALUE_TRAP/MELTING_ICE_CUBE)",
        "COMPOUNDING_MACHINE",
    )
    grade = ask("Evidence grade (A contemporaneous / B documents / C memory)", "C")
    conviction = ask("Conviction (LOW/MEDIUM/HIGH)", "MEDIUM")
    horizon = ask("Horizon", "")
    origin = ask("Origin (OWN/BORROWED/HYBRID)", "OWN")
    bet = ask("The bet, one sentence under 60 words", "")

    path.write_text(
        thesis_mod.scaffold(
            ticker,
            name=name,
            archetype=archetype,
            grade=grade,
            conviction=conviction,
            horizon=horizon,
            origin=origin,
            bet=bet,
        ),
        encoding="utf-8",
    )
    print(f"\nWrote {path}")
    print("Now write the pillars. Each one needs a kill condition, or check will fail it.")
    return EXIT_OK


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="theo", description="Thesis accountability manager.")
    parser.add_argument("--theses", default=str(thesis_mod.THESES_DIR), help="thesis directory")
    parser.add_argument("--ledger", default=None, help="path to the transaction spreadsheet (optional)")
    parser.add_argument("--as-at", default=None, help="value and date everything as at YYYY-MM-DD")
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("check", help="validate every thesis file (non-zero exit on problems)")
    p.set_defaults(func=cmd_check)

    p = subs.add_parser("list", help="one line per thesis")
    p.set_defaults(func=cmd_list)

    p = subs.add_parser("drift", help="show drift findings")
    p.add_argument("--all", action="store_true", help="include clean theses")
    p.set_defaults(func=cmd_drift)

    p = subs.add_parser("gaps", help="holdings with capital and no thesis")
    p.add_argument("--show-amounts", action="store_true")
    p.set_defaults(func=cmd_gaps)

    p = subs.add_parser("irr", help="decision-level IRR ladder")
    p.add_argument("--top", type=int, default=0)
    p.set_defaults(func=cmd_irr)

    p = subs.add_parser("show", help="render one slide")
    p.add_argument("ticker")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--current", action="store_true", help="live slide (default is entry)")
    group.add_argument("--exit", action="store_true", help="exit slide")
    p.add_argument("--version", default=None, help="explicit version slug, e.g. review-2024-08-20")
    p.add_argument("--format", choices=("png", "pdf", "html"), default="png")
    p.add_argument("--out", default=None)
    p.add_argument("--show-amounts", action="store_true")
    p.set_defaults(func=cmd_show)

    p = subs.add_parser("season", help="build the pre-season review pack")
    p.add_argument("--today", default=None, help="pretend it is this date (YYYY-MM-DD)")
    p.add_argument("--show-amounts", action="store_true")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_season)

    p = subs.add_parser("publish", help="build site/index.html")
    p.add_argument("--show-amounts", action="store_true")
    p.add_argument("--out", default=str(publish_mod.DEFAULT_OUT))
    p.set_defaults(func=cmd_publish)

    p = subs.add_parser("new", help="scaffold a blank thesis")
    p.add_argument("ticker")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_new)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # `season --today` is the same knob as the global `--as-at`.
    if getattr(args, "today", None) and not args.as_at:
        args.as_at = args.today
    if not hasattr(args, "show_amounts"):
        args.show_amounts = False
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
