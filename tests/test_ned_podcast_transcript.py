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


def _stub_ffmpeg(monkeypatch):
    """Skip real ffmpeg calls by copying the source bytes as the "compressed"
    output, and by treating _split_into_chunks as identity."""
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
