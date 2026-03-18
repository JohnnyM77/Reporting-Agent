# tests/test_results_pack_agent.py
#
# Tests for the standalone Results Pack Agent:
#   1.  is_result_day_trigger()         — primary trigger detection
#   2.  is_pack_document()              — pack document inclusion
#   3.  infer_result_type()             — HY vs FY inference
#   4.  detect_result_pack()            — full pack detection
#   5.  asx_date helpers                — date conversion utilities
#   6.  file naming helpers             — ResultPack.folder_name / file_prefix
#   7.  run_prompts() dry-run           — no Claude call made in dry-run
#   8.  build_valuation() dry-run       — no wally call made in dry-run
#   9.  RunSummary.print_summary()      — smoke test
#   10. PROMPT_REGISTRY completeness    — required keys present
#   11. NHC scenario                    — end-to-end pack detection from mock HTML

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy optional dependencies so the agent can be imported in CI
# ---------------------------------------------------------------------------
for _stub in (
    "anthropic",
    "playwright",
    "playwright.async_api",
    "googleapiclient",
    "googleapiclient.discovery",
    "googleapiclient.http",
    "google",
    "google.oauth2",
    "google.oauth2.credentials",
    "google.oauth2.service_account",
    "google.auth",
    "google.auth.transport",
    "google.auth.transport.requests",
    "openpyxl",
):
    if _stub not in sys.modules:
        sys.modules[_stub] = types.ModuleType(_stub)

sys.path.insert(0, str(Path(__file__).parent.parent))

