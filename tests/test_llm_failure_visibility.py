# tests/test_llm_failure_visibility.py
#
# Verifies the fix for Bob's silent-LLM-failure bug:
#
#   - MAX_LLM_CALLS_PER_RUN cap reached -> "skipped" placeholder, clearly
#     labelled as skipped/expected, never confused with a real analysis.
#   - A genuine Claude API exception -> retried once, then surfaced as a
#     loud ⚠️ failure (ERROR-level log + a digest block flagged for manual
#     review), never silently substituted with placeholder text that reads
#     like a completed analysis.
#   - The two cases must never produce the same output.

import sys
import types
from pathlib import Path
from unittest import mock

# ---------------------------------------------------------------------------
# Stub heavy optional dependencies so agent.py can be imported without them
# actually being installed (matches the pattern used by the other test_bob_*
# modules in this directory).
# ---------------------------------------------------------------------------
for _stub in (
    "anthropic", "playwright", "playwright.async_api", "googleapiclient",
    "googleapiclient.discovery", "googleapiclient.http",
    "google", "google.oauth2", "google.oauth2.credentials",
    "google.oauth2.service_account", "google.auth",
    "google.auth.transport", "google.auth.transport.requests",
):
    if _stub not in sys.modules:
        sys.modules[_stub] = types.ModuleType(_stub)

_pw_stub = types.ModuleType("playwright_fetch")
_pw_stub.fetch_pdf_with_playwright = None  # type: ignore[attr-defined]
sys.modules.setdefault("playwright_fetch", _pw_stub)

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent  # noqa: E402


def _fresh_counters() -> dict:
    return {
        "MAX_LLM_CALLS_PER_RUN": 15,
        "llm_calls": 0,
        "MAX_PDFS_PER_RUN": 10,
        "pdfs_downloaded": 0,
    }


# ---------------------------------------------------------------------------
# 1. Cap reached -> LLM_SKIPPED, distinct from a failure
# ---------------------------------------------------------------------------

def test_cap_reached_returns_skipped_without_calling_anthropic():
    counters = _fresh_counters()
    counters["llm_calls"] = counters["MAX_LLM_CALLS_PER_RUN"]  # already at the cap

    with mock.patch("agent._anthropic_client") as mock_client:
        out = agent.llm_chat("system", "user text", counters)

    assert out == agent.LLM_SKIPPED
    mock_client.assert_not_called()
    # Skipping must not itself consume another slot of the budget.
    assert counters["llm_calls"] == counters["MAX_LLM_CALLS_PER_RUN"]


def test_deep_capital_memo_skip_message_is_clearly_not_real_analysis():
    counters = _fresh_counters()
    counters["llm_calls"] = counters["MAX_LLM_CALLS_PER_RUN"]

    memo = agent.deep_capital_memo(
        "NHC", "NHC Entitlement Offer", "some raise text " * 100, counters,
    )

    assert memo["what_happened"] == agent.LLM_SKIP_MESSAGE
    assert "skipped" in memo["what_happened"].lower()
    assert "billing" not in memo["what_happened"].lower()
    assert not agent._is_llm_failure(memo)


# ---------------------------------------------------------------------------
# 2. Genuine API exception -> retried once, then LLM_FAILED + loud flag
# ---------------------------------------------------------------------------

def _make_failing_client(exc: Exception) -> mock.MagicMock:
    client = mock.MagicMock()
    client.messages.create.side_effect = exc
    return client


def test_api_exception_is_retried_once_then_fails_loudly():
    counters = _fresh_counters()
    boom = RuntimeError("Your credit balance is too low to access the Anthropic API.")
    client = _make_failing_client(boom)

    with mock.patch("agent._anthropic_client", return_value=client), \
         mock.patch("agent.time.sleep") as mock_sleep:
        out = agent.llm_chat("system", "user text", counters)

    assert out == agent.LLM_FAILED
    assert client.messages.create.call_count == 2  # original attempt + one retry
    mock_sleep.assert_called_once()
    assert "credit balance" in counters["last_llm_error"]
    # A real failure still counts as one attempted call for the run's budget.
    assert counters["llm_calls"] == 1


