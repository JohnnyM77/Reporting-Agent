# tests/test_ned_podcast_transcript.py
#
# Tests for Ned's single-episode podcast transcript fetcher.
#
# Offline throughout: all HTTP calls (iTunes lookup, RSS feed, audio download,
# Whisper API) are stubbed via monkeypatched requests. ffmpeg's involvement
# is bypassed by replacing _compress_for_whisper / _split_into_chunks with
# small file-copy shims, so the tests don't need ffmpeg on PATH.

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ned import podcast_transcript_fetcher as podcast_mod
from ned.youtube_transcript_fetcher import TranscriptError, TranscriptResult


# ---------------------------------------------------------------------------
# URL classification / routing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "https://example.com/audio/ep-42.mp3",
    "https://cdn.example.com/eps/42.m4a?token=abc",
    "https://example.com/audio.ogg",
    "https://example.com/audio.wav#t=10",
])
def test_direct_audio_urls_route_to_direct(url):
    resolved = podcast_mod.resolve_podcast_url(url)
    assert resolved.source_kind == "direct"
    assert resolved.audio_url == url


def test_apple_url_missing_id_raises():
    with pytest.raises(TranscriptError):
        podcast_mod.resolve_podcast_url(
            "https://podcasts.apple.com/us/podcast/the-show"
        )


@pytest.mark.parametrize("bad", ["", "   ", "https://example.com/", "not-a-url", "random string"])
def test_junk_urls_raise(bad):
    with pytest.raises(TranscriptError):
        podcast_mod.resolve_podcast_url(bad)


# ---------------------------------------------------------------------------
# Apple + RSS resolution
# ---------------------------------------------------------------------------
_RSS_XML = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example Show</title>
    <item>
      <title>Newest Episode</title>
      <guid>https://example.com/eps/999</guid>
      <enclosure url="https://cdn.example.com/eps/999.mp3" type="audio/mpeg"/>
    </item>
    <item>
      <title>Target Episode</title>
      <guid>tag:example.com,2024:42</guid>
      <enclosure url="https://cdn.example.com/eps/42.mp3" type="audio/mpeg"/>
    </item>
  </channel>
