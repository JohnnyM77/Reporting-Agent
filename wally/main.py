from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from .charts import render_range_chart, render_value_vs_price_chart
from .config import STANDARD_WATCHLISTS, TII75_WATCHLIST, LOW_THRESHOLD_PCT, build_run_context, load_email_settings, should_run_tii75
from .data_fetch import fetch_price_snapshot, fetch_valuation_snapshot
from .drive_upload import upload_or_replace_xlsx
from .email_report import build_html, build_combined_html, send_email
from .screening import TickerScreenResult, screen_snapshot
from .utils import safe_slug, write_json
from .watchlist_loader import load_watchlist
try:
    from .value_chart_builder import build_value_chart as _build_xlsx
    _XLSX_BUILDER_AVAILABLE = True
except ImportError:
    _XLSX_BUILDER_AVAILABLE = False
try:
    from .valuation_workbook import build_valuation_workbook as _build_valuation_workbook
    from .claude_analyst import analyse_opportunity as _analyse_opportunity
    _VALUATION_WORKBOOK_AVAILABLE = True
except ImportError:
    _VALUATION_WORKBOOK_AVAILABLE = False


@dataclass
class WatchlistProcessResult:
    """Result of processing a single watchlist."""
    watchlist_name: str
    run_date: str
    results: list[TickerScreenResult]
    flagged: list[TickerScreenResult]
    attachments: list[Path]
    chart_notes: dict[str, str]
    inline_images: list[tuple[str, Path]]


def _log_screen_result(row: TickerScreenResult) -> None:
    """Print a one-line screening diagnostic for a single ticker."""
    print(
        f"[wally] {row.ticker}"
        f" current={row.current_price:.2f}"
        f" low_52w={row.low_52w:.2f}"
        f" high_52w={row.high_52w:.2f}"
        f" distance_to_low_pct={row.distance_to_low_pct:.2f}"
        f" threshold={LOW_THRESHOLD_PCT:.2f}"
        f" flagged={row.flagged}",
        flush=True,
    )


