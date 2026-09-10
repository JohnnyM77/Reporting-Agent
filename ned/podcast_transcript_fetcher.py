# ned/podcast_transcript_fetcher.py
#
# Single-episode podcast transcript fetcher for Ned the News Agent.
#
# Podcasts don't ship with captions, so we have to make our own transcript:
#   1. Resolve the pasted URL to a direct audio URL (Apple Podcasts episode
#      links get resolved via the iTunes lookup API + the podcast's RSS feed;
#      RSS <enclosure> URLs and raw .mp3 links are used as-is).
#   2. Download the audio to a local file.
#   3. Compress it with ffmpeg to 16kHz mono Opus so it fits under Whisper's
#      25 MB per-request cap in one call. If it still doesn't, we split into
#      chunks and transcribe each.
#   4. Transcribe with OpenAI's Whisper API (whisper-1) — no local ML deps.
#   5. Return a TranscriptResult identical in shape to the YouTube fetcher's,
#      so downstream code (digest LLM step, email, save-to-file) is unchanged.
#
# ffmpeg is pre-installed on ubuntu-latest GitHub runners, so no setup is
# needed there. Locally you need it on PATH.

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlparse

import requests

from ned.youtube_transcript_fetcher import TranscriptResult, TranscriptError

# Whisper API accepts files up to 25 MB. We aim for 24 to leave headroom for
# HTTP multipart overhead.
_WHISPER_MAX_BYTES = 24 * 1024 * 1024
# ~55 minutes of speech at 24 kbps Opus mono @ 16 kHz is about 10 MB, so a
# typical hour-long interview fits in a single request after compression.
_COMPRESSED_BITRATE = "24k"
_COMPRESSED_SAMPLE_RATE = "16000"
_CHUNK_SECONDS = 20 * 60  # 20-minute chunks if we have to split
_HTTP_TIMEOUT = 60
_USER_AGENT = "Mozilla/5.0 (Ned; podcast transcript fetcher; +https://github.com/JohnnyM77/Reporting-Agent)"

_ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
# Anything ending in these extensions we treat as a direct audio URL.
_AUDIO_EXT_RE = re.compile(r"\.(mp3|m4a|aac|wav|ogg|opus|flac|mp4)(?:[?#]|$)", re.IGNORECASE)


@dataclass
class ResolvedPodcast:
    """What we managed to resolve from the pasted URL before downloading."""

    audio_url: str
    episode_title: str = ""
    show_title: str = ""
    source_kind: str = "direct"  # "direct" | "apple" | "rss"


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------
def _looks_like_audio_url(url: str) -> bool:
    """True for direct .mp3-style URLs (RSS enclosures land here too)."""
    return bool(_AUDIO_EXT_RE.search(urlparse(url).path))


