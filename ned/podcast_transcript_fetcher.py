# ned/podcast_transcript_fetcher.py
#
# Single-episode podcast transcript fetcher for Ned the News Agent.
#
# Two paths, cheapest first:
#
#   FREE PATH — <podcast:transcript>
#     A growing number of podcast RSS feeds publish an already-made transcript
#     alongside the audio, using the Podcasting 2.0 <podcast:transcript> tag
#     (namespace: https://podcastindex.org/namespace/1.0). If the matched
#     <item> carries one, we download and parse that file directly (VTT,
#     SRT, or plain text) — no audio download, no ffmpeg, no Whisper call,
#     no cost. This is both faster and more accurate: the transcript was
#     produced by the show itself, often reviewed by a human.
#
#   PAID PATH — Whisper
#     When no published transcript exists, or when parsing it fails, we
#     fall back to the audio pipeline:
#       1. Resolve the pasted URL to a direct audio URL (Apple Podcasts
#          links go via the iTunes lookup API + the podcast's RSS feed;
#          RSS <enclosure> URLs and raw .mp3 links are used as-is).
#       2. Download the audio to a local file.
#   3. (Whisper path continued) Compress with ffmpeg to 16kHz mono Opus so it fits under Whisper's
#      25 MB per-request cap in one call. If it's still oversized, OR if it's
#      simply long (a live run against an ~85-minute episode had its Whisper
#      request die mid-flight with RemoteDisconnected — the file was well
#      under the size cap at 15 MB, but one HTTP call covering that much
#      audio was apparently fragile over whatever network path sits between
#      the runner and OpenAI), we split into ~20-minute chunks and
#      transcribe each separately.
#   4. Transcribe with OpenAI's Whisper API (whisper-1) — no local ML deps.
#      Each Whisper call retries a few times with backoff on transient
#      connection errors (not on 4xx/5xx HTTP responses, which are treated
#      as real failures).
#   5. Return a TranscriptResult identical in shape to the YouTube fetcher's,
#      so downstream code (digest LLM step, email, save-to-file) is unchanged.
#
# ffmpeg is NOT preinstalled on ubuntu-latest GitHub runners as of this
# writing — the workflow installs it explicitly (`apt-get install ffmpeg`)
# before this module is imported. Locally you need it on PATH too.

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
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

# Whisper request retries: a live run against an ~85-minute episode (15.3 MB
# compressed, under the size cap) died with
# "ConnectionError: Remote end closed connection without response" after
# several minutes — a transient drop somewhere between the runner and
# OpenAI, not a client-side timeout. Retried network-level failures only;
# a 4xx/5xx HTTP response is a real failure and is never retried.
_MAX_WHISPER_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (5, 20)

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
    # Populated when the RSS <item> carries a <podcast:transcript> tag. When
    # set, we can skip the audio download + ffmpeg + Whisper path entirely
    # and read the show's own transcript instead.
    transcript_url: str = ""
    transcript_type: str = ""    # "text/plain" | "text/vtt" | "application/srt" | …


# The Podcasting 2.0 namespace transcripts live under. Feeds occasionally
# omit or vary the URI, so at parse time we compare by local tag name too
# rather than requiring an exact namespace match.
_PODCAST_NS = "https://podcastindex.org/namespace/1.0"

# When a feed publishes several <podcast:transcript> entries (e.g. both VTT
# and SRT), we prefer plain text > VTT > SRT — all fine, but plain text
# needs no timestamp parsing.
_TRANSCRIPT_TYPE_PRIORITY = {
    "text/plain": 0,
    "text/html": 1,      # rare; we strip tags before use
    "text/vtt": 2,
    "application/srt": 3,
    "application/x-subrip": 3,
    # Unknown or JSON: not currently parsed, deprioritised so a parseable
    # type wins when the feed offers both.
    "": 9,
}


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
# Two-letter country codes Apple accepts in the storefront path
# (`podcasts.apple.com/<cc>/...`). Extracted so a URL from the Australian
# storefront (`/au/`) queries the AU storefront -- without this a regional
# show like "The Story of Money" returns zero results from the default
# storefront and the whole run fails at resolve time.
_APPLE_COUNTRY_RE = re.compile(r"^/([a-z]{2})/podcast(?:s)?/", re.IGNORECASE)


