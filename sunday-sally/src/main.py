from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .alert_engine import classify_alert
from .chatgpt_handoff_builder import build_handoff_payload, save_handoff_payload
from .claude_analyst import analyse_company
from .document_fetcher import fetch_asx_announcements, save_source_documents
from .email_sender import send_summary_email
from .historical_multiple_analyzer import percentile_bucket, summarize_history, valuation_ratio
from .memo_generator import build_memo_text, save_memo
from .news_context_fetcher import fetch_news_context
from .pathing import repo_root, resolve_output_root
from .portfolio_loader import load_portfolio
from .price_monitor import fetch_price_data
from .run_logger import write_run_log
from .valuation_engine import fetch_valuation_snapshot
from .weekly_scheduler import run_window_info

# Value chart builder lives in the wally package at repo root
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
_build_value_chart = None
_drive_upload = None
_VALUE_CHART_AVAILABLE = False
try:
    from wally.value_chart_builder import build_value_chart as _build_value_chart
    _VALUE_CHART_AVAILABLE = True
except ImportError:
    pass
try:
    from wally.drive_upload import upload_or_replace_xlsx as _drive_upload
except ImportError:
    pass


def _load_settings() -> dict:
    cfg_path = os.environ.get("SALLY_CONFIG_PATH", "config/settings.yaml")
    return yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8")) or {}


def _run_folder(base_output_root: str, timezone: str) -> Path:
    now_local = dt.datetime.now(ZoneInfo(timezone))
    # resolve_output_root anchors to sunday-sally/ regardless of CWD
    return (
        resolve_output_root(base_output_root)
        / str(now_local.year)
        / f"{now_local.date().isoformat()} Weekly Review"
    )


