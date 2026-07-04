# tests/test_wally_asx_news.py
#
# Tests for wally/asx_news.py — significant ASX announcement lookup for
# flagged tickers — and its rendering in the Wally email report.

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub heavy optional dependencies so wally modules can be imported in CI
# ---------------------------------------------------------------------------
for _stub in (
    "anthropic", "openpyxl", "openpyxl.styles", "openpyxl.utils",
    "matplotlib", "matplotlib.pyplot", "matplotlib.figure",
    "matplotlib.axes", "matplotlib.patches", "matplotlib.lines",
    "matplotlib.ticker", "yfinance", "requests", "pandas",
):
    if _stub not in sys.modules:
        sys.modules[_stub] = types.ModuleType(_stub)

# asx_fetch does `from bs4 import BeautifulSoup` at import time
if "bs4" not in sys.modules:
    _bs4 = types.ModuleType("bs4")
    _bs4.BeautifulSoup = MagicMock()
    sys.modules["bs4"] = _bs4

sys.path.insert(0, str(Path(__file__).parent.parent))

from wally.asx_news import (  # noqa: E402
    NewsItem,
    asx_code,
    classify_significance,
    fetch_significant_news,
)


# ---------------------------------------------------------------------------
# A. ASX code extraction
# ---------------------------------------------------------------------------

class TestAsxCode:
    def test_ax_suffix_stripped(self):
        assert asx_code("BHP.AX") == "BHP"
        assert asx_code("360.AX") == "360"

    def test_lowercase_normalised(self):
        assert asx_code("bhp.ax") == "BHP"

    def test_non_asx_returns_none(self):
        assert asx_code("AMZN") is None       # NASDAQ, no suffix
        assert asx_code("2914.T") is None     # Tokyo
        assert asx_code("FFH.TO") is None     # TSX
        assert asx_code("") is None
        assert asx_code(".AX") is None


# ---------------------------------------------------------------------------
# B. Significance classification
# ---------------------------------------------------------------------------

class TestClassifySignificance:
    def test_price_sensitive_always_significant(self):
        assert classify_significance("Some Obscure Update", price_sensitive=True) == "Price sensitive"

    def test_price_sensitive_keeps_keyword_category(self):
        assert classify_significance("Trading Halt", price_sensitive=True) == "Trading halt / suspension"

    def test_price_sensitive_overrides_routine_filter(self):
        # If ASX marks a normally-routine notice price sensitive, keep it.
        assert classify_significance(
            "Change in substantial holding", price_sensitive=True
        ) == "Price sensitive"

    def test_keyword_categories(self):
        cases = {
            "Trading Halt": "Trading halt / suspension",
            "Completion of Institutional Placement": "Capital raising",
            "Entitlement Offer Booklet": "Capital raising",
            "Scheme of Arrangement — Update": "M&A / takeover",
            "FY26 Guidance Update": "Guidance / trading update",
            "Trading Update and Outlook": "Guidance / trading update",
            "Half-Year Results Presentation": "Results",
            "Appendix 4D and Half Year Report": "Results",
            "Quarterly Activities Report": "Results",
            "Dividend/Distribution - BHP": None,  # routine dividend admin notice
            "On-Market Buy-Back": "Dividend / capital return",
            "Resignation of Chief Executive Officer": "Board / executive change",
            "CEO Succession Announcement": "Board / executive change",
            "Response to ASX Price Query": "ASX query",
            "FDA Approval Received": "Contract / regulatory",
        }
        for title, expected in cases.items():
            assert classify_significance(title) == expected, title

    def test_routine_noise_filtered(self):
        for title in (
            "Change of Director's Interest Notice",
            "Appendix 3B",
            "Appendix 3Y",
            "Becoming a substantial holder",
            "Ceasing to be a substantial holder",
            "Change in substantial holding",
            "Notification regarding unquoted securities",
            "Application for quotation of securities - XYZ",
            "Notice of Annual General Meeting",
            "Appointment of Company Secretary",
            "Top 20 holders",
            "Daily Net Tangible Asset Statement",
        ):
            assert classify_significance(title) is None, title

    def test_unmatched_title_not_significant(self):
        assert classify_significance("Investor Webinar Invitation") is None

    def test_empty_title(self):
        assert classify_significance("") is None
        assert classify_significance(None) is None


# ---------------------------------------------------------------------------
# C. fetch_significant_news
# ---------------------------------------------------------------------------

def _raw(title, date="01/06/2026", ps=False, url="https://www.asx.com.au/x.pdf"):
    return {
        "exchange": "ASX", "ticker": "BHP", "date": date, "time": "10:00 am",
        "title": title, "url": url, "price_sensitive": ps,
    }


