# tests/test_bob_remuneration.py
#
# Coverage for the REMUNERATION path — the branch Bob takes when a company
# files an amended employee share plan, new LTI/STI rules, or an executive
# remuneration framework change. Same output contract as the results path
# (structured JSON, five-row quick-take card, summary, attached PDF), so the
# assertions here mirror test_bob_results_block.py: real headlines pinned as
# classifier tests; card shape asserted against email-client-safe rules;
# failure states must never render like a clean analysis.

import json
import sys
import types
from pathlib import Path
from unittest import mock

for _stub in (
    "anthropic", "playwright", "playwright.async_api", "googleapiclient",
    "googleapiclient.discovery", "googleapiclient.http",
    "google", "google.oauth2", "google.oauth2.credentials",
    "google.oauth2.service_account", "google.auth",
    "google.auth.transport", "google.auth.transport.requests",
):
    if _stub not in sys.modules:
        sys.modules[_stub] = types.ModuleType(_stub)

sys.modules["googleapiclient.http"].MediaInMemoryUpload = mock.MagicMock()

_pw_stub = types.ModuleType("playwright_fetch")
_pw_stub.fetch_pdf_with_playwright = None  # type: ignore[attr-defined]
sys.modules.setdefault("playwright_fetch", _pw_stub)

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent  # noqa: E402


# ---------------------------------------------------------------------------
# Classifier — must match plan-change headlines, reject grant-notice noise
# ---------------------------------------------------------------------------

def test_amended_employee_share_plan_matches():
    assert agent.looks_like_remuneration_title("Amended Employee Share Plan") is True
    assert agent.looks_like_remuneration_title(
        "Amendment to Employee Share Scheme"
    ) is True


def test_new_ltip_and_variants_match():
    assert agent.looks_like_remuneration_title("New Long Term Incentive Plan") is True
    assert agent.looks_like_remuneration_title("Adoption of FY26 LTIP") is True
    assert agent.looks_like_remuneration_title(
        "Long-Term Incentive Plan Amendments"
    ) is True
    assert agent.looks_like_remuneration_title(
        "Performance Rights Plan — Amended Rules"
    ) is True


def test_remuneration_framework_change_matches():
    assert agent.looks_like_remuneration_title(
        "FY26 Executive Remuneration Framework"
    ) is True
    assert agent.looks_like_remuneration_title(
        "Amended Remuneration Policy"
    ) is True


def test_rules_of_the_plan_matches():
    assert agent.looks_like_remuneration_title(
        "Rules of the Austco Employee Share Plan"
    ) is True


def test_explanatory_memorandum_for_plan_matches():
    assert agent.looks_like_remuneration_title(
        "Explanatory Memorandum — Performance Rights Plan"
    ) is True


def test_approval_of_incentive_plan_matches():
    assert agent.looks_like_remuneration_title(
        "Approval of Employee Share Plan"
    ) is True


def test_grant_notices_do_not_match():
    """Change of Director's Interest (Appendix 3Y) reports one grant *under*
    an existing plan. It's not a plan-change document, and the analysis
    would be pointless — the plan hasn't moved. Same for Appendix 3X/3Z.
    """
    assert agent.looks_like_remuneration_title(
        "Change of Director's Interest Notice — Grant of Performance Rights"
    ) is False
    assert agent.looks_like_remuneration_title(
        "Initial Director's Interest Notice"
    ) is False
    assert agent.looks_like_remuneration_title("Appendix 3Y") is False


def test_notice_of_meeting_does_not_match_on_its_own():
    """A Notice of Meeting is a wrapper — the remuneration report inside the
    annual report is already picked up under RESULTS_HY_FY, so catching the
    NoM here would double-analyse it. Only the standalone plan documents
    or the explicit "Approval of ..." resolutions trigger."""
    assert agent.looks_like_remuneration_title("Notice of Annual General Meeting") is False
    assert agent.looks_like_remuneration_title("Notice of Meeting") is False
    assert agent.looks_like_remuneration_title(
        "Notice of Annual General Meeting and Explanatory Memorandum"
    ) is False


def test_annual_report_does_not_match():
    """The remuneration report inside the AR is handled by RESULTS_HY_FY —
    the AR title itself must not spawn a second analysis."""
    assert agent.looks_like_remuneration_title("Annual Report 2026") is False


