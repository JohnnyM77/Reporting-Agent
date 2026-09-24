"""shared/asx.py: the ASX plumbing the three ASX fetchers share.

The header values pinned here are what each caller sent before the
consolidation; ASX's bot gate is sensitive to them, so a change must be
deliberate."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import asx  # noqa: E402

UA_122 = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
UA_124 = UA_122.replace("122.0.0.0", "124.0.0.0")


def test_urls():
    assert asx.ANNOUNCEMENTS_URL.format(ticker="NHC") == (
        "https://www.asx.com.au/asx/v2/statistics/announcements.do"
        "?asxCode=NHC&by=asxCode&period=M6&timeframe=D")
    assert asx.absolute_url("/asxpdf/x.pdf") == "https://www.asx.com.au/asxpdf/x.pdf"
    assert asx.absolute_url("https://other/x") == "https://other/x"


@pytest.mark.parametrize("url,ids", [
    ("https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=03012345", "03012345"),
    ("https://x/y?IDSID=abc&z=1", "abc"),
    ("https://x/y.pdf", None),
    ("", None),
])
def test_ids_id(url, ids):
    assert asx.extract_ids_id(url) == ids


def test_pdf_url_for_ids_id():
    assert asx.pdf_url_for_ids_id("03012345") == (
        "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=03012345")
    assert asx.pdf_url_for_ids_id(None) is None


@pytest.fixture
def real_requests(monkeypatch):
    """Some test files stub `requests` into sys.modules; use the real one."""
    mod = sys.modules.get("requests")
    if mod is not None and not hasattr(mod, "Session"):
        monkeypatch.delitem(sys.modules, "requests")
    import requests  # noqa: F401


def _headers(session):
    return {k: session.headers[k] for k in ("User-Agent", "Accept", "Referer")}


def test_session_headers_per_caller(real_requests):
    _pw = types.ModuleType("playwright_fetch")
    _pw.fetch_pdf_with_playwright = None
    sys.modules.setdefault("playwright_fetch", _pw)
    import agent
    from results_pack_agent.utils import http_session as rp_session
    from shared.asx_simple_fetcher import _make_session
    from wally.asx_news import _http_session as wally_session

    ref = "https://www.asx.com.au/"
    assert _headers(agent.http_session()) == {"User-Agent": UA_122, "Accept": "application/json, text/html, */*", "Referer": ref}
    assert _headers(rp_session()) == {"User-Agent": UA_122, "Accept": "*/*", "Referer": ref}
    assert _headers(_make_session()) == {"User-Agent": UA_122, "Accept": "text/html,application/xhtml+xml,*/*", "Referer": ref}
    assert _headers(wally_session()) == {"User-Agent": UA_124, "Accept": "*/*", "Referer": ref}


def test_bob_pdf_url_from_item_url():
    _pw = types.ModuleType("playwright_fetch")
    _pw.fetch_pdf_with_playwright = None
    sys.modules.setdefault("playwright_fetch", _pw)
    import agent

    f = agent.asx_pdf_url_from_item_url
    assert f("https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsid=0301") == (
        "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=0301")
    assert f("https://www.asx.com.au/asxpdf/20260101/pdf/abc.pdf") == "https://www.asx.com.au/asxpdf/20260101/pdf/abc.pdf"
    assert f("https://www.asx.com.au/markets/company/NHC") is None
    assert f("") is None
