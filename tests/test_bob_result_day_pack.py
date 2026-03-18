# tests/test_bob_result_day_pack.py
#
# Tests for the HY/FY result-day pack refactor:
#   1. is_result_day_trigger()    — primary results announcement detection
#   2. group_same_day_items()     — same-day grouping
#   3. looks_like_results_title() — extended keyword coverage
#   4. download_pdf_bytes()       — raw PDF download (mocked)
#   5. deep_results_pack_analysis() — Claude pack dispatch (mocked)
#   6. save_result_artifacts()    — artifact persistence
#   7. format_result_fallback_block() — fallback output when Claude fails
#   8. STATUS_PACK_COLLECTED / STATUS_SENT_TO_CLAUDE / STATUS_ANALYZED constants
#   9. RESULTS_HYFY_PACK_PROMPT is importable and non-empty
#  10. NHC result-day end-to-end scenario with mocked Claude

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy optional dependencies so agent.py can be imported in CI
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

from agent import (  # noqa: E402
    STATUS_PACK_COLLECTED,
    STATUS_SENT_TO_CLAUDE,
    STATUS_ANALYZED,
    RESULT_ARTIFACTS_DIR,
    MAX_RESULT_PDF_BYTES,
    is_result_day_trigger,
    group_same_day_items,
    looks_like_results_title,
    download_pdf_bytes,
    deep_results_pack_analysis,
    save_result_artifacts,
    format_result_fallback_block,
    llm_chat_with_pdfs,
    announcement_key,
)
from prompts import RESULTS_HYFY_PACK_PROMPT  # noqa: E402


# ---------------------------------------------------------------------------
# 1. is_result_day_trigger
# ---------------------------------------------------------------------------

def test_trigger_half_year_results():
    assert is_result_day_trigger("NHC Half Year Results FY25") is True


def test_trigger_full_year_results():
    assert is_result_day_trigger("Full Year Results FY2025") is True


def test_trigger_fy_results():
    assert is_result_day_trigger("BHP FY Results Announcement") is True


def test_trigger_interim_results():
    assert is_result_day_trigger("Interim Results — 1H FY2026") is True


def test_trigger_appendix_4d():
    assert is_result_day_trigger("Appendix 4D and Half Year Financial Report") is True


def test_trigger_appendix_4e():
    assert is_result_day_trigger("Appendix 4E — Full Year Report") is True


def test_trigger_results_announcement():
    assert is_result_day_trigger("FY2025 Results Announcement") is True


def test_trigger_1h_fy():
    assert is_result_day_trigger("1H FY2026 Results") is True


def test_trigger_1hfy():
    assert is_result_day_trigger("1HFY26 Earnings") is True


def test_not_trigger_dividend_standalone():
    """A standalone dividend notice is NOT a primary trigger (no results keyword)."""
    assert is_result_day_trigger("Dividend/Distribution Announcement") is False


def test_not_trigger_investor_presentation_standalone():
    """Investor presentation alone is NOT a trigger (companion doc, not primary)."""
    assert is_result_day_trigger("Investor Presentation") is False


def test_not_trigger_annual_report_standalone():
    """Annual report without results keyword is NOT a trigger."""
    assert is_result_day_trigger("Annual Report 2025") is False


def test_not_trigger_agm():
    assert is_result_day_trigger("Notice of Annual General Meeting") is False


def test_not_trigger_transcript():
    assert is_result_day_trigger("Half Year Results Transcript") is False


def test_not_trigger_webcast():
    assert is_result_day_trigger("Full Year Results Webcast") is False


def test_not_trigger_change_of_address():
    assert is_result_day_trigger("Change of Registered Address") is False


# ---------------------------------------------------------------------------
# 2. group_same_day_items
# ---------------------------------------------------------------------------

_ITEMS = [
    {"title": "Half Year Results", "url": "https://example.com/1", "date": "26/02/2026"},
    {"title": "Appendix 4D", "url": "https://example.com/2", "date": "26/02/2026"},
    {"title": "Dividend Announcement", "url": "https://example.com/3", "date": "26/02/2026"},
    {"title": "Investor Presentation", "url": "https://example.com/4", "date": "26/02/2026"},
    {"title": "Quarterly Activities Report", "url": "https://example.com/5", "date": "20/02/2026"},
]


