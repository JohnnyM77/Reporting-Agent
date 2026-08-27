# tests/test_bob_hi_retention.py
#
# HIGH IMPACT items survive on the dashboard for two days (agent.py,
# BOB_HI_RETENTION_DAYS). bob.json used to be a straight overwrite, so a
# results card — the most expensive thing Bob produces — disappeared the next
# morning whether or not anyone had read it.
#
# The two rules that matter, and the two ways this could go wrong:
#   - a carried item must never present as today's news (the dashboard would
#     be lying about its own date, which is worse than losing the card)
#   - a re-analysis of the same announcement must replace the old copy, not sit
#     beside it as a second result

import datetime as dt
import json
import sys
import types
from pathlib import Path

# Stub heavy optional dependencies so agent.py can be imported in CI
for _stub in (
    "anthropic", "playwright", "playwright.async_api", "googleapiclient",
    "googleapiclient.discovery", "googleapiclient.http",
    "google", "google.oauth2", "google.oauth2.credentials",
    "google.oauth2.service_account", "google.auth",
    "google.auth.transport", "google.auth.transport.requests",
):
    if _stub not in sys.modules:
        sys.modules[_stub] = types.ModuleType(_stub)

_pw_stub = types.ModuleType("playwright_fetch")
_pw_stub.fetch_pdf_with_playwright = None  # type: ignore[attr-defined]
sys.modules.setdefault("playwright_fetch", _pw_stub)

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import build_dashboard  # noqa: E402


TODAY = dt.date(2026, 8, 24)
YESTERDAY = "2026-08-23"
THREE_DAYS_AGO = "2026-08-21"


def _item(ticker, url, run_date=None, **extra):
    item = {"ticker": ticker, "title": f"{ticker} FY26 Results", "url": url,
            "type": "results", "analysis": {"bottom_line": "x"}}
    if run_date:
        item["run_date"] = run_date
    item.update(extra)
    return item


# ---------------------------------------------------------------------------
# Carry-forward selection
# ---------------------------------------------------------------------------

class TestCarryForward:
    def test_yesterday_is_kept(self):
        prev = [_item("ABB", "u/abb", YESTERDAY)]
        carried = agent._carry_forward_high_impact(prev, [], TODAY, 2)
        assert [i["ticker"] for i in carried] == ["ABB"]

    def test_older_than_the_window_is_dropped(self):
        prev = [_item("ABB", "u/abb", THREE_DAYS_AGO)]
        assert agent._carry_forward_high_impact(prev, [], TODAY, 2) == []

    def test_unstamped_items_are_dropped_not_kept_forever(self):
        """No run_date means it predates the feature. Guessing a date would pin
        it to the dashboard permanently."""
        prev = [_item("ABB", "u/abb")]
        assert agent._carry_forward_high_impact(prev, [], TODAY, 2) == []

    def test_reanalysed_announcement_does_not_duplicate(self):
        prev = [_item("ABB", "u/abb", YESTERDAY)]
        todays = [_item("ABB", "u/abb", TODAY.isoformat())]
        assert agent._carry_forward_high_impact(prev, todays, TODAY, 2) == []

    def test_same_ticker_different_announcement_is_kept(self):
        prev = [_item("ABB", "u/abb-results", YESTERDAY)]
        todays = [_item("ABB", "u/abb-guidance", TODAY.isoformat())]
        carried = agent._carry_forward_high_impact(prev, todays, TODAY, 2)
        assert [i["url"] for i in carried] == ["u/abb-results"]

    def test_missing_url_falls_back_to_ticker_and_title(self):
        prev = [_item("ABB", "", YESTERDAY)]
        todays = [_item("ABB", "", TODAY.isoformat())]
        assert agent._carry_forward_high_impact(prev, todays, TODAY, 2) == []

    def test_a_same_day_rerun_keeps_the_earlier_runs_cards(self):
        """A manual afternoon re-run must not wipe the morning's analysis."""
        prev = [_item("ABB", "u/abb", TODAY.isoformat())]
        todays = [_item("CSL", "u/csl", TODAY.isoformat())]
        carried = agent._carry_forward_high_impact(prev, todays, TODAY, 2)
        assert [i["ticker"] for i in carried] == ["ABB"]

    def test_carried_items_are_newest_first(self):
        prev = [_item("A", "u/a", "2026-08-22"), _item("B", "u/b", YESTERDAY)]
        carried = agent._carry_forward_high_impact(prev, [], TODAY, 3)
        assert [i["ticker"] for i in carried] == ["B", "A"]


