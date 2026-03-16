# tests/test_renderer.py
#
# Tests for master_engine/renderer.py

from __future__ import annotations

import datetime as dt
import json
import tempfile
from pathlib import Path

from master_engine.renderer import build_html, build_markdown, build_json_archive, write_digest
from master_engine.schemas import InvestorEvent, AGENT_BOB, EVENT_TYPE_EARNINGS_RELEASE, UNIVERSE_PORTFOLIO


def _event(ticker="NHC.AX", priority="CRITICAL", score=90):
    ev = InvestorEvent(
        ticker=ticker,
        company_name="New Hope Corporation",
        agent=AGENT_BOB,
        event_type=EVENT_TYPE_EARNINGS_RELEASE,
        headline=f"{ticker} Half Year Results",
        timestamp=dt.datetime.utcnow().isoformat() + "Z",
        summary="Strong results with increased dividend.",
        action="Read full report",
        source_links={"quote_page": "https://finance.yahoo.com/quote/NHC.AX"},
        universe=UNIVERSE_PORTFOLIO,
    )
    ev.priority = priority
    ev.score = score
    return ev


class TestBuildHtml:
    def test_html_contains_header(self):
        html = build_html([], "2025-06-01")
        assert "JOHNNY MASTER INVESTOR ALERT" in html

    def test_html_contains_run_date(self):
        html = build_html([], "2025-06-01")
        assert "2025-06-01" in html

    def test_html_contains_ticker_and_headline(self):
        events = [_event()]
        html = build_html(events, "2025-06-01")
        assert "NHC.AX" in html
        assert "Half Year Results" in html

    def test_html_is_valid_start(self):
        html = build_html([_event()], "2025-06-01")
        assert html.strip().startswith("<!DOCTYPE html>")

    def test_no_events_shows_no_alerts_message(self):
        html = build_html([], "2025-06-01")
        assert "No alerts" in html

    def test_priority_sections_present(self):
        events = [
            _event(priority="CRITICAL", score=90),
            _event(ticker="BHP.AX", priority="LOW", score=15),
        ]
        html = build_html(events, "2025-06-01")
        assert "CRITICAL" in html
        assert "LOW" in html


class TestBuildMarkdown:
    def test_markdown_header(self):
        md = build_markdown([], "2025-06-01")
        assert "# JOHNNY MASTER INVESTOR ALERT" in md

    def test_markdown_contains_events(self):
        events = [_event()]
        md = build_markdown(events, "2025-06-01")
        assert "NHC.AX" in md
        assert "Half Year Results" in md

    def test_markdown_links_formatted(self):
        events = [_event()]
        md = build_markdown(events, "2025-06-01")
        assert "Yahoo Finance" in md
        assert "https://finance.yahoo.com/quote/NHC.AX" in md

    def test_no_events_message(self):
        md = build_markdown([], "2025-06-01")
        assert "No alerts" in md


class TestBuildJsonArchive:
    def test_json_is_valid(self):
        events = [_event()]
        archive = build_json_archive(events, "2025-06-01")
        data = json.loads(archive)
        assert data["run_date"] == "2025-06-01"
        assert data["total_events"] == 1
        assert len(data["events"]) == 1

    def test_json_event_fields(self):
        events = [_event()]
        archive = build_json_archive(events, "2025-06-01")
        data = json.loads(archive)
        ev = data["events"][0]
        assert ev["ticker"] == "NHC.AX"
        assert ev["priority"] == "CRITICAL"
        assert ev["score"] == 90


class TestWriteDigest:
    def test_writes_three_files(self, tmp_path):
        events = [_event()]
        paths = write_digest(events, tmp_path, run_date="2025-06-01")
        assert "html" in paths
        assert "markdown" in paths
        assert "json" in paths
        for p in paths.values():
            assert p.exists()

    def test_html_file_content(self, tmp_path):
        events = [_event()]
        paths = write_digest(events, tmp_path, run_date="2025-06-01")
        content = paths["html"].read_text(encoding="utf-8")
        assert "JOHNNY MASTER INVESTOR ALERT" in content