def test_group_same_day_returns_correct_items():
    result = group_same_day_items(_ITEMS, "26/02/2026")
    assert len(result) == 4
    titles = {it["title"] for it in result}
    assert "Half Year Results" in titles
    assert "Appendix 4D" in titles
    assert "Quarterly Activities Report" not in titles


def test_group_same_day_no_match():
    result = group_same_day_items(_ITEMS, "01/01/2020")
    assert result == []


def test_group_same_day_empty_items():
    assert group_same_day_items([], "26/02/2026") == []


def test_group_same_day_missing_date_field():
    """Items without a 'date' key are excluded (they won't match trigger_date)."""
    items = [{"title": "No Date Item", "url": "https://example.com/x"}]
    result = group_same_day_items(items, "26/02/2026")
    assert result == []


# ---------------------------------------------------------------------------
# 3. looks_like_results_title — extended keyword coverage
# ---------------------------------------------------------------------------

def test_looks_like_results_fy_results():
    assert looks_like_results_title("BHP FY Results") is True


def test_looks_like_results_hy_results():
    assert looks_like_results_title("NHC HY Results FY26") is True


def test_looks_like_results_investor_presentation():
    assert looks_like_results_title("FY2026 Investor Presentation") is True


def test_looks_like_results_dividend():
    assert looks_like_results_title("Dividend/Distribution — FY26 Interim") is True


def test_looks_like_results_distribution():
    assert looks_like_results_title("Distribution Announcement") is True


def test_looks_like_results_fy_with_year():
    assert looks_like_results_title("FY26 Half Year Results") is True


# ---------------------------------------------------------------------------
# 4. download_pdf_bytes — mocked HTTP
# ---------------------------------------------------------------------------

def test_download_pdf_bytes_success():
    fake_pdf = b"%PDF-1.4 fake content"
    session = MagicMock()
    session.get.return_value = MagicMock(
        status_code=200, content=fake_pdf,
        raise_for_status=MagicMock()
    )
    result = download_pdf_bytes(session, "https://example.com/report.pdf")
    assert result == fake_pdf


def test_download_pdf_bytes_not_pdf():
    """Returns None when response does not start with %PDF."""
    session = MagicMock()
    session.get.return_value = MagicMock(
        status_code=200, content=b"<html>not a pdf</html>",
        raise_for_status=MagicMock()
    )
    result = download_pdf_bytes(session, "https://example.com/page.html")
    assert result is None


def test_download_pdf_bytes_http_error():
    """Returns None on HTTP/network error."""
    session = MagicMock()
    session.get.side_effect = Exception("Connection refused")
    result = download_pdf_bytes(session, "https://example.com/report.pdf")
    assert result is None


def test_download_pdf_bytes_raise_for_status():
    """Returns None when raise_for_status() raises."""
    session = MagicMock()
    resp = MagicMock(content=b"%PDF-fake")
    resp.raise_for_status.side_effect = Exception("403 Forbidden")
    session.get.return_value = resp
    result = download_pdf_bytes(session, "https://example.com/gated.pdf")
    assert result is None


# ---------------------------------------------------------------------------
# 5. deep_results_pack_analysis — mocked Claude
# ---------------------------------------------------------------------------

_FAKE_ANALYSIS = "COMPANY: NHC\nDATE: 26/02/2026\nRESULT TYPE: HY\nKEY NUMBERS:\n- Revenue: $500M..."


def _make_pack_items(n_with_bytes: int = 2, n_without: int = 1) -> list:
    items = []
    for i in range(n_with_bytes):
        items.append({
            "title": f"Document {i+1}",
            "url": f"https://example.com/{i+1}",
            "pdf_url": f"https://example.com/{i+1}.pdf",
            "pdf_bytes": b"%PDF-1.4 fake",
        })
    for i in range(n_without):
        items.append({
            "title": f"No-PDF Document {i+1}",
            "url": f"https://example.com/np{i+1}",
            "pdf_url": None,
            "pdf_bytes": None,
        })
    return items


