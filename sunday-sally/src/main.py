from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .alert_engine import classify_alert
from .chatgpt_handoff_builder import build_handoff_payload, save_handoff_payload
from .document_fetcher import fetch_asx_announcements, save_source_documents
from .email_sender import send_summary_email
from .historical_multiple_analyzer import percentile_bucket, summarize_history, valuation_ratio
from .memo_generator import build_memo_text, save_memo
from .news_context_fetcher import fetch_news_context
from .portfolio_loader import load_portfolio
from .price_monitor import fetch_price_data
from .run_logger import write_run_log
from .spreadsheet_request_builder import build_valuation_workbook
from .valuation_engine import fetch_valuation_snapshot
from .weekly_scheduler import run_window_info

# ---------------------------------------------------------------------------
# Telegram — imported here so the rest of the module works even if the
# telegram_sender module is not yet on the path (it lives at repo root).
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
try:
    from telegram_sender import send_message, send_run_summary, send_error_alert
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False

    def send_message(text, **kw):  # type: ignore[misc]
        pass

    def send_run_summary(summary_text, attachments=None, **kw):  # type: ignore[misc]
        pass

    def send_error_alert(agent_name, error_summary, **kw):  # type: ignore[misc]
        pass


def _load_settings() -> dict:
    cfg_path = os.environ.get("SALLY_CONFIG_PATH", "sunday-sally/config/settings.yaml")
    return yaml.safe_load(Path(cfg_path).read_text(encoding="utf-8")) or {}


def _run_folder(base_output_root: str, timezone: str) -> Path:
    now_local = dt.datetime.now(ZoneInfo(timezone))
    return Path(base_output_root) / str(now_local.year) / f"{now_local.date().isoformat()} Weekly Review"


def main() -> None:
    settings = _load_settings()
    tz = settings.get("timezone", "Asia/Singapore")
    thresholds = settings.get("thresholds", {})
    near_high_max = float(thresholds.get("near_high_distance_max", 0.05))

    run_folder = _run_folder(
        settings.get("outputs", {}).get("root", "sunday-sally/data/outputs"), tz
    )
    run_folder.mkdir(parents=True, exist_ok=True)

    TICKERS_FILE = _REPO_ROOT / "tickers.yaml"

    portfolio = load_portfolio(
        source_file=str(TICKERS_FILE),
        source_key="asx",
        exchange_suffix=".AX",
    )

    # --- Notify start ---
    now_local = dt.datetime.now(ZoneInfo(tz))
    if TELEGRAM_AVAILABLE:
        send_message(
            f"<b>📋 Sunday Sally started</b>\n"
            f"Date: {now_local.date().isoformat()}\n"
            f"Portfolio size: {len(portfolio)} companies"
        )

    flagged_rows = []
    output_files: list[str] = []   # collect files to send via Telegram

    try:
        for company in portfolio:
            price = fetch_price_data(company.exchange_ticker, company.ticker)
            if not price:
                continue

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
                continue

            announcements = fetch_asx_announcements(company.ticker)
            news = fetch_news_context(company.exchange_ticker)
            save_source_documents(company.ticker, run_folder, announcements)

            summary = {
                "company_name": price.company_name,
                "ticker": company.ticker,
                "review_date": now_local.date().isoformat(),
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
                    "Needs deeper manual review"
                    if alert.tier == "Tier 3: Deep Review"
                    else "Full valuation"
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

            xlsx_path = ticker_folder / "valuation_review.xlsx"
            build_valuation_workbook(xlsx_path, summary, history_rows, critical_rows)
            if xlsx_path.exists():
                output_files.append(str(xlsx_path))

            memo = build_memo_text(
                company={"ticker": company.ticker, "company_name": price.company_name},
                summary=summary,
                reasons=alert.reasons,
                doubts=hist.notes,
                decision_framing=(
                    "Hold but stop adding"
                    if alert.tier != "Tier 3: Deep Review"
                    else "Trim candidate"
                ),
            )
            memo_path = ticker_folder / "memo.md"
            save_memo(memo_path, memo)
            if memo_path.exists():
                output_files.append(str(memo_path))

            flagged_rows.append(summary)

        # --- Build summary text ---
        if flagged_rows:
            lines = [
                f"<b>✅ Sunday Sally — {now_local.date().isoformat()}</b>",
                "",
                f"<b>{len(flagged_rows)} company(s) flagged:</b>",
            ]
            for row in flagged_rows:
                tier_emoji = {"Tier 1: Watch": "👀", "Tier 2: Review": "🔍", "Tier 3: Deep Review": "🚨"}.get(
                    row["alert_tier"], "•"
                )
                lines.append(
                    f"{tier_emoji} <b>{row['ticker']}</b> — {row['alert_tier']}\n"
                    f"   Price: ${row['current_price']:.2f} | "
                    f"{row['distance_to_high_pct']}% below 52w high | "
                    f"PE: {row['trailing_pe']}"
                )
            summary_text = "\n".join(lines)
        else:
            summary_text = (
                f"<b>✅ Sunday Sally — {now_local.date().isoformat()}</b>\n\n"
                "No major valuation stretch alerts this week."
            )

        summary_email_path = run_folder / "summary_email.md"
        summary_email_path.write_text(summary_text.replace("<b>", "**").replace("</b>", "**"), encoding="utf-8")

        # --- Telegram delivery ---
        if TELEGRAM_AVAILABLE:
            send_run_summary(
                summary_text=summary_text,
                attachments=output_files,
                agent_name="Sunday Sally",
            )

        # --- Optional email (kept as fallback; works independently of Telegram) ---
        email_body = summary_text.replace("<b>", "").replace("</b>", "")
        email_ok = send_summary_email(
            subject=f"Sunday Sally Weekly Review — {now_local.date().isoformat()}",
            body_text=email_body,
            attachments=[summary_email_path],
        )

        run_log = {
            "job_name": settings.get("job_name", "sunday_sally_weekly_review"),
            "schedule": settings.get("schedule", {}),
            "run_window": run_window_info(tz),
            "portfolio_size": len(portfolio),
            "flagged_count": len(flagged_rows),
            "flagged_tickers": [r["ticker"] for r in flagged_rows],
            "outputs_root": str(run_folder),
            "email_sent": email_ok,
            "telegram_files_sent": len(output_files),
        }
        write_run_log(run_folder / "run_log.json", run_log)
        print(json.dumps(run_log, indent=2))

    except Exception as exc:
        # --- Error path: notify via Telegram then re-raise so GitHub Actions marks the job failed ---
        if TELEGRAM_AVAILABLE:
            send_error_alert("Sunday Sally", str(exc))
        raise


if __name__ == "__main__":
    main()
