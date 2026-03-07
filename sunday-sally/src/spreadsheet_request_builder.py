from __future__ import annotations

from pathlib import Path

import pandas as pd


def build_valuation_workbook(output_path: Path, summary: dict, history_rows: list[dict], decision_rows: list[dict]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    summary_df = pd.DataFrame([summary])
    history_df = pd.DataFrame(history_rows)
    comparison_df = pd.DataFrame([
        {
            "metric": "trailing_pe",
            "current": summary.get("trailing_pe"),
            "avg_3y": summary.get("pe_3y_avg"),
            "avg_5y": summary.get("pe_5y_avg"),
            "avg_10y": summary.get("pe_10y_avg"),
        }
    ])
    implied_df = pd.DataFrame([
        {
            "earnings_growth_required": "Estimate manually based on hurdle rate",
            "revenue_cagr_required": "Estimate manually",
            "margin_assumption_required": "Estimate manually",
            "assumptions_exceed_history": "Needs analyst judgment",
        }
    ])
    critical_df = pd.DataFrame(decision_rows)
    decision_df = pd.DataFrame([
        {"prompt": "Has the thesis changed?", "response": ""},
        {"prompt": "Has valuation outrun fundamentals?", "response": ""},
        {"prompt": "Is this a justified quality rerating?", "response": ""},
        {"prompt": "Should I stop adding?", "response": ""},
        {"prompt": "Is this a trim candidate?", "response": ""},
    ])

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary Dashboard", index=False)
        history_df.to_excel(writer, sheet_name="Historical Valuation", index=False)
        comparison_df.to_excel(writer, sheet_name="Valuation Comparison", index=False)
        implied_df.to_excel(writer, sheet_name="Implied Expectations", index=False)
        critical_df.to_excel(writer, sheet_name="Critical Review Notes", index=False)
        decision_df.to_excel(writer, sheet_name="Decision Framework", index=False)
