# tests/test_ned_youtube_transcript.py
#
# Tests for Ned's single-episode YouTube transcript fetcher.
#
# The live YouTube caption endpoint is never hit here — a fake API object is
# injected so URL parsing, manual-vs-generated selection, error handling, text
# cleaning and the end-to-end save/digest wiring are all exercised offline.

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled

from ned.youtube_transcript_fetcher import (
    TranscriptError,
    TranscriptResult,
    _clean_plain_text,
    _format_timestamp,
    _timestamped_text,
    extract_video_id,
    fetch_transcript,
)


# ---------------------------------------------------------------------------
# extract_video_id
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=oay6t8vh7b4",
        "https://youtube.com/watch?v=oay6t8vh7b4",
        "https://m.youtube.com/watch?v=oay6t8vh7b4",
        "https://www.youtube.com/watch?v=oay6t8vh7b4&t=42s&list=abc",
        "https://youtu.be/oay6t8vh7b4",
        "https://youtu.be/oay6t8vh7b4?t=120",
        "https://www.youtube.com/shorts/oay6t8vh7b4",
        "https://www.youtube.com/embed/oay6t8vh7b4",
        "https://www.youtube.com/live/oay6t8vh7b4",
        "youtu.be/oay6t8vh7b4",  # no scheme
        "oay6t8vh7b4",           # bare id
    ],
)
def test_extract_video_id_common_formats(url):
    assert extract_video_id(url) == "oay6t8vh7b4"


@pytest.mark.parametrize("bad", ["", "   ", "https://example.com/foo", "not a url", "https://www.youtube.com/"])
def test_extract_video_id_rejects_junk(bad):
    with pytest.raises(TranscriptError):
        extract_video_id(bad)


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------
def test_clean_plain_text_strips_whitespace_and_joins():
    segs = [
        {"text": "Hello   there", "start": 0.0},
        {"text": "world\n", "start": 2.0},
        {"text": "  again ", "start": 4.0},
    ]
    assert _clean_plain_text(segs) == "Hello there world again"


def test_timestamped_text_format():
    segs = [
        {"text": "start", "start": 0.0},
        {"text": "later", "start": 65.0},
        {"text": "hours", "start": 3725.0},
    ]
    out = _timestamped_text(segs).splitlines()
    assert out[0] == "[00:00] start"
    assert out[1] == "[01:05] later"
    assert out[2] == "[1:02:05] hours"


def test_format_timestamp():
    assert _format_timestamp(0) == "00:00"
    assert _format_timestamp(9) == "00:09"
    assert _format_timestamp(75) == "01:15"
    assert _format_timestamp(3661) == "1:01:01"


# ---------------------------------------------------------------------------
# fetch_transcript with a fake API
# ---------------------------------------------------------------------------
class _FakeTranscript:
    def __init__(self, segments, language="English", code="en", generated=False):
        self._segments = segments
        self.language = language
        self.language_code = code
        self.is_generated = generated

    def fetch(self):
        return list(self._segments)  # a plain list of dicts (no to_raw_data)


class _FakeList:
    """Mimics youtube_transcript_api's TranscriptList lookups."""

    def __init__(self, manual=None, generated=None):
        self._manual = manual
        self._generated = generated

    def find_manually_created_transcript(self, langs):
        if self._manual is None:
            raise NoTranscriptFound("vid", langs, [])
        return self._manual

    def find_generated_transcript(self, langs):
        if self._generated is None:
            raise NoTranscriptFound("vid", langs, [])
        return self._generated

    def find_transcript(self, langs):
        t = self._manual or self._generated
        if t is None:
            raise NoTranscriptFound("vid", langs, [])
        return t


class _FakeApi:
    def __init__(self, transcript_list=None, list_exc=None):
        self._list = transcript_list
        self._list_exc = list_exc

    def list(self, video_id):
        if self._list_exc is not None:
            raise self._list_exc
        return self._list


def test_fetch_prefers_manual_captions():
    manual = _FakeTranscript(
        [{"text": "manual one", "start": 0.0}, {"text": "manual two", "start": 3.0}],
        generated=False,
    )
    generated = _FakeTranscript([{"text": "auto", "start": 0.0}], generated=True)
    api = _FakeApi(_FakeList(manual=manual, generated=generated))

    res = fetch_transcript("vid", api=api)
    assert isinstance(res, TranscriptResult)
    assert res.is_generated is False
    assert res.caption_kind == "manual"
    assert res.plain_text == "manual one manual two"
    assert res.timestamped_text.startswith("[00:00] manual one")


