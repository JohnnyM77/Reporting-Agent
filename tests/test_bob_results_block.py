# tests/test_bob_results_block.py
#
# Covers the RESULTS_HY_FY output redesign:
#
#   - one structured LLM call returning the five locked metrics + a short
#     summary + the long-form analysis (which goes to a Google Doc, not the
#     email);
#   - the phone-first email block (table layout, colour-coded changes,
#     "n/a" for anything the model could not find — never a guess);
#   - the three failure states staying visibly distinct: cap-skip, API
#     failure, and JSON parse failure.

import json
import sys
import types
from pathlib import Path
from unittest import mock

# ---------------------------------------------------------------------------
# Stub heavy optional dependencies (same pattern as the other test_bob_* modules).
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

# create_analysis_doc imports this lazily; give the stub something to import.
sys.modules["googleapiclient.http"].MediaInMemoryUpload = mock.MagicMock()

_pw_stub = types.ModuleType("playwright_fetch")
_pw_stub.fetch_pdf_with_playwright = None  # type: ignore[attr-defined]
sys.modules.setdefault("playwright_fetch", _pw_stub)

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _counters(calls_used: int = 0) -> dict:
    return {
        "MAX_LLM_CALLS_PER_RUN": 25,
        "llm_calls": calls_used,
        "MAX_PDFS_PER_RUN": 10,
        "pdfs_downloaded": 0,
    }


def _metric(value, change, **extra):
    out = {"value": value, "change_pct": change}
    out.update(extra)
    return out


def _analysis(**overrides) -> dict:
    base = {
        "ticker": "CAT",
        "period": "FY25",
        "period_type": "full_year",
        "currency": "AUD",
        "metrics": {
            "revenue": _metric("$167.4m", "+19%", basis="reported"),
            "underlying_npat": _metric("$12.1m", "+34%", basis="underlying"),
            "underlying_eps": _metric("5.2c", "+31%", basis="underlying"),
            "dividend_ordinary": _metric("2.0c", "+11%", note="excl special"),
            "operating_cash_flow": _metric("$18.9m", "+22%", basis="reported"),
        },
        "summary": "Beat. Revenue drove it. Cash followed profit. Dividend up. Watch churn.",
        "full_analysis": "## Executive summary\n\n- Revenue up 19%\n",
        "_status": agent.RESULTS_STATUS_OK,
    }
    base.update(overrides)
    return base


def _mock_claude(payload, stop_reason: str = "end_turn") -> mock.MagicMock:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    client = mock.MagicMock()
    response = mock.MagicMock()
    response.content = [mock.MagicMock(text=text)]
    # Explicit stop_reason so counters["last_stop_reason"] tracks the real
    # API's contract — pass "max_tokens" to simulate a truncated response.
    response.stop_reason = stop_reason
    client.messages.create.return_value = response
    return client


# ---------------------------------------------------------------------------
# 1. One structured call per results item
# ---------------------------------------------------------------------------

def test_results_analysis_is_a_single_llm_call():
    """Under the run-call cap, one results item must cost exactly one call."""
    counters = _counters()
    client = _mock_claude(_analysis())

    with mock.patch("agent._anthropic_client", return_value=client):
        analysis = agent.deep_results_analysis(
            "CAT", "official report text " * 300, "deck text " * 300, counters,
        )

    assert client.messages.create.call_count == 1
    assert counters["llm_calls"] == 1
    assert analysis["_status"] == agent.RESULTS_STATUS_OK
    assert analysis["metrics"]["revenue"]["value"] == "$167.4m"
    assert analysis["summary"].startswith("Beat.")


def test_results_analysis_strips_markdown_fences():
    counters = _counters()
    fenced = "```json\n" + json.dumps(_analysis()) + "\n```"
    with mock.patch("agent._anthropic_client", return_value=_mock_claude(fenced)):
        analysis = agent.deep_results_analysis(
            "CAT", "official report text " * 300, "", counters,
        )
    assert analysis["_status"] == agent.RESULTS_STATUS_OK
    assert analysis["period"] == "FY25"


def test_default_call_cap_is_raised_for_reporting_season():
    assert agent.MAX_LLM_CALLS_PER_RUN >= 25