</rss>
"""


class _FakeResponse:
    def __init__(self, *, status=200, content=b"", json_body=None):
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", "replace") if content else ""
        self._json = json_body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


def test_apple_resolves_via_itunes_and_rss(monkeypatch):
    calls: list[str] = []

    def fake_get(url, params=None, timeout=None, headers=None, stream=False):
        calls.append(url)
        if "itunes.apple.com" in url:
            assert params and params.get("id") == "111"
            return _FakeResponse(json_body={
                "results": [{
                    "collectionName": "Example Show",
                    "feedUrl": "https://example.com/feed.rss",
                }]
            })
        if url == "https://example.com/feed.rss":
            return _FakeResponse(content=_RSS_XML)
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)

    resolved = podcast_mod.resolve_podcast_url(
        "https://podcasts.apple.com/us/podcast/example-show/id111?i=42"
    )
    assert resolved.source_kind == "apple"
    assert resolved.show_title == "Example Show"
    assert resolved.audio_url == "https://cdn.example.com/eps/42.mp3"
    assert resolved.episode_title == "Target Episode"
    # exactly one itunes call, one rss call
    assert any("itunes.apple.com" in c for c in calls)
    assert any("feed.rss" in c for c in calls)


def test_apple_falls_back_to_newest_when_no_match(monkeypatch):
    def fake_get(url, params=None, timeout=None, headers=None, stream=False):
        if "itunes.apple.com" in url:
            return _FakeResponse(json_body={
                "results": [{"feedUrl": "https://example.com/feed.rss",
                             "collectionName": "Example Show"}]
            })
        return _FakeResponse(content=_RSS_XML)

    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)

    # Episode id that doesn't match any guid / URL → falls back to newest item.
    resolved = podcast_mod.resolve_podcast_url(
        "https://podcasts.apple.com/us/podcast/example-show/id111?i=NOMATCH"
    )
    assert resolved.audio_url == "https://cdn.example.com/eps/999.mp3"
    assert resolved.episode_title == "Newest Episode"


def test_bad_rss_xml_raises(monkeypatch):
    def fake_get(url, params=None, timeout=None, headers=None, stream=False):
        if "itunes" in url:
            return _FakeResponse(json_body={
                "results": [{"feedUrl": "https://example.com/feed.rss"}]
            })
        return _FakeResponse(content=b"<not xml")
    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)
    with pytest.raises(TranscriptError):
        podcast_mod.resolve_podcast_url(
            "https://podcasts.apple.com/us/podcast/x/id111?i=1"
        )


def test_itunes_lookup_returns_no_results(monkeypatch):
    def fake_get(url, params=None, timeout=None, headers=None, stream=False):
        return _FakeResponse(json_body={"results": []})
    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)
    with pytest.raises(TranscriptError):
        podcast_mod.resolve_podcast_url(
            "https://podcasts.apple.com/us/podcast/x/id999?i=1"
        )


# ---------------------------------------------------------------------------
# fetch_podcast_transcript end-to-end (stubbed)
# ---------------------------------------------------------------------------
class _StreamingResponse:
    """Enough surface for requests.get(..., stream=True) with iter_content."""

    def __init__(self, payload: bytes, status=200):
        self.status_code = status
        self._payload = payload
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024):
        yield self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_ffmpeg(monkeypatch, duration_seconds: float = 300.0):
    """Skip real ffmpeg/ffprobe calls: copy source bytes as the "compressed"
    output, treat _split_into_chunks as a byte-count split, and report a
    fixed duration (default 5 min — short enough not to trigger duration-
    based chunking unless a test explicitly wants that)."""
    def fake_require_ffmpeg():
        return "/bin/true"

    def fake_compress(src, dst):
        # Copy the raw payload — good enough for size accounting and the
        # transcribe stub, which only cares about the file path.
        dst.write_bytes(src.read_bytes())

    def fake_split(src, out_dir):
        # For chunking coverage: split the file in two by byte count.
        data = src.read_bytes()
        half = len(data) // 2
        a = out_dir / "chunk-000.ogg"
        b = out_dir / "chunk-001.ogg"
        a.write_bytes(data[:half])
        b.write_bytes(data[half:])
        return [a, b]

    monkeypatch.setattr(podcast_mod, "_require_ffmpeg", fake_require_ffmpeg)
    monkeypatch.setattr(podcast_mod, "_compress_for_whisper", fake_compress)
    monkeypatch.setattr(podcast_mod, "_split_into_chunks", fake_split)
    monkeypatch.setattr(podcast_mod, "_audio_duration_seconds", lambda path: duration_seconds)


def _payload(size: int) -> bytes:
    # Real-looking bytes so the >4KB guard passes.
    return b"OggS" + b"\0" * (size - 4)


def test_direct_url_fetch_and_transcribe(monkeypatch):
    _stub_ffmpeg(monkeypatch)
    audio_bytes = _payload(2 * 1024 * 1024)  # 2 MB — well under Whisper limit

    def fake_get(url, params=None, timeout=None, headers=None, stream=False):
        assert stream, "audio download must stream"
        return _StreamingResponse(audio_bytes)

    def fake_transcribe(path):
        assert path.exists()
        return "hello world one two", [
            {"text": "hello world", "start": 0.0, "duration": 1.5},
            {"text": "one two", "start": 1.5, "duration": 1.5},
        ]

    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)
    monkeypatch.setattr(podcast_mod, "_transcribe_one", fake_transcribe)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    res = podcast_mod.fetch_podcast_transcript(
        "https://example.com/audio/ep-42.mp3"
    )
    assert isinstance(res, TranscriptResult)
    assert res.plain_text == "hello world one two"
    # timestamped text has [MM:SS] prefixes
    assert res.timestamped_text.startswith("[00:00] hello world")
    assert "[00:01] one two" in res.timestamped_text
    assert res.is_generated is True
    # Slug uses the URL stem when there's no title
    assert res.video_id == "ep-42"


def test_chunking_kicks_in_and_offsets_timestamps(monkeypatch):
    _stub_ffmpeg(monkeypatch)
    oversize = _payload(podcast_mod._WHISPER_MAX_BYTES + 1024)

    def fake_get(url, **kw):
        return _StreamingResponse(oversize)

    calls: list[Path] = []

    def fake_transcribe(path):
        calls.append(path)
        n = len(calls)
        # Each chunk reports its own 0-based times; the fetcher must offset the
        # second chunk by _CHUNK_SECONDS so downstream times are monotonic.
        return f"chunk{n}", [
            {"text": f"chunk{n} start", "start": 0.0, "duration": 10.0},
            {"text": f"chunk{n} end", "start": 60.0, "duration": 5.0},
        ]

    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)
    monkeypatch.setattr(podcast_mod, "_transcribe_one", fake_transcribe)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    res = podcast_mod.fetch_podcast_transcript("https://example.com/big.mp3")
    assert len(calls) == 2, "should have called transcribe once per chunk"
    # Concatenated text preserves both chunks
    assert "chunk1 start" in res.plain_text and "chunk2 start" in res.plain_text
    # Second chunk's timestamps are advanced by _CHUNK_SECONDS (20*60 = 1200s)
    starts = [s["start"] for s in res.segments]
    assert starts[0] == 0.0
    assert starts[2] >= podcast_mod._CHUNK_SECONDS  # 1200 or later
    # All strictly non-decreasing
    for a, b in zip(starts, starts[1:]):
        assert b >= a


def test_duration_triggers_chunking_even_under_size_cap(monkeypatch):
    """A long-but-small file (a live ~85-minute episode compressed to 15.3 MB,
    comfortably under the 24 MB cap) must still be chunked — size alone isn't
    the trigger, since a single very long Whisper request was what actually
    died on a live run."""
    _stub_ffmpeg(monkeypatch, duration_seconds=podcast_mod._CHUNK_SECONDS + 60)
    small_audio = _payload(2 * 1024 * 1024)  # 2 MB — well under the size cap

    def fake_get(url, **kw):
        return _StreamingResponse(small_audio)

    calls: list[Path] = []

    def fake_transcribe(path):
        calls.append(path)
        return f"chunk{len(calls)}", [
            {"text": f"chunk{len(calls)}", "start": 0.0, "duration": 1.0},
        ]

    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)
    monkeypatch.setattr(podcast_mod, "_transcribe_one", fake_transcribe)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    podcast_mod.fetch_podcast_transcript("https://example.com/long-but-small.mp3")
    assert len(calls) == 2, "duration over the threshold must trigger chunking"


def test_short_file_under_both_thresholds_is_a_single_call(monkeypatch):
    _stub_ffmpeg(monkeypatch, duration_seconds=120.0)  # 2 min, well under threshold
    small_audio = _payload(2 * 1024 * 1024)

    def fake_get(url, **kw):
        return _StreamingResponse(small_audio)

    calls: list[Path] = []

    def fake_transcribe(path):
        calls.append(path)
        return "single call", [{"text": "single call", "start": 0.0, "duration": 1.0}]

    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)
    monkeypatch.setattr(podcast_mod, "_transcribe_one", fake_transcribe)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    podcast_mod.fetch_podcast_transcript("https://example.com/short.mp3")
    assert len(calls) == 1


def test_transcribe_retries_transient_connection_errors_then_succeeds(monkeypatch, tmp_path):
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(_payload(4096 * 2))

    attempts = {"n": 0}

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise podcast_mod.requests.exceptions.ConnectionError(
                "Remote end closed connection without response"
            )

        class _R:
            status_code = 200
            text = ""

            def json(self):
                return {"text": "recovered after retries", "segments": []}

        return _R()

    sleeps: list[float] = []
    monkeypatch.setattr(podcast_mod.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(podcast_mod.requests, "post", fake_post)

    text, segs = podcast_mod._transcribe_one(audio)
    assert attempts["n"] == 3
    assert len(sleeps) == 2, "should back off before each retry, not before the first attempt"
    assert text == "recovered after retries"


def test_transcribe_gives_up_after_max_attempts(monkeypatch, tmp_path):
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(_payload(4096 * 2))

    attempts = {"n": 0}

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        attempts["n"] += 1
        raise podcast_mod.requests.exceptions.ConnectionError("still broken")

    monkeypatch.setattr(podcast_mod.time, "sleep", lambda s: None)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(podcast_mod.requests, "post", fake_post)

    with pytest.raises(TranscriptError) as ei:
        podcast_mod._transcribe_one(audio)
    assert attempts["n"] == podcast_mod._MAX_WHISPER_ATTEMPTS
    assert "attempts" in str(ei.value).lower()
    assert "ConnectionError" in str(ei.value)


def test_transcribe_does_not_retry_http_error_responses(monkeypatch, tmp_path):
    """A 4xx/5xx HTTP response is a real failure (bad key, rejected file) —
    retrying it wastes time and won't change the outcome, so it must not be
    retried the way a network-level exception is."""
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(_payload(4096 * 2))

    attempts = {"n": 0}

    class _R:
        status_code = 500
        text = "server error"

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        attempts["n"] += 1
        return _R()

    monkeypatch.setattr(podcast_mod.time, "sleep", lambda s: pytest.fail("should not sleep/retry on HTTP error"))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(podcast_mod.requests, "post", fake_post)

    with pytest.raises(TranscriptError):
        podcast_mod._transcribe_one(audio)
    assert attempts["n"] == 1


def test_transcribe_raises_when_no_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(_payload(4096 * 2))
    with pytest.raises(TranscriptError) as ei:
        podcast_mod._transcribe_one(audio)
    assert "OPENAI_API_KEY" in str(ei.value)


def test_transcribe_surfaces_http_error(monkeypatch, tmp_path):
    audio = tmp_path / "audio.ogg"
    audio.write_bytes(_payload(4096 * 2))

    class _R:
        status_code = 401
        text = '{"error":{"message":"bad key"}}'

    def fake_post(url, files=None, data=None, headers=None, timeout=None):
        return _R()

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(podcast_mod.requests, "post", fake_post)
    with pytest.raises(TranscriptError) as ei:
        podcast_mod._transcribe_one(audio)
    assert "401" in str(ei.value) and "bad key" in str(ei.value)


def test_download_rejects_tiny_body(monkeypatch, tmp_path):
    def fake_get(url, **kw):
        return _StreamingResponse(b"x")   # < 4 KB
    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)
    with pytest.raises(TranscriptError):
        podcast_mod._download_audio("https://x/a.mp3", tmp_path / "a.bin")


# ---------------------------------------------------------------------------
# main.py routing
# ---------------------------------------------------------------------------
def test_main_routes_podcast_env_var(monkeypatch):
    import ned.main as ned_main
    called = {}
    monkeypatch.setattr(ned_main, "run_podcast_digest",
                        lambda url: called.setdefault("url", url) or 0)
    monkeypatch.setattr(ned_main, "run_scan",
                        lambda: pytest.fail("should not scan"))
    monkeypatch.setattr(ned_main, "run_transcript_digest",
                        lambda url: pytest.fail("should not run YouTube"))
    monkeypatch.delenv("NED_TRANSCRIPT_URL", raising=False)
    monkeypatch.setenv("NED_PODCAST_URL", "https://example.com/audio/ep.mp3")
    ned_main.main([])
    assert called["url"] == "https://example.com/audio/ep.mp3"


def test_main_prefers_transcript_over_podcast(monkeypatch):
    """If both env vars are set, the YouTube path wins — matches CLI order."""
    import ned.main as ned_main
    called = {}
    monkeypatch.setattr(ned_main, "run_transcript_digest",
                        lambda url: called.setdefault("kind", "yt") or 0)
    monkeypatch.setattr(ned_main, "run_podcast_digest",
                        lambda url: called.setdefault("kind", "pod") or 0)
    monkeypatch.setattr(ned_main, "run_scan",
                        lambda: pytest.fail("should not scan"))
    monkeypatch.setenv("NED_TRANSCRIPT_URL", "https://youtu.be/abc")
    monkeypatch.setenv("NED_PODCAST_URL", "https://example.com/a.mp3")
    ned_main.main([])
    assert called == {"kind": "yt"}


def test_run_podcast_digest_reports_fetch_error(monkeypatch, tmp_path, capsys):
    import ned.main as ned_main
    monkeypatch.setattr(ned_main, "TRANSCRIPTS_DIR", tmp_path)
    for k in ("EMAIL_FROM", "EMAIL_TO", "EMAIL_APP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)

    def _boom(url):
        raise TranscriptError("Apple returned nothing.")

    monkeypatch.setattr(ned_main, "fetch_podcast_transcript", _boom)
    rc = ned_main.run_podcast_digest("https://podcasts.apple.com/x/id1?i=1")
    assert rc == 1
    assert "apple returned nothing" in capsys.readouterr().out.lower()


# ---------------------------------------------------------------------------
# Published <podcast:transcript> — the free path
# ---------------------------------------------------------------------------
_RSS_WITH_TRANSCRIPT_XML = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <title>Example Show</title>
    <item>
      <title>Newest Episode</title>
      <guid>https://example.com/eps/42</guid>
      <enclosure url="https://cdn.example.com/eps/42.mp3" type="audio/mpeg"/>
      <podcast:transcript url="https://cdn.example.com/eps/42.vtt" type="text/vtt"/>
      <podcast:transcript url="https://cdn.example.com/eps/42.srt" type="application/srt"/>
    </item>
  </channel>
</rss>
"""


