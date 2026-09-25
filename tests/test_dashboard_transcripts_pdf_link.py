# tests/test_dashboard_transcripts_pdf_link.py
#
# The transcripts dashboard section grew a "Download PDF" link. Make sure
# it renders when the entry carries a pdf_path and quietly disappears when
# the entry doesn't (so the card degrades cleanly when a PDF has been
# pruned or PDF generation failed).

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from dashboard.sections import transcripts as sec


def _entry(pdf_path: str | None = None) -> dict:
    return {
        "timestamp": "2026-09-24T06:00:00Z",
        "kind": "podcast",
        "source_url": "https://podcasts.apple.com/x/id1?i=42",
        "title": "Test Episode",
        "video_id": "test-episode",
        "used_whisper": True,
        "digest_markdown": "1. **TL;DR** — Just a test.\n",
        "chars": 12345,
        "pdf_path": pdf_path if pdf_path is not None else "",
    }


def test_card_renders_download_pdf_link_when_pdf_path_set():
    html = sec._transcript_card(_entry(pdf_path="transcripts/test-episode_20260924-060000.pdf"))
    assert "Download PDF" in html
    assert "href='transcripts/test-episode_20260924-060000.pdf'" in html


def test_card_omits_download_link_when_pdf_path_missing():
    html = sec._transcript_card(_entry(pdf_path=""))
    assert "Download PDF" not in html
    # Card must still show the title / provenance / summary
    assert "Test Episode" in html


def test_card_omits_download_link_when_pdf_path_absent():
    entry = _entry()
    del entry["pdf_path"]
    html = sec._transcript_card(entry)
    assert "Download PDF" not in html


def test_card_html_escapes_pdf_path():
    """A pathological pdf_path shouldn't break the HTML — it's stored in
    JSON that we don't fully trust from a defence-in-depth standpoint,
    even though we author the writes. Confirm the escaping."""
    html = sec._transcript_card(_entry(pdf_path="transcripts/weird & <name>.pdf"))
    assert "<name>" not in html
    assert "&amp;" in html


def test_card_shows_relevance_badge_and_json_tldr():
    entry = _entry()
    entry["portfolio_relevance"] = "High"
    entry["digest_json"] = {"tldr": "Straight from the JSON."}
    html = sec._transcript_card(entry)
    assert "HIGH RELEVANCE" in html
    assert "Straight from the JSON." in html


def test_card_without_relevance_has_no_badge():
    assert "RELEVANCE" not in sec._transcript_card(_entry())
