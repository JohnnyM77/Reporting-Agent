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