def test_rss_extracts_podcast_transcript_tag(monkeypatch):
    """The RSS parser should surface the <podcast:transcript> tag and prefer
    VTT over SRT when both are declared."""
    def fake_get(url, params=None, timeout=None, headers=None, stream=False):
        if "itunes.apple.com" in url:
            return _FakeResponse(json_body={"results": [{
                "feedUrl": "https://example.com/feed.rss",
                "collectionName": "Example Show",
            }]})
        return _FakeResponse(content=_RSS_WITH_TRANSCRIPT_XML)

    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)
    resolved = podcast_mod.resolve_podcast_url(
        "https://podcasts.apple.com/us/podcast/x/id111?i=42"
    )
    assert resolved.transcript_url == "https://cdn.example.com/eps/42.vtt"
    assert resolved.transcript_type == "text/vtt"


def test_rss_transcript_absent_when_no_tag(monkeypatch):
    """The pre-existing RSS (no podcast:transcript) leaves both fields empty
    so downstream code takes the Whisper fallback."""
    def fake_get(url, params=None, timeout=None, headers=None, stream=False):
        if "itunes.apple.com" in url:
            return _FakeResponse(json_body={"results": [{
                "feedUrl": "https://example.com/feed.rss",
            }]})
        return _FakeResponse(content=_RSS_XML)

    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)
    resolved = podcast_mod.resolve_podcast_url(
        "https://podcasts.apple.com/us/podcast/x/id111?i=42"
    )
    assert resolved.transcript_url == ""
    assert resolved.transcript_type == ""


