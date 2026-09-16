"""Tests for the Slinger section in scripts/build_dashboard.py.

The section renders JSON produced by sgx_agent._write_dashboard_json.
Both sides need to move together; a rename here without one on the
Slinger side would render an empty card while the JSON on disk still
looked right.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_build_dashboard():
    spec = importlib.util.spec_from_file_location(
        "build_dashboard",
        REPO_ROOT / "scripts" / "build_dashboard.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_slinger_section_renders_expected_content():
    bd = _load_build_dashboard()
    data = {
        "last_run": "2026-09-16",
        "silence": False,
        "high_impact": [
            {
                "ticker": "5DD",
                "issuer_name": "MICRO-MECHANICS (HOLDINGS) LTD.",
                "title": "Financial Statements::FY2026",
                "url": "https://links.sgx.com/x",
                "type": "results",
                "source_pdfs": [
                    {"url": "https://links.sgx.com/x/perf.pdf",
                     "name": "Performance Summary"},
                ],
                "analysis": {
                    "period": "FY2026",
                    "period_type": "full_year",
                    "metrics": {
                        "revenue": {"value": "S$75.5m",
                                    "change_pct": "+15.8%",
                                    "basis": "reported"},
                    },
                    "summary": "Strong FY26.",
                },
            }
        ],
        "material": [{"ticker": "D05", "title": "Cash Dividend",
                      "url": "https://x"}],
        "fyi": [{"ticker": "C6L", "title": "Something",
                 "url": "https://y"}],
    }
    html = bd._slinger_section(data)
    # Slinger persona + SGX badge in the header.
    assert "Singapore Slinger" in html
    assert ">SGX<" in html
    # Bob's flatten_metrics reads change_pct + value; both should surface.
    assert "S$75.5m" in html
    assert "+15.8%" in html
    # Source PDFs render as links back to SGX, not attachments.
    assert "Performance Summary" in html
    assert "perf.pdf" in html
    # Material + FYI counts appear in their block headers.
    assert "MATERIAL (1)" in html
    assert "FYI" in html and "(1)" in html


def test_slinger_section_empty_data_is_empty_string():
    """Prevent an orphan Slinger card on a first-install repo with no
    slinger.json yet."""
    bd = _load_build_dashboard()
    assert bd._slinger_section({}) == ""


def test_dashboard_wires_slinger_between_bob_and_wally():
    """A quick regression pin -- if someone deletes the Slinger call
    site from build_dashboard(), the site loses its SGX section
    silently. Pin the wiring here."""
    bd = _load_build_dashboard()
    source = (REPO_ROOT / "scripts" / "build_dashboard.py").read_text()
    assert "_slinger_section" in source
    assert "_load(\"slinger.json\")" in source or "_load('slinger.json')" in source
    # History wiring -- without it the "last two runs" feature silently
    # regresses to showing only the current run.
    assert "_update_slinger_history" in source
    assert "slinger_history" in source


def test_slinger_section_shows_previous_run_from_history():
    """When _update_slinger_history returns two runs, the section
    renders the current one open and the previous one collapsed inside
    a <details> block. Mirrors the Bob card behaviour."""
    bd = _load_build_dashboard()
    current = {
        "last_run": "2026-09-16",
        "silence": False,
        "high_impact": [
            {"ticker": "C07", "title": "Financial Statements::HY2026",
             "url": "https://x", "type": "results",
             "analysis": {"period": "HY2026",
                          "metrics": {"revenue": {"value": "S$100m",
                                                  "change_pct": "+5%"}},
                          "summary": "current run"}},
        ],
        "material": [], "fyi": [],
    }
    previous = {
        "last_run": "2026-09-15",
        "silence": False,
        "high_impact": [
            {"ticker": "LCC", "title": "Financial Statements::FY2026",
             "url": "https://y", "type": "results",
             "analysis": {"period": "FY2026",
                          "metrics": {"revenue": {"value": "S$50m",
                                                  "change_pct": "+3%"}},
                          "summary": "previous run"}},
        ],
        "material": [], "fyi": [],
    }
    html = bd._slinger_section(current, history=[current, previous])
    assert "current run" in html
    assert "previous run" in html
    assert "<details" in html
    assert "Previous run" in html
    assert "C07" in html and "LCC" in html


def test_update_slinger_history_prepends_new_run(tmp_path, monkeypatch):
    """The rolling history file should carry the two newest distinct
    runs, newest first."""
    bd = _load_build_dashboard()
    monkeypatch.setattr(bd, "DATA_DIR", tmp_path)

    run_a = {"last_run": "2026-09-14", "silence": False,
             "high_impact": [], "material": [], "fyi": []}
    run_b = {"last_run": "2026-09-15", "silence": False,
             "high_impact": [{"ticker": "LCC"}], "material": [], "fyi": []}
    run_c = {"last_run": "2026-09-16", "silence": False,
             "high_impact": [{"ticker": "C07"}], "material": [], "fyi": []}

    h1 = bd._update_slinger_history(run_a)
    assert [x["last_run"] for x in h1] == ["2026-09-14"]

    h2 = bd._update_slinger_history(run_b)
    assert [x["last_run"] for x in h2] == ["2026-09-15", "2026-09-14"]

    h3 = bd._update_slinger_history(run_c)
    assert [x["last_run"] for x in h3] == ["2026-09-16", "2026-09-15"]


def test_update_slinger_history_dedupes_identical_reruns(tmp_path, monkeypatch):
    """An identical rebuild (e.g. Bob or Theo rebuilding the dashboard
    without a new Slinger run) must not push the previous run out."""
    bd = _load_build_dashboard()
    monkeypatch.setattr(bd, "DATA_DIR", tmp_path)

    run_a = {"last_run": "2026-09-14", "silence": False,
             "high_impact": [], "material": [], "fyi": []}
    run_b = {"last_run": "2026-09-15", "silence": False,
             "high_impact": [{"ticker": "LCC"}], "material": [], "fyi": []}

    bd._update_slinger_history(run_a)
    bd._update_slinger_history(run_b)
    # Re-run with the same data; the history should not grow or churn.
    h_final = bd._update_slinger_history(run_b)
    assert [x["last_run"] for x in h_final] == ["2026-09-15", "2026-09-14"]


def test_update_slinger_history_returns_existing_when_slinger_missing(tmp_path, monkeypatch):
    """A dashboard rebuild triggered by (say) Theo, with no new Slinger
    data, must return the on-disk history unchanged -- otherwise the
    Slinger card loses its previous run whenever another agent runs."""
    bd = _load_build_dashboard()
    monkeypatch.setattr(bd, "DATA_DIR", tmp_path)
    (tmp_path / "slinger_history.json").write_text(
        '[{"last_run": "2026-09-15", "silence": false, '
        '"high_impact": [{"ticker": "LCC"}], "material": [], "fyi": []}]'
    )
    h = bd._update_slinger_history({})
    assert len(h) == 1
    assert h[0]["last_run"] == "2026-09-15"