def _country_from_apple_url(path: str) -> str:
    """Extract the storefront country code from an Apple Podcasts URL
    path, e.g. `/au/podcast/...` -> `au`. Returns an empty string when the
    URL has no country segment."""
    m = _APPLE_COUNTRY_RE.match(path or "")
    return m.group(1).lower() if m else ""


def _lookup_apple_results(params: dict, *, country: str = "") -> list[dict]:
    """Run one iTunes lookup and return ALL result rows.

    Tries the storefront (`country`) first -- regional shows like "The
    Story of Money" live in the AU storefront -- then the default
    storefront if the scoped call comes back empty. Never raises on "no
    results"; raises TranscriptError only on transport/HTTP failure.
    """
    def _fetch(p: dict) -> list[dict]:
        try:
            resp = requests.get(
                _ITUNES_LOOKUP,
                params=p,
                timeout=_HTTP_TIMEOUT,
                headers={"User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise TranscriptError(
                f"iTunes lookup failed (params={p}): {type(exc).__name__}: {exc}"
            )
        return data.get("results") or []

    if country:
        rows = _fetch({**params, "country": country})
        if rows:
            return rows
    return _fetch(params)


def _lookup_apple(kind_id: str, *, country: str = "") -> dict:
    """First iTunes row for a podcast id, or {}. Used for show-level
    lookups (feedUrl + show title)."""
    rows = _lookup_apple_results({"id": kind_id}, country=country)
    return rows[0] if rows else {}


# How many recent episodes Apple will return in one podcast lookup. 200 is
# the API maximum.
_APPLE_EPISODE_LIMIT = 200

_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _normalise_title(text: str) -> str:
    """Lower-case, strip punctuation and collapse spaces so an Apple page
    title and an RSS <title> compare equal despite smart quotes etc."""
    import html as _h
    text = _h.unescape(text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _episode_title_from_apple_page(url: str) -> str:
    """Read the episode title off Apple's own episode web page.

    Used only when the episode is older than the 200 most recent that the
    iTunes API returns. Apple's page carries the episode name in
    og:title; the <title> tag is the fallback ("<Episode> - <Show> -
    Apple Podcasts").
    """
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT, headers={"User-Agent": _USER_AGENT})
        resp.raise_for_status()
    except Exception as exc:
        print(f"[ned/podcast] Could not fetch Apple episode page: {type(exc).__name__}: {exc}")
        return ""
    body = resp.text or ""
    m = _OG_TITLE_RE.search(body)
    if m:
        return m.group(1).strip()
    m = _HTML_TITLE_RE.search(body)
    if m:
        # "Episode Name - Show Name - Apple Podcasts" -> "Episode Name"
        return m.group(1).split(" - ")[0].strip()
    return ""


def _find_apple_episode(
    *,
    podcast_id: str,
    episode_id: str,
    country: str,
    page_url: str,
) -> dict:
    """Return an episode dict with keys episodeUrl / episodeGuid /
    trackName / collectionName / feedUrl, or {} if the episode can't be
    identified.

    Apple's iTunes lookup does NOT resolve a podcast episode by its own
    `?i=` id -- `lookup?id=<EPISODE_ID>` returns zero rows for podcast
    episodes, with or without `country` or `entity`. That's why the last
    two attempts at this failed live. What does work:

      1. `lookup?id=<PODCAST_ID>&entity=podcastEpisode&limit=200` returns
         the show row plus its 200 most recent episodes; pick the row
         whose trackId equals the episode id.
      2. For older episodes, read the episode title off Apple's episode
         web page and match it against the show's RSS <item> titles.
    """
    rows = _lookup_apple_results(
        {"id": podcast_id, "entity": "podcastEpisode", "limit": _APPLE_EPISODE_LIMIT},
        country=country,
    )
    show_row = next((r for r in rows if r.get("wrapperType") != "podcastEpisode"), {})
    feed_url = show_row.get("feedUrl") or ""
    show_title = show_row.get("collectionName") or show_row.get("trackName") or ""

    for r in rows:
        if r.get("wrapperType") == "podcastEpisode" and str(r.get("trackId")) == str(episode_id):
            return {
                "episodeUrl": r.get("episodeUrl") or "",
                "episodeGuid": r.get("episodeGuid") or "",
                "trackName": r.get("trackName") or "",
                "collectionName": r.get("collectionName") or show_title,
                "feedUrl": r.get("feedUrl") or feed_url,
            }

    # Older than the 200-episode window: match by title in the RSS feed.
    if not feed_url:
        return {}
    title = _episode_title_from_apple_page(page_url)
    if not title:
        return {}
    wanted = _normalise_title(title)
    print(f"[ned/podcast] Episode not in Apple's recent list; matching RSS by title {title!r}")
    for it in _fetch_rss_items(feed_url):
        if _normalise_title(_rss_item_title(it)) == wanted:
            audio = _rss_item_enclosure(it)
            if audio:
                return {
                    "episodeUrl": audio,
                    "episodeGuid": _rss_item_guid(it),
                    "trackName": _rss_item_title(it),
                    "collectionName": show_title,
                    "feedUrl": feed_url,
                }
    return {}


def _resolve_apple(url: str) -> ResolvedPodcast:
    """Resolve an Apple Podcasts episode link to a direct audio URL.

    Apple URLs look like:
      https://podcasts.apple.com/us/podcast/<slug>/id<PODCAST_ID>?i=<EPISODE_ID>

    When the URL carries an episode id (`?i=<EPISODE_ID>`) we look that
    episode up directly through Apple's iTunes API, which returns
    `episodeUrl` (the direct MP3), `episodeGuid` (the show's own RSS
    <guid>), and `trackName` (the real title). That's the source of truth
    -- we do NOT try to match Apple's `i=` value against RSS items,
    because Apple's episode id is an Apple-internal id and does not
    appear in the source RSS feed. An earlier version of this code did
    that endswith-match and, when it failed (which was always for shows
    whose <guid> isn't derived from the Apple id), silently fell back to
    "return the newest item in the feed" -- which is how a request for
    the railroads-history episode of "The Story of Money" came back as
    the show's newest episode instead. Same bug hid an ad-only promo
    item behind another request.

    We still fetch the RSS feed after the episode lookup so we can find
    the matching <item> by its real <guid> and read its
    <podcast:transcript> tag (the free path). If the guid match fails,
    we keep the Apple-supplied audio URL and title and take the paid
    Whisper path -- that's a soft miss, not a silent-newest fallback.

    When the URL has NO `?i=`, the user asked for the show generically:
    take the newest item from the RSS feed. That's an explicit request
    for "whatever's newest", not a fallback.
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
    country = _country_from_apple_url(parsed.path)

    # -----------------------------------------------------------------
    # No `?i=` -- user asked for the show, not a specific episode.
    # Look up the podcast (for its feedUrl), then take the newest item.
    # -----------------------------------------------------------------
    if not episode_id:
        show = _lookup_apple(podcast_id, country=country)
        if not show:
            raise TranscriptError(
                f"Apple's iTunes lookup returned no podcast for id {podcast_id}."
            )
        feed_url = show.get("feedUrl")
        show_title = show.get("collectionName") or show.get("trackName") or ""
        if not feed_url:
            raise TranscriptError(
                "Apple returned the podcast but no RSS feed URL -- cannot "
                "reach the audio."
            )
        partial = _newest_item_from_rss(feed_url)
        partial.show_title = show_title
        partial.source_kind = "apple"
        return partial

    # -----------------------------------------------------------------
    # `?i=<EPISODE_ID>` -- ask Apple for that exact episode.
    # Pass entity=podcastEpisode so Apple returns the episode row, not
    # the parent podcast; pass country=<cc> for regional shows that
    # aren't in the default US storefront.
    # -----------------------------------------------------------------
    ep = _find_apple_episode(
        podcast_id=podcast_id,
        episode_id=episode_id,
        country=country,
        page_url=url,
    )
    if not ep:
        raise TranscriptError(
            f"Apple's iTunes lookup returned nothing for episode id "
            f"{episode_id} (URL {url!r}). Check the link is still live -- "
            "some shows drop old episodes and Apple stops resolving the id."
        )
    audio_url = ep.get("episodeUrl") or ""
    episode_guid = ep.get("episodeGuid") or ""
    episode_title = ep.get("trackName") or ""
    show_title = ep.get("collectionName") or ""
    feed_url = ep.get("feedUrl")
    if not audio_url:
        raise TranscriptError(
            f"Apple returned metadata for episode {episode_id} but no direct "
            "audio URL. Nothing to download; skipping."
        )

    # Best-effort: pull the show's RSS feed so we can find the matching
    # <item> by its real guid and read its <podcast:transcript>. If the
    # feedUrl isn't included in the episode payload, ask the podcast-level
    # lookup for it. If any of that fails, we still have the audio URL
    # from Apple and take the Whisper path -- a real fallback, no
    # silent-wrong-episode risk.
    transcript_url, transcript_type = "", ""
    if not feed_url:
        show = _lookup_apple(podcast_id, country=country)
        feed_url = (show or {}).get("feedUrl") or ""
        if not show_title:
            show_title = (show or {}).get("collectionName") or ""
    if feed_url and episode_guid:
        try:
            transcript_url, transcript_type = _transcript_for_guid_from_rss(
                feed_url, episode_guid,
            )
        except Exception as exc:
            # RSS unavailable / malformed / no matching item -- log and
            # carry the Apple audio URL forward. No silent wrong-episode.
            print(
                f"[ned/podcast] Could not find <podcast:transcript> for guid "
                f"{episode_guid!r} in {feed_url}: {type(exc).__name__}: {exc}. "
                "Falling through to Whisper."
            )

    return ResolvedPodcast(
        audio_url=audio_url,
        episode_title=episode_title,
        show_title=show_title,
        source_kind="apple",
        transcript_url=transcript_url,
        transcript_type=transcript_type,
    )


def _local_name(tag: str) -> str:
    """`{ns}foo` -> `foo`, `foo` -> `foo`. Namespace-lenient tag matching."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _pick_transcript(item: ET.Element) -> tuple[str, str]:
    """Return (transcript_url, transcript_type) if the RSS <item> carries a
    parseable <podcast:transcript>, else ("", "").

    A feed may declare multiple entries — one per format. We pick by the
    priority table above so a parseable type wins when the feed offers
    both. Unknown types fall through with a default priority so they can
    still be used if nothing better exists.
    """
    best: tuple[int, str, str] | None = None
    for child in item:
        if _local_name(child.tag) != "transcript":
            continue
        turl = (child.get("url") or "").strip()
        if not turl:
            continue
        ttype = (child.get("type") or "").strip().lower()
        priority = _TRANSCRIPT_TYPE_PRIORITY.get(ttype, 5)
        if best is None or priority < best[0]:
            best = (priority, turl, ttype)
    if best is None:
        return "", ""
    return best[1], best[2]


def _fetch_rss_items(feed_url: str) -> list[ET.Element]:
    """Fetch the feed, parse it, return the `<item>` elements or raise."""
    try:
        resp = requests.get(
            feed_url,
            timeout=_HTTP_TIMEOUT,
            headers={
                "User-Agent": _USER_AGENT,
                "Accept": "application/rss+xml, application/xml, */*",
            },
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

    items = [el for el in root.iter() if _local_name(el.tag) == "item"]
    if not items:
        raise TranscriptError("Podcast RSS feed contains no <item> entries.")
    return items


def _rss_item_enclosure(item: ET.Element) -> str:
    for child in item:
        if _local_name(child.tag) == "enclosure" and child.get("url"):
            return child.get("url", "")
    for child in item:
        if _local_name(child.tag) == "link":
            link = (child.text or "").strip()
            if _looks_like_audio_url(link):
                return link
    return ""


def _rss_item_title(item: ET.Element) -> str:
    for child in item:
        if _local_name(child.tag) == "title":
            return (child.text or "").strip()
    return ""


def _rss_item_guid(item: ET.Element) -> str:
    for child in item:
        if _local_name(child.tag) == "guid":
            return (child.text or "").strip()
    return ""


def _newest_item_from_rss(feed_url: str) -> ResolvedPodcast:
    """Take the newest RSS item -- the "no episode was requested" path.

    Used when the caller pastes a bare podcast landing page or an RSS
    feed URL with no episode selector. Never used to salvage a failed
    guid lookup: mis-picking an old episode was the whole reason this
    module got rewritten. Sets audio_url, episode_title, and (when
    present) the <podcast:transcript>; the caller fills in show_title
    and source_kind.
    """
    for it in _fetch_rss_items(feed_url):
        url = _rss_item_enclosure(it)
        if url:
            turl, ttype = _pick_transcript(it)
            return ResolvedPodcast(
                audio_url=url,
                episode_title=_rss_item_title(it),
                transcript_url=turl,
                transcript_type=ttype,
            )
    raise TranscriptError(
        "No RSS <item> in this feed has an audio enclosure."
    )


def _transcript_for_guid_from_rss(feed_url: str, guid: str) -> tuple[str, str]:
    """Return (transcript_url, transcript_type) from the RSS <item> whose
    <guid> matches `guid`, or ("", "") if the item exists but publishes
    no <podcast:transcript>. Raises TranscriptError when no <item> in the
    feed matches -- caller decides whether to fall back to Whisper or
    surface the error.

    Match is exact first, then endswith (handles the small number of
    feeds that stamp their guids with a URL prefix while Apple returns
    the bare id).
    """
    items = _fetch_rss_items(feed_url)
    for it in items:
        if _rss_item_guid(it) == guid:
            return _pick_transcript(it)
    for it in items:
        g = _rss_item_guid(it)
        if g and (g.endswith(guid) or guid.endswith(g)):
            return _pick_transcript(it)
    raise TranscriptError(
        f"No RSS <item> has guid {guid!r}; can't attach a "
        "<podcast:transcript>."
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
    # RSS feed URL pasted directly (no episode selector) -- take the newest.
    if url.lower().endswith((".xml", ".rss")) or "/rss" in url.lower() or "/feed" in url.lower():
        partial = _newest_item_from_rss(url)
        partial.source_kind = "rss"
        return partial

    raise TranscriptError(
        f"Don't know how to resolve this URL to a podcast episode: {url!r}. "
        "Paste a direct .mp3/.m4a URL, an Apple Podcasts episode link, or an "
        "RSS feed URL."
    )


# ---------------------------------------------------------------------------
# Published transcript (RSS <podcast:transcript>) — the free path
# ---------------------------------------------------------------------------
# Timestamps in WebVTT ("00:12:34.567") and SRT ("00:12:34,567") differ only
# in the fractional-second separator. This regex accepts either, and also
# tolerates the shorter mm:ss.mmm form some VTT files use for cues under
# an hour.
_TIMESTAMP_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d{1,3})")
_CUE_LINE_RE = re.compile(
    r"^\s*(" + _TIMESTAMP_RE.pattern + r")\s+-->\s+(" + _TIMESTAMP_RE.pattern + r")"
)
_STRIP_VTT_TAGS_RE = re.compile(r"<[^>]+>")


def _parse_timestamp(match: re.Match) -> float:
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    frac = match.group(4)
    return hours * 3600 + minutes * 60 + seconds + float(f"0.{frac}")


def _parse_vtt_or_srt(body: str) -> list[dict]:
    """Parse a WebVTT or SRT transcript into our {text, start, duration} shape.

    Both formats are cue-based: an optional identifier line, a
    "start --> end" timestamp line, one or more text lines, then a blank
    line. WebVTT can carry inline tags like <v Speaker> and <c.classname>
    which we strip so the LLM sees clean text.
    """
    segments: list[dict] = []
    lines = body.splitlines()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].strip()
        cue = _CUE_LINE_RE.match(line)
        if not cue:
            i += 1
            continue
        # Timestamps are the 1st and 6th capture groups (each nested regex
        # contributes 5 groups: outer wrapper + h/m/s/frac). We re-match the
        # start / end timestamps to get the numeric values.
        ts_matches = list(_TIMESTAMP_RE.finditer(line))
        start = _parse_timestamp(ts_matches[0])
        end = _parse_timestamp(ts_matches[1]) if len(ts_matches) > 1 else start
        # Following lines up to the next blank are the cue's text.
        text_parts: list[str] = []
        i += 1
        while i < n and lines[i].strip():
            text_parts.append(lines[i])
            i += 1
        text = " ".join(text_parts)
        text = _STRIP_VTT_TAGS_RE.sub("", text).strip()
        text = re.sub(r"\s+", " ", text)
        if text:
            segments.append({
                "text": text,
                "start": start,
                "duration": max(0.0, end - start),
            })
        # Advance past the blank line to the next cue.
        while i < n and not lines[i].strip():
            i += 1
    return segments


def _parse_plain_text(body: str) -> list[dict]:
    """Wrap a plain-text transcript in a single segment.

    We don't have timestamps to work with; the .timestamped.txt file will
    just show [00:00] for the whole thing, and that's fine — the LLM
    digest step doesn't care about timestamps.
    """
    clean = re.sub(r"\s+", " ", body).strip()
    if not clean:
        return []
    return [{"text": clean, "start": 0.0, "duration": 0.0}]


def _strip_html(body: str) -> str:
    """Extremely small HTML text extractor for text/html transcripts.

    Not a real parser — just enough to unwrap the paragraphs a captioning
    service typically emits when it publishes as text/html rather than
    text/plain. Anything more elaborate falls back to Whisper anyway.
    """
    return re.sub(r"<[^>]+>", " ", body)


def _fetch_published_transcript(resolved: ResolvedPodcast) -> tuple[str, list[dict]] | None:
    """Try the show's own <podcast:transcript> file. Returns
    (plain_text, segments) on success, or None when we should fall back to
    the audio + Whisper path.

    Never raises — a failed download, an unsupported type, or a parse that
    yields zero segments all return None so the caller can fall back
    cleanly. The Whisper path is the safety net.
    """
    if not resolved.transcript_url:
        return None

    print(
        f"[ned/podcast] Show publishes a transcript "
        f"({resolved.transcript_type or 'unknown type'}); "
        f"fetching {resolved.transcript_url}"
    )
    try:
        resp = requests.get(
            resolved.transcript_url,
            timeout=_HTTP_TIMEOUT,
            headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
        )
        resp.raise_for_status()
    except Exception as exc:
        print(
            f"[ned/podcast] Published transcript download failed "
            f"({type(exc).__name__}: {exc}); falling back to Whisper."
        )
        return None

    body = resp.text or ""
    ttype = (resolved.transcript_type or "").lower()

    # If the feed didn't declare a type, sniff from the body's first line.
    if not ttype:
        head = body.lstrip()[:16].upper()
        if head.startswith("WEBVTT"):
            ttype = "text/vtt"
        elif re.match(r"^\d+\s*\n\s*\d", body.lstrip()):
            ttype = "application/srt"
        else:
            ttype = "text/plain"

    if ttype in ("text/vtt", "application/srt", "application/x-subrip"):
        segments = _parse_vtt_or_srt(body)
    elif ttype == "text/html":
        segments = _parse_plain_text(_strip_html(body))
    elif ttype == "text/plain":
        segments = _parse_plain_text(body)
    else:
        print(
            f"[ned/podcast] Published transcript type {ttype!r} not supported; "
            "falling back to Whisper."
        )
        return None

    if not segments:
        print(
            "[ned/podcast] Published transcript parsed to zero segments; "
            "falling back to Whisper."
        )
        return None

    from ned.youtube_transcript_fetcher import _clean_plain_text
    plain_text = _clean_plain_text(segments)
    print(
        f"[ned/podcast] Using published transcript: {len(segments)} segment(s), "
        f"{len(plain_text)} chars — no Whisper call needed."
    )
    return plain_text, segments


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
            "the audio for Whisper. The GitHub Actions workflow installs it "
            "via apt-get; locally, install it yourself (macOS: `brew install "
            "ffmpeg`; Ubuntu: `apt-get install ffmpeg`; Windows: winget / choco)."
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


def _audio_duration_seconds(path: Path) -> float:
    """Duration of an audio file via ffprobe (ships alongside ffmpeg)."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise TranscriptError(
            "ffprobe is not installed / not on PATH. It ships with ffmpeg — "
            "install ffmpeg to get it too."
        )
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        out = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return float(out.stdout.strip())
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise TranscriptError(f"ffprobe failed to read audio duration: {exc}")


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
    resp = None
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_WHISPER_ATTEMPTS + 1):
        try:
            with open(audio_path, "rb") as fh:
                files = {"file": (audio_path.name, fh, "application/octet-stream")}
                data = {"model": "whisper-1", "response_format": "verbose_json"}
                resp = requests.post(
                    url,
                    files=files,
                    data=data,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=15 * 60,   # 15 min upload+processing ceiling
                )
            break
        except requests.exceptions.RequestException as exc:
            # Network-level failure (connection reset, timeout, etc.) — retry.
            # An HTTP 4xx/5xx response is not this branch; it's handled below
            # and never retried, since retrying won't fix a bad key or a
            # rejected file.
            last_exc = exc
            more_attempts_left = attempt < _MAX_WHISPER_ATTEMPTS
            print(
                f"[ned/podcast] Whisper request attempt {attempt}/{_MAX_WHISPER_ATTEMPTS} "
                f"failed ({type(exc).__name__}: {exc}); "
                + ("retrying…" if more_attempts_left else "giving up.")
            )
            if more_attempts_left:
                time.sleep(_RETRY_BACKOFF_SECONDS[attempt - 1])

    if resp is None:
        raise TranscriptError(
            f"Whisper API request failed after {_MAX_WHISPER_ATTEMPTS} attempts: "
            f"{type(last_exc).__name__}: {last_exc}"
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
    """Transcribe the compressed file, chunking if it's oversized OR long.

    Chunking on size alone isn't enough: a live run against an ~85-minute
    episode (15.3 MB compressed, comfortably under the 25 MB cap) still had
    its single Whisper request die mid-flight with a connection reset after
    several minutes. Splitting by duration too keeps each individual request
    short, which is both faster to retry and less exposed to whatever in the
    network path was killing the long-lived connection.
    """
    size = compressed.stat().st_size
    duration = _audio_duration_seconds(compressed)
    print(
        f"[ned/podcast] Compressed audio: {size / (1024 * 1024):.1f} MB, "
        f"{duration / 60:.1f} min"
    )
    if size <= _WHISPER_MAX_BYTES and duration <= _CHUNK_SECONDS:
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

    # FREE PATH — the show's own <podcast:transcript>, when the RSS feed
    # publishes one. No audio download, no Whisper, no cost. Returns None
    # on any failure so we fall back cleanly to the paid path.
    published = _fetch_published_transcript(resolved)
    if published is not None:
        plain_text, segments = published
        used_whisper = False
    else:
        # PAID PATH — download the audio and run it through Whisper.
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
        used_whisper = True

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
        # Whisper output is machine-generated; a published <podcast:transcript>
        # was produced by the show itself (often reviewed by a human), so
        # tag it as manual.
        is_generated=used_whisper,
        plain_text=plain,
        timestamped_text=ts,
        segments=segments,
        title=resolved.episode_title or resolved.show_title or slug,
        channel=resolved.show_title,
    )
