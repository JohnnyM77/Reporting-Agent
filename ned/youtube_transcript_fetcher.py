# ned/youtube_transcript_fetcher.py
#
# Single-episode YouTube transcript fetcher for Ned the News Agent.
#
# Unlike ned/youtube_scanner.py (which crawls channels via the YouTube Data
# API), this module takes ONE pasted YouTube link and pulls its caption track
# directly with youtube-transcript-api. No YOUTUBE_API_KEY is needed — the
# caption endpoint is keyed only by video ID.
#
# It produces two forms of the same transcript:
#   * plain_text       — one clean block, timestamps stripped, for the LLM
#   * timestamped_text  — [HH:MM:SS] lines, saved to file so a human can jump
#                         to a specific point later
#
# Works with youtube-transcript-api >= 1.0 (the instance-based API:
# `YouTubeTranscriptApi().list(...)` / `.fetch(...)`), which replaced the old
# static `get_transcript` classmethod.

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

# 11-character YouTube video IDs: letters, digits, hyphen, underscore.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Preferred caption languages, best first. English variants cover the ASX /
# LSE podcast and interview content Ned watches.
DEFAULT_LANGUAGES: tuple[str, ...] = ("en", "en-US", "en-GB", "en-AU")


class TranscriptError(Exception):
    """A transcript could not be produced for a video.

    Carries a human-readable message (captions disabled, none found, bad URL,
    network/blocking error) so callers can surface it instead of crashing on a
    raw library traceback.
    """


@dataclass
class TranscriptResult:
    """The outcome of fetching one video's transcript."""

    video_id: str
    language: str
    language_code: str
    is_generated: bool          # True = auto-generated captions, False = manual
    plain_text: str             # timestamps stripped, single clean block
    timestamped_text: str       # one "[HH:MM:SS] text" line per segment
    segments: list[dict] = field(default_factory=list)  # raw {text,start,duration}

    @property
    def caption_kind(self) -> str:
        return "auto-generated" if self.is_generated else "manual"


def extract_video_id(url: str) -> str:
    """Extract the 11-character video ID from a raw YouTube URL.

    Handles the common shapes:
      * https://www.youtube.com/watch?v=VIDEOID (with any extra query params)
      * https://youtu.be/VIDEOID
      * https://www.youtube.com/shorts/VIDEOID
      * https://www.youtube.com/embed/VIDEOID
      * https://www.youtube.com/live/VIDEOID
      * https://m.youtube.com/... variants of the above
      * a bare 11-character video ID passed straight through

    Raises TranscriptError if no valid ID can be found.
    """
    if not url or not url.strip():
        raise TranscriptError("No YouTube URL was provided.")

    candidate = url.strip()

    # A bare video ID pasted on its own.
    if _VIDEO_ID_RE.match(candidate):
        return candidate

    # Tolerate links pasted without a scheme (e.g. "youtu.be/oay6t8vh7b4").
    if "://" not in candidate:
        candidate = "https://" + candidate

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    # Strip a leading "www." / "m." / "music." so host checks are uniform.
    host = re.sub(r"^(www\.|m\.|music\.)", "", host)
    path = parsed.path or ""

    vid: str | None = None

    if host == "youtu.be":
        # youtu.be/VIDEOID  -> first path segment is the ID
        vid = path.lstrip("/").split("/")[0]
    elif host in ("youtube.com", "youtube-nocookie.com"):
        if path == "/watch":
            qs = parse_qs(parsed.query)
            vid = (qs.get("v") or [None])[0]
        else:
            # /shorts/ID, /embed/ID, /live/ID, /v/ID
            m = re.match(r"^/(?:shorts|embed|live|v)/([^/?#]+)", path)
            if m:
                vid = m.group(1)
            elif path.strip("/") and "/" not in path.strip("/"):
                # /VIDEOID style share links
                vid = path.strip("/")

    if vid and _VIDEO_ID_RE.match(vid):
        return vid

    raise TranscriptError(
        f"Could not find a YouTube video ID in the link: {url!r}"
    )