def test_call_cap_is_env_overridable():
    with mock.patch.dict("os.environ", {"MAX_LLM_CALLS": "40"}):
        assert agent._int_env("MAX_LLM_CALLS", 25) == 40
    with mock.patch.dict("os.environ", {"MAX_LLM_CALLS": "not-a-number"}):
        assert agent._int_env("MAX_LLM_CALLS", 25) == 25


# ---------------------------------------------------------------------------
# 1b. Output-token budget — the fix for the BHP/CSL parse_error regression
# ---------------------------------------------------------------------------

def test_results_call_uses_the_results_max_tokens_budget():
    """The shared 4096 default truncated large reporters mid-JSON. The results
    path must send its own, generously-sized budget so no realistic results
    announcement can be cut off."""
    counters = _counters()
    client = _mock_claude(_analysis())
    with mock.patch("agent._anthropic_client", return_value=client):
        agent.deep_results_analysis("CAT", "official report text " * 300, "", counters)

    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["max_tokens"] == agent.RESULTS_MAX_TOKENS
    assert kwargs["max_tokens"] >= 32000, (
        "the results budget must be big enough to hold metrics JSON + the 11-section "
        "markdown full_analysis for a large reporter like BHP or CSL"
    )


def test_non_results_calls_keep_the_shorter_default_budget():
    """The other paths (two-liners, acquisition, capital, trading update)
    return short JSON. Bumping their budget along with the results one would
    waste tokens on every FYI item — that's why the override is per-caller."""
    counters = _counters()
    client = _mock_claude({"what_happened": "x", "so_what": "y"})
    with mock.patch("agent._anthropic_client", return_value=client):
        agent.summarise_two_lines_llm("CAT", "Title", "some body text " * 100, counters)

    assert client.messages.create.call_args.kwargs["max_tokens"] == agent.DEFAULT_MAX_TOKENS
    assert agent.DEFAULT_MAX_TOKENS < agent.RESULTS_MAX_TOKENS


def test_results_max_tokens_is_env_overridable():
    with mock.patch.dict("os.environ", {"CLAUDE_RESULTS_MAX_TOKENS": "80000"}):
        assert agent._int_env("CLAUDE_RESULTS_MAX_TOKENS", 50000) == 80000
    with mock.patch.dict("os.environ", {"CLAUDE_RESULTS_MAX_TOKENS": "not-a-number"}):
        assert agent._int_env("CLAUDE_RESULTS_MAX_TOKENS", 50000) == 50000


# ---------------------------------------------------------------------------
# 1c. Truncation is named in the failure text
# ---------------------------------------------------------------------------

def test_truncated_response_produces_parse_error_that_names_the_cap():
    """The BHP/CSL regression: Claude returned a well-formed JSON that got
    cut off mid-string when the response hit max_tokens. The parse failure
    must say so plainly — 'not valid JSON' sends whoever's reading looking
    for the wrong bug."""
    counters = _counters()
    truncated_json = (
        '{"ticker":"BHP","period":"FY26","period_type":"full_year","currency":"USD",'
        '"metrics":{"revenue":{"value":"US$58,800m","change_pct":"+3%","basis":"reported"}},'
        '"summary":"Beat. Iron ore drove it. Cash conversion held.",'
        '"full_analysis":"## Executive summary\\n\\n- Revenue up 3% to US$58.8bn'
        # ... no closing brace, response was cut off here
    )
    client = _mock_claude(truncated_json, stop_reason="max_tokens")

    with mock.patch("agent._anthropic_client", return_value=client):
        analysis = agent.deep_results_analysis(
            "BHP", "report text " * 300, "deck text " * 300, counters,
        )

    assert analysis["_status"] == agent.RESULTS_STATUS_PARSE_ERROR
    assert analysis["_raw_text"] == truncated_json
    assert "truncated" in analysis["summary"].lower()
    assert str(agent.RESULTS_MAX_TOKENS) in analysis["summary"]
    assert "CLAUDE_RESULTS_MAX_TOKENS" in analysis["summary"]
    assert counters["last_stop_reason"] == "max_tokens"

    # The block still flags ANALYSIS FAILED and the Doc still preserves the
    # raw text — nothing about how the failure is named should regress those.
    block = agent.build_results_block("BHP", analysis, "https://asx.example/bhp")
    assert agent.RESULTS_FAILURE_BADGE in block["html"]
    doc_html = agent.build_analysis_doc_html("BHP", analysis, "https://asx.example/bhp")
    # The raw text is HTML-escaped in the Doc; pick a fragment that survives escaping.
    assert "US$58,800m" in doc_html
    assert "Raw model output" in doc_html