def _process_company(company, settings, run_folder, tz, near_high_max, thresholds) -> dict | None:
    """
    Run the full valuation pipeline for one company.
    Returns a summary dict if flagged, else None.
    One bad ticker never kills the whole run — wrap calls in try/except in main().
    """
    price = fetch_price_data(company.exchange_ticker, company.ticker)
    if not price:
        return None

    val = fetch_valuation_snapshot(company.exchange_ticker)
    hist = summarize_history(company.exchange_ticker, val.trailing_pe, val.ev_to_ebitda)

    pe_3_ratio = valuation_ratio(val.trailing_pe, hist.pe_3y_avg)
    pe_5_ratio = valuation_ratio(val.trailing_pe, hist.pe_5y_avg)
    pe_10_ratio = valuation_ratio(val.trailing_pe, hist.pe_10y_avg)

    evidence_strength = 0.6 if (val.fcf_yield and val.fcf_yield > 0.03) else 0.35
    alert = classify_alert(
        distance_to_high=price.distance_to_high,
        threshold=near_high_max,
        pe_ratio_3y=pe_3_ratio,
        pe_ratio_5y=pe_5_ratio,
        pe_ratio_10y=pe_10_ratio,
        valuation_percentile=hist.valuation_percentile,
        evidence_strength=evidence_strength,
        review_ratio=float(thresholds.get("review_ratio", 1.15)),
        deep_ratio=float(thresholds.get("deep_review_ratio", 1.35)),
    )

    if not alert.triggered:
        return None

    announcements = fetch_asx_announcements(company.ticker)
    news = fetch_news_context(company.exchange_ticker)
    save_source_documents(company.ticker, run_folder, announcements)

    summary = {
        "company_name": price.company_name,
        "ticker": company.ticker,
        "review_date": dt.datetime.now(ZoneInfo(tz)).date().isoformat(),
        "current_price": price.current_price,
        "high_52w": price.high_52w,
        "distance_to_high_pct": round(price.distance_to_high * 100, 2),
        "market_cap": price.market_cap,
        "trailing_pe": val.trailing_pe,
        "forward_pe": val.forward_pe,
        "ev_ebitda": val.ev_to_ebitda,
        "price_to_sales": val.price_to_sales,
        "fcf_yield": val.fcf_yield,
        "dividend_yield": val.dividend_yield,
        "pe_3y_avg": hist.pe_3y_avg,
        "pe_5y_avg": hist.pe_5y_avg,
        "pe_10y_avg": hist.pe_10y_avg,
        "valuation_percentile": hist.valuation_percentile,
        "valuation_percentile_bucket": percentile_bucket(hist.valuation_percentile),
        "alert_tier": alert.tier,
        "sally_verdict": (
            "Trim candidate"
            if alert.tier == "Tier 3: Deep Review"
            else "Hold but stop adding"
            if alert.tier == "Tier 2: Review"
            else "Watch only"
        ),
    }

    handoff = build_handoff_payload(
        company={"ticker": company.ticker, "company_name": price.company_name},
        valuation=summary,
        history={"notes": hist.notes},
        announcements=announcements,
        news=news,
        run_date=summary["review_date"],
    )

    ticker_folder = run_folder / company.ticker
    save_handoff_payload(ticker_folder / "handoff_payload.json", handoff)

    critical_rows = [
        {
            "issue": "Valuation stretch",
            "bull_case": "Quality rerating",
            "bear_case": "Narrative-driven",
            "evidence": "; ".join(alert.reasons),
            "sally_judgment": summary["sally_verdict"],
        },
        {
            "issue": "Cash conversion",
            "bull_case": "FCF improving",
            "bear_case": "Earnings ahead of cash",
            "evidence": f"FCF yield={val.fcf_yield}",
            "sally_judgment": "Verify statutory cash flow",
        },
    ]
    history_rows = [
        {
            "reporting_period": "current",
            "pe": val.trailing_pe,
            "ev_ebitda": val.ev_to_ebitda,
            "notes": "; ".join(hist.notes),
        }
    ]

    claude_analysis = analyse_company(
        ticker=company.ticker,
        company_name=price.company_name,
        summary=summary,
        reasons=alert.reasons,
        news=[n.get("headline", "") for n in news] if isinstance(news, list) else [],
    )

    # Build the value chart workbook + PNG (requires a valuations/<ticker>.yaml config).
    if _VALUE_CHART_AVAILABLE:
        try:
            _build_value_chart(
                company.exchange_ticker,
                output_path=str(ticker_folder / "value_chart.xlsx"),
                save_png=True,
            )
            print(f"[sally] value chart built for {company.ticker}")
        except FileNotFoundError:
            print(f"[sally] WARNING: no valuations config for {company.ticker} — add valuations/{company.ticker.lower()}_ax.yaml to get a chart")
        except Exception as exc:
            print(f"[sally] value chart build failed for {company.ticker}: {exc}")

    memo = build_memo_text(
        company={"ticker": company.ticker, "company_name": price.company_name},
        summary=summary,
        reasons=alert.reasons,
        doubts=hist.notes,
        decision_framing=summary["sally_verdict"],
        claude_analysis=claude_analysis,
    )
    save_memo(ticker_folder / "memo.md", memo)

    return summary


