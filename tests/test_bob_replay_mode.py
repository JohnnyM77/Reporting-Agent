# tests/test_bob_replay_mode.py
#
# Tests for the replay/test mode added to Bob.
#
# Covers:
#   1. fetch_asx_announcements_replay() — date-range filtering, no 24-hr cutoff
#   2. fetch_asx_announcements_by_ids()  — ID-based filtering
#   3. save_result_artifacts() dir_prefix — replay subdirectory naming
#   4. run_replay() — bypasses 24-hr filter, ignores COMPLETED state, does not
#      update production state by default, collects same-day PDFs, labels output

import json
import sys
import types
import datetime as dt
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub heavy optional dependencies so agent.py can be imported in CI
# ---------------------------------------------------------------------------
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

from agent import (  # noqa: E402
    SEEN_STATE_PATH,
    STATUS_COMPLETED,
    STATUS_FAILED,
    RESULT_ARTIFACTS_DIR,
    announcement_key,
    fetch_asx_announcements_replay,
    fetch_asx_announcements_by_ids,
    save_result_artifacts,
    run_replay,
    load_seen_state,
    mark_state,
    now_sgt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_html_rows(rows_data):
    """Build minimal ASX announcements HTML with the given rows."""
    rows_html = ""
    for date_str, time_str, title, href in rows_data:
        rows_html += (
            f"<tr>"
            f"<td>{date_str}</td><td>{time_str}</td>"
            f'<td><a href="{href}">{title}</a></td>'
            f"</tr>\n"
        )
    return f"<html><body><table>{rows_html}</table></body></html>"


# ---------------------------------------------------------------------------
# 1. fetch_asx_announcements_replay — date-range filtering
# ---------------------------------------------------------------------------

class TestFetchReplay:
    """fetch_asx_announcements_replay filters by calendar date, ignores 24-hr cutoff."""

    def test_returns_items_on_exact_date(self):
        """Items dated exactly on from_date/to_date are included."""
        html = _make_html_rows([
            ("17/03/2026", "09:30 am", "NHC Half Year Results FY26",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072858"),
            ("17/03/2026", "10:00 am", "NHC Investor Presentation",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072859"),
        ])
        session = MagicMock()
        session.get.return_value = MagicMock(text=html, status_code=200)

        target = dt.date(2026, 3, 17)
        results = fetch_asx_announcements_replay(session, "NHC", target, target)

        assert len(results) == 2
        assert all(it["date"] == "17/03/2026" for it in results)

    def test_excludes_items_outside_date_range(self):
        """Items with dates outside the specified range are excluded."""
        html = _make_html_rows([
            ("16/03/2026", "09:30 am", "NHC Old News",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072855"),
            ("17/03/2026", "09:30 am", "NHC Half Year Results FY26",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072858"),
            ("18/03/2026", "09:30 am", "NHC Future News",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072860"),
        ])
        session = MagicMock()
        session.get.return_value = MagicMock(text=html, status_code=200)

        from_d = dt.date(2026, 3, 17)
        to_d = dt.date(2026, 3, 17)
        results = fetch_asx_announcements_replay(session, "NHC", from_d, to_d)

        assert len(results) == 1
        assert results[0]["title"] == "NHC Half Year Results FY26"

    def test_date_range_spanning_multiple_days(self):
        """Items on any date within from_date..to_date (inclusive) are returned."""
        html = _make_html_rows([
            ("17/03/2026", "09:30 am", "Day 1 Announcement",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=00000001"),
            ("18/03/2026", "10:00 am", "Day 2 Announcement",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=00000002"),
            ("19/03/2026", "11:00 am", "Day 3 Outside Range",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=00000003"),
        ])
        session = MagicMock()
        session.get.return_value = MagicMock(text=html, status_code=200)

        results = fetch_asx_announcements_replay(
            session, "NHC", dt.date(2026, 3, 17), dt.date(2026, 3, 18)
        )
        assert len(results) == 2

    def test_bypasses_24hr_cutoff(self):
        """Items from 60 days ago are still returned — no 24-hour restriction."""
        old_date = now_sgt().date() - dt.timedelta(days=60)
        old_str = old_date.strftime("%d/%m/%Y")
        html = _make_html_rows([
            (old_str, "09:30 am", "Old NHC Half Year Results",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=09999999"),
        ])
        session = MagicMock()
        session.get.return_value = MagicMock(text=html, status_code=200)

        results = fetch_asx_announcements_replay(session, "NHC", old_date, old_date)
        assert len(results) == 1, "Replay fetch must return items regardless of age"

    def test_deduplicates_by_url(self):
        """Duplicate URLs are removed."""
        html = _make_html_rows([
            ("17/03/2026", "09:30 am", "NHC Result",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072858"),
            ("17/03/2026", "09:30 am", "NHC Result",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072858"),
        ])
        session = MagicMock()
        session.get.return_value = MagicMock(text=html, status_code=200)

        target = dt.date(2026, 3, 17)
        results = fetch_asx_announcements_replay(session, "NHC", target, target)
        assert len(results) == 1

    def test_returns_empty_list_for_date_with_no_announcements(self):
        """Empty table → empty list."""
        session = MagicMock()
        session.get.return_value = MagicMock(
            text="<html><body><table></table></body></html>", status_code=200
        )
        results = fetch_asx_announcements_replay(
            session, "NHC", dt.date(2026, 3, 17), dt.date(2026, 3, 17)
        )
        assert results == []


# ---------------------------------------------------------------------------
# 2. fetch_asx_announcements_by_ids — explicit ID filtering
# ---------------------------------------------------------------------------

class TestFetchByIds:
    """fetch_asx_announcements_by_ids returns only items whose URL contains a requested idsId."""

    def test_returns_matching_ids(self):
        html = _make_html_rows([
            ("17/03/2026", "09:30 am", "NHC Half Year Results",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072858"),
            ("17/03/2026", "10:00 am", "NHC Presentation",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072859"),
            ("17/03/2026", "11:00 am", "NHC Dividend",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072860"),
        ])
        session = MagicMock()
        session.get.return_value = MagicMock(text=html, status_code=200)

        results = fetch_asx_announcements_by_ids(
            session, "NHC", ["03072858", "03072859"]
        )
        assert len(results) == 2
        urls = [r["url"] for r in results]
        assert any("03072858" in u for u in urls)
        assert any("03072859" in u for u in urls)
        assert not any("03072860" in u for u in urls)

    def test_no_match_returns_empty(self):
        html = _make_html_rows([
            ("17/03/2026", "09:30 am", "NHC Result",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072858"),
        ])
        session = MagicMock()
        session.get.return_value = MagicMock(text=html, status_code=200)

        results = fetch_asx_announcements_by_ids(session, "NHC", ["99999999"])
        assert results == []

    def test_deduplicates_by_url(self):
        html = _make_html_rows([
            ("17/03/2026", "09:30 am", "NHC Result",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072858"),
            ("17/03/2026", "09:30 am", "NHC Result",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072858"),
        ])
        session = MagicMock()
        session.get.return_value = MagicMock(text=html, status_code=200)

        results = fetch_asx_announcements_by_ids(session, "NHC", ["03072858"])
        assert len(results) == 1


# ---------------------------------------------------------------------------
# 3. save_result_artifacts with dir_prefix
# ---------------------------------------------------------------------------

class TestSaveReplayArtifacts:
    """save_result_artifacts with dir_prefix='replay_' writes to replay subdirectory."""

    def _make_pack_items(self):
        return [
            {"title": "NHC Half Year Results", "url": "https://asx.com.au/r1",
             "pdf_url": "https://asx.com.au/r1.pdf", "pdf_bytes": b"%PDF-1.4 test"},
        ]

    def test_replay_prefix_creates_separate_directory(self, tmp_path):
        """Replay artifacts are stored under replay_{date}/ not {date}/."""
        pack_items = self._make_pack_items()
        with patch("agent.RESULT_ARTIFACTS_DIR", tmp_path):
            art_dir = save_result_artifacts(
                "NHC", "17/03/2026", pack_items, "Test analysis", dir_prefix="replay_"
            )

        assert "replay_" in str(art_dir)
        assert art_dir.name == "replay_2026-03-17"

    def test_production_call_without_prefix(self, tmp_path):
        """Default call (no dir_prefix) still uses original {date}/ path."""
        pack_items = self._make_pack_items()
        with patch("agent.RESULT_ARTIFACTS_DIR", tmp_path):
            art_dir = save_result_artifacts(
                "NHC", "17/03/2026", pack_items, "Test analysis"
            )

        assert art_dir.name == "2026-03-17"

    def test_replay_artifacts_are_saved(self, tmp_path):
        """Artifact files are created in the replay directory."""
        pack_items = self._make_pack_items()
        with patch("agent.RESULT_ARTIFACTS_DIR", tmp_path):
            art_dir = save_result_artifacts(
                "NHC", "17/03/2026", pack_items, "Replay analysis text", dir_prefix="replay_"
            )

        assert (art_dir / "claude_analysis.txt").exists()
        assert (art_dir / "pack_metadata.json").exists()
        assert (art_dir / "summary.md").exists()
        assert "Replay analysis text" in (art_dir / "claude_analysis.txt").read_text()


# ---------------------------------------------------------------------------
# 4. run_replay() — integration tests with mocked network & Claude
# ---------------------------------------------------------------------------

class TestRunReplay:
    """run_replay() exercises the full replay pipeline end-to-end with mocks."""

    _ANN_HTML = _make_html_rows([
        ("17/03/2026", "09:30 am", "NHC 1H FY2026 Results",
         "/asx/v2/statistics/displayAnnouncement.do?idsId=03072858"),
        ("17/03/2026", "10:00 am", "NHC Investor Presentation",
         "/asx/v2/statistics/displayAnnouncement.do?idsId=03072859"),
        ("17/03/2026", "11:00 am", "NHC Dividend Announcement",
         "/asx/v2/statistics/displayAnnouncement.do?idsId=03072860"),
    ])

    def _mock_session(self, ann_html=None):
        """Return a mock session that serves announcements HTML then PDF bytes."""
        s = MagicMock()
        ann_resp = MagicMock(text=ann_html or self._ANN_HTML, status_code=200)
        pdf_resp = MagicMock(content=b"%PDF-1.4 mock pdf content", status_code=200)
        pdf_resp.raise_for_status = MagicMock()
        s.get.side_effect = lambda url, **kwargs: (
            ann_resp if "announcements.do" in url else pdf_resp
        )
        return s

    def test_bypass_24hr_filter(self, tmp_path, capsys):
        """run_replay processes items regardless of their age (no 24-hr cutoff)."""
        with (
            patch("agent.http_session", return_value=self._mock_session()),
            patch("agent.RESULT_ARTIFACTS_DIR", tmp_path),
            patch("agent.SEEN_STATE_PATH", tmp_path / "state.json"),
            patch("agent.deep_results_pack_analysis", return_value="Claude mock analysis"),
            patch("agent.strawman_post", return_value="Strawman mock"),
            patch.dict("os.environ", {"GDRIVE_FOLDER_ID": ""}, clear=False),
        ):
            run_replay(
                replay_ticker="NHC",
                from_date=dt.date(2026, 3, 17),
                to_date=dt.date(2026, 3, 17),
            )

        captured = capsys.readouterr()
        assert "NHC" in captured.out
        assert "REPLAY" in captured.out

    def test_ignores_completed_state(self, tmp_path, capsys):
        """run_replay processes items even if they are already COMPLETED in production state."""
        # Pre-populate production state with all three announcements as COMPLETED
        state_path = tmp_path / "state.json"
        prod_state = {}
        for ids_id in ["03072858", "03072859", "03072860"]:
            url = f"https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsId={ids_id}"
            key = announcement_key("NHC", url)
            mark_state(prod_state, key, "NHC", f"NHC item {ids_id}", STATUS_COMPLETED)
        state_path.write_text(json.dumps(prod_state), encoding="utf-8")

        with (
            patch("agent.http_session", return_value=self._mock_session()),
            patch("agent.RESULT_ARTIFACTS_DIR", tmp_path),
            patch("agent.SEEN_STATE_PATH", state_path),
            patch("agent.deep_results_pack_analysis", return_value="Claude mock analysis"),
            patch("agent.strawman_post", return_value="Strawman mock"),
            patch.dict("os.environ", {"GDRIVE_FOLDER_ID": ""}, clear=False),
        ):
            run_replay(
                replay_ticker="NHC",
                from_date=dt.date(2026, 3, 17),
                to_date=dt.date(2026, 3, 17),
            )

        captured = capsys.readouterr()
        # Should still produce output even though all items were COMPLETED
        assert "NHC" in captured.out
        assert "REPLAY" in captured.out

    def test_does_not_update_production_state_by_default(self, tmp_path):
        """By default run_replay must NOT modify the production state file."""
        state_path = tmp_path / "state.json"
        initial_state = {}
        state_path.write_text(json.dumps(initial_state), encoding="utf-8")

        with (
            patch("agent.http_session", return_value=self._mock_session()),
            patch("agent.RESULT_ARTIFACTS_DIR", tmp_path),
            patch("agent.SEEN_STATE_PATH", state_path),
            patch("agent.deep_results_pack_analysis", return_value="Claude mock analysis"),
            patch("agent.strawman_post", return_value="Strawman mock"),
            patch.dict("os.environ", {"GDRIVE_FOLDER_ID": ""}, clear=False),
        ):
            run_replay(
                replay_ticker="NHC",
                from_date=dt.date(2026, 3, 17),
                to_date=dt.date(2026, 3, 17),
                update_production_state=False,
            )

        # State file must be unchanged (still empty)
        final_state = json.loads(state_path.read_text())
        assert final_state == initial_state, (
            "run_replay must not update production state unless update_production_state=True"
        )

    def test_updates_production_state_when_requested(self, tmp_path):
        """When update_production_state=True the production state file is updated."""
        state_path = tmp_path / "state.json"
        state_path.write_text("{}", encoding="utf-8")

        with (
            patch("agent.http_session", return_value=self._mock_session()),
            patch("agent.RESULT_ARTIFACTS_DIR", tmp_path),
            patch("agent.SEEN_STATE_PATH", state_path),
            patch("agent.deep_results_pack_analysis", return_value="Claude mock analysis"),
            patch("agent.strawman_post", return_value="Strawman mock"),
            patch.dict("os.environ", {"GDRIVE_FOLDER_ID": ""}, clear=False),
        ):
            run_replay(
                replay_ticker="NHC",
                from_date=dt.date(2026, 3, 17),
                to_date=dt.date(2026, 3, 17),
                update_production_state=True,
            )

        final_state = json.loads(state_path.read_text())
        assert len(final_state) > 0, "State should be updated when update_production_state=True"

    def test_nhc_collects_all_same_day_pdfs(self, tmp_path, capsys):
        """NHC replay on 2026-03-17 gathers all 3 same-day PDFs into the pack."""
        mock_deep = MagicMock(return_value="Claude analysis result")

        with (
            patch("agent.http_session", return_value=self._mock_session()),
            patch("agent.RESULT_ARTIFACTS_DIR", tmp_path),
            patch("agent.SEEN_STATE_PATH", tmp_path / "state.json"),
            patch("agent.deep_results_pack_analysis", mock_deep),
            patch("agent.strawman_post", return_value="Strawman"),
            patch.dict("os.environ", {"GDRIVE_FOLDER_ID": ""}, clear=False),
        ):
            run_replay(
                replay_ticker="NHC",
                from_date=dt.date(2026, 3, 17),
                to_date=dt.date(2026, 3, 17),
            )

        # deep_results_pack_analysis should have been called with pack_items for all 3 docs
        assert mock_deep.call_count == 1
        _, kwargs = mock_deep.call_args if mock_deep.call_args.kwargs else (mock_deep.call_args.args, {})
        call_args = mock_deep.call_args.args
        ticker_arg = call_args[0]
        pack_arg = call_args[1]
        assert ticker_arg == "NHC"
        assert len(pack_arg) == 3, f"Expected 3 same-day items in pack, got {len(pack_arg)}"

    def test_saves_artifacts_to_replay_directory(self, tmp_path):
        """Artifacts are saved under outputs/results/NHC/replay_{date}/."""
        with (
            patch("agent.http_session", return_value=self._mock_session()),
            patch("agent.RESULT_ARTIFACTS_DIR", tmp_path),
            patch("agent.SEEN_STATE_PATH", tmp_path / "state.json"),
            patch("agent.deep_results_pack_analysis", return_value="Analysis content"),
            patch("agent.strawman_post", return_value="Strawman"),
            patch.dict("os.environ", {"GDRIVE_FOLDER_ID": ""}, clear=False),
        ):
            run_replay(
                replay_ticker="NHC",
                from_date=dt.date(2026, 3, 17),
                to_date=dt.date(2026, 3, 17),
            )

        replay_dir = tmp_path / "NHC" / "replay_2026-03-17"
        assert replay_dir.exists(), f"Expected replay artifact directory {replay_dir}"
        assert (replay_dir / "claude_analysis.txt").exists()

    def test_output_labelled_replay(self, tmp_path, capsys):
        """Output is clearly labelled as REPLAY in both logs and digest text."""
        with (
            patch("agent.http_session", return_value=self._mock_session()),
            patch("agent.RESULT_ARTIFACTS_DIR", tmp_path),
            patch("agent.SEEN_STATE_PATH", tmp_path / "state.json"),
            patch("agent.deep_results_pack_analysis", return_value="Analysis"),
            patch("agent.strawman_post", return_value="Strawman"),
            patch.dict("os.environ", {"GDRIVE_FOLDER_ID": ""}, clear=False),
        ):
            run_replay(
                replay_ticker="NHC",
                from_date=dt.date(2026, 3, 17),
                to_date=dt.date(2026, 3, 17),
            )

        captured = capsys.readouterr()
        assert "REPLAY" in captured.out, "Digest output must contain 'REPLAY' label"
        assert "REPLAY" in captured.err or "REPLAY" in captured.out

    def test_announcement_ids_mode(self, tmp_path, capsys):
        """--announcement-ids fetches exactly those IDs and processes the pack."""
        ids_html = _make_html_rows([
            ("17/03/2026", "09:30 am", "NHC 1H FY2026 Results",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=03072858"),
        ])
        session = MagicMock()
        ann_resp = MagicMock(text=ids_html, status_code=200)
        pdf_resp = MagicMock(content=b"%PDF-1.4 test", status_code=200)
        pdf_resp.raise_for_status = MagicMock()
        session.get.side_effect = lambda url, **kw: (
            ann_resp if "announcements.do" in url else pdf_resp
        )

        with (
            patch("agent.http_session", return_value=session),
            patch("agent.RESULT_ARTIFACTS_DIR", tmp_path),
            patch("agent.SEEN_STATE_PATH", tmp_path / "state.json"),
            patch("agent.deep_results_pack_analysis", return_value="Analysis"),
            patch("agent.strawman_post", return_value="Strawman"),
            patch.dict("os.environ", {"GDRIVE_FOLDER_ID": ""}, clear=False),
        ):
            run_replay(
                replay_ticker="NHC",
                from_date=dt.date(2026, 3, 17),
                to_date=dt.date(2026, 3, 17),
                announcement_ids=["03072858"],
            )

        captured = capsys.readouterr()
        assert "03072858" in captured.out or "NHC" in captured.out

    def test_no_hyfy_trigger_falls_through_to_fyi(self, tmp_path, capsys):
        """Items with no HY/FY trigger title are listed as FYI in replay output."""
        fyi_html = _make_html_rows([
            ("17/03/2026", "09:30 am", "NHC Change of Director Interests",
             "/asx/v2/statistics/displayAnnouncement.do?idsId=09000001"),
        ])
        session = MagicMock()
        session.get.return_value = MagicMock(text=fyi_html, status_code=200)

        with (
            patch("agent.http_session", return_value=session),
            patch("agent.RESULT_ARTIFACTS_DIR", tmp_path),
            patch("agent.SEEN_STATE_PATH", tmp_path / "state.json"),
            patch.dict("os.environ", {"GDRIVE_FOLDER_ID": ""}, clear=False),
        ):
            run_replay(
                replay_ticker="NHC",
                from_date=dt.date(2026, 3, 17),
                to_date=dt.date(2026, 3, 17),
            )

        captured = capsys.readouterr()
        assert "FYI" in captured.out
        assert "REPLAY" in captured.out

    def test_no_announcements_found_exits_gracefully(self, tmp_path, capsys):
        """run_replay handles empty results gracefully."""
        session = MagicMock()
        session.get.return_value = MagicMock(
            text="<html><body><table></table></body></html>", status_code=200
        )

        with (
            patch("agent.http_session", return_value=session),
            patch("agent.RESULT_ARTIFACTS_DIR", tmp_path),
            patch("agent.SEEN_STATE_PATH", tmp_path / "state.json"),
            patch.dict("os.environ", {"GDRIVE_FOLDER_ID": ""}, clear=False),
        ):
            run_replay(
                replay_ticker="NHC",
                from_date=dt.date(2026, 3, 17),
                to_date=dt.date(2026, 3, 17),
            )

        captured = capsys.readouterr()
        # The "no announcements found" message is emitted via log() which prints to stdout
        combined = captured.out + captured.err
        assert "no announcements" in combined.lower(), (
            "Expected 'no announcements found' in log output"
        )


# ---------------------------------------------------------------------------
# 5. _parse_cli_args — CLI argument parsing
# ---------------------------------------------------------------------------

class TestParseCLIArgs:
    """_parse_cli_args correctly parses replay mode arguments."""

    def test_parse_replay_ticker_and_date(self):
        from agent import _parse_cli_args
        with patch("sys.argv", ["agent.py", "--replay-ticker", "NHC", "--replay-date", "2026-03-17"]):
            args = _parse_cli_args()
        assert args.replay_ticker == "NHC"
        assert args.replay_date == "2026-03-17"

    def test_parse_from_to_dates(self):
        from agent import _parse_cli_args
        with patch("sys.argv", ["agent.py", "--replay-ticker", "NHC",
                                 "--from-date", "2026-03-17", "--to-date", "2026-03-18"]):
            args = _parse_cli_args()
        assert args.from_date == "2026-03-17"
        assert args.to_date == "2026-03-18"

    def test_parse_announcement_ids(self):
        from agent import _parse_cli_args
        with patch("sys.argv", ["agent.py", "--replay-ticker", "NHC",
                                 "--announcement-ids", "03072858,03072859"]):
            args = _parse_cli_args()
        assert args.announcement_ids == "03072858,03072859"

    def test_update_production_state_flag(self):
        from agent import _parse_cli_args
        with patch("sys.argv", ["agent.py", "--replay-ticker", "NHC",
                                 "--replay-date", "2026-03-17",
                                 "--update-production-state"]):
            args = _parse_cli_args()
        assert args.update_production_state is True

    def test_defaults_when_no_replay_args(self):
        from agent import _parse_cli_args
        with patch("sys.argv", ["agent.py"]):
            args = _parse_cli_args()
        assert args.replay_ticker == ""
        assert args.replay_date == ""
        assert args.from_date == ""
        assert args.to_date == ""
        assert args.announcement_ids == ""
        assert args.update_production_state is False

    def test_force_flag_still_works(self):
        from agent import _parse_cli_args
        with patch("sys.argv", ["agent.py", "--force"]):
            args = _parse_cli_args()
        assert args.force is True