def test_price_query_response_does_not_match():
    """The ASX price-query response template mentions 'employee share scheme'
    as an example of a thing the company hasn't done. That is the exact
    string that would false-positive without a HARD NO."""
    assert agent.looks_like_remuneration_title(
        "Response to ASX Aware Query"
    ) is False
    assert agent.looks_like_remuneration_title(
        "Response to Price Query"
    ) is False


def test_chairman_address_does_not_match():
    assert agent.looks_like_remuneration_title(
        "Chairman's Address to Shareholders"
    ) is False


def test_classify_from_title_only_routes_to_remuneration():
    assert agent.classify_from_title_only(
        "Amended Employee Share Plan"
    ) == "REMUNERATION"
    assert agent.classify_from_title_only(
        "New Long Term Incentive Plan"
    ) == "REMUNERATION"
    # Anything else stays where it is — remuneration is checked after results
    # and before acquisition, and none of those should be affected.
    assert agent.classify_from_title_only(
        "NHC Half Year Results"
    ) == "RESULTS_HY_FY"
    assert agent.classify_from_title_only(
        "Acquisition of XYZ Ltd"
    ) == "ACQUISITION"


# ---------------------------------------------------------------------------
# deep_remuneration_memo — one structured LLM call, streaming, tagged _status
# ---------------------------------------------------------------------------

def _counters(calls_used: int = 0) -> dict:
    return {
        "MAX_LLM_CALLS_PER_RUN": 25,
        "llm_calls": calls_used,
        "MAX_PDFS_PER_RUN": 10,
        "pdfs_downloaded": 0,
    }


def _analysis(**overrides) -> dict:
    base = {
        "ticker": "AHC",
        "plan_name": "FY26 Amended Employee Share Plan",
        "plan_type": "ESP",
        "participants": "All permanent employees (~250 people)",
        "verdict": "mixed",
        "alignment_score": "3",
        "quick_take": {
            "funding":              {"value": "New issue", "note": "up to 1% of shares p.a."},
            "quantum":              {"value": "$5,000 per employee", "note": "up from $3,000 in prior plan"},
            "hurdles":              {"value": "None — service-only", "note": "no performance test"},
            "vesting_period":       {"value": "3-year cliff", "note": "no post-vest holding lock"},
            "shareholder_dilution": {"value": "up to 3%", "note": "over 3-year plan life"},
        },
        "summary": "Mixed. Quantum lifted 66% with no new hurdles. Service-only vesting rewards tenure not performance. Dilution capped at 3% is manageable but not trivial. Watch the FY27 review for a tightening of hurdles.",
        "full_analysis": "## Verdict\n\nMixed — better funded but no stronger.\n",
    }
    base.update(overrides)
    return base


def _mock_claude(payload, stop_reason: str = "end_turn") -> mock.MagicMock:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    client = mock.MagicMock()
    response = mock.MagicMock()
    response.content = [mock.MagicMock(text=text)]
    response.stop_reason = stop_reason
    client.messages.create.return_value = response
    stream_cm = mock.MagicMock()
    stream_cm.text_stream = iter([text])
    stream_cm.get_final_message.return_value = response
    stream_cm.__enter__.return_value = stream_cm
    stream_cm.__exit__.return_value = False
    client.messages.stream.return_value = stream_cm
    return client


def test_deep_remuneration_memo_is_one_streaming_call():
    """Above ~10-minute expected duration the SDK forces streaming; the
    remuneration budget hits that threshold like results does."""
    counters = _counters()
    client = _mock_claude(_analysis())
    with mock.patch("agent._anthropic_client", return_value=client):
        analysis = agent.deep_remuneration_memo(
            "AHC", "Amended Employee Share Plan",
            "plan rules text " * 400, counters,
        )
    assert client.messages.stream.call_count == 1
    assert client.messages.create.call_count == 0
    assert counters["llm_calls"] == 1
    assert analysis["_status"] == agent.RESULTS_STATUS_OK
    assert analysis["verdict"] == "mixed"


def test_deep_remuneration_memo_uses_remuneration_token_budget():
    counters = _counters()
    client = _mock_claude(_analysis())
    with mock.patch("agent._anthropic_client", return_value=client):
        agent.deep_remuneration_memo(
            "AHC", "Amended Employee Share Plan",
            "plan rules text " * 400, counters,
        )
    kwargs = client.messages.stream.call_args.kwargs
    assert kwargs["max_tokens"] == agent.REMUNERATION_MAX_TOKENS
    assert kwargs["max_tokens"] >= 32000