from results_pack_agent.models import Announcement, ResultPack, RunSummary  # noqa: E402
from results_pack_agent.pack_detector import (  # noqa: E402
    detect_result_pack,
    infer_result_type,
    is_pack_document,
    is_result_day_trigger,
)
from results_pack_agent.prompts import (  # noqa: E402
    ARTIFACT_SUFFIX,
    PROMPT_REGISTRY,
)
from results_pack_agent.utils import (  # noqa: E402
    asx_date_to_iso,
    asx_date_to_prefix,
    iso_to_asx_date,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ann(title: str, date: str = "18/03/2026", ticker: str = "NHC") -> Announcement:
    return Announcement(ticker=ticker, title=title, date=date, time="10:00", url=f"https://example.com/{title[:20]}")


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


def test_not_trigger_dividend():
    assert is_result_day_trigger("Dividend/Distribution Announcement") is False


def test_not_trigger_investor_presentation_standalone():
    assert is_result_day_trigger("Investor Presentation") is False


def test_not_trigger_transcript():
    assert is_result_day_trigger("Half Year Results Transcript") is False


def test_not_trigger_webcast():
    assert is_result_day_trigger("Full Year Results Webcast") is False


def test_not_trigger_agm():
    assert is_result_day_trigger("Notice of Annual General Meeting") is False


# ---------------------------------------------------------------------------
# 2. is_pack_document
# ---------------------------------------------------------------------------

def test_pack_doc_dividend():
    assert is_pack_document("Dividend/Distribution Announcement") is True


def test_pack_doc_investor_presentation():
    assert is_pack_document("Investor Presentation — FY26 Results") is True


def test_pack_doc_appendix_4d():
    assert is_pack_document("Appendix 4D") is True


def test_pack_doc_annual_report():
    assert is_pack_document("Annual Report 2026") is True


def test_pack_doc_not_transcript():
    assert is_pack_document("Results Webcast Recording") is False


def test_pack_doc_random_announcement():
    # Generic announcement without any keywords should NOT be included
    assert is_pack_document("Change of Registered Address") is False


# ---------------------------------------------------------------------------
# 3. infer_result_type
# ---------------------------------------------------------------------------

def test_infer_hy():
    anns = [
        _make_ann("NHC Half Year Results FY26"),
        _make_ann("Appendix 4D"),
        _make_ann("Dividend Announcement"),
    ]
    assert infer_result_type(anns) == "HY"


def test_infer_fy():
    anns = [
        _make_ann("Full Year Results FY2026"),
        _make_ann("Annual Report"),
        _make_ann("Appendix 4E"),
    ]
    assert infer_result_type(anns) == "FY"


def test_infer_default_fy_when_ambiguous():
    anns = [_make_ann("Investor Presentation")]
    # No strong HY/FY signal → defaults to FY
    assert infer_result_type(anns) == "FY"


# ---------------------------------------------------------------------------
# 4. detect_result_pack
# ---------------------------------------------------------------------------

_NHC_ANNS = [
    _make_ann("NHC Half Year Results FY26", date="18/03/2026"),
    _make_ann("Appendix 4D", date="18/03/2026"),
    _make_ann("Dividend/Distribution Announcement", date="18/03/2026"),
    _make_ann("Investor Presentation", date="18/03/2026"),
    _make_ann("Quarterly Activities Report", date="20/01/2026"),
]


def test_detect_pack_nhc_hy():
    pack = detect_result_pack(_NHC_ANNS, report_type="HY")
    assert pack is not None
    assert pack.result_type == "HY"
    assert pack.result_date == "18/03/2026"
    assert pack.ticker == "NHC"
    # All 4 same-day documents should be in the pack
    assert len(pack.announcements) >= 2
    titles = {a.title for a in pack.announcements}
    assert "NHC Half Year Results FY26" in titles


def test_detect_pack_no_trigger():
    anns = [_make_ann("Quarterly Activities Report")]
    assert detect_result_pack(anns) is None


def test_detect_pack_type_filter_excludes_fy():
    """When report_type=HY, an FY-only trigger should be skipped."""
    anns = [
        _make_ann("Full Year Results FY2026", date="30/08/2025"),
        _make_ann("Annual Report", date="30/08/2025"),
    ]
    # Requesting HY should NOT match FY-only triggers
    pack = detect_result_pack(anns, report_type="HY")
    assert pack is None


def test_detect_pack_target_date():
    import datetime as dt
    pack = detect_result_pack(
        _NHC_ANNS,
        target_date=dt.date(2026, 3, 18),
    )
    assert pack is not None
    assert pack.result_date == "18/03/2026"


def test_detect_pack_wrong_target_date():
    import datetime as dt
    pack = detect_result_pack(
        _NHC_ANNS,
        target_date=dt.date(2025, 1, 1),  # no announcements on this day
    )
    assert pack is None


# ---------------------------------------------------------------------------
# 5. Date helper utilities
# ---------------------------------------------------------------------------

def test_asx_date_to_prefix():
    assert asx_date_to_prefix("18/03/2026") == "260318"


def test_asx_date_to_iso():
    assert asx_date_to_iso("18/03/2026") == "2026-03-18"


def test_iso_to_asx_date():
    assert iso_to_asx_date("2026-03-18") == "18/03/2026"


# ---------------------------------------------------------------------------
# 6. ResultPack file-naming helpers
# ---------------------------------------------------------------------------

def test_resultpack_folder_name():
    pack = ResultPack(
        ticker="NHC",
        company_name="New Hope Corporation",
        result_date="18/03/2026",
        result_type="HY",
    )
    assert pack.folder_name == "260318-NHC-HY-Results-Pack"


def test_resultpack_file_prefix():
    pack = ResultPack(
        ticker="NHC",
        company_name="New Hope Corporation",
        result_date="18/03/2026",
        result_type="FY",
    )
    assert pack.file_prefix == "260318-NHC-FY"


def test_resultpack_date_prefix():
    pack = ResultPack(
        ticker="BHP",
        company_name="BHP Group",
        result_date="01/01/2026",
        result_type="FY",
    )
    assert pack.date_prefix == "260101"


# ---------------------------------------------------------------------------
# 7. run_prompts dry-run (no Claude call)
# ---------------------------------------------------------------------------

def test_run_prompts_dry_run(tmp_path):
    from results_pack_agent.claude_runner import run_prompts

    pack = ResultPack(
        ticker="NHC",
        company_name="New Hope Corporation",
        result_date="18/03/2026",
        result_type="HY",
        announcements=[_make_ann("NHC Half Year Results FY26")],
    )

    # Dry run should not call Claude and should return artifact paths
    artifacts = run_prompts(
        pack=pack,
        output_folder=tmp_path,
        prompts_to_run=["management_report", "equity_report"],
        dry_run=True,
    )

    assert "management_report" in artifacts
    assert "equity_report" in artifacts
    # In dry-run mode, no files should actually be written
    assert not any(tmp_path.glob("*.md"))


# ---------------------------------------------------------------------------
# 8. build_valuation dry-run (no wally call)
# ---------------------------------------------------------------------------

def test_build_valuation_dry_run(tmp_path):
    from results_pack_agent.valuation_runner import build_valuation

    result = build_valuation(
        ticker="NHC.AX",
        output_folder=tmp_path,
        file_prefix="260318-NHC-HY",
        dry_run=True,
    )
    assert result is not None
    assert "260318-NHC-HY-Value-Chart.xlsx" in result
    # No file should be created in dry-run mode
    assert not any(tmp_path.glob("*.xlsx"))


# ---------------------------------------------------------------------------
# 9. RunSummary.print_summary smoke test
# ---------------------------------------------------------------------------

def test_run_summary_print(capsys):
    summary = RunSummary(
        ticker="NHC",
        result_date="18/03/2026",
        result_type="HY",
        pdfs_downloaded=3,
        prompts_run=["management_report", "equity_report", "strawman_post"],
        local_folder="/tmp/260318-NHC-HY-Results-Pack",
        drive_folder_url="https://drive.google.com/drive/folders/abc123",
        valuation_path="/tmp/260318-NHC-HY-Value-Chart.xlsx",
        artifacts={"management_report": "/tmp/260318-NHC-HY-Management-Report.md"},
    )
    summary.print_summary()
    captured = capsys.readouterr()
    assert "NHC" in captured.out
    assert "HY" in captured.out
    assert "260318" in captured.out or "18/03/2026" in captured.out
    assert "3" in captured.out                    # pdfs_downloaded
    # Verify the exact Drive URL (set on the RunSummary above) appears in output
    assert "https://drive.google.com/drive/folders/abc123" in captured.out


# ---------------------------------------------------------------------------
# 10. PROMPT_REGISTRY completeness
# ---------------------------------------------------------------------------

def test_prompt_registry_has_required_keys():
    required = {"management_report", "equity_report", "strawman_post"}
    assert required.issubset(set(PROMPT_REGISTRY.keys()))


def test_prompt_registry_non_empty():
    for key, prompt in PROMPT_REGISTRY.items():
        assert isinstance(prompt, str), f"Prompt '{key}' is not a string"
        assert len(prompt) > 100, f"Prompt '{key}' looks too short"


def test_artifact_suffix_has_required_keys():
    required = {"management_report", "equity_report", "strawman_post"}
    assert required.issubset(set(ARTIFACT_SUFFIX.keys()))


# ---------------------------------------------------------------------------
# 11. NHC scenario — end-to-end pack detection from mock HTML
# ---------------------------------------------------------------------------

_NHC_MOCK_HTML = """
<html><body><table>
<tr>
  <td>18/03/2026</td><td>10:00 am</td>
  <td><a href="/asx/v2/announcements/NHC?id=1">NHC Half Year Results FY26</a></td>
</tr>
<tr>
  <td>18/03/2026</td><td>10:01 am</td>
  <td><a href="/asx/v2/announcements/NHC?id=2">Appendix 4D</a></td>
</tr>
<tr>
  <td>18/03/2026</td><td>10:02 am</td>
  <td><a href="/asx/v2/announcements/NHC?id=3">Dividend/Distribution Announcement</a></td>
</tr>
<tr>
  <td>18/03/2026</td><td>10:03 am</td>
  <td><a href="/asx/v2/announcements/NHC?id=4">Investor Presentation — 1H FY26</a></td>
</tr>
<tr>
  <td>20/01/2026</td><td>09:00 am</td>
  <td><a href="/asx/v2/announcements/NHC?id=5">Quarterly Activities Report</a></td>
</tr>
</table></body></html>
"""


def test_nhc_end_to_end_pack_detection():
    """Full scenario: parse mock HTML, detect HY pack, verify structure."""
    from results_pack_agent.asx_fetcher import _parse_announcements_html

    anns = _parse_announcements_html(_NHC_MOCK_HTML, ticker="NHC")
    assert len(anns) == 5

    pack = detect_result_pack(anns, report_type="HY")
    assert pack is not None
    assert pack.ticker == "NHC"
    assert pack.result_type == "HY"
    assert pack.result_date == "18/03/2026"

    titles = {a.title for a in pack.announcements}
    assert "NHC Half Year Results FY26" in titles
    assert "Appendix 4D" in titles
    assert "Quarterly Activities Report" not in titles

    assert pack.folder_name == "260318-NHC-HY-Results-Pack"
    assert pack.file_prefix == "260318-NHC-HY"


# ---------------------------------------------------------------------------
# 12. JSON response parsing (_parse_announcements_json)
# ---------------------------------------------------------------------------

_NHC_MOCK_JSON = """{
  "data": [
    {
      "header": "NHC Half Year Results FY26",
      "releasedDate": "18/03/2026 10:00 am",
      "url": "/asx/v2/statistics/displayAnnouncement.do?idsId=1001",
      "documentKey": "1001"
    },
    {
      "header": "Appendix 4D",
      "releasedDate": "18/03/2026 10:01 am",
      "url": "/asx/v2/statistics/displayAnnouncement.do?idsId=1002",
      "documentKey": "1002"
    },
    {
      "header": "Dividend/Distribution Announcement",
      "releasedDate": "18/03/2026 10:02 am",
      "url": "/asx/v2/statistics/displayAnnouncement.do?idsId=1003",
      "documentKey": "1003"
    },
    {
      "header": "Investor Presentation — 1H FY26",
      "releasedDate": "18/03/2026 10:03 am",
      "url": "/asx/v2/statistics/displayAnnouncement.do?idsId=1004",
      "documentKey": "1004"
    },
    {
      "header": "Quarterly Activities Report",
      "releasedDate": "20/01/2026 09:00 am",
      "url": "/asx/v2/statistics/displayAnnouncement.do?idsId=1005",
      "documentKey": "1005"
    }
  ]
}"""

_NHC_MOCK_JSON_ISO_DATES = """{
  "data": [
    {
      "header": "NHC Half Year Results FY26",
      "releasedDate": "2026-03-18T10:00:00.000+11:00",
      "url": "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsId=2001",
      "documentKey": "2001"
    },
    {
      "header": "Appendix 4D",
      "releasedDate": "2026-03-18T10:01:00.000+11:00",
      "url": "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsId=2002",
      "documentKey": "2002"
    }
  ]
}"""


def test_parse_json_dmy_dates():
    """JSON response with DD/MM/YYYY dates is parsed correctly."""
    from results_pack_agent.asx_fetcher import _parse_announcements_json

    anns = _parse_announcements_json(_NHC_MOCK_JSON, ticker="NHC")
    assert len(anns) == 5
    assert anns[0].title == "NHC Half Year Results FY26"
    assert anns[0].date == "18/03/2026"
    assert anns[0].time == "10:00 am"
    assert "asx.com.au" in anns[0].url
    assert anns[0].url == "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsId=1001"


def test_parse_json_iso_dates():
    """JSON response with ISO 8601 dates is parsed correctly."""
    from results_pack_agent.asx_fetcher import _parse_announcements_json

    anns = _parse_announcements_json(_NHC_MOCK_JSON_ISO_DATES, ticker="NHC")
    assert len(anns) == 2
    assert anns[0].date == "18/03/2026"


def test_parse_json_date_filter():
    """JSON parser respects from_date/to_date window."""
    import datetime as dt
    from results_pack_agent.asx_fetcher import _parse_announcements_json

    target = dt.date(2026, 3, 18)
    anns = _parse_announcements_json(
        _NHC_MOCK_JSON, ticker="NHC", from_date=target, to_date=target
    )
    # Only the 4 announcements on 18/03/2026 should be returned
    assert len(anns) == 4
    assert all(a.date == "18/03/2026" for a in anns)


def test_parse_json_invalid_returns_empty():
    """Non-JSON or wrong-shape responses return an empty list (HTML fallback)."""
    from results_pack_agent.asx_fetcher import _parse_announcements_json

    assert _parse_announcements_json("not json", ticker="NHC") == []
    assert _parse_announcements_json("<html><body></body></html>", ticker="NHC") == []
    assert _parse_announcements_json('{"other": []}', ticker="NHC") == []


def test_nhc_end_to_end_pack_detection_json():
    """Full scenario: parse mock JSON, detect HY pack, verify structure."""
    from results_pack_agent.asx_fetcher import _parse_announcements_json

    anns = _parse_announcements_json(_NHC_MOCK_JSON, ticker="NHC")
    assert len(anns) == 5

    pack = detect_result_pack(anns, report_type="HY")
    assert pack is not None
    assert pack.ticker == "NHC"
    assert pack.result_type == "HY"
    assert pack.result_date == "18/03/2026"

    titles = {a.title for a in pack.announcements}
    assert "NHC Half Year Results FY26" in titles
    assert "Appendix 4D" in titles
    assert "Quarterly Activities Report" not in titles

    assert pack.folder_name == "260318-NHC-HY-Results-Pack"
    assert pack.file_prefix == "260318-NHC-HY"


def test_parse_asx_release_date_formats():
    """_parse_asx_release_date handles all known date string formats."""
    import datetime as dt
    from results_pack_agent.asx_fetcher import _parse_asx_release_date

    expected = dt.date(2026, 3, 18)
    assert _parse_asx_release_date("18/03/2026") == expected
    assert _parse_asx_release_date("18/03/2026 10:00 am") == expected
    assert _parse_asx_release_date("2026-03-18") == expected
    assert _parse_asx_release_date("2026-03-18T10:00:00.000+11:00") == expected
    assert _parse_asx_release_date("") is None
    assert _parse_asx_release_date("not-a-date") is None


# ---------------------------------------------------------------------------
# 13. find_nearest_result_dates
# ---------------------------------------------------------------------------

def test_find_nearest_result_dates_basic():
    """find_nearest_result_dates returns ISO dates for trigger announcements."""
    from results_pack_agent.pack_detector import find_nearest_result_dates

    anns = [
        _make_ann("NHC Half Year Results FY26", date="18/03/2026"),
        _make_ann("Full Year Results FY2025", date="28/08/2025"),
        _make_ann("Quarterly Activities Report", date="20/01/2026"),
    ]
    dates = find_nearest_result_dates(anns)
    assert "2026-03-18" in dates
    assert "2025-08-28" in dates
    # Quarterly report should NOT appear
    assert "2026-01-20" not in dates


def test_find_nearest_result_dates_report_type_filter():
    """find_nearest_result_dates respects report_type filter."""
    from results_pack_agent.pack_detector import find_nearest_result_dates

    anns = [
        _make_ann("NHC Half Year Results FY26", date="18/03/2026"),
        _make_ann("Full Year Results FY2025", date="28/08/2025"),
        _make_ann("Appendix 4D", date="18/03/2026"),
    ]
    hy_dates = find_nearest_result_dates(anns, report_type="HY")
    fy_dates = find_nearest_result_dates(anns, report_type="FY")

    assert "2026-03-18" in hy_dates
    assert "2025-08-28" in fy_dates
    # FY-only trigger should not appear when filtering for HY
    assert "2025-08-28" not in hy_dates


def test_find_nearest_result_dates_empty():
    """find_nearest_result_dates returns empty list when no triggers found."""
    from results_pack_agent.pack_detector import find_nearest_result_dates

    anns = [_make_ann("Quarterly Activities Report", date="20/01/2026")]
    assert find_nearest_result_dates(anns) == []


def test_find_nearest_result_dates_n_limit():
    """find_nearest_result_dates respects the n limit."""
    from results_pack_agent.pack_detector import find_nearest_result_dates

    anns = [
        _make_ann("Half Year Results FY26", date="18/03/2026"),
        _make_ann("Full Year Results FY25", date="28/08/2025"),
        _make_ann("Half Year Results FY25", date="19/03/2025"),
    ]
    dates = find_nearest_result_dates(anns, n=2)
    assert len(dates) <= 2


# ---------------------------------------------------------------------------
# 14. RunSummary failure fields
# ---------------------------------------------------------------------------

def test_run_summary_failure_fields():
    """RunSummary with failure_reason prints structured failure output."""
    summary = RunSummary(
        ticker="NHC",
        result_date="N/A",
        result_type="HY",
        pdfs_downloaded=0,
        prompts_run=[],
        local_folder="N/A",
        drive_folder_url=None,
        valuation_path=None,
        failure_reason="TICKER_VALID_BUT_NO_MATCHING_DATE",
        failure_message="No HY result pack found for NHC on 2026-03-17.",
        nearest_dates=["2026-03-18", "2025-09-18"],
    )
    assert not summary.success
    summary_str = ""
    import io, sys as _sys
    captured = io.StringIO()
    old_stdout = _sys.stdout
    _sys.stdout = captured
    try:
        summary.print_summary()
    finally:
        _sys.stdout = old_stdout
    out = captured.getvalue()
    assert "TICKER_VALID_BUT_NO_MATCHING_DATE" in out
    assert "2026-03-18" in out
    assert "2025-09-18" in out


def test_run_summary_success_has_no_failure_reason():
    """RunSummary without failure_reason is considered successful."""
    summary = RunSummary(
        ticker="NHC",
        result_date="18/03/2026",
        result_type="HY",
        pdfs_downloaded=3,
        prompts_run=["management_report"],
        local_folder="/tmp/test",
        drive_folder_url=None,
        valuation_path=None,
    )
    assert summary.success
    assert summary.failure_reason is None


# ---------------------------------------------------------------------------
# 15. run() function — structured failure returns (no sys.exit)
# ---------------------------------------------------------------------------

def test_run_no_announcements_returns_failure(monkeypatch):
    """run() returns a structured failure RunSummary when no announcements found."""
    from results_pack_agent import main as rpa_main
    from unittest.mock import patch

    with patch("results_pack_agent.main.fetch_announcements", return_value=[]):
        summary = rpa_main.run(ticker="INVALID", report_type="HY")

    assert not summary.success
    assert summary.failure_reason == "NO_ANNOUNCEMENTS_FOUND"


def test_run_wrong_date_returns_nearest_dates(monkeypatch):
    """run() suggests nearest dates when the requested date has no pack."""
    from results_pack_agent import main as rpa_main
    from unittest.mock import patch

    mock_anns = [
        _make_ann("NHC Half Year Results FY26", date="18/03/2026"),
        _make_ann("Appendix 4D", date="18/03/2026"),
        _make_ann("Dividend/Distribution Announcement", date="18/03/2026"),
    ]

    with patch("results_pack_agent.main.fetch_announcements", return_value=mock_anns):
        # Request date 2026-03-17 (one day off) — should suggest 2026-03-18
        summary = rpa_main.run(ticker="NHC", report_type="HY", target_date="2026-03-17")

    assert not summary.success
    assert summary.failure_reason == "TICKER_VALID_BUT_NO_MATCHING_DATE"
    assert "2026-03-18" in summary.nearest_dates


def test_run_no_date_finds_latest(monkeypatch):
    """run() with no date finds the latest result pack automatically."""
    from results_pack_agent import main as rpa_main
    from unittest.mock import patch, MagicMock

    mock_anns = [
        _make_ann("NHC Half Year Results FY26", date="18/03/2026"),
        _make_ann("Appendix 4D", date="18/03/2026"),
        _make_ann("Dividend/Distribution Announcement", date="18/03/2026"),
    ]

    # Patch out the heavy operations so the test doesn't touch disk/network
    with patch("results_pack_agent.main.fetch_announcements", return_value=mock_anns), \
         patch("results_pack_agent.main.download_pack_pdfs", return_value=0), \
         patch("results_pack_agent.main.save_pack_metadata", return_value=MagicMock()), \
         patch("results_pack_agent.main.run_prompts", return_value={}), \
         patch("results_pack_agent.main.make_output_folder", return_value=MagicMock()):

        summary = rpa_main.run(ticker="NHC", report_type="HY")

    assert summary.success
    assert summary.result_date == "18/03/2026"
    assert summary.result_type == "HY"


def test_run_exact_date_match_succeeds(monkeypatch):
    """run() with correct exact date finds the pack and succeeds."""
    from results_pack_agent import main as rpa_main
    from unittest.mock import patch, MagicMock

    mock_anns = [
        _make_ann("NHC Half Year Results FY26", date="18/03/2026"),
        _make_ann("Appendix 4D", date="18/03/2026"),
        _make_ann("Dividend/Distribution Announcement", date="18/03/2026"),
    ]

    with patch("results_pack_agent.main.fetch_announcements", return_value=mock_anns), \
         patch("results_pack_agent.main.download_pack_pdfs", return_value=0), \
         patch("results_pack_agent.main.save_pack_metadata", return_value=MagicMock()), \
         patch("results_pack_agent.main.run_prompts", return_value={}), \
         patch("results_pack_agent.main.make_output_folder", return_value=MagicMock()):

        summary = rpa_main.run(ticker="NHC", report_type="HY", target_date="2026-03-18")

    assert summary.success
    assert summary.result_date == "18/03/2026"


# ---------------------------------------------------------------------------
# 16. Company name resolution
# ---------------------------------------------------------------------------

def test_resolve_company_name_nhc():
    """_resolve_company_name returns 'New Hope Corporation Limited' for NHC."""
    from results_pack_agent.main import _resolve_company_name

    name = _resolve_company_name("NHC")
    assert name == "New Hope Corporation Limited"


def test_resolve_company_name_unknown():
    """_resolve_company_name falls back to ticker for unknown tickers."""
    from results_pack_agent.main import _resolve_company_name

    name = _resolve_company_name("ZZUNKNOWN")
    assert name == "ZZUNKNOWN"


# ---------------------------------------------------------------------------
# 17. list_recent_dates smoke test
# ---------------------------------------------------------------------------

def test_list_recent_dates(capsys, monkeypatch):
    """list_recent_dates() prints result-day candidates for a ticker."""
    from results_pack_agent import main as rpa_main
    from unittest.mock import patch

    mock_anns = [
        _make_ann("NHC Half Year Results FY26", date="18/03/2026"),
        _make_ann("Full Year Results FY25", date="28/08/2025"),
        _make_ann("Quarterly Activities Report", date="20/01/2026"),
    ]

    with patch("results_pack_agent.main.fetch_announcements", return_value=mock_anns):
        rpa_main.list_recent_dates("NHC")

    captured = capsys.readouterr()
    assert "NHC" in captured.out
    assert "2026-03-18" in captured.out
    assert "2025-08-28" in captured.out
    # Quarterly report should not appear
    assert "2026-01-20" not in captured.out


# ---------------------------------------------------------------------------
# 18. Additional trigger keyword coverage
# ---------------------------------------------------------------------------

def test_trigger_financial_results():
    assert is_result_day_trigger("NHC Financial Results FY26") is True


def test_trigger_preliminary_final_report():
    assert is_result_day_trigger("Preliminary Final Report FY2026") is True


def test_trigger_half_year_result_singular():
    """'half year result' (singular) should trigger."""
    assert is_result_day_trigger("NHC Half Year Result") is True



# ---------------------------------------------------------------------------
# 19. NHC regression — 17/03/2026 result date (mandatory regression test)
# ---------------------------------------------------------------------------

_NHC_17MAR_MOCK_HTML = """
<html><body><table>
<tr>
  <td>17/03/2026</td><td>10:00 am</td>
  <td><a href="/asx/v2/announcements/NHC?id=10">Half Year Results Presentation</a></td>
</tr>
<tr>
  <td>17/03/2026</td><td>10:01 am</td>
  <td><a href="/asx/v2/announcements/NHC?id=11">FY26 Half Year Results</a></td>
</tr>
<tr>
  <td>17/03/2026</td><td>10:02 am</td>
  <td><a href="/asx/v2/announcements/NHC?id=12">Dividend/Distribution - NHC</a></td>
</tr>
<tr>
  <td>17/03/2026</td><td>10:03 am</td>
  <td><a href="/asx/v2/announcements/NHC?id=13">Appendix 4D and Half Year Financial Report</a></td>
</tr>
<tr>
  <td>20/01/2026</td><td>09:00 am</td>
  <td><a href="/asx/v2/announcements/NHC?id=14">Quarterly Activities Report</a></td>
</tr>
</table></body></html>
"""

_NHC_17MAR_MOCK_JSON = """{
  "data": [
    {
      "header": "Half Year Results Presentation",
      "releasedDate": "17/03/2026 10:00 am",
      "url": "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsId=3001",
      "documentKey": "3001"
    },
    {
      "header": "FY26 Half Year Results",
      "releasedDate": "17/03/2026 10:01 am",
      "url": "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsId=3002",
      "documentKey": "3002"
    },
    {
      "header": "Dividend/Distribution - NHC",
      "releasedDate": "17/03/2026 10:02 am",
      "url": "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsId=3003",
      "documentKey": "3003"
    },
    {
      "header": "Appendix 4D and Half Year Financial Report",
      "releasedDate": "17/03/2026 10:03 am",
      "url": "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsId=3004",
      "documentKey": "3004"
    },
    {
      "header": "Quarterly Activities Report",
      "releasedDate": "20/01/2026 09:00 am",
      "url": "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsId=3005",
      "documentKey": "3005"
    }
  ]
}"""

_NHC_17MAR_MOCK_JSON_V1 = """{
  "data": [
    {
      "id": "3001",
      "header": "Half Year Results Presentation",
      "document_date": "2026-03-17T10:00:00+11:00",
      "url": "https://www.asx.com.au/announcements/NHC/3001"
    },
    {
      "id": "3002",
      "header": "FY26 Half Year Results",
      "document_date": "2026-03-17T10:01:00+11:00",
      "url": "https://www.asx.com.au/announcements/NHC/3002"
    },
    {
      "id": "3003",
      "header": "Dividend/Distribution - NHC",
      "document_date": "2026-03-17T10:02:00+11:00",
      "url": "https://www.asx.com.au/announcements/NHC/3003"
    },
    {
      "id": "3004",
      "header": "Appendix 4D and Half Year Financial Report",
      "document_date": "2026-03-17T10:03:00+11:00",
      "url": "https://www.asx.com.au/announcements/NHC/3004"
    },
    {
      "id": "3005",
      "header": "Quarterly Activities Report",
      "document_date": "2026-01-20T09:00:00+11:00",
      "url": "https://www.asx.com.au/announcements/NHC/3005"
    }
  ]
}"""


def test_nhc_regression_17mar2026_html():
    """Mandatory regression: NHC HY pack on 17/03/2026 detected from HTML."""
    from results_pack_agent.asx_fetcher import _parse_announcements_html

    anns = _parse_announcements_html(_NHC_17MAR_MOCK_HTML, ticker="NHC")
    assert len(anns) == 5

    pack = detect_result_pack(anns, report_type="HY")
    assert pack is not None, "NHC HY pack on 17/03/2026 must be detected"
    assert pack.ticker == "NHC"
    assert pack.result_type == "HY"
    assert pack.result_date == "17/03/2026"

    titles = {a.title for a in pack.announcements}
    assert "Half Year Results Presentation" in titles
    assert "FY26 Half Year Results" in titles
    assert "Dividend/Distribution - NHC" in titles
    assert "Appendix 4D and Half Year Financial Report" in titles
    assert "Quarterly Activities Report" not in titles

    assert pack.folder_name == "260317-NHC-HY-Results-Pack"
    assert pack.file_prefix == "260317-NHC-HY"


def test_nhc_regression_17mar2026_json():
    """Mandatory regression: NHC HY pack on 17/03/2026 detected from v2 JSON."""
    from results_pack_agent.asx_fetcher import _parse_announcements_json

    anns = _parse_announcements_json(_NHC_17MAR_MOCK_JSON, ticker="NHC")
    assert len(anns) == 5

    pack = detect_result_pack(anns, report_type="HY")
    assert pack is not None, "NHC HY pack on 17/03/2026 must be detected"
    assert pack.result_date == "17/03/2026"
    assert pack.folder_name == "260317-NHC-HY-Results-Pack"


# ---------------------------------------------------------------------------
# 20. v1 JSON API format (_parse_announcements_json_v1)
# ---------------------------------------------------------------------------

def test_parse_json_v1_iso_dates():
    """v1 JSON (document_date ISO 8601) is parsed correctly."""
    from results_pack_agent.asx_fetcher import _parse_announcements_json_v1

    anns = _parse_announcements_json_v1(_NHC_17MAR_MOCK_JSON_V1, ticker="NHC")
    assert len(anns) == 5
    assert anns[0].title == "Half Year Results Presentation"
    assert anns[0].date == "17/03/2026"
    assert "asx.com.au" in anns[0].url


def test_parse_json_v1_id_url_fallback():
    """v1 parser constructs URL from 'id' when 'url' field is absent."""
    from results_pack_agent.asx_fetcher import _parse_announcements_json_v1

    payload = """{
      "data": [
        {
          "id": "9999",
          "header": "FY26 Half Year Results",
          "document_date": "2026-03-17T10:00:00+11:00"
        }
      ]
    }"""
    anns = _parse_announcements_json_v1(payload, ticker="NHC")
    assert len(anns) == 1
    assert "NHC/9999" in anns[0].url


def test_parse_json_v1_date_filter():
    """v1 JSON parser respects from_date/to_date window."""
    import datetime as dt
    from results_pack_agent.asx_fetcher import _parse_announcements_json_v1

    target = dt.date(2026, 3, 17)
    anns = _parse_announcements_json_v1(
        _NHC_17MAR_MOCK_JSON_V1, ticker="NHC", from_date=target, to_date=target
    )
    assert len(anns) == 4
    assert all(a.date == "17/03/2026" for a in anns)


def test_parse_json_v1_invalid_returns_empty():
    """v1 parser returns empty list on invalid input."""
    from results_pack_agent.asx_fetcher import _parse_announcements_json_v1

    assert _parse_announcements_json_v1("not json", ticker="NHC") == []
    assert _parse_announcements_json_v1('{"other": []}', ticker="NHC") == []


def test_nhc_regression_17mar2026_json_v1():
    """Mandatory regression: NHC HY pack on 17/03/2026 detected from v1 JSON."""
    from results_pack_agent.asx_fetcher import _parse_announcements_json_v1

    anns = _parse_announcements_json_v1(_NHC_17MAR_MOCK_JSON_V1, ticker="NHC")
    assert len(anns) == 5

    pack = detect_result_pack(anns, report_type="HY")
    assert pack is not None, "NHC HY pack on 17/03/2026 must be detected from v1 JSON"
    assert pack.result_date == "17/03/2026"
    assert pack.folder_name == "260317-NHC-HY-Results-Pack"
    assert pack.file_prefix == "260317-NHC-HY"

    titles = {a.title for a in pack.announcements}
    assert "Half Year Results Presentation" in titles
    assert "FY26 Half Year Results" in titles
    assert "Dividend/Distribution - NHC" in titles
    assert "Appendix 4D and Half Year Financial Report" in titles


# ---------------------------------------------------------------------------
# 21. fetch_announcements — fallback chain (unit test with mocked HTTP)
# ---------------------------------------------------------------------------

def test_fetch_announcements_v2_json_used_when_available():
    """fetch_announcements returns v2 JSON results when v2 succeeds."""
    from unittest.mock import MagicMock, patch
    from results_pack_agent.asx_fetcher import fetch_announcements

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "application/json"}
    mock_resp.text = _NHC_17MAR_MOCK_JSON
    mock_resp.raise_for_status = MagicMock()

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    anns = fetch_announcements("NHC", session=mock_session)
    assert len(anns) == 5
    assert anns[0].ticker == "NHC"