def _looks_like_apple(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith("apple.com") and "/podcast" in urlparse(url).path.lower()


# ---------------------------------------------------------------------------
# Apple Podcasts resolution
# ---------------------------------------------------------------------------
def _resolve_apple(url: str) -> ResolvedPodcast:
    """Resolve an Apple Podcasts episode link to a direct audio URL.

    Apple URLs look like:
      https://podcasts.apple.com/us/podcast/<slug>/id<PODCAST_ID>?i=<EPISODE_ID>

    Apple's own iTunes lookup API gives us the podcast's RSS feed URL when
    queried by the podcast id; we then walk the RSS feed's <item> nodes until
    we find one whose <itunes:episodeGuid> or trackId matches the episode id
    (or, as a fallback, the newest item).
    """
    parsed = urlparse(url)
    m = re.search(r"/id(\d+)", parsed.path)
    if not m:
        raise TranscriptError(
            "Apple Podcasts URL is missing the numeric podcast id "
            "(expected /id123456789 in the path)."
        )
    podcast_id = m.group(1)
    episode_id = (parse_qs(parsed.query).get("i") or [""])[0]

    # 1. Podcast id -> RSS feed URL (+ show title, as a nicety)
    try:
        resp = requests.get(
            _ITUNES_LOOKUP,
            params={"id": podcast_id, "entity": "podcast"},
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise TranscriptError(
            f"Could not look up Apple podcast id {podcast_id}: "
            f"{type(exc).__name__}: {exc}"
        )
    results = data.get("results") or []
    if not results:
        raise TranscriptError(
            f"Apple's iTunes lookup returned no podcast for id {podcast_id}."
        )
    show = results[0]
    feed_url = show.get("feedUrl")
    show_title = show.get("collectionName") or show.get("trackName") or ""
    if not feed_url:
        raise TranscriptError(
            "Apple returned the podcast but no RSS feed URL — cannot reach "
            "the audio."
        )

    # 2. RSS -> the matching item's enclosure URL
    audio_url, ep_title = _episode_from_rss(feed_url, episode_id)
    return ResolvedPodcast(
        audio_url=audio_url,
        episode_title=ep_title,
        show_title=show_title,
        source_kind="apple",
    )


def _episode_from_rss(feed_url: str, episode_id: str) -> tuple[str, str]:
    """Return (audio_url, title) from a podcast RSS feed.

    Matching order for the requested episode:
      1. item whose <guid> ends with the Apple episode id
      2. item whose enclosure URL contains the episode id
      3. the newest item (feeds are ordered newest-first by convention)
    """
    try:
        resp = requests.get(
            feed_url,
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/rss+xml, application/xml, */*"},
        )
        resp.raise_for_status()
    except Exception as exc:
        raise TranscriptError(
            f"Could not fetch the podcast's RSS feed ({feed_url}): "
            f"{type(exc).__name__}: {exc}"
        )
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise TranscriptError(f"Podcast RSS feed is not valid XML: {exc}")

    # RSS 2.0: <channel><item>...</item></channel>. Feeds vary on namespaces
    # for enclosure and guid, so search broadly.
    items = list(root.iter("item"))
    if not items:
        raise TranscriptError("Podcast RSS feed contains no <item> entries.")

    def _enclosure_url(item: ET.Element) -> str:
        enc = item.find("enclosure")
        if enc is not None and enc.get("url"):
            return enc.get("url", "")
        # Fall back to <link> only when it looks like an audio URL — many feeds
        # put a webpage link there.
        link = item.findtext("link", "").strip()
        return link if _looks_like_audio_url(link) else ""

    def _title(item: ET.Element) -> str:
        return (item.findtext("title") or "").strip()

    # Match by guid or URL-embedded episode id.
    if episode_id:
        for it in items:
            guid = (it.findtext("guid") or "").strip()
            if guid.endswith(episode_id):
                url = _enclosure_url(it)
                if url:
                    return url, _title(it)
        for it in items:
            url = _enclosure_url(it)
            if url and episode_id in url:
                return url, _title(it)

    # Fall back to the newest item with an enclosure.
    for it in items:
        url = _enclosure_url(it)
        if url:
            return url, _title(it)
    raise TranscriptError(
        "Could not find an audio enclosure for that episode in the RSS feed."
    )


# ---------------------------------------------------------------------------
# URL entry point
# ---------------------------------------------------------------------------
def resolve_podcast_url(url: str) -> ResolvedPodcast:
    """Route the pasted URL through the right resolver."""
    if not url or not url.strip():
        raise TranscriptError("No podcast URL was provided.")
    url = url.strip()
    if "://" not in url:
        url = "https://" + url

    if _looks_like_apple(url):
        return _resolve_apple(url)
    if _looks_like_audio_url(url):
        return ResolvedPodcast(audio_url=url, source_kind="direct")
    # RSS feed URL pasted directly (no episode selector) — take the newest.
    if url.lower().endswith((".xml", ".rss")) or "/rss" in url.lower() or "/feed" in url.lower():
        audio_url, ep_title = _episode_from_rss(url, episode_id="")
        return ResolvedPodcast(
            audio_url=audio_url, episode_title=ep_title, source_kind="rss"
        )

    raise TranscriptError(
        f"Don't know how to resolve this URL to a podcast episode: {url!r}. "
        "Paste a direct .mp3/.m4a URL, an Apple Podcasts episode link, or an "
        "RSS feed URL."
    )


# ---------------------------------------------------------------------------
# Audio download + compression + transcription
# ---------------------------------------------------------------------------
def _download_audio(url: str, dest: Path) -> None:
    """Stream the audio to disk. Raises TranscriptError on any HTTP problem."""
    try:
        with requests.get(
            url,
            stream=True,
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT},
        ) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
    except Exception as exc:
        raise TranscriptError(
            f"Could not download audio from {url}: {type(exc).__name__}: {exc}"
        )
    if dest.stat().st_size < 4096:
        raise TranscriptError(
            f"Downloaded audio from {url} is suspiciously small "
            f"({dest.stat().st_size} bytes) — check the URL."
        )


def _require_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise TranscriptError(
            "ffmpeg is not installed / not on PATH. It's needed to compress "
            "the audio for Whisper. On ubuntu-latest runners it's preinstalled; "
            "locally, install it (macOS: `brew install ffmpeg`; Ubuntu: "
            "`apt-get install ffmpeg`; Windows: winget / choco)."
        )
    return path


def _compress_for_whisper(src: Path, dst: Path) -> None:
    """Re-encode to 16 kHz mono Opus so a typical hour fits under 25 MB."""
    ffmpeg = _require_ffmpeg()
    cmd = [
        ffmpeg, "-y", "-i", str(src),
        "-vn",                                # drop any video/artwork stream
        "-ac", "1",                           # mono
        "-ar", _COMPRESSED_SAMPLE_RATE,       # 16 kHz — Whisper's native rate
        "-c:a", "libopus", "-b:a", _COMPRESSED_BITRATE,
        str(dst),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        raise TranscriptError(
            f"ffmpeg failed to compress audio: {exc.stderr.decode('utf-8', 'replace')[-500:]}"
        )


def _split_into_chunks(src: Path, out_dir: Path) -> list[Path]:
    """Split `src` into ~_CHUNK_SECONDS pieces named `chunk-000.ogg`, etc."""
    ffmpeg = _require_ffmpeg()
    pattern = str(out_dir / "chunk-%03d.ogg")
    cmd = [
        ffmpeg, "-y", "-i", str(src),
        "-f", "segment", "-segment_time", str(_CHUNK_SECONDS),
        "-c", "copy",
        pattern,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        raise TranscriptError(
            f"ffmpeg failed to split audio: {exc.stderr.decode('utf-8', 'replace')[-500:]}"
        )
    return sorted(out_dir.glob("chunk-*.ogg"))


def _transcribe_one(audio_path: Path) -> tuple[str, list[dict]]:
    """Whisper-1 transcription of a single file <= 25 MB.

    Returns (plain_text, segments) where each segment is
    {"text": str, "start": float, "duration": float} — same shape as the
    YouTube fetcher's `segments`, so downstream text helpers work unchanged.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise TranscriptError(
            "OPENAI_API_KEY is not set. Add it as a GitHub Actions secret to "
            "run the podcast transcript workflow."
        )

    # Prefer verbose_json so we get timestamped segments for the .timestamped.txt
    # save. Fall back to plain text if the SDK / API stops supporting it.
    url = "https://api.openai.com/v1/audio/transcriptions"
    with open(audio_path, "rb") as fh:
        files = {"file": (audio_path.name, fh, "application/octet-stream")}
        data = {"model": "whisper-1", "response_format": "verbose_json"}
        try:
            resp = requests.post(
                url,
                files=files,
                data=data,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15 * 60,   # 15 min upload+processing ceiling
            )
        except Exception as exc:
            raise TranscriptError(
                f"Whisper API request failed: {type(exc).__name__}: {exc}"
            )

    if resp.status_code >= 400:
        raise TranscriptError(
            f"Whisper API returned HTTP {resp.status_code}: "
            f"{resp.text[:500]}"
        )

    try:
        body = resp.json()
    except Exception:
        raise TranscriptError(
            f"Whisper API returned non-JSON: {resp.text[:500]}"
        )

    text = (body.get("text") or "").strip()
    raw_segments = body.get("segments") or []
    segments: list[dict] = []
    for s in raw_segments:
        start = float(s.get("start", 0.0) or 0.0)
        end = float(s.get("end", start) or start)
        segments.append({
            "text": (s.get("text") or "").strip(),
            "start": start,
            "duration": max(0.0, end - start),
        })
    if not segments and text:
        # Whisper occasionally returns text without segments for very short
        # inputs; synthesise a single segment so the timestamped file isn't
        # empty.
        segments = [{"text": text, "start": 0.0, "duration": 0.0}]
    return text, segments


def _transcribe_prepared(compressed: Path, work_dir: Path) -> tuple[str, list[dict]]:
    """Transcribe the compressed file, chunking if it's still oversized."""
    size = compressed.stat().st_size
    if size <= _WHISPER_MAX_BYTES:
        return _transcribe_one(compressed)

    chunks = _split_into_chunks(compressed, work_dir)
    if not chunks:
        raise TranscriptError("Splitting the audio into chunks produced no files.")

    combined_text_parts: list[str] = []
    combined_segments: list[dict] = []
    time_offset = 0.0
    for i, chunk in enumerate(chunks):
        text, segs = _transcribe_one(chunk)
        combined_text_parts.append(text)
        for s in segs:
            combined_segments.append({
                "text": s["text"],
                "start": s["start"] + time_offset,
                "duration": s["duration"],
            })
        # Advance the offset by the actual chunk duration (last segment end).
        if segs:
            last = segs[-1]
            time_offset = max(time_offset + _CHUNK_SECONDS, last["start"] + last["duration"])
        else:
            time_offset += _CHUNK_SECONDS
    return " ".join(p for p in combined_text_parts if p), combined_segments


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def _slug(text: str, fallback: str) -> str:
    text = text or fallback
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return (slug[:60] or fallback).lower()


def fetch_podcast_transcript(url: str) -> TranscriptResult:
    """Fetch a podcast episode's transcript.

    Reuses YouTube's TranscriptResult so downstream code (LLM digest,
    save-to-file, email) doesn't have to branch. `video_id` is populated
    with a stable slug derived from the show / episode / host filename so
    the saved files have a readable name.
    """
    resolved = resolve_podcast_url(url)
    print(
        f"[ned/podcast] Resolved -> {resolved.audio_url} "
        f"({resolved.source_kind}; show={resolved.show_title!r}, "
        f"episode={resolved.episode_title!r})"
    )

    with tempfile.TemporaryDirectory(prefix="ned-podcast-") as td:
        work = Path(td)
        raw = work / "audio.bin"
        _download_audio(resolved.audio_url, raw)
        raw_mb = raw.stat().st_size / (1024 * 1024)
        print(f"[ned/podcast] Downloaded {raw_mb:.1f} MB")

        compressed = work / "audio.ogg"
        _compress_for_whisper(raw, compressed)
        comp_mb = compressed.stat().st_size / (1024 * 1024)
        print(f"[ned/podcast] Compressed to {comp_mb:.1f} MB (16kHz mono Opus)")

        plain_text, segments = _transcribe_prepared(compressed, work)
        print(f"[ned/podcast] Whisper returned {len(segments)} segment(s), "
              f"{len(plain_text)} chars")

    # Reuse the same helpers as YouTube for the timestamped file.
    from ned.youtube_transcript_fetcher import _clean_plain_text, _timestamped_text
    plain = _clean_plain_text(segments) if segments else plain_text
    ts = _timestamped_text(segments)

    slug_source = resolved.episode_title or resolved.show_title or Path(urlparse(resolved.audio_url).path).stem
    slug = _slug(slug_source, fallback="podcast")

    return TranscriptResult(
        video_id=slug,
        language="English",
        language_code="en",
        is_generated=True,       # Whisper output is machine-generated
        plain_text=plain,
        timestamped_text=ts,
        segments=segments,
    )