def test_normal_parse_failure_still_uses_the_generic_message():
    """When the stop_reason is not max_tokens, the failure message should not
    misdiagnose the problem — a model that returned prose instead of JSON is
    a prompt/model bug, not a truncation."""
    counters = _counters()
    with mock.patch("agent._anthropic_client",
                    return_value=_mock_claude("Here's a plain-prose answer.", stop_reason="end_turn")):
        analysis = agent.deep_results_analysis("CAT", "report text " * 300, "", counters)

    assert analysis["_status"] == agent.RESULTS_STATUS_PARSE_ERROR
    assert "truncated" not in analysis["summary"].lower()
    assert "not valid JSON" in analysis["summary"]


# ---------------------------------------------------------------------------
# 2. The five metric rows
# ---------------------------------------------------------------------------

def test_five_metrics_render_in_the_locked_order():
    block = agent.build_results_block("CAT", _analysis(), "https://asx.example/cat")
    labels = [label for _key, label in agent.RESULTS_METRIC_ROWS]
    assert labels == [
        "Revenue", "Underlying NPAT", "Underlying EPS",
        "Ordinary dividend", "Operating cash flow",
    ]
    positions = [block["html"].index(label) for label in labels]
    assert positions == sorted(positions), "metric rows must render in the locked order"


def test_missing_figure_renders_na_and_is_never_guessed():
    analysis = _analysis(metrics={"revenue": _metric("$167.4m", "+19%")})
    block = agent.build_results_block("CAT", analysis, "https://asx.example/cat")
    npat = agent._results_metric(analysis, "underlying_npat")
    assert npat == {"value": "n/a", "change": "n/a", "sub": ""}
    assert "n/a" in block["text"]
    # muted grey, not a green/red implication of movement
    assert agent._change_colour("n/a") == agent.COLOR_CHANGE_NEUTRAL


def test_not_disclosed_is_normalised_to_na():
    analysis = _analysis(metrics={"revenue": _metric("Not disclosed", "Not disclosed")})
    assert agent._results_metric(analysis, "revenue")["value"] == "n/a"
    assert agent._results_metric(analysis, "revenue")["change"] == "n/a"


def test_change_colours_are_direction_coded():
    assert agent._change_colour("+19%") == agent.COLOR_CHANGE_UP
    assert agent._change_colour("-8%") == agent.COLOR_CHANGE_DOWN
    assert agent._change_colour("−8%") == agent.COLOR_CHANGE_DOWN  # unicode minus
    assert agent._change_colour("n/m") == agent.COLOR_CHANGE_NEUTRAL
    assert agent._change_colour("") == agent.COLOR_CHANGE_NEUTRAL


def test_negative_change_is_coloured_red_inline_not_by_class():
    analysis = _analysis(metrics={
        "revenue": _metric("$120.0m", "-12%"),
        "underlying_npat": _metric("-$4.2m", "n/m"),
        "underlying_eps": _metric("-3.1c", "n/m"),
        "dividend_ordinary": _metric("nil", "n/a"),
        "operating_cash_flow": _metric("-$2.0m", "n/m"),
    })
    html = agent.build_results_block("CAT", analysis, "https://asx.example/cat")["html"]
    assert f'style="color:{agent.COLOR_CHANGE_DOWN}' in html
    assert 'class=' not in html, "email clients strip <style>; colours must be inline"


# ---------------------------------------------------------------------------
# 3. Edge cases from the testing checklist
# ---------------------------------------------------------------------------

def test_loss_maker_renders_negative_eps_intact():
    analysis = _analysis(metrics={
        "underlying_npat": _metric("-$4.2m", "n/m", basis="underlying"),
        "underlying_eps": _metric("-3.1c", "n/m", basis="underlying"),
    })
    block = agent.build_results_block("DRO", analysis, "https://asx.example/dro")
    assert "-3.1c" in block["text"] and "-3.1c" in block["html"]
    assert "-$4.2m" in block["html"]


