from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path

from .charts import render_range_chart, render_value_vs_price_chart
from .config import STANDARD_WATCHLISTS, TII75_WATCHLIST, build_run_context, load_email_settings, should_run_tii75
from .data_fetch import fetch_price_snapshot
from .drive_upload import upload_or_replace_xlsx
from .email_report import build_html, send_email
from .screening import TickerScreenResult, screen_snapshot
from .utils import safe_slug, write_json
from .watchlist_loader import load_watchlist
try:
    from .value_chart_builder import build_value_chart as _build_xlsx
    _XLSX_BUILDER_AVAILABLE = True
except ImportError:
    _XLSX_BUILDER_AVAILABLE = False


def _process_watchlist(watchlist_path: str, force: bool = False) -> int:
    wl = load_watchlist(watchlist_path)
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
            results.append(row)
            if row.flagged:
                flagged.append(row)
                range_png = render_range_chart(row, ctx.output_root)
                attachments.append(range_png)
                value_png, note = render_value_vs_price_chart(ticker, ctx.output_root)
                chart_notes[ticker] = note
                if value_png:
                    attachments.append(value_png)

                # Build xlsx + PNG value chart if a valuations config exists
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
                    except FileNotFoundError:
                        pass  # no valuations/<ticker>.yaml yet — not an error
                    except Exception as xlsx_err:
                        print(f"[wally] xlsx build failed for {ticker}: {xlsx_err}", flush=True)
        except Exception as e:
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

    settings = load_email_settings()
    subject = f"Wally — {wl.name} — {ctx.run_dt.date().isoformat()}"
    png_cids = {r.ticker: f"chart_{r.ticker.lower().replace('.', '_')}"
                for r in flagged
                if any(cid == f"chart_{r.ticker.lower().replace('.', '_')}" for cid, _ in inline_images)}
    html = build_html(wl.name, ctx.run_dt.date().isoformat(), results, flagged, chart_notes, inline_pngs=png_cids)
    text = (
        f"Wally report\nWatchlist: {wl.name}\nChecked: {len(results)}\nFlagged: {len(flagged)}\n"
        f"Flagged tickers: {', '.join([r.ticker for r in flagged]) if flagged else 'None'}"
    )
    send_email(settings, subject, text, html, attachments, inline_images=inline_images if inline_images else None)

    return len(flagged)


def _run_standard(force: bool = False) -> None:
    for path in STANDARD_WATCHLISTS:
        _process_watchlist(path, force=force)


def _run_tii75(force: bool = False) -> None:
    today = dt.datetime.now(dt.timezone.utc).date()
    if should_run_tii75(today, force=force):
        _process_watchlist(TII75_WATCHLIST, force=force)
    else:
        print("[wally] Skipping TII75 this week (fortnightly gate). Use --force to run.", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Wally watchlist low-screen agent")
    parser.add_argument("--watchlist", help="Path to one watchlist YAML")
    parser.add_argument("--all-standard-watchlists", action="store_true", help="Run TII/JM/Aussie Tech")
    parser.add_argument("--tii75", action="store_true", help="Run TII75 watchlist with fortnightly gate")
    parser.add_argument("--force", action="store_true", help="Force run (bypass fortnightly gate)")

    args = parser.parse_args()

    if args.watchlist:
        _process_watchlist(args.watchlist, force=args.force)
        return

    if args.all_standard_watchlists:
        _run_standard(force=args.force)

    if args.tii75:
        _run_tii75(force=args.force)

    if not args.watchlist and not args.all_standard_watchlists and not args.tii75:
        parser.error("Choose --watchlist, --all-standard-watchlists, or --tii75")


if __name__ == "__main__":
    main()