def test_deep_remuneration_memo_no_content_does_not_burn_a_call():
    counters = _counters()
    analysis = agent.deep_remuneration_memo(
        "AHC", "Amended Employee Share Plan", "short", counters,
    )
    assert analysis["_status"] == agent.RESULTS_STATUS_NO_CONTENT
    assert counters["llm_calls"] == 0


def test_deep_remuneration_memo_truncation_is_named():
    """The BHP/CSL failure mode: JSON cut off at max_tokens must render as a
    parse_error that names the cap, not the generic 'not valid JSON'."""
    counters = _counters()
    truncated = (
        '{"ticker":"AHC","plan_name":"ESP","plan_type":"ESP","summary":"..."'
        ',"full_analysis":"## Verdict\\n\\nMixed'
        # cut off — no closing brace
    )
    client = _mock_claude(truncated, stop_reason="max_tokens")
    with mock.patch("agent._anthropic_client", return_value=client):
        analysis = agent.deep_remuneration_memo(
            "AHC", "Amended Employee Share Plan",
            "plan rules " * 400, counters,
        )
    assert analysis["_status"] == agent.RESULTS_STATUS_PARSE_ERROR
    assert analysis["_raw_text"] == truncated
    assert "truncated" in analysis["summary"].lower()
    assert "CLAUDE_REMUNERATION_MAX_TOKENS" in analysis["summary"]


# ---------------------------------------------------------------------------
# Email block — phone-first, table-based, inline styles only
# ---------------------------------------------------------------------------

def test_remuneration_block_is_table_based_and_phone_width():
    block = agent.build_remuneration_block(
        "AHC", _analysis(), "https://asx.example/ahc",
    )
    html = block["html"]
    assert "<table" in html and "</table>" in html
    assert "max-width:360px" in html
    for banned in ("display:flex", "display:grid", "<style", "flex-direction"):
        assert banned not in html, f"{banned} is unreliable in email clients"


def test_remuneration_block_has_verdict_pill_and_title():
    """The header must carry the verdict pill so a phone-glance tells the
    reader whether to read the summary or just archive it."""
    block = agent.build_remuneration_block(
        "AHC", _analysis(), "https://asx.example/ahc",
    )
    html = block["html"]
    assert "AHC — FY26 Amended Employee Share Plan" in html
    assert "MIXED" in html
    # The badge colours are inline (not classes) because email clients
    # strip <style> blocks.
    assert agent.COLOR_ALIGN_MIXED in html


def test_remuneration_verdict_colour_ladder():
    """Aligned = green, misaligned = red, mixed = amber. A missing verdict
    falls back on the numeric score, and a total absence falls back to
    the mixed-amber "REVIEW" pill so a reader is never given a green
    thumbs-up on nothing."""
    for verdict, colour in [
        ("aligned",    agent.COLOR_ALIGN_GOOD),
        ("misaligned", agent.COLOR_ALIGN_BAD),
        ("mixed",      agent.COLOR_ALIGN_MIXED),
    ]:
        label, resolved = agent._remuneration_verdict({"verdict": verdict})
        assert resolved == colour, verdict
        assert label

    # Score fallback.
    _, colour = agent._remuneration_verdict({"alignment_score": "5"})
    assert colour == agent.COLOR_ALIGN_GOOD
    _, colour = agent._remuneration_verdict({"alignment_score": "1"})
    assert colour == agent.COLOR_ALIGN_BAD
    # Nothing at all — amber REVIEW, never green.
    _, colour = agent._remuneration_verdict({})
    assert colour != agent.COLOR_ALIGN_GOOD


def test_five_quick_take_rows_render_in_order():
    block = agent.build_remuneration_block(
        "AHC", _analysis(), "https://asx.example/ahc",
    )
    html = block["html"]
    labels = ["Funding", "Quantum", "Hurdles", "Vesting", "Dilution"]
    positions = [html.index(label) for label in labels]
    assert positions == sorted(positions), (
        "the five quick-take rows must render in the locked order — "
        "funding, quantum, hurdles, vesting, dilution"
    )