def test_no_dividend_name_renders_nil():
    analysis = _analysis(metrics={"dividend_ordinary": _metric("nil", "n/a")})
    block = agent.build_results_block("DRO", analysis, "https://asx.example/dro")
    assert "nil" in block["text"]
    assert "Ordinary dividend" in block["html"]


def test_special_dividend_exclusion_note_is_surfaced():
    analysis = _analysis(metrics={
        "dividend_ordinary": _metric("2.0c", "+11%", note="excl 5.0c special"),
    })
    block = agent.build_results_block("NHC", analysis, "https://asx.example/nhc")
    assert "excl 5.0c special" in block["html"]
    assert "excl 5.0c special" in block["text"]


def test_usd_reporter_renders_us_dollars_not_bare_dollars():
    analysis = _analysis(
        ticker="BHP", period="FY25", currency="USD",
        metrics={
            "revenue": _metric("$54,200m", "+3%"),
            "underlying_npat": _metric("$10,500m", "-2%"),
            "underlying_eps": _metric("207.1c", "-2%"),
            "dividend_ordinary": _metric("110.0c", "-8%"),
            "operating_cash_flow": _metric("$20,700m", "+1%"),
        },
    )
    block = agent.build_results_block("BHP", analysis, "https://asx.example/bhp")
    assert "US$54,200m" in block["html"]
    assert "reported in US$" in block["html"]
    # No bare "$54,200m" that could be read as Australian dollars.
    assert "\">$54,200m" not in block["html"]
    assert "A$" not in block["html"]


def test_currency_prefix_is_idempotent_and_sign_safe():
    assert agent._apply_currency_prefix("US$4,180m", "USD") == "US$4,180m"
    assert agent._apply_currency_prefix("-$4.2m", "USD") == "-US$4.2m"
    assert agent._apply_currency_prefix("5.2c", "AUD") == "5.2c"  # no $ to prefix
    # AUD stays a bare "$" — the card's context line already says "reported in A$".
    assert agent._apply_currency_prefix("$1m", "AUD") == "$1m"
    assert agent._apply_currency_prefix("$1m", "NZD") == "NZ$1m"


def test_quarterly_4c_renders_with_na_npat_and_eps():
    analysis = _analysis(
        period="Q3 FY25", period_type="quarterly",
        metrics={
            "revenue": _metric("$8.1m", "+14%"),
            "underlying_npat": _metric("n/a", "n/a"),
            "underlying_eps": _metric("n/a", "n/a"),
            "dividend_ordinary": _metric("nil", "n/a"),
            "operating_cash_flow": _metric("-$1.2m", "n/m"),
        },
    )
    block = agent.build_results_block("BRN", analysis, "https://asx.example/brn")
    assert "Quarterly, reported in A$" in block["html"]
    assert "Underlying NPAT" in block["html"]
    assert block["text"].count("n/a") >= 4


# ---------------------------------------------------------------------------
# 4. Email block shape (mobile-first, email-client-safe)
# ---------------------------------------------------------------------------

def test_email_block_is_table_based_and_phone_width():
    html = agent.build_results_block("CAT", _analysis(), "https://asx.example/cat")["html"]
    assert "<table" in html and "</table>" in html
    assert "max-width:360px" in html
    for banned in ("display:flex", "display:grid", "<style", "flex-direction"):
        assert banned not in html, f"{banned} is unreliable in email clients"


def test_email_block_has_header_badge_title_and_context_line():
    html = agent.build_results_block("CAT", _analysis(), "https://asx.example/cat")["html"]
    assert "HIGH IMPACT" in html
    assert "CAT — FY25 results" in html
    assert "Full-year, reported in A$" in html
    assert agent.COLOR_RESULTS_HEADER in html


def test_email_block_has_both_link_rows():
    block = agent.build_results_block(
        "CAT", _analysis(), "https://asx.example/cat", doc_link="https://docs.google.com/document/d/abc/view",
    )
    assert "Full analysis (Google Drive)" in block["html"]
    assert "Source announcement (ASX)" in block["html"]
    assert "https://docs.google.com/document/d/abc/view" in block["html"]
    assert "https://asx.example/cat" in block["html"]
    assert "https://docs.google.com/document/d/abc/view" in block["text"]