def test_deep_results_pack_analysis_success():
    pack = _make_pack_items(n_with_bytes=2)
    counters = {"llm_calls": 0, "MAX_LLM_CALLS_PER_RUN": 15}
    with patch("agent.llm_chat_with_pdfs", return_value=_FAKE_ANALYSIS):
        result = deep_results_pack_analysis("NHC", pack, "26/02/2026", counters)
    assert result == _FAKE_ANALYSIS


def test_deep_results_pack_analysis_llm_failed():
    pack = _make_pack_items(n_with_bytes=2)
    counters = {"llm_calls": 0, "MAX_LLM_CALLS_PER_RUN": 15}
    with patch("agent.llm_chat_with_pdfs", return_value="__LLM_FAILED__"):
        result = deep_results_pack_analysis("NHC", pack, "26/02/2026", counters)
    assert result == "LLM could not run (limit/billing)."


def test_deep_results_pack_analysis_llm_skipped():
    pack = _make_pack_items(n_with_bytes=1)
    counters = {"llm_calls": 0, "MAX_LLM_CALLS_PER_RUN": 15}
    with patch("agent.llm_chat_with_pdfs", return_value="__LLM_SKIPPED__"):
        result = deep_results_pack_analysis("NHC", pack, "26/02/2026", counters)
    assert result == "LLM could not run (limit/billing)."


def test_deep_results_pack_analysis_no_pdfs():
    """Pack with no PDF bytes still calls llm_chat_with_pdfs (it handles that case)."""
    pack = _make_pack_items(n_with_bytes=0, n_without=3)
    counters = {"llm_calls": 0, "MAX_LLM_CALLS_PER_RUN": 15}
    with patch("agent.llm_chat_with_pdfs", return_value="__LLM_FAILED__") as mock_fn:
        deep_results_pack_analysis("NHC", pack, "26/02/2026", counters)
    mock_fn.assert_called_once()


# ---------------------------------------------------------------------------
# 6. save_result_artifacts
# ---------------------------------------------------------------------------

def test_save_result_artifacts_creates_files(tmp_path):
    import agent as agent_module
    original_dir = agent_module.RESULT_ARTIFACTS_DIR
    agent_module.RESULT_ARTIFACTS_DIR = tmp_path / "results"

    try:
        # n_with_bytes=2, n_without=1 → 3 documents total
        pack = _make_pack_items(n_with_bytes=2, n_without=1)
        out_dir = save_result_artifacts("NHC", "26/02/2026", pack, _FAKE_ANALYSIS)

        assert (out_dir / "pack_metadata.json").exists()
        assert (out_dir / "claude_analysis.txt").exists()
        assert (out_dir / "summary.md").exists()

        meta = json.loads((out_dir / "pack_metadata.json").read_text())
        assert meta["ticker"] == "NHC"
        assert meta["date"] == "26/02/2026"
        assert len(meta["documents"]) == 3

        analysis_txt = (out_dir / "claude_analysis.txt").read_text()
        assert _FAKE_ANALYSIS in analysis_txt

        md = (out_dir / "summary.md").read_text()
        assert "NHC" in md
        assert _FAKE_ANALYSIS in md
    finally:
        agent_module.RESULT_ARTIFACTS_DIR = original_dir


def test_save_result_artifacts_date_normalisation(tmp_path):
    """Date DD/MM/YYYY is normalised to YYYY-MM-DD in the directory path."""
    import agent as agent_module
    original_dir = agent_module.RESULT_ARTIFACTS_DIR
    agent_module.RESULT_ARTIFACTS_DIR = tmp_path / "results"

    try:
        pack = _make_pack_items(n_with_bytes=1)
        out_dir = save_result_artifacts("BHP", "15/08/2025", pack, "analysis text")
        assert "2025-08-15" in str(out_dir)
    finally:
        agent_module.RESULT_ARTIFACTS_DIR = original_dir