def test_fetch_announcements_falls_back_to_v2_html():
    """fetch_announcements falls back to HTML parsing when v2 JSON has no data key."""
    from unittest.mock import MagicMock, patch
    from results_pack_agent.asx_fetcher import fetch_announcements

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "text/html"}
    mock_resp.text = _NHC_17MAR_MOCK_HTML
    mock_resp.raise_for_status = MagicMock()

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    anns = fetch_announcements("NHC", session=mock_session)
    assert len(anns) == 5


def test_fetch_announcements_falls_back_to_v1_when_v2_empty():
    """fetch_announcements falls back to v1 JSON when v2 returns no items."""
    from unittest.mock import MagicMock
    from results_pack_agent.asx_fetcher import fetch_announcements

    empty_resp = MagicMock()
    empty_resp.status_code = 200
    empty_resp.headers = {"Content-Type": "text/html"}
    empty_resp.text = "<html><body>No table here.</body></html>"
    empty_resp.raise_for_status = MagicMock()

    v1_resp = MagicMock()
    v1_resp.status_code = 200
    v1_resp.headers = {"Content-Type": "application/json"}
    v1_resp.text = _NHC_17MAR_MOCK_JSON_V1
    v1_resp.raise_for_status = MagicMock()

    # v2 gets empty HTML; v1 gets the JSON payload; company page never called
    mock_session = MagicMock()
    mock_session.get.side_effect = [empty_resp, v1_resp]

    anns = fetch_announcements("NHC", session=mock_session)
    assert len(anns) == 5
    # v1 URL was called (second get call)
    assert mock_session.get.call_count == 2