def test_long_form_analysis_stays_out_of_the_email():
    analysis = _analysis(full_analysis="## Executive summary\n\n" + "long form prose " * 400)
    block = agent.build_results_block("CAT", analysis, "https://asx.example/cat")
    assert "long form prose" not in block["html"]
    assert "long form prose" not in block["text"]
    assert analysis["summary"] in block["text"]


def test_summary_is_html_escaped():
    analysis = _analysis(summary="Margins <b>fell</b> & guidance cut.")
    html = agent.build_results_block("CAT", analysis, "https://asx.example/cat")["html"]
    assert "&lt;b&gt;fell&lt;/b&gt;" in html
    assert "<b>fell</b>" not in html


def test_build_email_accepts_structured_blocks_alongside_plain_strings():
    block = agent.build_results_block("CAT", _analysis(), "https://asx.example/cat")
    body_text, body_html = agent.build_email([block], ["NHC: plain string block"], [])
    assert "CAT — FY25 results" in body_text
    assert "NHC: plain string block" in body_text
    # The card's own HTML must survive un-escaped; the plain block must not.
    assert "max-width:360px" in body_html
    assert "NHC: plain string block" in body_html


# ---------------------------------------------------------------------------
# 5. Failure handling — three distinct, visible states
# ---------------------------------------------------------------------------

def test_cap_skip_says_skipped_and_is_not_a_failure():
    counters = _counters(calls_used=25)
    analysis = agent.deep_results_analysis(
        "CAT", "official report text " * 300, "", counters,
    )
    assert analysis["_status"] == agent.RESULTS_STATUS_SKIPPED
    assert analysis["summary"] == "Analysis skipped: run-call cap reached"
    assert not agent._is_llm_failure(analysis)

    block = agent.build_results_block("CAT", analysis, "https://asx.example/cat")
    assert "Analysis skipped: run-call cap reached" in block["text"]
    assert agent.RESULTS_FAILURE_BADGE not in block["html"]


def test_api_failure_surfaces_the_real_error_and_flags_the_block():
    counters = _counters()
    client = mock.MagicMock()
    client.messages.create.side_effect = RuntimeError("429 rate_limit_error")

    with mock.patch("agent._anthropic_client", return_value=client), \
         mock.patch("agent.time.sleep"):
        analysis = agent.deep_results_analysis(
            "CAT", "official report text " * 300, "", counters,
        )

    assert analysis["_status"] == agent.RESULTS_STATUS_FAILED
    assert "rate_limit_error" in analysis["summary"], "the real error class/status must be surfaced"
    assert agent._is_llm_failure(analysis)

    block = agent.build_results_block("CAT", analysis, "https://asx.example/cat")
    assert agent.RESULTS_FAILURE_BADGE in block["html"]
    assert agent.RESULTS_FAILURE_BADGE in block["text"]


def test_json_parse_failure_is_loud_and_keeps_the_raw_text():
    counters = _counters()
    junk = "Here is the analysis you asked for: revenue was up a lot this year."
    with mock.patch("agent._anthropic_client", return_value=_mock_claude(junk)):
        analysis = agent.deep_results_analysis(
            "CAT", "official report text " * 300, "", counters,
        )

    assert analysis["_status"] == agent.RESULTS_STATUS_PARSE_ERROR
    assert analysis["_raw_text"] == junk
    assert agent._is_llm_failure(analysis)

    block = agent.build_results_block("CAT", analysis, "https://asx.example/cat")
    assert agent.RESULTS_FAILURE_BADGE in block["html"]
    # It must NOT look like a clean, terse analysis.
    assert junk not in block["html"]

    # The raw text is not lost — it goes to the Doc.
    doc_html = agent.build_analysis_doc_html("CAT", analysis, "https://asx.example/cat")
    assert junk in doc_html
    assert "Raw model output" in doc_html


