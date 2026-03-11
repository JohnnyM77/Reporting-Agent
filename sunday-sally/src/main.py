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
from .spreadsheet_request_builder import build_valuation_workbook
from .valuation_engine import fetch_valuation_snapshot
from .weekly_scheduler import run_window_info

# Value chart builder lives in the wally package at repo root
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
try:
    from wally.value_chart_builder import build_value_chart as _build_value_chart
    from wally.drive_upload import upload_or_replace_xlsx as _drive_upload
    _VALUE_CHART_AVAILABLE = True
except ImportError:
    _VALUE_CHART_AVAILABLE = False


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

    # Routing: prefer the value chart workbook (with graph) when a valuations
    # config exists; fall back to the basic valuation_review workbook otherwise.
    chart_built = False
    if _VALUE_CHART_AVAILABLE:
        try:
            _build_value_chart(
                company.exchange_ticker,
                output_path=str(ticker_folder / "value_chart.xlsx"),
            )
            chart_built = True
            print(f"[sally] value chart built for {company.ticker}")
        except FileNotFoundError:
            pass  # no valuations config — fall through to basic workbook
        except Exception as exc:
            print(f"[sally] value chart build failed for {company.ticker}: {exc}")

    if not chart_built:
        build_valuation_workbook(
            ticker_folder / "valuation_review.xlsx",
            summary,
            history_rows,
            critical_rows,
            claude_analysis=claude_analysis,
        )

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
            f"Sunday Sally Weekly Review — {now_local.date().isoformat()}",
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
            f"Sunday Sally Weekly Review — {now_local.date().isoformat()}",
            "No valuation stretch alerts this week. Nothing near 52-week highs.",
        ]

    if skipped_tickers:
        lines += ["", f"Tickers skipped due to data errors: {', '.join(skipped_tickers)}"]

    # -------------------------------------------------------------------------
    # Build attachments: summary text + per-ticker xlsx + memo
    # Each file is given a clear display name that includes the ticker so
    # they don't all show up as "memo.md" in the email client.
    # -------------------------------------------------------------------------
    attachments: list[tuple[Path, str]] = []

    summary_email_path = run_folder / "summary_email.md"
    summary_email_path.write_text("\n".join(lines), encoding="utf-8")
    attachments.append((summary_email_path, "summary.md"))

    for row in flagged_rows:
        ticker = row["ticker"]
        ticker_folder = run_folder / ticker

        # Attach the value chart (with graph) if it was built, otherwise
        # fall back to the basic valuation review workbook.
        chart = ticker_folder / "value_chart.xlsx"
        xlsx = ticker_folder / "valuation_review.xlsx"
        if chart.exists():
            attachments.append((chart, f"{ticker}_value_chart.xlsx"))
        elif xlsx.exists():
            attachments.append((xlsx, f"{ticker}_valuation_review.xlsx"))

        memo = ticker_folder / "memo.md"
        if memo.exists():
            attachments.append((memo, f"{ticker}_memo.md"))

    flag_count = len(flagged_rows)
    subject = (
        f"Sunday Sally — {now_local.date().isoformat()} — "
        + (f"{flag_count} flagged" if flag_count else "All clear")
    )

    email_ok = send_summary_email(
        subject=subject,
        body_text="\n".join(lines),
        attachments=attachments,
    )

    # Upload value chart xlsx files to Google Drive (YYMMDD-TICKER naming)
    drive_folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
    drive_uploads: list[str] = []
    if _VALUE_CHART_AVAILABLE and drive_folder_id:
        for row in flagged_rows:
            ticker = row["ticker"]
            chart = run_folder / ticker / "value_chart.xlsx"
            if chart.exists():
                drive_name = f"{now_local.strftime('%y%m%d')}-{ticker}"
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