# ---------------------------------------------------------------------------
# Writing the file
# ---------------------------------------------------------------------------

class TestEmit:
    def test_todays_items_are_stamped_and_previous_day_carried(self, tmp_path, monkeypatch):
        out = tmp_path / "docs" / "data" / "bob.json"
        out.parent.mkdir(parents=True)
        out.write_text(json.dumps({
            "last_run": YESTERDAY,
            "high_impact": [_item("ABB", "u/abb", YESTERDAY)],
            "material": [{"ticker": "BHP"}],
            "fyi": [{"ticker": "CSL"}],
        }))
        monkeypatch.setattr(agent, "today_sgt_date", lambda: TODAY)
        monkeypatch.setattr(agent.Path, "resolve", lambda self: tmp_path / "agent.py")

        agent._emit_bob_dashboard_json([_item("CSL", "u/csl")], [], [], False)

        data = json.loads(out.read_text())
        assert data["last_run"] == TODAY.isoformat()
        stamps = {i["ticker"]: i["run_date"] for i in data["high_impact"]}
        assert stamps == {"CSL": TODAY.isoformat(), "ABB": YESTERDAY}
        # Material and FYI are deliberately one-day only.
        assert data["material"] == [] and data["fyi"] == []

    def test_caller_dicts_are_not_mutated(self, tmp_path, monkeypatch):
        out = tmp_path / "docs" / "data" / "bob.json"
        out.parent.mkdir(parents=True)
        monkeypatch.setattr(agent, "today_sgt_date", lambda: TODAY)
        monkeypatch.setattr(agent.Path, "resolve", lambda self: tmp_path / "agent.py")

        original = _item("CSL", "u/csl")
        agent._emit_bob_dashboard_json([original], [], [], False)
        assert "run_date" not in original

    def test_unreadable_previous_file_does_not_raise(self, tmp_path, monkeypatch):
        out = tmp_path / "docs" / "data" / "bob.json"
        out.parent.mkdir(parents=True)
        out.write_text("{ not json")
        monkeypatch.setattr(agent, "today_sgt_date", lambda: TODAY)
        monkeypatch.setattr(agent.Path, "resolve", lambda self: tmp_path / "agent.py")

        agent._emit_bob_dashboard_json([_item("CSL", "u/csl")], [], [], False)
        assert len(json.loads(out.read_text())["high_impact"]) == 1


# ---------------------------------------------------------------------------
# Rendering — carried must be visibly not-today
# ---------------------------------------------------------------------------

class TestRender:
    DATA = {
        "last_run": TODAY.isoformat(),
        "silence": False,
        "high_impact": [
            _item("CSL", "u/csl", TODAY.isoformat()),
            _item("ABB", "u/abb", YESTERDAY),
        ],
        "material": [], "fyi": [],
    }

    def test_status_counts_today_only(self):
        html = build_dashboard._bob_section(self.DATA)
        assert "1 HIGH IMPACT" in html, "yesterday's card must not inflate today's count"

    def test_carried_items_get_their_own_dated_heading(self):
        html = build_dashboard._bob_section(self.DATA)
        assert "STILL WORTH A LOOK" in html
        assert build_dashboard._fmt_date(YESTERDAY) in html

    def test_carried_cards_are_collapsed_and_todays_are_open(self):
        today_only = build_dashboard._hi_item_card(_item("CSL", "u/csl"))
        carried = build_dashboard._hi_item_card(_item("ABB", "u/abb"), carried=True)
        assert "<details open" in today_only
        assert "<details open" not in carried
        assert "<details" in carried, "carried analysis must still be openable"

    def test_all_cards_render_when_today_is_empty(self):
        data = dict(self.DATA, high_impact=[_item("ABB", "u/abb", YESTERDAY)])
        html = build_dashboard._bob_section(data)
        assert "All clear" in html
        assert "ABB" in html, "a quiet morning must not hide yesterday's card"

    def test_json_without_run_dates_still_renders_as_today(self):
        """An older bob.json predating the stamp must not silently show every
        item under a 'still worth a look' heading."""
        data = dict(self.DATA, high_impact=[_item("CSL", "u/csl")])
        html = build_dashboard._bob_section(data)
        assert "1 HIGH IMPACT" in html
        assert "STILL WORTH A LOOK" not in html
