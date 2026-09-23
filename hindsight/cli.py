"""Command line.

    python -m hindsight run [--mode daily|portfolio|autopsy|scorecard] [--dry-run] [--force] [--no-email]
    python -m hindsight sell NHC [--dry-run] [--force] [--no-email]
    python -m hindsight respond NHC --action held|sold|trimmed|added|ignored --note "..."
"""

from __future__ import annotations

import argparse
import sys

from .config import load_config
from .schemas import HindsightReport, JohnnyResponse
from .store import StoreConfigError, open_store


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m hindsight", description="Captain Hindsight")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="scheduled run (daily triage + event reviews)")
    r.add_argument("--mode", default="daily", choices=["daily", "portfolio", "autopsy", "scorecard"])
    r.add_argument("--ticker", default=None)
    for sp in (r, s := sub.add_parser("sell", help="full sell review of one holding from Sally's latest output")):
        sp.add_argument("--dry-run", action="store_true", help="build prompts and facts, no model call, no email")
        sp.add_argument("--force", action="store_true", help="re-run even if this event was already reviewed")
        sp.add_argument("--no-email", action="store_true")
    s.add_argument("ticker")

    resp = sub.add_parser("respond", help="record what you did about a Hindsight report")
    resp.add_argument("ticker")
    resp.add_argument("--action", required=True, choices=["held", "sold", "trimmed", "added", "ignored"])
    resp.add_argument("--note", default="")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = load_config()
    try:
        store = open_store(cfg)
    except StoreConfigError as exc:
        print(f"[hindsight] STORE MISCONFIGURED: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "respond":
        t = args.ticker.upper().replace(".AX", "")
        reports = store.query("hindsight_report", HindsightReport, ticker=t)
        rec = JohnnyResponse(ticker=t, action=args.action, note=args.note,
                             report_id=reports[-1].id if reports else "", source_refs=["cli"])
        store.put("johnny_response", rec)
        store.append_text(f"case_files/{t}.md",
                          f"## {rec.created_at[:10]} | JOHNNY'S RESPONSE\n- Action: {rec.action}\n- Note: {rec.note}\n\n")
        print(f"[hindsight] recorded: {t} {args.action}")
        return 0

    from .runner import execute, make_run

    run = make_run(cfg, store, dry_run=args.dry_run, force=args.force)
    if args.cmd == "sell":
        summary = execute(run, mode="sell", ticker=args.ticker.upper().replace(".AX", ""), send=not args.no_email)
    else:
        summary = execute(run, mode=args.mode, ticker=args.ticker, send=not args.no_email)
    failed = [b for b in summary["reports"] if b["status"].startswith("FAILED")]
    return 1 if failed and args.cmd == "sell" else 0


if __name__ == "__main__":
    raise SystemExit(main())
