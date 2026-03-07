from __future__ import annotations

from pathlib import Path
import json


def build_handoff_payload(company: dict, valuation: dict, history: dict, announcements: list[dict], news: list[dict], run_date: str) -> dict:
    return {
        "company": company,
        "run_date": run_date,
        "valuation_summary": valuation,
        "historical_valuation_summary": history,
        "recent_announcements": announcements,
        "news_context": news,
        "instructions": {
            "tone": "skeptical, evidence-based, challenge bullish narratives",
            "deliverables": ["valuation_review.xlsx", "memo.md", "email_summary_snippet.md"],
            "guardrails": [
                "never auto-sell",
                "52-week highs are review triggers not sell signals",
                "compare narrative vs numbers",
                "check statutory profit and cash conversion",
            ],
        },
    }


def save_handoff_payload(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