def main() -> None:
    # ── Startup diagnostics ───────────────────────────────────────────────────
    print("[sally] Starting up...")
    print(f"[sally] Value chart builder available: {_VALUE_CHART_AVAILABLE}")
    print(f"[sally] Drive uploader available: {_drive_upload is not None}")
    _oauth_ok = all([
        os.environ.get("GDRIVE_CLIENT_ID", "").strip(),
        os.environ.get("GDRIVE_CLIENT_SECRET", "").strip(),
        os.environ.get("GDRIVE_REFRESH_TOKEN", "").strip(),
    ])
    _sa_ok = bool(os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip())
    _gfolder_ok = bool(os.environ.get("GDRIVE_FOLDER_ID", "").strip())
    if _oauth_ok:
        print("[sally] Drive auth: OAuth2 user credentials (GDRIVE_CLIENT_ID/SECRET/REFRESH_TOKEN) ✓")
    elif _sa_ok:
        print("[sally] Drive auth: service account (WARNING — will fail on personal Gmail Drive — add OAuth2 secrets instead)")
    else:
        print("[sally] Drive auth: NO credentials set — uploads will be skipped")
    print(f"[sally] GDRIVE_FOLDER_ID set: {_gfolder_ok}")
    _email_ok_pre = all([
        os.environ.get("EMAIL_FROM") or os.environ.get("EMAIL_USER"),
        os.environ.get("EMAIL_TO"),
        os.environ.get("SMTP_PASS") or os.environ.get("EMAIL_APP_PASSWORD"),
    ])
    print(f"[sally] Email credentials present: {_email_ok_pre}")
    if not _email_ok_pre:
        missing = [k for k in ("EMAIL_FROM", "EMAIL_TO", "SMTP_PASS") if not os.environ.get(k)]
        print(f"[sally] WARNING: missing env vars → {missing}")

    settings = _load_settings()
    tz = settings.get("timezone", "Asia/Singapore")
    thresholds = settings.get("thresholds", {})
    near_high_max = float(thresholds.get("near_high_distance_max", 0.05))

    run_folder = _run_folder(
        settings.get("outputs", {}).get("root", "data/outputs"), tz
    )
    run_folder.mkdir(parents=True, exist_ok=True)

    tickers_file = repo_root() / "tickers.yaml"
    portfolio = load_portfolio(
        source_file=str(tickers_file),
        source_key="asx",
        exchange_suffix=".AX",
    )

    flagged_rows: list[dict] = []
    skipped_tickers: list[str] = []

    for company in portfolio:
        try:
            result = _process_company(
                company, settings, run_folder, tz, near_high_max, thresholds
            )
            if result:
                flagged_rows.append(result)
        except Exception as exc:
            print(f"[main] ERROR processing {company.ticker}: {exc}")
            skipped_tickers.append(company.ticker)

    now_local = dt.datetime.now(ZoneInfo(tz))

    # -------------------------------------------------------------------------
    # Build email body
    # -------------------------------------------------------------------------
    if flagged_rows:
        lines = [
            f"Selling Sally Weekly Review — {now_local.date().isoformat()}",
            f"{len(flagged_rows)} company/companies flagged this week.",
            "",
            "Flagged companies:",
        ]
        for row in flagged_rows:
            lines.append(
                f"  {row['ticker']} ({row['alert_tier']}) — "
                f"{row['distance_to_high_pct']}% below 52w high — "
                f"{row['sally_verdict']}"
            )
        lines += [
            "",
            "Valuation workbooks and review memos are attached.",
            "Each .xlsx has six sheets: Summary, Historical, Comparison,",
            "Implied Expectations, Critical Review, Decision Framework.",
        ]
    else:
        lines = [
            f"Selling Sally Weekly Review — {now_local.date().isoformat()}",
            "No valuation stretch alerts this week. Nothing near 52-week highs.",
        ]

    if skipped_tickers:
        lines += ["", f"Tickers skipped due to data errors: {', '.join(skipped_tickers)}"]

    # -------------------------------------------------------------------------
    # Build attachments and HTML email body with inline PNG charts
    # -------------------------------------------------------------------------
    attachments: list[tuple[Path, str]] = []
    inline_images: list[tuple[str, Path]] = []   # (content_id, png_path)

    summary_email_path = run_folder / "summary_email.md"
    summary_email_path.write_text("\n".join(lines), encoding="utf-8")
    attachments.append((summary_email_path, "summary.md"))

    for row in flagged_rows:
        ticker = row["ticker"]
        ticker_folder = run_folder / ticker

        chart = ticker_folder / "value_chart.xlsx"
        if chart.exists():
            attachments.append((chart, f"{ticker}_value_chart.xlsx"))

        png = ticker_folder / "value_chart.png"
        if png.exists():
            cid = f"chart_{ticker.lower()}"
            inline_images.append((cid, png))

        memo = ticker_folder / "memo.md"
        if memo.exists():
            attachments.append((memo, f"{ticker}_memo.md"))

    # Build HTML body — summary table + inline chart images for each flagged ticker
    html_parts = [
        f"<h2>Selling Sally Weekly Review — {now_local.date().isoformat()}</h2>",
    ]
    if flagged_rows:
        html_parts.append(f"<p><strong>{len(flagged_rows)} company/companies flagged this week.</strong></p>")
        html_parts.append(
            "<table border='1' cellspacing='0' cellpadding='6' style='border-collapse:collapse'>"
            "<tr style='background:#1F2D4E;color:white'>"
            "<th>Ticker</th><th>Alert Tier</th><th>% Below 52w High</th><th>Verdict</th></tr>"
        )
        for row in flagged_rows:
            html_parts.append(
                f"<tr><td><b>{row['ticker']}</b></td><td>{row['alert_tier']}</td>"
                f"<td>{row['distance_to_high_pct']}%</td><td>{row['sally_verdict']}</td></tr>"
            )
        html_parts.append("</table>")
        for row in flagged_rows:
            ticker = row["ticker"]
            cid = f"chart_{ticker.lower()}"
            if any(c == cid for c, _ in inline_images):
                html_parts.append(
                    f"<h3>{ticker} — Value Chart</h3>"
                    f"<img src='cid:{cid}' style='max-width:100%;border:1px solid #ccc'><br>"
                )
        html_parts.append("<p>Value chart workbooks and memos are attached.</p>")
    else:
        html_parts.append("<p>No valuation stretch alerts this week. Nothing near 52-week highs.</p>")
    if skipped_tickers:
        html_parts.append(f"<p><em>Tickers skipped due to data errors: {', '.join(skipped_tickers)}</em></p>")
    body_html = "\n".join(html_parts)

    flag_count = len(flagged_rows)
    subject = (
        f"Selling Sally — {now_local.date().isoformat()} — "
        + (f"{flag_count} flagged" if flag_count else "All clear")
    )

    email_ok = send_summary_email(
        subject=subject,
        body_text="\n".join(lines),
        attachments=attachments,
        inline_images=inline_images if inline_images else None,
        body_html=body_html,
    )

    # Upload value chart xlsx files to Google Drive (YYMMDD-TICKER.xlsx naming)
    drive_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
    drive_uploads: list[str] = []
    if drive_folder_id and _drive_upload is not None:
        for row in flagged_rows:
            ticker = row["ticker"]
            chart = run_folder / ticker / "value_chart.xlsx"
            if chart.exists():
                drive_name = f"{now_local.strftime('%y%m%d')}-{ticker}.xlsx"
                try:
                    url = _drive_upload(chart, drive_name, folder_id=drive_folder_id)
                    drive_uploads.append(drive_name)
                    print(f"[sally] Drive → {drive_name}: {url}")
                except Exception as exc:
                    print(f"[sally] Drive upload failed for {ticker}: {exc}")

    run_log = {
        "job_name": settings.get("job_name", "sunday_sally_weekly_review"),
        "schedule": settings.get("schedule", {}),
        "run_window": run_window_info(tz),
        "portfolio_size": len(portfolio),
        "flagged_count": flag_count,
        "flagged_tickers": [r["ticker"] for r in flagged_rows],
        "skipped_tickers": skipped_tickers,
        "attachments_sent": [name for _, name in attachments],
        "outputs_root": str(run_folder),
        "email_sent": email_ok,
        "drive_uploads": drive_uploads,
    }
    write_run_log(run_folder / "run_log.json", run_log)

    print(json.dumps(run_log, indent=2))


if __name__ == "__main__":
    main()