def test_fetch_falls_back_to_generated_captions():
    generated = _FakeTranscript([{"text": "auto only", "start": 1.0}], generated=True)
    api = _FakeApi(_FakeList(manual=None, generated=generated))

    res = fetch_transcript("vid", api=api)
    assert res.is_generated is True
    assert res.caption_kind == "auto-generated"
    assert res.plain_text == "auto only"


def test_fetch_raises_clear_error_when_disabled():
    api = _FakeApi(list_exc=TranscriptsDisabled("vid"))
    with pytest.raises(TranscriptError) as ei:
        fetch_transcript("vid", api=api)
    assert "disabled" in str(ei.value).lower()


def test_fetch_raises_clear_error_when_none_found():
    api = _FakeApi(_FakeList(manual=None, generated=None))
    with pytest.raises(TranscriptError) as ei:
        fetch_transcript("vid", api=api)
    assert "no transcript" in str(ei.value).lower()


def test_fetch_raises_clear_error_on_network_failure():
    api = _FakeApi(list_exc=RuntimeError("proxy blocked"))
    with pytest.raises(TranscriptError) as ei:
        fetch_transcript("vid", api=api)
    assert "could not retrieve" in str(ei.value).lower()


def test_fetch_handles_to_raw_data_objects():
    class _Fetched:
        def to_raw_data(self):
            return [{"text": "raw", "start": 0.0, "duration": 1.0}]

    class _T(_FakeTranscript):
        def fetch(self):
            return _Fetched()

    manual = _T([], generated=False)
    res = fetch_transcript("vid", api=_FakeApi(_FakeList(manual=manual)))
    assert res.plain_text == "raw"