def test_api_exception_recovers_on_retry():
    """If the retry succeeds, the caller must get the real response, not a failure."""
    counters = _fresh_counters()
    client = mock.MagicMock()
    ok_response = mock.MagicMock()
    ok_response.content = [mock.MagicMock(text="real analysis text")]
    client.messages.create.side_effect = [RuntimeError("timeout"), ok_response]

    with mock.patch("agent._anthropic_client", return_value=client), \
         mock.patch("agent.time.sleep"):
        out = agent.llm_chat("system", "user text", counters)

    assert out == "real analysis text"
    assert client.messages.create.call_count == 2


def test_deep_capital_memo_failure_is_flagged_for_manual_review():
    counters = _fresh_counters()
    client = _make_failing_client(RuntimeError("credit balance too low"))

    with mock.patch("agent._anthropic_client", return_value=client), \
         mock.patch("agent.time.sleep"):
        memo = agent.deep_capital_memo(
            "CAT", "CAT Entitlement Offer", "some raise text " * 100, counters,
        )

    assert agent._is_llm_failure(memo)
    assert memo["what_happened"].startswith(agent.LLM_FAILURE_FLAG)
    assert "credit balance too low" in memo["what_happened"]
    # Must not be the old vague placeholder that looked like a real (if terse) analysis.
    assert memo["what_happened"] != "LLM could not run (limit/billing)."


def test_digest_block_flags_failed_analysis_in_heading():
    """The rendered HIGH IMPACT block heading must visibly flag a failure —
    it must not look identical to a successful '(analysed via PDF)' block."""
    counters = _fresh_counters()
    client = _make_failing_client(RuntimeError("Your credit balance is too low."))

    with mock.patch("agent._anthropic_client", return_value=client), \
         mock.patch("agent.time.sleep"):
        analysis = agent.deep_results_analysis(
            "CAT", "official report text " * 300, "deck text " * 300, counters,
        )

    assert agent._is_llm_failure(analysis), (
        "deep_results_analysis's executive_summary is a list — "
        "_is_llm_failure must look inside list values, not just plain strings"
    )

    block = agent.build_high_impact_block(
        ticker="CAT",
        heading="Results (HY/FY)",
        memo=analysis,
        url="https://example.com/cat-results",
        analysed_via_pdf=False,
        kind="results",
        failed=agent._is_llm_failure(analysis),
    )
    heading_line = block.splitlines()[0]

    assert "⚠️" in heading_line, f"failure flag must be in the heading line itself, got: {heading_line!r}"
    assert "manual review needed" in heading_line
    assert "(analysed via PDF)" not in block

    # A successful block (failed=False) must NOT carry the same heading flag,
    # even though the word "Results" appears in both.
    ok_block = agent.build_high_impact_block(
        ticker="CAT", heading="Results (HY/FY)", memo={"executive_summary": ["Solid FY."]},
        url="https://example.com/cat-results", analysed_via_pdf=True, kind="results",
        failed=False,
    )
    assert "⚠️" not in ok_block.splitlines()[0]


def test_skip_and_failure_never_produce_identical_output():
    """The whole point of the fix: a cap-skip and a genuine failure must be
    distinguishable, both in the raw sentinel and in the rendered memo."""
    skip_counters = _fresh_counters()
    skip_counters["llm_calls"] = skip_counters["MAX_LLM_CALLS_PER_RUN"]
    skip_memo = agent.deep_trading_update_memo(
        "BHP", "BHP Trading Update", "guidance text " * 100, skip_counters,
    )

    fail_counters = _fresh_counters()
    client = _make_failing_client(RuntimeError("rate limited"))
    with mock.patch("agent._anthropic_client", return_value=client), \
         mock.patch("agent.time.sleep"):
        fail_memo = agent.deep_trading_update_memo(
            "BHP", "BHP Trading Update", "guidance text " * 100, fail_counters,
        )

    assert skip_memo != fail_memo
    assert not agent._is_llm_failure(skip_memo)
    assert agent._is_llm_failure(fail_memo)
