"""Unit tests for sgx_fetch.py's pure helpers.

The Playwright-driven fetch path itself is covered by the sgx_daily.yml
workflow run (integration test). These tests cover the surrounding
logic that can regress silently under a spec change: URL construction
and row normalization.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

# Make the repo root importable when pytest runs from tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sgx_fetch import (  # noqa: E402
    _list_url,
    _normalize_row,
    _sgt_stamp,
    _within_window,
    SGT,
)


class TestListUrl:
    def test_matches_flutter_shape(self):
        """The exact query-param shape Flutter fires (verified in PR #166).
        Any drift here will regress to totalItems=0 or 401."""
        url = _list_url("D05", "20060916_160000", "20260915_180000")
        assert url == (
            "https://api.sgx.com/announcements/v1.1/securitycode"
            "?value=D05"
            "&securityCodeParams=D05"
            "&pagestart=0&pagesize=25"
            "&periodstart=20060916_160000"
            "&periodend=20260915_180000"
            "&exactsearch=true"
        )

    def test_page_size_override(self):
        url = _list_url("U11", "20060916_160000", "20260915_180000", page_size=100)
        assert "pagesize=100" in url


class TestSgtStamp:
    def test_format(self):
        when = dt.datetime(2026, 9, 15, 18, 30, 0, tzinfo=SGT)
        assert _sgt_stamp(when) == "20260915_183000"


class TestNormalizeRow:
    def _sample_row(self, **overrides):
        row = {
            "ref_id": "SG260909OTHRLOH7",
            "id": "HSKCJLYOMOK8QMC4",
            "sub": "ANNC18",
            "cat": "ANNC",
            "category_name": "General Announcement",
            "title": "General Announcement::DBS Categorically Rejects Claim",
            "url": "https://links.sgx.com/1.0.0/corporate-announcements/HSKCJLYOMOK8QMC4/abcd",
            "submission_date": "20260909",
            # 2026-09-09 19:30 SGT = 2026-09-09 11:30 UTC
            "submission_date_time": 1788953400000,
            "issuers": [
                {
                    "stock_code": "D05",
                    "issuer_name": "DBS GROUP HOLDINGS LTD",
                    "security_name": "DBS GROUP HOLDINGS LTD",
                }
            ],
        }
        row.update(overrides)
        return row

    def test_core_ASX_compatible_keys_present(self):
        """Downstream Bob code branches on `exchange`/`ticker`/`date`/
        `title`/`url`. If any of these disappear, the classifier and
        renderer will silently misbehave."""
        item = _normalize_row(self._sample_row())
        for key in ("exchange", "ticker", "date", "time", "title", "url"):
            assert key in item, f"missing ASX-compatible key: {key}"
        assert item["exchange"] == "SGX"
        assert item["ticker"] == "D05"
        assert item["title"].startswith("General Announcement::")
        assert item["url"].startswith("https://links.sgx.com/")

    def test_sgx_extras_kept_alongside(self):
        """SGX classifier will key off sub/cat/category_name/ref_id."""
        item = _normalize_row(self._sample_row())
        assert item["ref_id"] == "SG260909OTHRLOH7"
        assert item["id"] == "HSKCJLYOMOK8QMC4"
        assert item["sub"] == "ANNC18"
        assert item["cat"] == "ANNC"
        assert item["category_name"] == "General Announcement"
        assert item["issuer_name"] == "DBS GROUP HOLDINGS LTD"

    def test_date_and_time_converted_to_sgt(self):
        """SGX submission_date_time is unix millis UTC; we render as SGT
        dd/mm/yyyy to match ASX's shape."""
        item = _normalize_row(self._sample_row())
        assert item["date"] == "09/09/2026"
        # 19:30 SGT
        assert "07:30 PM" in item["time"]

    def test_missing_issuers_survives(self):
        item = _normalize_row(self._sample_row(issuers=[]))
        assert item["ticker"] == ""
        assert item["issuer_name"] == ""

    def test_missing_timestamp_survives(self):
        item = _normalize_row(
            self._sample_row(submission_date_time=None, broadcast_date_time=None)
        )
        assert item["date"] == ""
        assert item["time"] == ""


class TestWithinWindow:
    def _item(self, ts_ms):
        return {"submission_ts_ms": ts_ms}

    def test_no_dates_keeps_everything(self):
        assert _within_window(self._item(1788953400000), None, None) is True
        assert _within_window({"submission_ts_ms": None}, None, None) is True

    def test_from_date_filters_older(self):
        # 2026-09-09 19:30 SGT
        item = self._item(1788953400000)
        # from_date one day earlier -> kept
        assert _within_window(item, dt.date(2026, 9, 8), None) is True
        # from_date one day later -> dropped
        assert _within_window(item, dt.date(2026, 9, 10), None) is False

    def test_to_date_filters_newer(self):
        item = self._item(1788953400000)
        assert _within_window(item, None, dt.date(2026, 9, 10)) is True
        assert _within_window(item, None, dt.date(2026, 9, 8)) is False

    def test_undated_item_kept(self):
        """Undated rows are kept rather than silently dropped -- caller
        can decide whether to skip them."""
        assert _within_window({"submission_ts_ms": None},
                              dt.date(2026, 1, 1), dt.date(2026, 1, 2)) is True
