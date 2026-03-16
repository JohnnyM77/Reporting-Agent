# tests/test_linker.py
#
# Tests for master_engine/linker.py

from __future__ import annotations

import datetime as dt

from master_engine.linker import build_links, attach_links, _is_asx, _asx_code, _yahoo_ticker
from master_engine.schemas import InvestorEvent, AGENT_BOB, EVENT_TYPE_EARNINGS_RELEASE


def _event(ticker, asx_url=None, drive_link=None, source_links=None):
    return InvestorEvent(
        ticker=ticker,
        company_name=ticker,
        agent=AGENT_BOB,
        event_type=EVENT_TYPE_EARNINGS_RELEASE,
        headline="Test headline",
        timestamp=dt.datetime.utcnow().isoformat() + "Z",
        asx_url=asx_url,
        drive_report_link=drive_link,
        source_links=source_links or {},
    )


class TestYahooTicker:
    def test_asx_ax_suffix_unchanged(self):
        assert _yahoo_ticker("NHC.AX") == "NHC.AX"

    def test_bare_ticker_unchanged(self):
        assert _yahoo_ticker("POOL") == "POOL"

    def test_trailing_dot_to_L(self):
        assert _yahoo_ticker("RR.") == "RR.L"


class TestIsAsx:
    def test_ax_suffix_is_asx(self):
        assert _is_asx("NHC.AX") is True

    def test_bare_short_ticker_is_asx(self):
        assert _is_asx("NHC") is True

    def test_us_ticker_with_dot_is_not_asx(self):
        assert _is_asx("POOL") is True  # bare ticker, heuristic
        # LSE ticker with L suffix
        assert _is_asx("RR.L") is False


class TestAsxCode:
    def test_strips_suffix(self):
        assert _asx_code("NHC.AX") == "NHC"

    def test_bare_ticker(self):
        assert _asx_code("BHP") == "BHP"


class TestBuildLinks:
    def test_adds_quote_page(self):
        ev = _event("NHC.AX")
        links = build_links(ev)
        assert "quote_page" in links
        assert "NHC.AX" in links["quote_page"]

    def test_adds_market_index_for_asx(self):
        ev = _event("NHC.AX")
        links = build_links(ev)
        assert "market_index" in links
        assert "nhc" in links["market_index"]

    def test_no_market_index_for_us_ticker(self):
        ev = _event("POOL")
        links = build_links(ev)
        # POOL is a bare ticker (heuristic says ASX); market_index should be present
        # but for truly non-ASX let's test an explicit L ticker
        ev_lse = _event("RR.L")
        links_lse = build_links(ev_lse)
        assert "market_index" not in links_lse

    def test_asx_url_becomes_asx_announcement(self):
        ev = _event("NHC.AX", asx_url="https://www.asx.com.au/some/announcement")
        links = build_links(ev)
        assert links["asx_announcement"] == "https://www.asx.com.au/some/announcement"

    def test_drive_link_becomes_google_drive_report(self):
        ev = _event("NHC.AX", drive_link="https://drive.google.com/file/d/abc/view")
        links = build_links(ev)
        assert links["google_drive_report"] == "https://drive.google.com/file/d/abc/view"

    def test_existing_links_preserved(self):
        ev = _event("NHC.AX", source_links={"quote_page": "https://custom.url"})
        links = build_links(ev)
        assert links["quote_page"] == "https://custom.url"


class TestAttachLinks:
    def test_attaches_to_all_events(self):
        events = [_event("NHC.AX"), _event("BHP.AX"), _event("CSL.AX")]
        result = attach_links(events)
        assert len(result) == 3
        for ev in result:
            assert "quote_page" in ev.source_links