def test_fetch_announcements_falls_back_to_company_page_when_v1_empty():
    """fetch_announcements falls back to company page when v1 also returns nothing."""
    from unittest.mock import MagicMock
    from results_pack_agent.asx_fetcher import fetch_announcements

    empty_resp = MagicMock()
    empty_resp.status_code = 200
    empty_resp.headers = {"Content-Type": "text/html"}
    empty_resp.text = "<html><body></body></html>"
    empty_resp.raise_for_status = MagicMock()

    company_page_resp = MagicMock()
    company_page_resp.status_code = 200
    company_page_resp.headers = {"Content-Type": "text/html"}
    company_page_resp.text = _NHC_17MAR_MOCK_HTML
    company_page_resp.raise_for_status = MagicMock()

    # v2 empty, v1 empty, company page has data
    mock_session = MagicMock()
    mock_session.get.side_effect = [empty_resp, empty_resp, company_page_resp]

    anns = fetch_announcements("NHC", session=mock_session)
    assert len(anns) == 5
    assert mock_session.get.call_count == 3


# ---------------------------------------------------------------------------
# 22. Defaults: upload / strawman / valuation are ON by default
# ---------------------------------------------------------------------------

def test_run_defaults_include_strawman(monkeypatch):
    """run() includes strawman_post in prompts by default (include_strawman=True)."""
    from results_pack_agent import main as rpa_main
    from unittest.mock import patch, MagicMock

    mock_anns = [
        _make_ann("NHC Half Year Results FY26", date="17/03/2026"),
        _make_ann("Appendix 4D", date="17/03/2026"),
    ]

    with patch("results_pack_agent.main.fetch_announcements", return_value=mock_anns), \
         patch("results_pack_agent.main.download_pack_pdfs", return_value=0), \
         patch("results_pack_agent.main.save_pack_metadata", return_value=MagicMock()), \
         patch("results_pack_agent.main.run_prompts", return_value={}) as mock_run_prompts, \
         patch("results_pack_agent.main.make_output_folder", return_value=MagicMock()):

        rpa_main.run(ticker="NHC", report_type="HY")

    call_kwargs = mock_run_prompts.call_args
    # strawman_post must be in prompts_to_run by default
    prompts = call_kwargs[1].get("prompts_to_run") or call_kwargs[0][2]
    assert "strawman_post" in prompts


