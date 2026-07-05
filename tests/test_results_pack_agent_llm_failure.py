# tests/test_results_pack_agent_llm_failure.py
#
# Verifies the Results Pack Agent (the tool used to backfill a results
# analysis Bob missed) applies the same failure-visibility standard as Bob:
#
#   - A genuine Claude API exception is retried once, then surfaces as a
#     loud failure — never a placeholder that looks like a real analysis,
#     and never a "success" RunSummary with a silently-broken analysis.
#   - A malformed/undersized PDF is downgraded to extracted text instead of
#     silently vanishing from the pack.

import io
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
    "openpyxl",
):
    if _stub not in sys.modules:
        sys.modules[_stub] = types.ModuleType(_stub)

sys.path.insert(0, str(Path(__file__).parent.parent))

from results_pack_agent import claude_runner  # noqa: E402
from results_pack_agent.models import Announcement, ResultPack  # noqa: E402
from shared.pdf_llm import LLM_FAILED  # noqa: E402


def _minimal_pdf_bytes() -> bytes:
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _make_pack(anns) -> ResultPack:
    return ResultPack(ticker="CAT", company_name="Catapult Group International Ltd",
                       result_date="30/06/2026", result_type="FY", announcements=anns)


def _fake_anthropic_module(client: mock.MagicMock) -> types.ModuleType:
    mod = types.ModuleType("anthropic")
    mod.Anthropic = mock.MagicMock(return_value=client)  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# 1. No usable content at all -> NO_CONTENT, not a silent empty success
# ---------------------------------------------------------------------------

def test_call_claude_returns_no_content_when_nothing_attachable():
    ann = Announcement(ticker="CAT", title="2026 Annual Report", date="30/06/2026",
                        time="08:00", url="https://example.com/cat", pdf_bytes=None)
    out = claude_runner._call_claude("system prompt", "Ticker: CAT", [ann])
    assert out == claude_runner.NO_CONTENT


# ---------------------------------------------------------------------------
# 2. Genuine API exception -> retried once, then LLM_FAILED
# ---------------------------------------------------------------------------

def test_call_claude_retries_once_then_fails_loudly(monkeypatch):
    ann = Announcement(ticker="CAT", title="2026 Annual Report", date="30/06/2026",
                        time="08:00", url="https://example.com/cat",
                        pdf_bytes=_minimal_pdf_bytes())

    client = mock.MagicMock()
    client.messages.create.side_effect = RuntimeError(
        "Your credit balance is too low to access the Anthropic API."
    )
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic_module(client))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    with mock.patch("results_pack_agent.claude_runner.time.sleep") as mock_sleep:
        out = claude_runner._call_claude("system prompt", "Ticker: CAT", [ann])

    assert out == LLM_FAILED
    assert client.messages.create.call_count == 2  # original + one retry
    mock_sleep.assert_called_once()


# ---------------------------------------------------------------------------
# 3. run_prompts flags failures instead of writing a fake-looking file
# ---------------------------------------------------------------------------

def test_run_prompts_flags_failure_and_writes_visible_placeholder(tmp_path, monkeypatch):
    ann = Announcement(ticker="CAT", title="2026 Annual Report", date="30/06/2026",
                        time="08:00", url="https://example.com/cat",
                        pdf_bytes=_minimal_pdf_bytes())
    pack = _make_pack([ann])

    with mock.patch("results_pack_agent.claude_runner._call_claude", return_value=LLM_FAILED):
        artifacts = claude_runner.run_prompts(
            pack=pack, output_folder=tmp_path,
            prompts_to_run=["management_report"],
        )

    assert artifacts["_llm_failures"] == ["management_report"]
    out_file = Path(artifacts["management_report"])
    content = out_file.read_text(encoding="utf-8")
    assert "⚠️ Analysis failed" in content
    assert "manual review needed" in content


def test_run_prompts_no_failures_key_when_all_succeed(tmp_path):
    ann = Announcement(ticker="CAT", title="2026 Annual Report", date="30/06/2026",
                        time="08:00", url="https://example.com/cat",
                        pdf_bytes=_minimal_pdf_bytes())
    pack = _make_pack([ann])

    with mock.patch("results_pack_agent.claude_runner._call_claude", return_value="Real analysis text"):
        artifacts = claude_runner.run_prompts(
            pack=pack, output_folder=tmp_path,
            prompts_to_run=["management_report"],
        )

    assert "_llm_failures" not in artifacts


# ---------------------------------------------------------------------------
# 4. main.run() must not report success when Claude analysis failed
# ---------------------------------------------------------------------------

def test_main_run_marks_summary_failed_when_claude_analysis_fails(monkeypatch):
    from results_pack_agent import main as rpa_main

    anns = [
        Announcement(ticker="CAT", title="2026 Annual Report", date="30/06/2026",
                     time="08:00", url="https://example.com/cat-annual-report"),
    ]

    with mock.patch("results_pack_agent.main.fetch_announcements", return_value=anns), \
         mock.patch("results_pack_agent.main.download_pack_pdfs", return_value=1), \
         mock.patch("results_pack_agent.main.save_pack_metadata", return_value=mock.MagicMock()), \
         mock.patch("results_pack_agent.main.run_prompts",
                    return_value={"management_report": "x.md", "_llm_failures": ["management_report"]}), \
         mock.patch("results_pack_agent.main.make_output_folder", return_value=Path("/tmp/does-not-matter")), \
         mock.patch("results_pack_agent.main.detect_result_pack") as mock_detect:
        mock_detect.return_value = _make_pack(anns)

        summary = rpa_main.run(ticker="CAT", report_type="FY")

    assert not summary.success
    assert summary.failure_reason == "CLAUDE_ANALYSIS_FAILED"
    assert "management_report" in summary.failure_message
