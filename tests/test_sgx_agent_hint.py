"""Unit tests for sgx_agent's results_hint plumbing.

The hint is a free-form paragraph the user passes via dispatch input
(e.g. "Haw Par's main asset is its UOB stake, not Tiger Balm trading")
that must reach the LLM's user prompt on every deep-analysis call in
that run. A regression that silently drops the hint would look like a
clean run but produce a business-agnostic analysis -- exactly the
outcome the hint exists to prevent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sgx_agent  # noqa: E402


def _item():
    return {
        "ticker": "HO2",
        "issuer_name": "HAW PAR CORPORATION LTD.",
        "title": "Financial Statements::HY2026",
    }


class TestResultsHint:
    def test_hint_appears_in_user_prompt(self):
        """The hint text must land in the user prompt _anthropic_call
        receives, so the model actually sees it."""
        captured = {}

        def fake_call(pdf_paths, system, user, counters):
            captured["user"] = user
            return '{"period": "HY2026", "period_type": "half_year", ' \
                   '"metrics": {}, "summary": "", "full_analysis": ""}'

        with patch.object(sgx_agent, "_anthropic_call", side_effect=fake_call):
            sgx_agent._run_results_analysis(
                item=_item(),
                pdf_paths=[Path("fake.pdf")],
                counters={"llm_calls": 0},
                results_hint=(
                    "Haw Par's main asset is its holding in UOB, "
                    "not the Tiger Balm business."
                ),
            )

        assert "captured" and "user" in captured
        assert "Haw Par" in captured["user"]
        assert "UOB" in captured["user"]
        assert "Tiger Balm" in captured["user"]

    def test_empty_hint_leaves_prompt_unchanged(self):
        """No hint -> the user prompt has no hint block at all (no
        stray "User-supplied context:" header hanging over nothing)."""
        captured = {}

        def fake_call(pdf_paths, system, user, counters):
            captured["user"] = user
            return '{"period": "", "period_type": "", "metrics": {}, ' \
                   '"summary": "", "full_analysis": ""}'

        with patch.object(sgx_agent, "_anthropic_call", side_effect=fake_call):
            sgx_agent._run_results_analysis(
                item=_item(),
                pdf_paths=[Path("fake.pdf")],
                counters={"llm_calls": 0},
                results_hint="",
            )

        assert "User-supplied context" not in captured["user"]

    def test_whitespace_only_hint_is_treated_as_empty(self):
        captured = {}

        def fake_call(pdf_paths, system, user, counters):
            captured["user"] = user
            return '{"period": "", "period_type": "", "metrics": {}, ' \
                   '"summary": "", "full_analysis": ""}'

        with patch.object(sgx_agent, "_anthropic_call", side_effect=fake_call):
            sgx_agent._run_results_analysis(
                item=_item(),
                pdf_paths=[Path("fake.pdf")],
                counters={"llm_calls": 0},
                results_hint="   \n\n  ",
            )

        assert "User-supplied context" not in captured["user"]
