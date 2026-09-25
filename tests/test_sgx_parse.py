"""Unit tests for sgx_agent._parse_analysis_json and its helpers.

The parser has four progressive stages, each preserving the model's
content exactly. This file pins the failure modes we've actually hit
in production (each named after the run that surfaced it) plus the
happy paths.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sgx_agent  # noqa: E402


class TestStraightParse:
    def test_bare_json_object(self):
        text = '{"period": "FY26", "metrics": {}, "summary": "ok"}'
        parsed = sgx_agent._parse_analysis_json(text)
        assert parsed["period"] == "FY26"

    def test_json_wrapped_in_code_fence(self):
        text = '```json\n{"period": "FY26"}\n```'
        parsed = sgx_agent._parse_analysis_json(text)
        assert parsed["period"] == "FY26"

    def test_json_with_leading_prose(self):
        """Some responses have a preamble sentence -- the outermost-
        {...} span fallback should still find the JSON."""
        text = 'Here is the analysis:\n{"period": "FY26"}'
        parsed = sgx_agent._parse_analysis_json(text)
        assert parsed["period"] == "FY26"


class TestSanitiseStringBodies:
    def test_raw_newline_in_string_recovers(self):
        """Bob's SPZ case: a literal newline inside a JSON string
        breaks strict json.loads. The sanitiser re-encodes it as \\n."""
        # Note the literal newline inside the summary string.
        text = '{"period": "FY26", "summary": "line one\nline two"}'
        parsed = sgx_agent._parse_analysis_json(text)
        assert parsed is not None
        assert "line one" in parsed["summary"]
        assert "line two" in parsed["summary"]


class TestExtractFullAnalysisRaw:
    """The C07 failure mode -- model wrote a long full_analysis
    markdown blob with unescaped inner quotes. The 4th-stage fallback
    extracts full_analysis as raw text so the inner quotes never have
    to be JSON-escaped."""

    def test_unescaped_inner_quotes_in_full_analysis(self):
        text = (
            '{'
            '"period": "1H26",'
            '"period_type": "half_year",'
            '"currency": "USD",'
            '"metrics": {"revenue": {"value": "US$9,991m", "change_pct": "-8%"}},'
            '"summary": "Miss.",'
            '"full_analysis": "The announcement notes '
            '"impairment of non-depreciable intangibles" but gives '
            'no detail. Bottom line: hold."'
            '}'
        )
        parsed = sgx_agent._parse_analysis_json(text)
        assert parsed is not None
        # Header fields still parse.
        assert parsed["period"] == "1H26"
        assert parsed["currency"] == "USD"
        assert parsed["metrics"]["revenue"]["change_pct"] == "-8%"
        assert parsed["summary"] == "Miss."
        # Full analysis is preserved raw, quotes and all.
        assert "impairment of non-depreciable intangibles" in parsed["full_analysis"]
        assert "Bottom line: hold." in parsed["full_analysis"]

    def test_multi_paragraph_markdown_with_inner_quotes(self):
        """The kind of thing Slinger asks for: 800-1500 words of real
        analysis, headings, bullets, quoted phrases from the report."""
        body = (
            "## Executive summary\n\n"
            "- Underlying NPAT dropped 11% to US$473m\n"
            "- Astra's contribution fell on \"weak coal prices\" per the release\n\n"
            "## Bottom line\n\n"
            "Hold. Management calls this \"a cyclical trough\" but did not "
            "quantify recovery timing."
        )
        text = (
            '{"period": "1H26", "metrics": {}, "summary": "cyclical", '
            f'"full_analysis": "{body}"'
            '}'
        )
        parsed = sgx_agent._parse_analysis_json(text)
        assert parsed is not None
        assert parsed["period"] == "1H26"
        assert "weak coal prices" in parsed["full_analysis"]
        assert "cyclical trough" in parsed["full_analysis"]

    def test_extract_helper_returns_none_when_no_full_analysis_key(self):
        text = '{"period": "FY26"}'
        assert sgx_agent._extract_full_analysis_raw(text) is None


class TestGiveUp:
    def test_completely_broken_returns_none(self):
        assert sgx_agent._parse_analysis_json("not json at all") is None

    def test_llm_sentinel_returns_none(self):
        from shared.pdf_llm import LLM_FAILED, LLM_SKIPPED
        assert sgx_agent._parse_analysis_json(LLM_FAILED) is None
        assert sgx_agent._parse_analysis_json(LLM_SKIPPED) is None
