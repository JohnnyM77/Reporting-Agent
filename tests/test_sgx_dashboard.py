"""Unit tests for sgx_agent's dashboard JSON writer.

The dashboard rendering in scripts/build_dashboard.py reuses Bob's
helpers (_render_analysis_sections + _flatten_metrics), so the shape of
docs/data/slinger.json must match docs/data/bob.json for those helpers
to work. A regression that renames a top-level key or changes the
metric-value dict shape would render Slinger's card as empty on the
site while the JSON file itself still updated -- exactly the kind of
silent break Bob has been bitten by before.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sgx_agent  # noqa: E402


def _item(**kw):
    base = {
        "ticker": "5DD",
        "issuer_name": "MICRO-MECHANICS (HOLDINGS) LTD.",
        "title": "Financial Statements::FY2026",
        "url": "https://links.sgx.com/x",
        "ref_id": "SG26X",
        "sub": "ANNC17",
        "cat": "ANNC",
        "category_name": "Financial Statements and Related",
    }
    base.update(kw)
    return base


def _analysis(**kw):
    base = {
        "period": "FY2026",
        "period_type": "full_year",
        "metrics": {
            "revenue": {"value": "S$75.5m", "change_pct": "+15.8%", "basis": "reported"},
            "underlying_npat": {"value": "S$15.9m", "change_pct": "+28.3%", "basis": "reported"},
        },
        "summary": "Strong FY26.",
        "full_analysis": "## Segment split\n\n- Retail: strong",
    }
    base.update(kw)
    return base


def test_dashboard_shape_matches_bob(tmp_path, monkeypatch):
    monkeypatch.setattr(sgx_agent, "SLINGER_DASHBOARD_JSON", tmp_path / "slinger.json")
    item = _item()
    item["_pdf_sources"] = [
        ("https://links.sgx.com/x/perf.pdf", "Performance Summary"),
        ("https://links.sgx.com/x/press.pdf", "Press Statement"),
    ]
    sgx_agent._write_dashboard_json([
        (item, "RESULTS_HY_FY", _analysis()),
        (_item(ticker="D05", sub="DIVD"), "DIVIDEND", None),
        (_item(ticker="C6L", sub="ANNC18"), "OTHER", None),
    ])
    data = json.loads((tmp_path / "slinger.json").read_text())
    assert set(data.keys()) >= {"last_run", "silence", "high_impact", "material", "fyi"}
    assert isinstance(data["high_impact"], list)
    assert isinstance(data["material"], list)
    assert isinstance(data["fyi"], list)

    hi = data["high_impact"][0]
    # The dashboard's _hi_item_card reads these keys.
    assert hi["ticker"] == "5DD"
    assert hi["type"] == "results"
    assert hi["issuer_name"] == "MICRO-MECHANICS (HOLDINGS) LTD."
    assert hi["url"] == "https://links.sgx.com/x"
    # And Bob's _flatten_metrics reads change_pct + dividend_ordinary.
    assert hi["analysis"]["metrics"]["revenue"]["change_pct"] == "+15.8%"
    # PDF sources are the SGX-specific extra.
    assert len(hi["source_pdfs"]) == 2
    assert hi["source_pdfs"][0]["url"].endswith("perf.pdf")
    assert hi["source_pdfs"][0]["name"] == "Performance Summary"


def test_dividend_goes_to_material(tmp_path, monkeypatch):
    monkeypatch.setattr(sgx_agent, "SLINGER_DASHBOARD_JSON", tmp_path / "slinger.json")
    sgx_agent._write_dashboard_json([
        (_item(ticker="D05", sub="DIVD"), "DIVIDEND", None),
    ])
    data = json.loads((tmp_path / "slinger.json").read_text())
    assert not data["high_impact"]
    assert len(data["material"]) == 1
    assert data["material"][0]["ticker"] == "D05"


def test_other_goes_to_fyi(tmp_path, monkeypatch):
    monkeypatch.setattr(sgx_agent, "SLINGER_DASHBOARD_JSON", tmp_path / "slinger.json")
    sgx_agent._write_dashboard_json([
        (_item(ticker="C6L", sub="ANNC18"), "OTHER", None),
    ])
    data = json.loads((tmp_path / "slinger.json").read_text())
    assert not data["high_impact"]
    assert not data["material"]
    assert len(data["fyi"]) == 1


def test_raw_text_is_stripped(tmp_path, monkeypatch):
    """A parse_error analysis stashes the raw model text under _raw_text
    for the attached PDF; that has no business being echoed into the
    dashboard JSON."""
    monkeypatch.setattr(sgx_agent, "SLINGER_DASHBOARD_JSON", tmp_path / "slinger.json")
    analysis = _analysis()
    analysis["_raw_text"] = "SECRET raw model output"
    sgx_agent._write_dashboard_json([
        (_item(), "RESULTS_HY_FY", analysis),
    ])
    data = json.loads((tmp_path / "slinger.json").read_text())
    assert "_raw_text" not in data["high_impact"][0]["analysis"]
    assert "SECRET" not in json.dumps(data)