def test_missing_quick_take_value_renders_not_disclosed():
    """'not disclosed' is the plan-side equivalent of results 'n/a', and it
    is explicitly the token the prompt tells the model to emit when a
    parameter is genuinely missing. It must never be guessed at."""
    analysis = _analysis(quick_take={"funding": {"value": "New issue"}})
    row = agent._remuneration_quick_take_row(analysis, "quantum")
    assert row["value"] == "not disclosed"
    block = agent.build_remuneration_block("AHC", analysis, "https://asx.example/ahc")
    assert "not disclosed" in block["text"]


def test_summary_is_html_escaped():
    analysis = _analysis(summary="Hurdles <b>weakened</b> & quantum up 66%.")
    html = agent.build_remuneration_block(
        "AHC", analysis, "https://asx.example/ahc",
    )["html"]
    assert "&lt;b&gt;weakened&lt;/b&gt;" in html
    assert "<b>weakened</b>" not in html


def test_full_analysis_stays_out_of_the_email():
    """The long-form analysis goes to the PDF, not the email — same rule as
    the results card."""
    analysis = _analysis(
        full_analysis="## Verdict\n\n" + "long form prose " * 400,
    )
    block = agent.build_remuneration_block(
        "AHC", analysis, "https://asx.example/ahc",
    )
    assert "long form prose" not in block["html"]
    assert "long form prose" not in block["text"]
    assert analysis["summary"] in block["text"]


def test_asx_link_row_present():
    block = agent.build_remuneration_block(
        "AHC", _analysis(), "https://asx.example/ahc-plan",
    )
    assert "https://asx.example/ahc-plan" in block["html"]
    assert "Source announcement (ASX)" in block["html"]
    assert "📎 Full remuneration analysis PDF attached" in block["html"]


# ---------------------------------------------------------------------------
# PDF-doc HTML body — carries the metric table, summary, and the long-form
# analysis; raw text preserved on a parse failure.
# ---------------------------------------------------------------------------

def test_doc_html_contains_quick_take_summary_and_full_analysis():
    analysis = _analysis(full_analysis=(
        "## Verdict\n\nMixed — better funded but no stronger.\n\n"
        "## What changed vs previous plan\n\n"
        "| Parameter | Old plan | New plan | Delta |\n"
        "|---|---|---|---|\n"
        "| Quantum | $3,000 | $5,000 | +66% |\n"
    ))
    html = agent.build_remuneration_doc_html(
        "AHC", analysis, "https://asx.example/ahc",
    )
    assert "<h1" in html and "AHC — FY26 Amended Employee Share Plan" in html
    assert "Verdict" in html
    assert analysis["summary"] in html
    assert html.count("<table") >= 2   # quick-take table + the markdown table
    assert "https://asx.example/ahc" in html


def test_doc_html_parse_error_keeps_raw_text():
    counters = _counters()
    # A fragment with no apostrophes / angle brackets so the assertion
    # matches whether or not the doc HTML-escapes the raw text.
    junk = "The analysis you asked for: revenue was up a lot."
    with mock.patch("agent._anthropic_client", return_value=_mock_claude(junk)):
        analysis = agent.deep_remuneration_memo(
            "AHC", "Amended Employee Share Plan",
            "plan text " * 400, counters,
        )
    assert analysis["_status"] == agent.RESULTS_STATUS_PARSE_ERROR
    html = agent.build_remuneration_doc_html("AHC", analysis, "https://asx.example/ahc")
    assert junk in html
    assert "Raw model output" in html


def test_parse_error_block_is_not_a_clean_card():
    counters = _counters()
    junk = "Plain-prose. No JSON."
    with mock.patch("agent._anthropic_client", return_value=_mock_claude(junk)):
        analysis = agent.deep_remuneration_memo(
            "AHC", "Amended Employee Share Plan",
            "plan text " * 400, counters,
        )
    block = agent.build_remuneration_block("AHC", analysis, "https://asx.example/ahc")
    assert agent.RESULTS_FAILURE_BADGE in block["html"]
    assert junk not in block["html"]


def test_dashboard_has_remuneration_type():
    """The docs/data/bob.json 'type' Bob emits must be recognised by the
    dashboard, otherwise the site drops back to a generic 'HIGH IMPACT'
    label and none of the quick-take rows render."""
    from scripts import build_dashboard
    assert "remuneration" in build_dashboard._BADGE_COLOURS
    assert "remuneration" in build_dashboard._FIELD_MAP