class TestFetchSignificantNews:
    def test_non_asx_ticker_returns_none_without_fetch(self):
        with patch("asx_fetch.fetch_asx_announcements_html") as mock_fetch:
            assert fetch_significant_news("AMZN") is None
            mock_fetch.assert_not_called()

    def test_filters_and_sorts_newest_first(self):
        raw = [
            _raw("Change of Director's Interest Notice", "20/06/2026"),
            _raw("Trading Halt", "10/06/2026", ps=True, url="https://www.asx.com.au/a.pdf"),
            _raw("FY26 Guidance Update", "18/06/2026", url="https://www.asx.com.au/b.pdf"),
            _raw("Appendix 3B", "17/06/2026"),
        ]
        with patch("asx_fetch.fetch_asx_announcements_html", return_value=raw), \
             patch("wally.asx_news._http_session", return_value=MagicMock()):
            items = fetch_significant_news("BHP.AX")

        assert [i.title for i in items] == ["FY26 Guidance Update", "Trading Halt"]
        assert items[0].category == "Guidance / trading update"
        assert items[1].price_sensitive is True
        assert items[1].category == "Trading halt / suspension"
        assert all(i.ticker == "BHP.AX" for i in items)

    def test_lookback_window_passed_to_fetch(self):
        import datetime as dt
        with patch("asx_fetch.fetch_asx_announcements_html", return_value=[]) as mock_fetch, \
             patch("wally.asx_news._http_session", return_value=MagicMock()):
            fetch_significant_news("BHP.AX", lookback_days=60)

        _, kwargs = mock_fetch.call_args
        window = kwargs["to_date"] - kwargs["from_date"]
        assert window == dt.timedelta(days=60)
        assert kwargs["to_date"] == dt.date.today()

    def test_max_items_cap(self):
        raw = [
            _raw("Trading Halt", f"{d:02d}/06/2026", ps=True, url=f"https://www.asx.com.au/{d}.pdf")
            for d in range(1, 16)
        ]
        with patch("asx_fetch.fetch_asx_announcements_html", return_value=raw), \
             patch("wally.asx_news._http_session", return_value=MagicMock()):
            items = fetch_significant_news("BHP.AX", max_items=5)
        assert len(items) == 5
        # newest first
        assert items[0].date == "15/06/2026"

    def test_fetch_error_returns_empty_list(self):
        with patch("asx_fetch.fetch_asx_announcements_html", side_effect=RuntimeError("boom")), \
             patch("wally.asx_news._http_session", return_value=MagicMock()):
            assert fetch_significant_news("BHP.AX") == []

    def test_empty_window_returns_empty_list(self):
        with patch("asx_fetch.fetch_asx_announcements_html", return_value=[]), \
             patch("wally.asx_news._http_session", return_value=MagicMock()):
            assert fetch_significant_news("BHP.AX") == []


# ---------------------------------------------------------------------------
# D. Email rendering
# ---------------------------------------------------------------------------

class TestEmailRendering:
    def _flagged_row(self, ticker="BHP.AX"):
        from wally.screening import TickerScreenResult
        return TickerScreenResult(
            ticker=ticker, company_name="BHP Group", current_price=40.0,
            low_52w=39.0, high_52w=50.0, distance_to_low_pct=2.6,
            below_high_pct=20.0, flagged=True,
        )

    def _news_item(self):
        return NewsItem(
            ticker="BHP.AX", date="10/06/2026", time="10:00 am",
            title="Trading Halt", url="https://www.asx.com.au/a.pdf",
            category="Trading halt / suspension", price_sensitive=True,
        )

    def test_build_html_includes_news_section(self):
        from wally.email_report import build_html
        row = self._flagged_row()
        html = build_html(
            "Test WL", "2026-07-04", [row], [row], {},
            news={"BHP.AX": [self._news_item()]},
        )
        assert "Significant ASX announcements" in html
        assert "Trading Halt" in html
        assert "https://www.asx.com.au/a.pdf" in html
        assert "PRICE SENSITIVE" in html
        assert "Trading halt / suspension" in html

    def test_build_html_empty_news_shows_none_found(self):
        from wally.email_report import build_html
        row = self._flagged_row()
        html = build_html("Test WL", "2026-07-04", [row], [row], {}, news={"BHP.AX": []})
        assert "Significant ASX announcements" in html
        assert "No significant announcements found" in html

    def test_build_html_non_asx_ticker_has_no_news_section(self):
        from wally.email_report import build_html
        row = self._flagged_row(ticker="AMZN")
        html = build_html("Test WL", "2026-07-04", [row], [row], {}, news={})
        assert "Significant ASX announcements" not in html

    def test_build_html_backwards_compatible_without_news(self):
        from wally.email_report import build_html
        row = self._flagged_row()
        html = build_html("Test WL", "2026-07-04", [row], [row], {})
        assert "BHP.AX" in html

    def test_build_combined_html_includes_news_section(self):
        from wally.email_report import build_combined_html
        row = self._flagged_row()
        html = build_combined_html([
            {
                "watchlist_name": "Test WL",
                "run_date": "2026-07-04",
                "results": [row],
                "flagged": [row],
                "chart_notes": {},
                "inline_pngs": None,
                "news": {"BHP.AX": [self._news_item()]},
            }
        ])
        assert "Significant ASX announcements" in html
        assert "Trading Halt" in html

    def test_build_combined_html_without_news_key(self):
        from wally.email_report import build_combined_html
        row = self._flagged_row()
        html = build_combined_html([
            {
                "watchlist_name": "Test WL",
                "run_date": "2026-07-04",
                "results": [row],
                "flagged": [row],
                "chart_notes": {},
                "inline_pngs": None,
            }
        ])
        assert "BHP.AX" in html
        assert "Significant ASX announcements" not in html