def _process_watchlist(watchlist_path: str, force: bool = False, send_individual_email: bool = True, is_tii75: bool = False) -> WatchlistProcessResult:
    """Process a watchlist and optionally send individual email.
    
    Args:
        watchlist_path: Path to the watchlist YAML file
        force: Force processing even if gated
        send_individual_email: If True, send email immediately. If False, return data for combined email.
        is_tii75: If True, apply TII75 canonical validation when loading.
    
    Returns:
        WatchlistProcessResult with all processed data
    """
    wl = load_watchlist(watchlist_path, validate_tii75=is_tii75)
    ctx = build_run_context()
    ctx.output_root.mkdir(parents=True, exist_ok=True)

    results: list[TickerScreenResult] = []
    flagged: list[TickerScreenResult] = []
    attachments: list[Path] = []
    chart_notes: dict[str, str] = {}
    inline_images: list[tuple[str, Path]] = []  # (content-id, png_path)

    for ticker in wl.tickers:
        try:
            snap = fetch_price_snapshot(ticker)
            if not snap:
                results.append(
                    TickerScreenResult(
                        ticker=ticker,
                        company_name=ticker,
                        current_price=0,
                        low_52w=0,
                        high_52w=0,
                        distance_to_low_pct=0,
                        below_high_pct=0,
                        flagged=False,
                        error="No market data",
                    )
                )
                continue

            row = screen_snapshot(snap)
            _log_screen_result(row)
            results.append(row)
            if row.flagged:
                flagged.append(row)
                range_png = render_range_chart(row, ctx.output_root)
                attachments.append(range_png)
                value_png, note = render_value_vs_price_chart(ticker, ctx.output_root)
                chart_notes[ticker] = note
                if value_png:
                    attachments.append(value_png)

                # Build full canonical value chart workbook (5 sheets).
                # value_chart_builder.build_value_chart is the SOLE primary path.
                # A fallback summary workbook is only used when the full build fails,
                # and is always named explicitly as a fallback.
                if _XLSX_BUILDER_AVAILABLE:
                    try:
                        xlsx_out = ctx.output_root / f"{ticker.lower().replace('.', '_')}_value_chart.xlsx"
                        xlsx_path = _build_xlsx(ticker, output_path=str(xlsx_out), save_png=True)
                        attachments.append(Path(xlsx_path))
                        chart_notes[ticker] = "Value chart xlsx attached"
                        # Register PNG for inline email embedding
                        png_path = Path(str(xlsx_path).replace(".xlsx", ".png"))
                        if png_path.exists():
                            cid = f"chart_{ticker.lower().replace('.', '_')}"
                            inline_images.append((cid, png_path))
                        # Upload to Drive: YYMMDD-TICKER.xlsx naming convention
                        drive_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
                        if drive_folder_id:
                            drive_name = f"{ctx.run_dt.strftime('%y%m%d')}-{ticker.split('.')[0]}.xlsx"
                            try:
                                url = upload_or_replace_xlsx(
                                    Path(xlsx_path), drive_name, folder_id=drive_folder_id
                                )
                                print(f"[wally] Drive → {drive_name}: {url}", flush=True)
                            except Exception as drive_err:
                                print(f"[wally] Drive upload failed for {ticker}: {drive_err}", flush=True)
                    except Exception as xlsx_err:
                        # Full value-chart build failed — log exact reason, then fallback.
                        print(f"[wally] ERROR building full workbook for {ticker}: {xlsx_err}", flush=True)
                        if _VALUATION_WORKBOOK_AVAILABLE:
                            try:
                                val_snap = fetch_valuation_snapshot(ticker)
                                fallback_summary = {
                                    "company_name": row.company_name,
                                    "ticker": ticker,
                                    "current_price": row.current_price,
                                    "low_52w": row.low_52w,
                                    "high_52w": row.high_52w,
                                    "distance_to_low_pct": round(row.distance_to_low_pct, 2),
                                    "trailing_pe": val_snap.trailing_pe,
                                    "forward_pe": val_snap.forward_pe,
                                    "ev_ebitda": val_snap.ev_to_ebitda,
                                    "price_to_sales": val_snap.price_to_sales,
                                    "fcf_yield": val_snap.fcf_yield,
                                    "dividend_yield": val_snap.dividend_yield,
                                }
                                reasons = [
                                    f"Trading {row.distance_to_low_pct:.1f}% above "
                                    f"52-week low of {row.low_52w:.2f}"
                                ]
                                claude_analysis = _analyse_opportunity(
                                    ticker, row.company_name, fallback_summary, reasons
                                )
                                history_rows = [
                                    {
                                        "period": "current",
                                        "trailing_pe": val_snap.trailing_pe,
                                        "forward_pe": val_snap.forward_pe,
                                        "ev_ebitda": val_snap.ev_to_ebitda,
                                    }
                                ]
                                decision_rows = [
                                    {
                                        "issue": "Near 52-week low",
                                        "bull_case": "Quality business at discounted price",
                                        "bear_case": "Value trap / structural decline",
                                        "evidence": f"Distance to 52w low: {row.distance_to_low_pct:.1f}%",
                                        "wally_judgment": "Requires thesis review",
                                    }
                                ]
                                # Fallback file: explicitly named to distinguish from full output
                                fallback_out = ctx.output_root / f"{ticker.lower().replace('.', '_')}_fallback_review.xlsx"
                                _build_valuation_workbook(
                                    output_path=fallback_out,
                                    summary=fallback_summary,
                                    history_rows=history_rows,
                                    decision_rows=decision_rows,
                                    claude_analysis=claude_analysis,
                                )
                                attachments.append(fallback_out)
                                note = (
                                    "Fallback review workbook attached (Claude analysis)"
                                    if claude_analysis
                                    else "Fallback review workbook attached"
                                )
                                chart_notes[ticker] = note
                                print(f"[wally] Fallback workbook created: {fallback_out.name}", flush=True)
                                # Register the 52-week range chart as inline email image so
                                # the email body shows a visual chart (same experience as
                                # tickers with a valuations config whose value-chart PNG is
                                # registered inline above).
                                cid = f"chart_{ticker.lower().replace('.', '_')}"
                                inline_images.append((cid, range_png))
                                # Upload to Drive
                                drive_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
                                if drive_folder_id:
                                    drive_name = f"{ctx.run_dt.strftime('%y%m%d')}-{ticker.split('.')[0]}_fallback.xlsx"
                                    try:
                                        url = upload_or_replace_xlsx(
                                            fallback_out, drive_name, folder_id=drive_folder_id
                                        )
                                        print(f"[wally] Drive → {drive_name}: {url}", flush=True)
                                    except Exception as drive_err:
                                        print(f"[wally] Drive upload failed for {ticker}: {drive_err}", flush=True)
                            except Exception as fallback_err:
                                print(f"[wally] Fallback workbook build failed for {ticker}: {fallback_err}", flush=True)
        except Exception as e:
            print(f"[wally] Error processing {ticker}: {e}", flush=True)
            results.append(
                TickerScreenResult(
                    ticker=ticker,
                    company_name=ticker,
                    current_price=0,
                    low_52w=0,
                    high_52w=0,
                    distance_to_low_pct=0,
                    below_high_pct=0,
                    flagged=False,
                    error=str(e),
                )
            )

    payload = {
        "watchlist_name": wl.name,
        "watchlist_file": str(wl.source_path),
        "run_timestamp_utc": ctx.run_dt.isoformat(),
        "checked_tickers": wl.tickers,
        "flagged_tickers": [r.ticker for r in flagged],
        "results": [r.to_dict() for r in results],
    }
    json_path = ctx.output_root / f"{safe_slug(wl.name)}.json"
    write_json(json_path, payload)

    # Write dashboard data (merge with existing wally.json so multiple watchlists accumulate)
    _dash_path = Path("docs/data/wally.json")
    try:
        _dash_path.parent.mkdir(parents=True, exist_ok=True)
        existing = json.loads(_dash_path.read_text()) if _dash_path.exists() else {}
        watchlists = existing.get("watchlists", {})
        watchlists[wl.name] = {
            "run_timestamp": ctx.run_dt.isoformat(),
            "total": len(results),
            "flagged_count": len(flagged),
            "flagged": [
                {
                    "ticker": r.ticker,
                    "company_name": r.company_name,
                    "current_price": r.current_price,
                    "low_52w": r.low_52w,
                    "high_52w": r.high_52w,
                    "distance_to_low_pct": r.distance_to_low_pct,
                    "below_high_pct": r.below_high_pct,
                }
                for r in flagged
            ],
        }
        _dash_path.write_text(
            json.dumps({"last_run": ctx.run_dt.isoformat(), "watchlists": watchlists}, indent=2),
            encoding="utf-8",
        )
        print(f"[wally] Dashboard data written → {_dash_path}", flush=True)
    except Exception as _e:
        print(f"[wally] Dashboard write failed: {_e}", flush=True)

    # Send individual email if requested
    if send_individual_email:
        settings = load_email_settings()
        subject = f"Wally the Watcher — {wl.name} — {ctx.run_dt.date().isoformat()}"
        png_cids = {r.ticker: f"chart_{r.ticker.lower().replace('.', '_')}"
                    for r in flagged
                    if any(cid == f"chart_{r.ticker.lower().replace('.', '_')}" for cid, _ in inline_images)}
        html = build_html(wl.name, ctx.run_dt.date().isoformat(), results, flagged, chart_notes, inline_pngs=png_cids)
        text = (
            f"Wally the Watcher report\nWatchlist: {wl.name}\nChecked: {len(results)}\nFlagged: {len(flagged)}\n"
            f"Flagged tickers: {', '.join([r.ticker for r in flagged]) if flagged else 'None'}"
        )
        send_email(settings, subject, text, html, attachments, inline_images=inline_images if inline_images else None)

    return WatchlistProcessResult(
        watchlist_name=wl.name,
        run_date=ctx.run_dt.date().isoformat(),
        results=results,
        flagged=flagged,
        attachments=attachments,
        chart_notes=chart_notes,
        inline_images=inline_images,
    )