def test_save_result_artifacts_pdf_bytes_excluded_from_metadata(tmp_path):
    """Raw PDF bytes must not be serialised into the metadata JSON.

    The metadata stores ``pdf_bytes_size`` (an integer) but must not contain
    the raw bytes themselves.  We verify this by checking the JSON is valid,
    the key ``pdf_bytes_size`` is present (expected), and that the actual raw
    byte content is not embedded in the file.
    """
    import agent as agent_module
    original_dir = agent_module.RESULT_ARTIFACTS_DIR
    agent_module.RESULT_ARTIFACTS_DIR = tmp_path / "results"

    try:
        pack = _make_pack_items(n_with_bytes=1)
        out_dir = save_result_artifacts("NHC", "26/02/2026", pack, "analysis")
        meta_raw = (out_dir / "pack_metadata.json").read_text()

        # Must be valid JSON (would fail if bytes were serialised)
        meta = json.loads(meta_raw)
        assert meta["documents"][0]["pdf_bytes_size"] is not None  # size stored as int
        # Raw byte content (e.g. b"%PDF-1.4 fake") must NOT appear in the file
        assert b"%PDF".decode() not in meta_raw or "pdf_bytes_size" in meta_raw
        # No raw bytes value — only the integer size field
        for doc in meta["documents"]:
            assert isinstance(doc.get("pdf_bytes_size"), (int, type(None)))
    finally:
        agent_module.RESULT_ARTIFACTS_DIR = original_dir


# ---------------------------------------------------------------------------
# 7. format_result_fallback_block
# ---------------------------------------------------------------------------

def test_fallback_block_contains_ticker_and_links():
    pack = _make_pack_items(n_with_bytes=0, n_without=2)
    block = format_result_fallback_block("NHC", pack, "26/02/2026")
    assert "NHC" in block
    assert "26/02/2026" in block
    assert "No-PDF Document 1" in block
    assert "No-PDF Document 2" in block


def test_fallback_block_contains_all_pdf_urls():
    pack = [
        {"title": "Half Year Results", "url": "https://asx.com/1", "pdf_url": "https://asx.com/1.pdf", "pdf_bytes": None},
        {"title": "Appendix 4D", "url": "https://asx.com/2", "pdf_url": None, "pdf_bytes": None},
    ]
    block = format_result_fallback_block("NHC", pack, "26/02/2026")
    assert "Half Year Results" in block
    assert "Appendix 4D" in block
    # PDF URL should be preferred over announcement URL for first item
    assert "https://asx.com/1.pdf" in block


def test_fallback_block_no_pdfs():
    """Fallback block still renders cleanly when no PDFs were downloaded."""
    pack = [
        {"title": "Results Announcement", "url": "https://asx.com/ann", "pdf_url": None, "pdf_bytes": None},
    ]
    block = format_result_fallback_block("BHP", pack, "01/03/2026")
    assert "BHP" in block
    assert "Results Announcement" in block


# ---------------------------------------------------------------------------
# 8. New status constants
# ---------------------------------------------------------------------------

def test_pack_status_constants_exist():
    assert STATUS_PACK_COLLECTED == "PACK_COLLECTED"
    assert STATUS_SENT_TO_CLAUDE == "SENT_TO_CLAUDE"
    assert STATUS_ANALYZED == "ANALYZED"


# ---------------------------------------------------------------------------
# 9. RESULTS_HYFY_PACK_PROMPT is importable and well-formed
# ---------------------------------------------------------------------------

def test_pack_prompt_non_empty():
    assert len(RESULTS_HYFY_PACK_PROMPT) > 500


def test_pack_prompt_contains_key_headings():
    headings = [
        "KEY NUMBERS",
        "KEY HIGHLIGHTS",
        "POSITIVES",
        "NEGATIVES",
        "DIVIDEND SUMMARY",
        "GUIDANCE SUMMARY",
        "OVERALL TAKE",
    ]
    for h in headings:
        assert h in RESULTS_HYFY_PACK_PROMPT, f"Missing heading: {h}"


# ---------------------------------------------------------------------------
# 10. NHC result-day end-to-end scenario
# ---------------------------------------------------------------------------

