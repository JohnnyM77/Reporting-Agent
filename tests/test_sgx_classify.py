"""Unit tests for sgx_classify.py.

SGX classification correctness matters because it determines which
announcements get expensive deep LLM analysis vs cheap FYI two-liners.
A regression that reclassifies "Financial Statements" as OTHER would
silently stop the results-card path from ever firing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgx_classify import (  # noqa: E402
    classify_sgx_announcement,
    is_results_announcement,
    is_price_sensitive_category,
)


def _item(**kw):
    """Build an sgx_fetch-shaped row for classification."""
    base = {
        "exchange": "SGX",
        "ticker": "D05",
        "title": "",
        "url": "",
        "ref_id": "",
        "sub": "",
        "cat": "",
        "category_name": "",
    }
    base.update(kw)
    return base


class TestSubCodes:
    def test_annc17_is_results(self):
        """ANNC17 is the SGX code for Financial Statements. Locking it
        prevents a regression that silently stops the deep-analysis path."""
        assert classify_sgx_announcement(_item(sub="ANNC17")) == "RESULTS_HY_FY"

    def test_annc13_is_share_buyback(self):
        """ANNC13 on-market share buyback -- these fire nightly for the
        big banks; must never be classified as HIGH IMPACT."""
        assert classify_sgx_announcement(_item(sub="ANNC13")) == "SHARE_BUYBACK"

    def test_annc15_is_other(self):
        """Employee stock option grants -- OTHER (FYI stream)."""
        assert classify_sgx_announcement(_item(sub="ANNC15")) == "OTHER"

    def test_divd_is_dividend(self):
        assert classify_sgx_announcement(_item(sub="DIVD")) == "DIVIDEND"

    def test_unknown_sub_falls_through_to_title(self):
        """An unknown sub falls to category_name then title -- not silently OTHER."""
        item = _item(sub="ANNC99", title="Some Category::Rights Issue at S$5.00")
        assert classify_sgx_announcement(item) == "CAPITAL_OR_DEBT_RAISE"


class TestCategoryName:
    def test_financial_statements_wins(self):
        assert classify_sgx_announcement(_item(
            category_name="Financial Statements and Related",
        )) == "RESULTS_HY_FY"

    def test_full_year_results_wins(self):
        assert classify_sgx_announcement(_item(
            category_name="Full Year Results",
        )) == "RESULTS_HY_FY"

    def test_cash_dividend_wins(self):
        assert classify_sgx_announcement(_item(
            category_name="Cash Dividend/ Distribution",
        )) == "DIVIDEND"

    def test_share_buy_back_wins(self):
        assert classify_sgx_announcement(_item(
            category_name="Share Buy Back-On Market",
        )) == "SHARE_BUYBACK"


class TestTitleFallback:
    def test_general_announcement_general_falls_to_other(self):
        """A pure 'General Announcement' with no keywords in the body ->
        OTHER. Bob's ASX classifier does the same."""
        item = _item(
            sub="ANNC18",
            category_name="General Announcement",
            title="General Announcement::Change of Company Secretary",
        )
        assert classify_sgx_announcement(item) == "OTHER"

    def test_general_announcement_acquisition_in_title(self):
        item = _item(
            sub="ANNC18",
            category_name="General Announcement",
            title="General Announcement::Proposed Acquisition of X Pte Ltd",
        )
        assert classify_sgx_announcement(item) == "ACQUISITION"

    def test_hard_no_voting_results(self):
        """"Results of AGM" should NOT match results.  Same rule Bob's ASX
        classifier applies -- voting results != financial results."""
        item = _item(
            sub="ANNC18",
            title="Results of Annual General Meeting held on ...",
        )
        assert classify_sgx_announcement(item) == "OTHER"

    def test_hard_no_transcript(self):
        item = _item(sub="ANNC18", title="Investor Call Transcript - 1H FY26")
        assert classify_sgx_announcement(item) == "OTHER"

    def test_singular_result_still_matches(self):
        """BXB bit us on ASX by using 'Result' (singular). SGX titles vary
        too -- verify the regex accepts both."""
        item = _item(
            sub="ANNC18",
            title="General Announcement::Full-Year Result Announcement FY26",
        )
        assert classify_sgx_announcement(item) == "RESULTS_HY_FY"


class TestHelpers:
    def test_is_results_announcement(self):
        assert is_results_announcement(_item(sub="ANNC17")) is True
        assert is_results_announcement(_item(sub="ANNC13")) is False

    def test_is_price_sensitive_category(self):
        assert is_price_sensitive_category(_item(sub="ANNC17")) is True
        assert is_price_sensitive_category(
            _item(category_name="Rights Issue")
        ) is True
        # OTHER / DIVIDEND / SHARE_BUYBACK are not "price-sensitive" per the
        # helper (they're routine); if this changes we probably want an
        # explicit review, hence pinning.
        assert is_price_sensitive_category(_item(sub="ANNC13")) is False
        assert is_price_sensitive_category(_item(sub="DIVD")) is False
        assert is_price_sensitive_category(_item(sub="ANNC15")) is False