def _process_watchlists_combined(watchlist_paths: list[str], force: bool = False) -> None:
    """Process multiple watchlists and send one combined email."""
    print(f"[wally] Processing {len(watchlist_paths)} watchlists with combined email...", flush=True)
    
    all_results = []
    all_attachments = []
    all_inline_images = []
    
    for path in watchlist_paths:
        is_tii75 = (path == TII75_WATCHLIST)
        result = _process_watchlist(path, force=force, send_individual_email=False, is_tii75=is_tii75)
        all_results.append(result)
        all_attachments.extend(result.attachments)
        all_inline_images.extend(result.inline_images)
    
    # Build combined email
    if not all_results:
        print("[wally] No watchlists processed", flush=True)
        return
    
    settings = load_email_settings()
    run_date = all_results[0].run_date
    
    # Prepare data for combined HTML
    watchlist_data = []
    for result in all_results:
        png_cids = {r.ticker: f"chart_{r.ticker.lower().replace('.', '_')}"
                    for r in result.flagged
                    if any(cid == f"chart_{r.ticker.lower().replace('.', '_')}" for cid, _ in result.inline_images)}
        watchlist_data.append({
            "watchlist_name": result.watchlist_name,
            "run_date": result.run_date,
            "results": result.results,
            "flagged": result.flagged,
            "chart_notes": result.chart_notes,
            "inline_pngs": png_cids,
        })
    
    html = build_combined_html(watchlist_data)
    
    # Build text summary
    total_checked = sum(len(r.results) for r in all_results)
    total_flagged = sum(len(r.flagged) for r in all_results)
    watchlist_names = [r.watchlist_name for r in all_results]
    
    text = (
        f"Wally the Watcher — Combined Report\n"
        f"Run date: {run_date}\n"
        f"Watchlists: {', '.join(watchlist_names)}\n"
        f"Total checked: {total_checked}\n"
        f"Total flagged: {total_flagged}\n"
        f"\nFlagged tickers:\n"
    )
    for result in all_results:
        if result.flagged:
            text += f"\n{result.watchlist_name}:\n"
            text += f"  {', '.join([r.ticker for r in result.flagged])}\n"
        else:
            text += f"\n{result.watchlist_name}: None\n"
    
    subject = f"Wally the Watcher — Combined Report — {run_date}"
    send_email(settings, subject, text, html, all_attachments, inline_images=all_inline_images if all_inline_images else None)
    print(f"[wally] Combined email sent for {len(all_results)} watchlist(s)", flush=True)