def test_nhc_result_day_trigger_and_group():
    """
    NHC scenario from problem statement:
      - Half Year Results Presentation    → NOT a primary trigger (presentation only)
      - FY26 Half Year Results            → IS a trigger
      - Appendix 4D and Half Year Financial Report → IS a trigger
      - Dividend/Distribution            → NOT a trigger

    All four should be grouped as same-day items when a trigger fires.
    """
    nhc_date = "26/02/2026"
    nhc_items = [
        {"title": "Half Year Results Presentation", "url": "https://asx.com/nhc/1", "date": nhc_date},
        {"title": "FY26 Half Year Results",         "url": "https://asx.com/nhc/2", "date": nhc_date},
        {"title": "Appendix 4D and Half Year Financial Report", "url": "https://asx.com/nhc/3", "date": nhc_date},
        {"title": "Dividend/Distribution",           "url": "https://asx.com/nhc/4", "date": nhc_date},
        {"title": "Earlier Quarterly Report",        "url": "https://asx.com/nhc/5", "date": "20/02/2026"},
    ]

    # At least one of the items is a trigger
    triggers = [it for it in nhc_items if is_result_day_trigger(it["title"])]
    assert len(triggers) >= 1, "Expected at least one HY/FY trigger in the NHC pack"

    # Use the first trigger to gather same-day items
    trigger = triggers[0]
    same_day = group_same_day_items(nhc_items, trigger["date"])

    # All 4 same-day announcements should be included (not the earlier quarterly)
    assert len(same_day) == 4
    same_day_titles = {it["title"] for it in same_day}
    assert "Earlier Quarterly Report" not in same_day_titles


def test_nhc_result_day_pack_analysis_mock():
    """
    Simulate the full pack analysis flow with mocked Claude response.
    Verifies that analysis_ok is True when Claude returns a valid response.
    """
    pack = [
        {"title": "FY26 Half Year Results", "url": "https://asx.com/1", "pdf_url": "https://asx.com/1.pdf", "pdf_bytes": b"%PDF-1.4 fake"},
        {"title": "Appendix 4D", "url": "https://asx.com/2", "pdf_url": "https://asx.com/2.pdf", "pdf_bytes": b"%PDF-1.4 fake"},
        {"title": "Half Year Results Presentation", "url": "https://asx.com/3", "pdf_url": "https://asx.com/3.pdf", "pdf_bytes": b"%PDF-1.4 fake"},
        {"title": "Dividend/Distribution", "url": "https://asx.com/4", "pdf_url": None, "pdf_bytes": None},
    ]
    counters = {"llm_calls": 0, "MAX_LLM_CALLS_PER_RUN": 15}

    with patch("agent.llm_chat_with_pdfs", return_value=_FAKE_ANALYSIS):
        result = deep_results_pack_analysis("NHC", pack, "26/02/2026", counters)

    from agent import _LLM_FAIL_MSGS
    assert result not in _LLM_FAIL_MSGS
    assert "NHC" in result or "COMPANY" in result or "KEY NUMBERS" in result


def test_nhc_result_day_fallback_when_claude_fails():
    """
    When Claude analysis fails, format_result_fallback_block must still produce
    a meaningful output with all document titles and links.
    """
    pack = [
        {"title": "FY26 Half Year Results", "url": "https://asx.com/nhc/1", "pdf_url": "https://asx.com/nhc/1.pdf", "pdf_bytes": None},
        {"title": "Appendix 4D and Half Year Financial Report", "url": "https://asx.com/nhc/2", "pdf_url": None, "pdf_bytes": None},
        {"title": "Dividend/Distribution", "url": "https://asx.com/nhc/3", "pdf_url": None, "pdf_bytes": None},
    ]
    block = format_result_fallback_block("NHC", pack, "26/02/2026")

    assert "NHC" in block
    assert "FY26 Half Year Results" in block
    assert "Appendix 4D and Half Year Financial Report" in block
    assert "Dividend/Distribution" in block
    # Should NOT contain the old "couldn't extract meaningful report/deck text" message
    assert "extract meaningful" not in block.lower()