# ---------------------------------------------------------------------------
# end-to-end pipeline (no network, no email)
# ---------------------------------------------------------------------------
def test_run_transcript_digest_saves_files_and_reports(monkeypatch, tmp_path, capsys):
    import ned.main as ned_main

    monkeypatch.setattr(ned_main, "TRANSCRIPTS_DIR", tmp_path)
    # Isolate the dashboard history file too — a successful run appends to it
    # and would otherwise write into the real repo's docs/data/ on every test
    # run.
    monkeypatch.setattr(
        ned_main, "_TRANSCRIPTS_HISTORY_PATH",
        tmp_path / "docs" / "data" / "transcripts.json",
    )
    # No email env, no LLM key -> prints digest, no crash.
    for k in ("EMAIL_FROM", "EMAIL_TO", "EMAIL_APP_PASSWORD", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    fake_result = TranscriptResult(
        video_id="oay6t8vh7b4",
        language="English",
        language_code="en",
        is_generated=False,
        plain_text="clean transcript body",
        timestamped_text="[00:00] clean transcript body",
        segments=[{"text": "clean transcript body", "start": 0.0}],
    )
    monkeypatch.setattr(ned_main, "fetch_transcript", lambda vid, **kw: fake_result)

    rc = ned_main.run_transcript_digest("https://www.youtube.com/watch?v=oay6t8vh7b4")
    assert rc == 0

    saved = list(tmp_path.glob("oay6t8vh7b4_*.txt"))
    assert any(p.name.endswith(".timestamped.txt") for p in saved)
    assert any(not p.name.endswith(".timestamped.txt") for p in saved)
    plain_file = [p for p in saved if not p.name.endswith(".timestamped.txt")][0]
    assert plain_file.read_text(encoding="utf-8") == "clean transcript body"

    out = capsys.readouterr().out
    assert "Saved plain transcript" in out
    assert "Video ID: oay6t8vh7b4" in out


def test_run_transcript_digest_handles_fetch_error(monkeypatch, tmp_path, capsys):
    import ned.main as ned_main

    monkeypatch.setattr(ned_main, "TRANSCRIPTS_DIR", tmp_path)
    for k in ("EMAIL_FROM", "EMAIL_TO", "EMAIL_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)

    def _boom(vid, **kw):
        raise TranscriptError("Captions are disabled for this video.")

    monkeypatch.setattr(ned_main, "fetch_transcript", _boom)

    rc = ned_main.run_transcript_digest("https://youtu.be/oay6t8vh7b4")
    assert rc == 1
    assert "disabled" in capsys.readouterr().out.lower()


def test_main_routes_env_var_to_transcript(monkeypatch):
    import ned.main as ned_main

    called = {}
    monkeypatch.setattr(ned_main, "run_transcript_digest", lambda url: called.setdefault("url", url) or 0)
    monkeypatch.setattr(ned_main, "run_scan", lambda: pytest.fail("should not scan"))
    monkeypatch.setenv("NED_TRANSCRIPT_URL", "https://youtu.be/oay6t8vh7b4")

    ned_main.main([])
    assert called["url"] == "https://youtu.be/oay6t8vh7b4"


# ---------------------------------------------------------------------------
# transcripts.json persistence (feeds the dashboard section)
# ---------------------------------------------------------------------------
def _yt_run_result():
    return TranscriptResult(
        video_id="oay6t8vh7b4",
        language="English",
        language_code="en",
        is_generated=False,
        plain_text="clean transcript body",
        timestamped_text="[00:00] clean transcript body",
        segments=[{"text": "clean transcript body", "start": 0.0}],
    )


def test_youtube_run_appends_transcripts_history(monkeypatch, tmp_path):
    """A successful YouTube run must append one entry to
    docs/data/transcripts.json (the file the dashboard section reads)."""
    import json as _json
    import ned.main as ned_main

    hist_path = tmp_path / "docs" / "data" / "transcripts.json"
    monkeypatch.setattr(ned_main, "_TRANSCRIPTS_HISTORY_PATH", hist_path)
    monkeypatch.setattr(ned_main, "TRANSCRIPTS_DIR", tmp_path)
    for k in ("EMAIL_FROM", "EMAIL_TO", "EMAIL_APP_PASSWORD", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(ned_main, "fetch_transcript", lambda vid, **kw: _yt_run_result())

    rc = ned_main.run_transcript_digest("https://www.youtube.com/watch?v=oay6t8vh7b4")
    assert rc == 0
    data = _json.loads(hist_path.read_text())
    assert isinstance(data, list) and len(data) == 1
    e = data[0]
    assert e["kind"] == "youtube"
    assert e["source_url"] == "https://www.youtube.com/watch?v=oay6t8vh7b4"
    assert e["video_id"] == "oay6t8vh7b4"
    assert e["caption_kind"] == "manual"
    assert "timestamp" in e and e["timestamp"].endswith("Z")


def test_transcripts_history_dedupes_and_caps(monkeypatch, tmp_path):
    """Re-running the same episode updates the entry in place, and the file
    is capped at _TRANSCRIPTS_HISTORY_MAX."""
    import json as _json
    import ned.main as ned_main

    hist_path = tmp_path / "docs" / "data" / "transcripts.json"
    monkeypatch.setattr(ned_main, "_TRANSCRIPTS_HISTORY_PATH", hist_path)
    monkeypatch.setattr(ned_main, "_TRANSCRIPTS_HISTORY_MAX", 3)

    # Seed 4 entries — one of them a duplicate of what we're about to append.
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    hist_path.write_text(_json.dumps([
        {"kind": "youtube", "source_url": "https://x/1", "n": 1},
        {"kind": "youtube", "source_url": "https://x/2", "n": 2},
        {"kind": "youtube", "source_url": "https://x/3", "n": 3},
        {"kind": "youtube", "source_url": "https://x/dup", "n": 4},
    ]))

    ned_main._append_transcript_history({
        "kind": "youtube",
        "source_url": "https://x/dup",
        "title": "updated",
    })

    data = _json.loads(hist_path.read_text())
    # Cap is 3, dup was replaced (not appended), newest-first order.
    assert len(data) == 3
    assert data[0]["source_url"] == "https://x/dup"
    assert data[0]["title"] == "updated"
    assert not any(e.get("n") == 4 for e in data), "old dup should have been removed"


def test_transcripts_history_survives_a_corrupt_file(monkeypatch, tmp_path):
    """A malformed docs/data/transcripts.json must not kill the run."""
    import json as _json
    import ned.main as ned_main

    hist_path = tmp_path / "docs" / "data" / "transcripts.json"
    hist_path.parent.mkdir(parents=True, exist_ok=True)
    hist_path.write_text("not valid json { : :")
    monkeypatch.setattr(ned_main, "_TRANSCRIPTS_HISTORY_PATH", hist_path)

    ned_main._append_transcript_history({"kind": "youtube", "source_url": "https://x/1"})
    data = _json.loads(hist_path.read_text())
    assert isinstance(data, list) and len(data) == 1
