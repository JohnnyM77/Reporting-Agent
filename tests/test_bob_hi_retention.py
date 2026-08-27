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


# ---------------------------------------------------------------------------
# Analysis pages published to the site
#
# The invariant that matters: a card and the page it links to expire together.
# A card promising "full analysis" and delivering a 404 is worse than a card
# with no link, which is the state this replaced (a /tmp path committed into
# bob.json pointing at a file destroyed with the CI container).
# ---------------------------------------------------------------------------

class TestAnalysisPages:
    def test_page_name_carries_the_date_not_the_mtime(self):
        name = agent.analysis_page_name("ABB", "FY26", TODAY)
        assert name == "ABB-FY26-2026-08-24.html"

    def test_page_name_survives_a_missing_period(self):
        assert agent.analysis_page_name("LAU", "", TODAY) == "LAU-2026-08-24.html"

    def test_page_name_slugs_awkward_periods(self):
        assert agent.analysis_page_name("BHP", "1H FY2026", TODAY) == "BHP-1H-FY2026-2026-08-24.html"

    def test_write_returns_a_relative_url_and_writes_the_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "_analysis_dir", lambda: tmp_path / "analysis")
        url = agent.write_analysis_page(
            "ABB", {"period": "FY26", "full_analysis": "# Heading\n\nBody text."},
            "https://asx/abb", TODAY,
        )
        assert url == "analysis/ABB-FY26-2026-08-24.html"
        written = (tmp_path / "analysis" / "ABB-FY26-2026-08-24.html").read_text()
        assert "Body text." in written

    def test_write_failure_is_not_fatal(self, tmp_path, monkeypatch):
        """Losing the page degrades a card. It must never cost the digest."""
        monkeypatch.setattr(agent, "_analysis_dir", lambda: tmp_path / "analysis")
        monkeypatch.setattr(agent, "build_analysis_doc_html",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert agent.write_analysis_page("ABB", {}, "u", TODAY) is None

    def test_prune_removes_pages_past_the_window_and_keeps_the_rest(self, tmp_path, monkeypatch):
        d = tmp_path / "analysis"; d.mkdir()
        for stamp in ("2026-08-24", "2026-08-23", "2026-08-21"):
            (d / f"ABB-FY26-{stamp}.html").write_text("x")
        monkeypatch.setattr(agent, "_analysis_dir", lambda: d)

        assert agent.prune_analysis_pages(TODAY, 2) == 1
        assert sorted(p.name for p in d.glob("*.html")) == [
            "ABB-FY26-2026-08-23.html", "ABB-FY26-2026-08-24.html",
        ]

    def test_prune_leaves_files_it_does_not_understand(self, tmp_path, monkeypatch):
        d = tmp_path / "analysis"; d.mkdir()
        (d / "index.html").write_text("x")
        (d / "notes.html").write_text("x")
        monkeypatch.setattr(agent, "_analysis_dir", lambda: d)
        assert agent.prune_analysis_pages(TODAY, 2) == 0
        assert len(list(d.glob("*.html"))) == 2

    def test_prune_on_a_missing_directory_is_a_no_op(self, tmp_path, monkeypatch):
        monkeypatch.setattr(agent, "_analysis_dir", lambda: tmp_path / "nope")
        assert agent.prune_analysis_pages(TODAY, 2) == 0

    def test_a_carried_card_keeps_its_page(self, tmp_path, monkeypatch):
        """Yesterday's card is still on the dashboard, so its page must survive
        the same prune."""
        out = tmp_path / "docs" / "data" / "bob.json"
        out.parent.mkdir(parents=True)
        analysis_dir = tmp_path / "docs" / "analysis"; analysis_dir.mkdir()
        (analysis_dir / "ABB-FY26-2026-08-23.html").write_text("x")

        out.write_text(json.dumps({
            "last_run": YESTERDAY,
            "high_impact": [_item("ABB", "u/abb", YESTERDAY,
                                  analysis_url="analysis/ABB-FY26-2026-08-23.html")],
            "material": [], "fyi": [],
        }))
        monkeypatch.setattr(agent, "today_sgt_date", lambda: TODAY)
        monkeypatch.setattr(agent, "_analysis_dir", lambda: analysis_dir)
        monkeypatch.setattr(agent.Path, "resolve", lambda self: tmp_path / "agent.py")

        agent._emit_bob_dashboard_json([], [], [], False)

        carried = json.loads(out.read_text())["high_impact"][0]
        assert carried["analysis_url"] == "analysis/ABB-FY26-2026-08-23.html"
        assert (analysis_dir / "ABB-FY26-2026-08-23.html").exists()

    def test_a_link_whose_page_is_gone_is_dropped_not_published(self, tmp_path, monkeypatch):
        out = tmp_path / "docs" / "data" / "bob.json"
        out.parent.mkdir(parents=True)
        analysis_dir = tmp_path / "docs" / "analysis"; analysis_dir.mkdir()

        out.write_text(json.dumps({
            "last_run": YESTERDAY,
            "high_impact": [_item("ABB", "u/abb", YESTERDAY,
                                  analysis_url="analysis/ABB-FY26-2026-08-23.html")],
            "material": [], "fyi": [],
        }))
        monkeypatch.setattr(agent, "today_sgt_date", lambda: TODAY)
        monkeypatch.setattr(agent, "_analysis_dir", lambda: analysis_dir)
        monkeypatch.setattr(agent.Path, "resolve", lambda self: tmp_path / "agent.py")

        agent._emit_bob_dashboard_json([], [], [], False)
        assert json.loads(out.read_text())["high_impact"][0]["analysis_url"] is None

    def test_dashboard_links_the_page_when_present(self):
        html = build_dashboard._hi_item_card(
            _item("ABB", "u/abb", analysis_url="analysis/ABB-FY26-2026-08-24.html"))
        assert "analysis/ABB-FY26-2026-08-24.html" in html
        assert "Full analysis" in html

    def test_dashboard_shows_no_analysis_link_when_absent(self):
        html = build_dashboard._hi_item_card(_item("ABB", "u/abb"))
        assert "Full analysis" not in html

    def test_an_old_bob_json_still_renders_its_drive_link(self):
        html = build_dashboard._hi_item_card(
            _item("ABB", "u/abb", doc_link="https://docs.google.com/d/x"))
        assert "https://docs.google.com/d/x" in html

    def test_web_page_is_mobile_readable(self, tmp_path, monkeypatch):
        """HTML over PDF is only worth it if it reflows on a phone."""
        monkeypatch.setattr(agent, "_analysis_dir", lambda: tmp_path / "analysis")
        agent.write_analysis_page("ABB", {"period": "FY26"}, "u", TODAY)
        page = (tmp_path / "analysis" / "ABB-FY26-2026-08-24.html").read_text()
        assert 'name="viewport"' in page
        assert page.count("<meta charset") == 1, "charset must not be duplicated"

    def test_the_pdf_and_drive_html_is_left_alone(self):
        """_as_web_page is applied on the way to disk, not in the builder."""
        doc = agent.build_analysis_doc_html("ABB", {"period": "FY26"}, "u")
        assert 'name="viewport"' not in doc
