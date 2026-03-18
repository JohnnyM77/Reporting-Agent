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