def _run_standard(force: bool = False, combined_email: bool = False) -> None:
    if combined_email:
        _process_watchlists_combined(STANDARD_WATCHLISTS, force=force)
    else:
        for path in STANDARD_WATCHLISTS:
            _process_watchlist(path, force=force, send_individual_email=True)


def _run_tii75(force: bool = False, combined_email: bool = False) -> None:
    today = dt.datetime.now(dt.timezone.utc).date()
    if force:
        print("[wally] TII75 forced run enabled: bypassing schedule gate", flush=True)
    gate_result = should_run_tii75(today, force=force)
    if gate_result:
        _process_watchlist(TII75_WATCHLIST, force=force, send_individual_email=not combined_email, is_tii75=True)
    else:
        print("[wally] TII75 skipped: fortnightly gate not satisfied (use --force to override)", flush=True)


def _run_all_combined(force: bool = False) -> None:
    """Run all watchlists (standard + TII75) with combined email."""
    today = dt.datetime.now(dt.timezone.utc).date()
    watchlists_to_run = list(STANDARD_WATCHLISTS)

    if force:
        print("[wally] TII75 forced run enabled: bypassing schedule gate", flush=True)
    if should_run_tii75(today, force=force):
        watchlists_to_run.append(TII75_WATCHLIST)
    else:
        print("[wally] TII75 skipped: fortnightly gate not satisfied (use --force to include it)", flush=True)

    _process_watchlists_combined(watchlists_to_run, force=force)


def _debug_ticker(ticker: str) -> None:
    """Load TII75 watchlist, check ticker presence, fetch data, and print screening result."""
    wl = load_watchlist(TII75_WATCHLIST, validate_tii75=True)
    ticker_upper = ticker.strip().upper()
    if ticker_upper in wl.tickers:
        print(f"[wally] {ticker_upper} is present in TII75 watchlist", flush=True)
    else:
        print(f"[wally] WARNING: {ticker_upper} is NOT in TII75 watchlist", flush=True)

    snap = fetch_price_snapshot(ticker_upper)
    if not snap:
        print(f"[wally] No market data returned for {ticker_upper}", flush=True)
        return

    row = screen_snapshot(snap)
    _log_screen_result(row)


def main() -> None:
    parser = argparse.ArgumentParser(description="Wally watchlist low-screen agent")
    parser.add_argument("--watchlist", help="Path to one watchlist YAML")
    parser.add_argument("--all-standard-watchlists", action="store_true", help="Run TII/JM/Aussie Tech")
    parser.add_argument("--tii75", action="store_true", help="Run TII75 watchlist with fortnightly gate")
    parser.add_argument("--force", action="store_true", help="Force run (bypass fortnightly gate)")
    parser.add_argument("--combined-email", action="store_true", help="Send one combined email for all watchlists")
    parser.add_argument("--all-combined", action="store_true", help="Run all watchlists (standard + TII75) with combined email")
    parser.add_argument("--debug-ticker", metavar="TICKER", help="Debug a single ticker: load TII75, fetch data, print result, no email")

    args = parser.parse_args()

    if args.debug_ticker:
        _debug_ticker(args.debug_ticker)
        return

    if args.watchlist:
        _process_watchlist(args.watchlist, force=args.force, send_individual_email=True)
        return

    if args.all_combined:
        _run_all_combined(force=args.force)
        return

    if args.all_standard_watchlists:
        _run_standard(force=args.force, combined_email=args.combined_email)

    if args.tii75:
        _run_tii75(force=args.force, combined_email=args.combined_email)

    if not args.watchlist and not args.all_standard_watchlists and not args.tii75 and not args.all_combined:
        parser.error("Choose --watchlist, --all-standard-watchlists, --tii75, --all-combined, or --debug-ticker TICKER")


if __name__ == "__main__":
    main()