def test_pick_transcript_priority_plain_over_vtt_over_srt():
    """Plain text wins over VTT wins over SRT, regardless of order in the XML."""
    xml = """<item xmlns:podcast="https://podcastindex.org/namespace/1.0">
      <podcast:transcript url="https://x/1.srt" type="application/srt"/>
      <podcast:transcript url="https://x/2.vtt" type="text/vtt"/>
      <podcast:transcript url="https://x/3.txt" type="text/plain"/>
    </item>"""
    import xml.etree.ElementTree as _ET
    item = _ET.fromstring(xml)
    turl, ttype = podcast_mod._pick_transcript(item)
    assert ttype == "text/plain"
    assert turl == "https://x/3.txt"


# ---------------------------------------------------------------------------
# VTT + SRT parsers
# ---------------------------------------------------------------------------
_VTT_SAMPLE = """WEBVTT

00:00:00.000 --> 00:00:04.500
<v Bob>Hello world</v>

00:00:04.500 --> 00:00:07.000
This is a test.

00:01:02.500 --> 00:01:05.000
<c.speaker>Second speaker</c> here.
"""


_SRT_SAMPLE = """1
00:00:00,000 --> 00:00:04,500
Hello world

2
00:00:04,500 --> 00:00:07,000
This is a test.

3
00:01:02,500 --> 00:01:05,000
Second speaker here.
"""


