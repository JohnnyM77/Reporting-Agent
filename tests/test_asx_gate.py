"""ASX consent-gate detection and the list fetch's diagnostics.

The click-through itself was exercised with a real headless Chromium against
a local stand-in for the gate (button and <input> variants); these tests pin
the parts that don't need a browser."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asx_fetch  # noqa: E402

GATE = ('<html><body><h1>Access to this site</h1><p>General Conditions</p>'
        '<form method="post"><input type="submit" value="Agree and proceed"></form></body></html>')
LIST_OLD_ONLY = ('<html><head><link rel="stylesheet" href="/asx/v2/markets/css/normalize.css"></head><body><table>'
                 '<tr><td>01/03/2026 09:00 AM</td><td></td>'
                 '<td><a href="/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=0299">Old notice</a></td></tr>'
                 '</table></body></html>')


def test_gate_detection():
    assert asx_fetch._looks_like_gate(GATE)
    assert asx_fetch._looks_like_gate("<html><body><button>Agree and Proceed</button></body></html>")
    assert not asx_fetch._looks_like_gate(LIST_OLD_ONLY)


def test_pdf_fetcher_treats_a_bare_agree_button_as_the_gate():
    # Load the real file: other test modules put a stub playwright_fetch in
    # sys.modules so agent.py never launches a browser.
    import importlib.util

    spec = importlib.util.spec_from_file_location("real_playwright_fetch", Path(asx_fetch.__file__).with_name("playwright_fetch.py"))
    pf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pf)
    assert pf._looks_like_gate_html("<form><button>Agree and proceed</button></form>")
    assert pf._looks_like_gate_html(GATE)
    assert not pf._looks_like_gate_html(LIST_OLD_ONLY)


def _fetch_with_body(body, capsys):
    session = mock.MagicMock()
    session.get.return_value = mock.MagicMock(text=body, raise_for_status=lambda: None)
    with mock.patch.object(asx_fetch, "_playwright_fetch_v2", return_value=[]):
        items = asx_fetch.fetch_asx_announcements_html(session, "BHP", from_date=dt.date(2026, 9, 20))
    return items, capsys.readouterr().out


def test_quiet_ticker_is_logged_as_quiet_not_unexpected(capsys):
    items, out = _fetch_with_body(LIST_OLD_ONLY, capsys)
    assert items == []
    assert "no announcements in the window for BHP (list page OK)" in out
    assert "Unexpected response" not in out


def test_gate_on_direct_http_is_named(capsys):
    _, out = _fetch_with_body(GATE, capsys)
    assert "ASX consent gate on direct HTTP for BHP" in out