def test_skip_and_failure_never_render_identically():
    skip_counters = _counters(calls_used=25)
    skipped = agent.deep_results_analysis("CAT", "report text " * 300, "", skip_counters)

    fail_counters = _counters()
    client = mock.MagicMock()
    client.messages.create.side_effect = RuntimeError("500 api_error")
    with mock.patch("agent._anthropic_client", return_value=client), \
         mock.patch("agent.time.sleep"):
        failed = agent.deep_results_analysis("CAT", "report text " * 300, "", fail_counters)

    skip_block = agent.build_results_block("CAT", skipped, "https://asx.example/cat")
    fail_block = agent.build_results_block("CAT", failed, "https://asx.example/cat")
    assert skip_block != fail_block
    assert skipped["summary"] != failed["summary"]


def test_no_extractable_content_does_not_burn_an_llm_call():
    counters = _counters()
    analysis = agent.deep_results_analysis("CAT", "short", "", counters)
    assert analysis["_status"] == agent.RESULTS_STATUS_NO_CONTENT
    assert counters["llm_calls"] == 0


# ---------------------------------------------------------------------------
# 6. Google Doc creation
# ---------------------------------------------------------------------------

def test_doc_html_contains_metric_table_summary_and_full_analysis():
    analysis = _analysis(full_analysis=(
        "## Executive summary\n\n"
        "- Revenue up **19%** to $167.4m\n"
        "- Cash conversion held\n\n"
        "## Key numbers\n\n"
        "| Metric | FY25 | FY24 | Change |\n"
        "|---|---|---|---|\n"
        "| Revenue | $167.4m | $140.7m | +19% |\n"
    ))
    doc_html = agent.build_analysis_doc_html("CAT", analysis, "https://asx.example/cat")

    assert "<h1" in doc_html and "CAT — FY25 results" in doc_html
    assert "Executive summary" in doc_html
    assert "<strong>19%</strong>" in doc_html
    assert "<li" in doc_html
    assert doc_html.count("<table") >= 2  # metric table + the markdown table
    assert analysis["summary"] in doc_html
    assert "https://asx.example/cat" in doc_html


def test_markdown_to_html_handles_headings_lists_and_tables():
    html = agent._markdown_to_html(
        "## Positives\n\n- Up 19%\n- Margin held\n\n"
        "1. First\n2. Second\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n\n"
        "Closing paragraph."
    )
    assert "<h2" in html
    assert "<ul" in html and "<ol" in html
    assert "<table" in html
    assert "<p" in html and "Closing paragraph." in html
    assert "|---|" not in html, "the separator row must not leak into the output"


def test_create_analysis_doc_converts_to_a_native_google_doc():
    service = mock.MagicMock()
    service.files().create().execute.return_value = {
        "id": "doc123",
        "webViewLink": "https://docs.google.com/document/d/doc123/edit",
    }
    service.reset_mock()
    service.files().create().execute.return_value = {
        "id": "doc123",
        "webViewLink": "https://docs.google.com/document/d/doc123/edit",
    }

    with mock.patch("agent.drive_service", return_value=service), \
         mock.patch.dict("os.environ", {"EMAIL_TO": "johnny@example.com"}):
        link = agent.create_analysis_doc("CAT", "FY25", "<html><body>hi</body></html>", "folder-1")

    assert link == "https://docs.google.com/document/d/doc123/edit"

    create_kwargs = [c.kwargs for c in service.files().create.call_args_list if c.kwargs.get("body")]
    assert create_kwargs, "files().create must be called with a metadata body"
    body = create_kwargs[-1]["body"]
    assert body["mimeType"] == "application/vnd.google-apps.document", (
        "uploading with the Docs mimeType is what makes Drive convert it to a "
        "native Doc that reflows on mobile"
    )
    assert body["parents"] == ["folder-1"]
    assert body["name"].startswith("CAT FY25 analysis ")

    # The service account owns the file, so the human recipient needs reader access.
    perm_kwargs = service.permissions().create.call_args.kwargs
    assert perm_kwargs["body"]["emailAddress"] == "johnny@example.com"
    assert perm_kwargs["body"]["role"] == "reader"
    assert perm_kwargs["fileId"] == "doc123"


def test_create_analysis_doc_without_a_folder_is_a_noop():
    with mock.patch("agent.drive_service") as svc:
        assert agent.create_analysis_doc("CAT", "FY25", "<html></html>", "") == ""
    svc.assert_not_called()