def test_parse_vtt_strips_tags_and_reads_timestamps():
    segs = podcast_mod._parse_vtt_or_srt(_VTT_SAMPLE)
    assert len(segs) == 3
    assert segs[0]["text"] == "Hello world"
    assert segs[0]["start"] == 0.0
    assert segs[0]["duration"] == 4.5
    assert segs[1]["text"] == "This is a test."
    assert segs[2]["start"] == 62.5
    assert segs[2]["text"] == "Second speaker here."


def test_parse_srt_reads_comma_separator():
    segs = podcast_mod._parse_vtt_or_srt(_SRT_SAMPLE)
    assert len(segs) == 3
    assert segs[0]["text"] == "Hello world"
    # SRT uses comma as decimal separator — must be handled identically to VTT.
    assert segs[0]["duration"] == 4.5
    assert segs[2]["start"] == 62.5


def test_parse_plain_text_wraps_in_single_segment():
    segs = podcast_mod._parse_plain_text("Hello   world.\n\nSecond   line.")
    assert len(segs) == 1
    assert segs[0]["text"] == "Hello world. Second line."
    assert segs[0]["start"] == 0.0


def test_parse_empty_yields_no_segments():
    assert podcast_mod._parse_vtt_or_srt("") == []
    assert podcast_mod._parse_plain_text("   \n\n  ") == []


