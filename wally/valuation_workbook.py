"""Valuation workbook builder for tickers without a pre-built valuations YAML config.

Generates a multi-sheet Excel workbook containing:
  - Summary Dashboard (price snapshot + valuation metrics)
  - Historical Valuation
  - Valuation Comparison
  - Implied Expectations
  - Critical Review Notes
  - Decision Framework
  - Claude AI Analysis (optional — present when ANTHROPIC_API_KEY is set)

This is the fallback path used by Wally when a ticker (e.g. a US or Japanese
equity from the TII75 watchlist) has no valuations/<ticker>.yaml config file.
"""
from __future__ import annotations

from pathlib import Path


def build_valuation_workbook(
    output_path: Path,
    summary: dict,
    history_rows: list[dict],
    decision_rows: list[dict],
    claude_analysis: dict | None = None,
) -> None:
    """Write a valuation review workbook to *output_path*.

    Args:
        output_path: Destination .xlsx file path.
        summary: Dict of current snapshot metrics (ticker, company_name,
            current_price, low_52w, high_52w, distance_to_low_pct, trailing_pe, …).
        history_rows: List of dicts with historical valuation data.
        decision_rows: List of dicts describing critical review questions.
        claude_analysis: Optional dict from :func:`wally.claude_analyst.analyse_opportunity`
            with keys verdict, bull_case, bear_case, what_must_be_true, recommendation.
    """
    import pandas as pd

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary_df = pd.DataFrame([summary])
    history_df = (
        pd.DataFrame(history_rows)
        if history_rows
        else pd.DataFrame([{"note": "No historical data available"}])
    )
    comparison_df = pd.DataFrame(
        [
            {
                "metric": "trailing_pe",
                "current": summary.get("trailing_pe"),
                "avg_3y": "Manual lookup required",
                "avg_5y": "Manual lookup required",
                "avg_10y": "Manual lookup required",
            }
        ]
    )
    implied_df = pd.DataFrame(
        [
            {
                "earnings_growth_required": "Estimate manually based on hurdle rate",
                "revenue_cagr_required": "Estimate manually",
                "margin_assumption_required": "Estimate manually",
                "assumptions_exceed_history": "Needs analyst judgment",
            }
        ]
    )
    critical_df = (
        pd.DataFrame(decision_rows)
        if decision_rows
        else pd.DataFrame([{"issue": "Near 52-week low", "notes": "Research required"}])
    )
    decision_df = pd.DataFrame(
        [
            {"prompt": "Is this a cyclical dip or structural decline?", "response": ""},
            {"prompt": "Has the investment thesis changed?", "response": ""},
            {"prompt": "Is the business still compounding at high rates?", "response": ""},
            {
                "prompt": "Is this a justified price fall (earnings miss) or market overreaction?",
                "response": "",
            },
            {"prompt": "At what price would I add conviction?", "response": ""},
        ]
    )

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary Dashboard", index=False)
        history_df.to_excel(writer, sheet_name="Historical Valuation", index=False)
        comparison_df.to_excel(writer, sheet_name="Valuation Comparison", index=False)
        implied_df.to_excel(writer, sheet_name="Implied Expectations", index=False)
        critical_df.to_excel(writer, sheet_name="Critical Review Notes", index=False)
        decision_df.to_excel(writer, sheet_name="Decision Framework", index=False)
        if claude_analysis:
            ai_df = pd.DataFrame(
                [
                    {"section": "Verdict", "analysis": claude_analysis.get("verdict", "")},
                    {"section": "Bull Case", "analysis": claude_analysis.get("bull_case", "")},
                    {"section": "Bear Case", "analysis": claude_analysis.get("bear_case", "")},
                    {
                        "section": "What Must Be True",
                        "analysis": claude_analysis.get("what_must_be_true", ""),
                    },
                    {
                        "section": "Recommendation",
                        "analysis": claude_analysis.get("recommendation", ""),
                    },
                ]
            )
            ai_df.to_excel(writer, sheet_name="Claude AI Analysis", index=False)