def _format_timestamp(seconds: float) -> str:
    """Seconds -> "H:MM:SS" (hours dropped when zero, e.g. "04:07")."""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _clean_plain_text(segments: list[dict]) -> str:
    """Join segment texts into one block with timestamps stripped.

    Auto-generated captions sprinkle newlines and stray double spaces inside
    segments; collapse all runs of whitespace to single spaces so the LLM gets
    clean prose.
    """
    joined = " ".join((seg.get("text") or "").strip() for seg in segments)
    return re.sub(r"\s+", " ", joined).strip()


def _timestamped_text(segments: list[dict]) -> str:
    """One "[HH:MM:SS] text" line per segment, for jumping to a point later."""
    lines: list[str] = []
    for seg in segments:
        text = re.sub(r"\s+", " ", (seg.get("text") or "").strip())
        if not text:
            continue
        ts = _format_timestamp(float(seg.get("start", 0.0) or 0.0))
        lines.append(f"[{ts}] {text}")
    return "\n".join(lines)


def fetch_transcript(
    video_id: str,
    languages: tuple[str, ...] = DEFAULT_LANGUAGES,
    api=None,
) -> TranscriptResult:
    """Fetch and normalise the transcript for a single video ID.

    Prefers a manually-created transcript, then falls back to auto-generated
    captions (the library exposes these through different lookups), and finally
    to any available transcript in another language.

    Catches TranscriptsDisabled / NoTranscriptFound and re-raises them as a
    TranscriptError with a clear message, so a video with captions turned off
    produces a readable explanation rather than a crash.

    `api` may be injected for testing; otherwise a real YouTubeTranscriptApi()
    is used.
    """
    from youtube_transcript_api import (
        NoTranscriptFound,
        TranscriptsDisabled,
        YouTubeTranscriptApi,
    )

    langs = list(languages)
    api = api or YouTubeTranscriptApi()

    try:
        transcript_list = api.list(video_id)
    except TranscriptsDisabled:
        raise TranscriptError(
            f"Captions are disabled for this video ({video_id}); no transcript "
            "is available."
        )
    except NoTranscriptFound:
        raise TranscriptError(
            f"No transcript could be found for this video ({video_id})."
        )
    except Exception as exc:  # network error, IP block, unavailable video, …
        raise TranscriptError(
            f"Could not retrieve transcript for {video_id}: "
            f"{type(exc).__name__}: {exc}"
        )

    # Manual captions and auto-generated captions are looked up separately.
    transcript_obj = None
    try:
        transcript_obj = transcript_list.find_manually_created_transcript(langs)
    except NoTranscriptFound:
        try:
            transcript_obj = transcript_list.find_generated_transcript(langs)
        except NoTranscriptFound:
            # Last resort: any transcript in any language the video offers.
            try:
                transcript_obj = transcript_list.find_transcript(langs)
            except NoTranscriptFound:
                raise TranscriptError(
                    f"No transcript in {', '.join(langs)} (or any fallback "
                    f"language) was found for this video ({video_id})."
                )

    try:
        fetched = transcript_obj.fetch()
    except (TranscriptsDisabled, NoTranscriptFound) as exc:
        raise TranscriptError(
            f"Transcript for {video_id} could not be fetched: "
            f"{type(exc).__name__}."
        )
    except Exception as exc:
        raise TranscriptError(
            f"Could not download transcript for {video_id}: "
            f"{type(exc).__name__}: {exc}"
        )

    # youtube-transcript-api >= 1.0 returns a FetchedTranscript exposing
    # to_raw_data(); older/mocked objects may already be a list of dicts.
    if hasattr(fetched, "to_raw_data"):
        segments = fetched.to_raw_data()
    else:
        segments = [
            {
                "text": getattr(s, "text", "") if not isinstance(s, dict) else s.get("text", ""),
                "start": getattr(s, "start", 0.0) if not isinstance(s, dict) else s.get("start", 0.0),
                "duration": getattr(s, "duration", 0.0) if not isinstance(s, dict) else s.get("duration", 0.0),
            }
            for s in fetched
        ]

    if not segments:
        raise TranscriptError(
            f"The transcript for {video_id} came back empty."
        )

    return TranscriptResult(
        video_id=video_id,
        language=getattr(transcript_obj, "language", "") or "",
        language_code=getattr(transcript_obj, "language_code", "") or "",
        is_generated=bool(getattr(transcript_obj, "is_generated", False)),
        plain_text=_clean_plain_text(segments),
        timestamped_text=_timestamped_text(segments),
        segments=segments,
    )