# ---------------------------------------------------------------------------
# End-to-end free path
# ---------------------------------------------------------------------------
def test_fetch_uses_published_transcript_and_skips_whisper(monkeypatch):
    """When the RSS carries a transcript, the fetcher must skip audio download,
    ffmpeg, and Whisper entirely — no OpenAI cost, no ffmpeg process."""

    def fake_get(url, params=None, timeout=None, headers=None, stream=False):
        if "itunes.apple.com" in url:
            return _FakeResponse(json_body={"results": [{
                "feedUrl": "https://example.com/feed.rss",
                "collectionName": "Example Show",
            }]})
        if url == "https://example.com/feed.rss":
            return _FakeResponse(content=_RSS_WITH_TRANSCRIPT_XML)
        if url == "https://cdn.example.com/eps/42.vtt":
            return _FakeResponse(content=_VTT_SAMPLE.encode("utf-8"))
        raise AssertionError(
            f"unexpected GET {url!r} — audio download should have been skipped"
        )

    # Wire in "explode if called" stubs for the audio path so a regression is
    # loud rather than silent.
    def _explode(*a, **kw):
        raise AssertionError("Whisper path must not be reached when a published transcript exists")

    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)
    monkeypatch.setattr(podcast_mod, "_download_audio", _explode)
    monkeypatch.setattr(podcast_mod, "_compress_for_whisper", _explode)
    monkeypatch.setattr(podcast_mod, "_transcribe_prepared", _explode)
    monkeypatch.setattr(podcast_mod, "_transcribe_one", _explode)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)   # proof there's no key path

    res = podcast_mod.fetch_podcast_transcript(
        "https://podcasts.apple.com/us/podcast/x/id111?i=42"
    )
    assert res.is_generated is False, "published transcript is not machine output"
    assert "Hello world" in res.plain_text
    assert "This is a test." in res.plain_text
    assert res.timestamped_text.startswith("[00:00] Hello world")


