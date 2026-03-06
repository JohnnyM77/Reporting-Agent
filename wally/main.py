from __future__ import annotations

import argparse
import datetime as dt
import os
from pathlib import Path

import yaml

from .charts import render_range_chart, render_value_vs_price_chart
from .config import STANDARD_WATCHLISTS, TII75_WATCHLIST, build_run_context, load_email_settings, should_run_tii75
from .data_fetch import fetch_price_history_daily_with_fallback, fetch_price_snapshot, write_price_csv
from .email_report import EmailAsset, build_html, send_email
from .screening import TickerScreenResult, screen_snapshot
from .spreadsheet import generate_asx_value_spreadsheet
from .utils import safe_slug, write_json
from .watchlist_loader import load_watchlist


def _load_valuation_yaml(ticker: str) -> dict | None:
    p = Path("valuations") / f"{ticker.lower().replace('.', '_')}.yaml"
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _build_spreadsheet_config(ticker: str, company_name: str, valuation_cfg: dict, drive_folder_id: str) -> dict:
    eps_map = (valuation_cfg.get("series") or {}).get("eps", {}) or {}
    div_map = (valuation_cfg.get("series") or {}).get("dividend", {}) or {}
    years = sorted(set(int(y) for y in set(eps_map.keys()) | set(div_map.keys())))

    earnings = []
    for y in years:
        ann_date = dt.date(y, 8, 31)
        eps = float(eps_map.get(y, 0.0))
        div = float(div_map.get(y, 0.0))
        earnings.append((ann_date, eps, div, f"FY{y} Full Year", "Loaded from valuations config"))

    return {
        "company_name": company_name,
        "ticker": ticker.replace(".AX", ""),
        "multiple": int(valuation_cfg.get("multiple", 15)),
        "rror": float(valuation_cfg.get("required_return_dividend", 0.04)),
        "pe_smooth_days": 60,
        "earnings": sorted(earnings, key=lambda x: x[0]),
        "drive_folder_id": drive_folder_id,
    }


def _process_watchlist(watchlist_path: str, force: bool = False) -> int:
    wl = load_watchlist(watchlist_path)
    ctx = build_run_context()
    ctx.output_root.mkdir(parents=True, exist_ok=True)

    results: list[TickerScreenResult] = []
    flagged: list[TickerScreenResult] = []
    assets: list[EmailAsset] = []
    chart_notes: dict[str, str] = {}
    spreadsheet_links: dict[str, str] = {}
    inline_chart_ids: dict[str, str] = {}
    drive_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()

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
                assets.append(EmailAsset(path=range_png))
                value_png, note = render_value_vs_price_chart(ticker, ctx.output_root)
                chart_notes[ticker] = note
                if value_png:
                    cid = f"value_{ticker.lower().replace('.', '_')}"
                    inline_chart_ids[ticker] = cid
                    assets.append(EmailAsset(path=value_png, inline=True, content_id=cid))

                valuation_cfg = _load_valuation_yaml(ticker)
                if valuation_cfg:
                    hist_df = fetch_price_history_daily_with_fallback(ticker)
                    if not hist_df.empty:
                        price_csv = ctx.output_root / f"{ticker.lower().replace('.', '_')}_prices.csv"
                        write_price_csv(hist_df, price_csv)
                        sheet_cfg = _build_spreadsheet_config(ticker, row.company_name, valuation_cfg, drive_folder_id)
                        xlsx_out = ctx.output_root / f"{ticker.lower().replace('.', '_')}_value.xlsx"
                        try:
                            link = generate_asx_value_spreadsheet(sheet_cfg, str(price_csv), str(xlsx_out))
                            spreadsheet_links[ticker] = link
                            assets.append(EmailAsset(path=xlsx_out))
                            chart_notes[ticker] = "Generated value spreadsheet and uploaded to Drive"
                        except Exception as e:
                            chart_notes[ticker] = f"Spreadsheet generation/upload failed: {e}"
                    else:
                        chart_notes[ticker] = "No price history available even after fallback windows"
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
        "spreadsheet_links": spreadsheet_links,
    }
    write_json(ctx.output_root / f"{safe_slug(wl.name)}.json", payload)

    settings = load_email_settings()
    subject = f"Wally — {wl.name} — {ctx.run_dt.date().isoformat()}"
    html = build_html(wl.name, ctx.run_dt.date().isoformat(), results, flagged, chart_notes, spreadsheet_links, inline_chart_ids)
    text = (
        f"Wally report\nWatchlist: {wl.name}\nChecked: {len(results)}\nFlagged: {len(flagged)}\n"
        f"Flagged tickers: {', '.join([r.ticker for r in flagged]) if flagged else 'None'}"
    )
    send_email(settings, subject, text, html, assets)
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