def test_run_skip_strawman_excludes_it(monkeypatch):
    """run() excludes strawman_post when skip_strawman=True."""
    from results_pack_agent import main as rpa_main
    from unittest.mock import patch, MagicMock

    mock_anns = [
        _make_ann("NHC Half Year Results FY26", date="17/03/2026"),
        _make_ann("Appendix 4D", date="17/03/2026"),
    ]

    with patch("results_pack_agent.main.fetch_announcements", return_value=mock_anns), \
         patch("results_pack_agent.main.download_pack_pdfs", return_value=0), \
         patch("results_pack_agent.main.save_pack_metadata", return_value=MagicMock()), \
         patch("results_pack_agent.main.run_prompts", return_value={}) as mock_run_prompts, \
         patch("results_pack_agent.main.make_output_folder", return_value=MagicMock()):

        rpa_main.run(ticker="NHC", report_type="HY", skip_strawman=True)

    call_kwargs = mock_run_prompts.call_args
    prompts = call_kwargs[1].get("prompts_to_run") or call_kwargs[0][2]
    assert "strawman_post" not in prompts


# ---------------------------------------------------------------------------
# 23. Error message — no "ticker may be invalid" when fetch returns empty
# ---------------------------------------------------------------------------

def test_no_announcements_error_message_does_not_blame_ticker():
    """run() error message must NOT say 'ticker may be invalid' on fetch failure."""
    from results_pack_agent import main as rpa_main
    from unittest.mock import patch

    with patch("results_pack_agent.main.fetch_announcements", return_value=[]):
        summary = rpa_main.run(ticker="NHC")

    assert summary.failure_reason == "NO_ANNOUNCEMENTS_FOUND"
    msg = summary.failure_message or ""
    # Must NOT blame the ticker
    assert "ticker may be invalid" not in msg.lower()
    assert "not listed on asx" not in msg.lower()
    # Must give a useful diagnostic direction
    assert "fetch" in msg.lower() or "parsing" in msg.lower()