def test_fetch_falls_back_to_whisper_when_transcript_download_fails(monkeypatch):
    """A published transcript whose download 500s must fall back cleanly to
    the audio path rather than propagate the HTTP error."""
    _stub_ffmpeg(monkeypatch, duration_seconds=60.0)

    def fake_get(url, params=None, timeout=None, headers=None, stream=False):
        if "itunes.apple.com" in url:
            return _FakeResponse(json_body={"results": [{
                "feedUrl": "https://example.com/feed.rss",
                "collectionName": "Example Show",
            }]})
        if url == "https://example.com/feed.rss":
            return _FakeResponse(content=_RSS_WITH_TRANSCRIPT_XML)
        if url == "https://cdn.example.com/eps/42.vtt":
            return _FakeResponse(status=500, content=b"error")
        # audio download
        return _StreamingResponse(_payload(2 * 1024 * 1024))

    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)
    monkeypatch.setattr(
        podcast_mod, "_transcribe_one",
        lambda p: ("whisper output", [{"text": "whisper output", "start": 0.0, "duration": 1.0}]),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    res = podcast_mod.fetch_podcast_transcript(
        "https://podcasts.apple.com/us/podcast/x/id111?i=42"
    )
    assert res.is_generated is True, "fallback was Whisper output"
    assert res.plain_text == "whisper output"


def test_fetch_falls_back_when_published_transcript_type_is_unsupported(monkeypatch):
    """A feed that publishes a transcript in an unknown type (e.g. JSON) must
    fall back to Whisper rather than error."""
    xml = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <item>
      <enclosure url="https://cdn.example.com/eps/42.mp3" type="audio/mpeg"/>
      <podcast:transcript url="https://cdn.example.com/eps/42.json" type="application/json"/>
    </item>
  </channel>
</rss>"""
    _stub_ffmpeg(monkeypatch, duration_seconds=60.0)

    def fake_get(url, params=None, timeout=None, headers=None, stream=False):
        if "itunes.apple.com" in url:
            return _FakeResponse(json_body={"results": [{"feedUrl": "https://example.com/feed.rss"}]})
        if url == "https://example.com/feed.rss":
            return _FakeResponse(content=xml)
        if url.endswith(".json"):
            return _FakeResponse(content=b'{"segments":[]}')
        return _StreamingResponse(_payload(2 * 1024 * 1024))

    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)
    monkeypatch.setattr(
        podcast_mod, "_transcribe_one",
        lambda p: ("whisper output", [{"text": "whisper output", "start": 0.0, "duration": 1.0}]),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    res = podcast_mod.fetch_podcast_transcript(
        "https://podcasts.apple.com/us/podcast/x/id111?i=42"
    )
    assert res.is_generated is True


def test_fetch_sniffs_type_when_rss_declares_none(monkeypatch):
    """A <podcast:transcript> without a type= attribute is still usable: the
    body's first line ('WEBVTT') identifies it as VTT, so the fetcher parses
    it and skips Whisper."""
    xml = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:podcast="https://podcastindex.org/namespace/1.0">
  <channel>
    <item>
      <enclosure url="https://cdn.example.com/eps/42.mp3" type="audio/mpeg"/>
      <podcast:transcript url="https://cdn.example.com/eps/42.txt"/>
    </item>
  </channel>
</rss>"""

    def fake_get(url, params=None, timeout=None, headers=None, stream=False):
        if "itunes.apple.com" in url:
            return _FakeResponse(json_body={"results": [{"feedUrl": "https://example.com/feed.rss"}]})
        if url == "https://example.com/feed.rss":
            return _FakeResponse(content=xml)
        if url.endswith(".txt"):
            return _FakeResponse(content=_VTT_SAMPLE.encode("utf-8"))
        raise AssertionError("no fallback expected")

    monkeypatch.setattr(podcast_mod.requests, "get", fake_get)
    monkeypatch.setattr(podcast_mod, "_download_audio",
                        lambda *a, **kw: pytest.fail("must not download audio"))
    monkeypatch.setattr(podcast_mod, "_transcribe_one",
                        lambda *a, **kw: pytest.fail("must not call Whisper"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    res = podcast_mod.fetch_podcast_transcript(
        "https://podcasts.apple.com/us/podcast/x/id111?i=42"
    )
    assert res.is_generated is False
    assert "Hello world" in res.plain_text
