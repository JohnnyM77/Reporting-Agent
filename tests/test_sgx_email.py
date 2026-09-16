"""Unit tests for sgx_email.py -- structural/rendering checks.

The email HTML has constraints that must not regress:
  - No `<style>` blocks, no `class=` attributes, no display:flex/grid
    (Gmail/Outlook strip or mishandle them).
  - Currency prefix `S$` in the results card, never bare `$` (which
    reads as USD).
  - "SGX" badge in the header so a reader can tell it apart from
    Bob's ASX digest at a glance.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgx_email import build_email, build_analysis_pdf_html  # noqa: E402


def _item(**kw):
    base = {
        "exchange": "SGX",
        "ticker": "D05",
        "issuer_name": "DBS GROUP HOLDINGS LTD",
        "title": "General Announcement::Test",
        "url": "https://links.sgx.com/x",
        "ref_id": "SGREF01",
        "date": "15/09/2026",
        "time": "08:00 AM",
        "sub": "ANNC18",
        "cat": "ANNC",
        "category_name": "General Announcement",
    }
    base.update(kw)
    return base


def _analysis(**kw):
    # Keys MUST match what RESULTS_HYFY_PROMPT tells the model to emit:
    # dividend_ordinary + change_pct. An earlier version of this fixture
    # used ordinary_dividend / change, which masked a bug where every SGX
    # dividend row rendered as n/a and the YoY column stayed blank.
    base = {
        "period": "1H FY26",
        "period_type": "half-year",
        "metrics": {
            "revenue": {"value": "S$5,180m", "change_pct": "+8% YoY", "basis": ""},
            "underlying_npat": {"value": "S$2,890m", "change_pct": "+11% YoY", "basis": "underlying"},
            "underlying_eps": {"value": "S$1.02", "change_pct": "+11% YoY", "basis": "underlying"},
            "dividend_ordinary": {"value": "60c", "change_pct": "+5c", "basis": "interim"},
            "operating_cash_flow": {"value": "S$4,120m", "change_pct": "", "basis": ""},
        },
        "summary": "Solid first half; asset quality benign.",
        "full_analysis": (
            "## Segment split\n\n"
            "- Retail: strong\n"
            "- Wholesale: soft\n\n"
            "**Outlook:** management guides to mid-single-digit growth."
        ),
    }
    base.update(kw)
    return base


class TestEmailStructure:
    def test_empty_produces_no_announcements_message(self):
        subject, text, html = build_email([], hours_back=24)
        assert "Bob SG" in subject
        assert "No SGX announcements" in text
        assert "No SGX announcements" in html

    def test_no_style_blocks_or_classes(self):
        """Email-client compatibility rule -- Gmail/Outlook strip <style>
        blocks and mishandle class-based selectors."""
        classified = [
            (_item(), "OTHER", None),
            (_item(sub="ANNC17"), "RESULTS_HY_FY", _analysis()),
        ]
        _, _, html = build_email(classified, hours_back=24)
        assert "<style" not in html, "no <style> blocks -- inline styles only"
        assert " class=" not in html, "no class= attributes -- inline styles only"
        assert "display:flex" not in html and "display: flex" not in html
        assert "display:grid" not in html and "display: grid" not in html

    def test_sgx_badge_in_header(self):
        _, _, html = build_email([], hours_back=24)
        # The header carries a visible "SGX" badge so it's not mistaken
        # for the ASX digest.
        assert ">SGX<" in html or ">SGX &" in html


class TestBucketing:
    def test_results_lands_in_high_impact(self):
        _, text, html = build_email(
            [(_item(sub="ANNC17"), "RESULTS_HY_FY", _analysis())],
            hours_back=24,
        )
        assert "HIGH IMPACT" in text
        assert "HIGH IMPACT" in html

    def test_dividend_lands_in_material(self):
        _, text, html = build_email(
            [(_item(sub="DIVD", title="Cash Dividend/ Distribution::Interim"),
              "DIVIDEND", None)],
            hours_back=24,
        )
        assert "MATERIAL" in text
        assert "MATERIAL" in html

    def test_other_lands_in_fyi(self):
        _, text, html = build_email(
            [(_item(), "OTHER", None)],
            hours_back=24,
        )
        assert "FYI" in text
        assert "FYI" in html


class TestResultsCard:
    def test_card_uses_sgd_prefix(self):
        """Every figure in the SGX results card must be S$-prefixed;
        bare `$` reads as USD and is not acceptable."""
        _, _, html = build_email(
            [(_item(sub="ANNC17"), "RESULTS_HY_FY", _analysis())],
            hours_back=24,
        )
        assert "S$5,180m" in html
        assert "S$2,890m" in html
        # And a card-level label reminding the reader we're in SGD.
        assert "S$" in html
        assert "SGD" in html

    def test_card_shows_metric_labels(self):
        _, _, html = build_email(
            [(_item(sub="ANNC17"), "RESULTS_HY_FY", _analysis())],
            hours_back=24,
        )
        for label in ("Revenue", "Underlying NPAT", "Underlying EPS",
                      "Ordinary dividend", "Operating cash flow"):
            assert label in html, f"missing metric label: {label}"

    def test_card_shows_yoy_change_column(self):
        """Regression pin -- the metric card must render the +% YoY value
        the LLM sends in change_pct. Earlier code read `change`, which
        the prompt never emits, so the YoY column stayed blank for every
        SGX report."""
        _, _, html = build_email(
            [(_item(sub="ANNC17"), "RESULTS_HY_FY", _analysis())],
            hours_back=24,
        )
        assert "+8% YoY" in html
        assert "+11% YoY" in html

    def test_card_renders_dividend_row(self):
        """The dividend row must resolve dividend_ordinary (matches the
        prompt schema). Earlier code read ordinary_dividend, which meant
        the row rendered as `n/a` regardless of what the model returned."""
        _, _, html = build_email(
            [(_item(sub="ANNC17"), "RESULTS_HY_FY", _analysis())],
            hours_back=24,
        )
        # The label appears in every rendering; the value is what proves
        # the correct key was read.
        assert "60c" in html

    def test_email_body_does_not_inline_full_analysis(self):
        """The deep analysis moved out of the email body into an attached
        PDF (user wanted a forwardable file). The body must not contain
        the markdown body content."""
        _, _, html = build_email(
            [(_item(sub="ANNC17"), "RESULTS_HY_FY", _analysis())],
            hours_back=24,
        )
        # These strings only appear inside full_analysis markdown.
        assert "Segment split" not in html
        assert "<strong>Outlook:</strong>" not in html
        # And there's a signpost telling the reader where the analysis is.
        assert "attached as PDF" in html
        # No stray Google Doc link either.
        assert "docs.google.com" not in html

    def test_email_body_renders_source_pdf_links(self):
        """When the item carries _pdf_sources, the card renders each one
        as a clickable link -- source PDFs are LINKED, not attached."""
        item = _item(sub="ANNC17")
        item["_pdf_sources"] = [
            ("https://links.sgx.com/1.0.0/annc/x/perf.pdf", "Performance Summary"),
            ("https://links.sgx.com/1.0.0/annc/x/cfo.pdf", "CFO Presentation"),
        ]
        _, _, html = build_email(
            [(item, "RESULTS_HY_FY", _analysis())],
            hours_back=24,
        )
        assert "Source PDFs" in html
        assert "Performance Summary" in html
        assert "CFO Presentation" in html
        assert "https://links.sgx.com/1.0.0/annc/x/perf.pdf" in html
        assert "https://links.sgx.com/1.0.0/annc/x/cfo.pdf" in html

    def test_email_body_omits_source_pdf_block_when_none(self):
        """No _pdf_sources -> no 'Source PDFs' block (rather than an
        empty section)."""
        _, _, html = build_email(
            [(_item(sub="ANNC17"), "RESULTS_HY_FY", _analysis())],
            hours_back=24,
        )
        assert "Source PDFs" not in html

    def test_no_analysis_falls_back_to_two_liner(self):
        """A results item that failed LLM analysis still renders -- as a
        two-liner in HIGH IMPACT rather than the full card."""
        _, _, html = build_email(
            [(_item(sub="ANNC17", title="Financial Statements::HY26"),
              "RESULTS_HY_FY", None)],
            hours_back=24,
        )
        # Two-liner shape means no metric table.
        assert "Revenue" not in html
        # But the source link still appears.
        assert "links.sgx.com" in html


class TestAnalysisPdfHtml:
    """The standalone HTML that sgx_agent renders to a PDF and attaches
    to the email. This is what the user forwards to friends -- it must
    be self-contained (a complete document) and carry the deep analysis
    the email body deliberately omits."""

    def test_is_a_complete_html_document(self):
        html = build_analysis_pdf_html(_item(sub="ANNC17"), _analysis())
        assert "<!doctype html>" in html.lower()
        assert "<html" in html
        assert "</html>" in html
        # A4 print rule for Chrome print-to-PDF.
        assert "@page" in html
        assert "A4" in html

    def test_carries_the_full_analysis_body(self):
        html = build_analysis_pdf_html(_item(sub="ANNC17"), _analysis())
        # Everything the email body deliberately omits must live here.
        assert "Segment split" in html
        assert "<strong>Outlook:</strong>" in html
        assert "Full analysis" in html

    def test_carries_the_metric_table_and_summary(self):
        html = build_analysis_pdf_html(_item(sub="ANNC17"), _analysis())
        # Metric labels + at least one populated value + the YoY column.
        assert "Underlying NPAT" in html
        assert "S$2,890m" in html
        assert "+11% YoY" in html
        # Summary from the analysis dict.
        assert "Solid first half" in html

    def test_no_analysis_body_renders_placeholder(self):
        analysis = _analysis(full_analysis="")
        html = build_analysis_pdf_html(_item(sub="ANNC17"), analysis)
        assert "No deep analysis available" in html
