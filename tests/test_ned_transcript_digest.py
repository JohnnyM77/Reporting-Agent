# tests/test_ned_transcript_digest.py
#
# Ned's portfolio-aware transcript digest: the portfolio context loader, the
# JSON parse + markdown fallback, the LLM call wrapper (chunking, truncation,
# failures), the email, and the redesigned PDF. No network: the Anthropic
# call is replaced with a fake `llm_send`.

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ned import transcript_pdf as pdfmod  # noqa: E402
from ned.portfolio_context import load_portfolio_context  # noqa: E402
from ned.transcript_digest import (  # noqa: E402
    DigestResult,
    chunk_text,
    digest_json_to_markdown,
    normalise_digest,
    parse_digest_response,
    sort_portfolio_impact,
)
from ned.youtube_transcript_fetcher import TranscriptResult  # noqa: E402

FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "ned_transcript_digest_sample.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Portfolio context loader
# ---------------------------------------------------------------------------
def test_context_from_real_repo_files_has_holdings_theses_and_watchlists():
    text = load_portfolio_context(_REPO_ROOT)
    for ticker in ("BXB", "DRO", "CAT"):
        assert ticker in text, ticker
    # A real kill condition from theses/BXB.md survives verbatim.
    assert "Kill: I sell it for any reason short of genuine need" in text
    assert "VAS Vanguard Australian Shares Index ETF [ETF]" in text
    # Watchlist tickers lose the .AX suffix.
    assert "JM Watch List:" in text and ".AX" not in text
    assert len(text) <= 25_000


def test_context_never_cuts_kill_conditions_when_trimming():
    full = load_portfolio_context(_REPO_ROOT)
    tight = load_portfolio_context(_REPO_ROOT, max_chars=15_000)
    assert full.count("Kill:") == tight.count("Kill:") > 20
    assert "Bet:" not in tight          # the_bet is what gets trimmed


def test_context_missing_directory_does_not_raise(tmp_path):
    text = load_portfolio_context(tmp_path / "nope")
    assert "No portfolio files could be loaded" in text


def test_context_survives_malformed_files_and_skips_drafts(tmp_path):
    (tmp_path / "tickers.yaml").write_text("asx: [unclosed", encoding="utf-8")
    (tmp_path / "theses").mkdir()
    (tmp_path / "theses" / "GOOD.md").write_text(
        "---\nticker: GOOD\nname: Good Co\nstatus: HELD\npillars:\n"
        "  - id: P1\n    claim: It works.\n    kill_condition: It stops working.\n"
        "    status: INTACT\n---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "theses" / "DRAFT.md").write_text(
        "---\nticker: DRAFTY\ndraft: true\n---\n", encoding="utf-8",
    )
    (tmp_path / "theses" / "BROKEN.md").write_text("---\n: : :\n  - [\n---\n", encoding="utf-8")
    (tmp_path / "watchlists").mkdir()
    (tmp_path / "watchlists" / "w.yaml").write_text(
        "name: Mine\ntickers:\n  - ABC.AX\n  - ticker: XYZ\n", encoding="utf-8",
    )
    text = load_portfolio_context(tmp_path)
    assert "GOOD Good Co" in text
    assert "Kill: It stops working." in text
    assert "DRAFTY" not in text
    assert "Mine: ABC, XYZ" in text


def test_context_is_read_as_utf8(tmp_path):
    (tmp_path / "tickers.yaml").write_bytes("asx:\n  NHC: Café Holdings\n".encode("utf-8"))
    assert "Café Holdings" in load_portfolio_context(tmp_path)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def test_parse_clean_json():
    d, md = parse_digest_response(json.dumps(_fixture()))
    assert d is not None and d["portfolio_relevance"] == "Medium"
    assert md.startswith("1. **TL;DR**: Jay Cooke")


def test_parse_fenced_json():
    d, _ = parse_digest_response("```json\n" + json.dumps(_fixture()) + "\n```")
    assert d is not None and d["tldr"].startswith("Jay Cooke")


def test_parse_json_with_prose_around_it():
    d, _ = parse_digest_response("Here you go:\n" + json.dumps(_fixture()) + "\nHope that helps.")
    assert d is not None


def test_parse_garbage_falls_back_to_markdown():
    d, md = parse_digest_response("## Not JSON\n- just markdown { oops")
    assert d is None
    assert md == "## Not JSON\n- just markdown { oops"


def test_impact_sorted_kill_challenges_supports_then_rest_held_first():
    items = [
        {"ticker": "N", "effect": "Neutral", "type": "Held"},
        {"ticker": "SW", "effect": "Supports", "type": "Watchlist"},
        {"ticker": "SH", "effect": "Supports", "type": "Held"},
        {"ticker": "C", "effect": "Challenges", "type": "Held"},
        {"ticker": "K", "effect": "Kill condition at risk", "type": "Watchlist"},
    ]
    assert [i["ticker"] for i in sort_portfolio_impact(items)] == ["K", "C", "SH", "SW", "N"]


def test_normalise_coerces_bad_enums_and_flat_parallels():
    d = normalise_digest({
        "portfolio_relevance": "high",
        "portfolio_impact": [{"ticker": "X", "effect": "kill condition at risk", "type": "held",
                              "action": "whatever"}],
        "medium_term_lens": {"historical_parallels": [
            "Railways then, data centres now. Where it breaks: they fund capex from cash flow."
        ]},
    })
    assert d["portfolio_relevance"] == "High"
    assert d["portfolio_impact"][0]["effect"] == "Kill condition at risk"
    assert d["portfolio_impact"][0]["type"] == "Held"
    assert d["portfolio_impact"][0]["action"] == "No action"
    p = d["medium_term_lens"]["historical_parallels"][0]
    assert p["parallel"] == "Railways then, data centres now."
    assert p["where_it_breaks"] == "they fund capex from cash flow."


def test_markdown_rendering_is_dashboard_compatible():
    from dashboard.sections.transcripts import _transcript_digest_summary
    md = digest_json_to_markdown(normalise_digest(_fixture()))
    assert "## What it means for my portfolio" in md
    assert _transcript_digest_summary(md).startswith("Jay Cooke financed")


def test_chunk_text_splits_without_losing_content():
    text = ("One sentence here. " * 400).strip()
    chunks = chunk_text(text, 1000)
    assert len(chunks) > 5
    assert all(len(c) <= 1000 for c in chunks)
    assert " ".join(chunks).split() == text.split()


# ---------------------------------------------------------------------------
# llm_transcript_digest
# ---------------------------------------------------------------------------
class _FakeSend:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []

    def __call__(self, client, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        text, stop = r if isinstance(r, tuple) else (r, "end_turn")
        return SimpleNamespace(
            text=text, stop_reason=stop, truncated=(stop == "max_tokens"),
            input_tokens=100, output_tokens=50,
        )


@pytest.fixture
def ned_main(monkeypatch):
    import ned.main as m
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(m, "make_client", lambda key: object())
    return m


def test_digest_uses_sonnet_system_context_and_max_tokens(ned_main, monkeypatch):
    fake = _FakeSend([json.dumps(_fixture())])
    monkeypatch.setattr(ned_main, "llm_send", fake)
    res = ned_main.llm_transcript_digest(
        "https://x", "transcript body", [0],
        kind="podcast", title="Ep", channel="Show", portfolio_context="CTX-BLOCK",
    )
    assert res.ok and res.relevance == "Medium" and res.parts == 1
    call = fake.calls[0]
    assert call["model"] == ned_main.TRANSCRIPT_MODEL == "claude-sonnet-4-6"
    assert call["max_tokens"] == 6000
    assert "CTX-BLOCK" in call["system"]
    assert "slow to buy, even slower to sell" in call["system"]
    user = call["messages"][0]["content"]
    assert "Title: Ep" in user and "Show: Show" in user and "transcript body" in user


def test_digest_markdown_reply_is_kept_not_failed(ned_main, monkeypatch):
    monkeypatch.setattr(ned_main, "llm_send", _FakeSend(["## Digest\n- a point"]))
    res = ned_main.llm_transcript_digest("u", "t", [0])
    assert res.ok and res.digest_json is None and res.relevance == "Unrated"
    assert "a point" in res.markdown


def test_digest_truncated_reply_is_a_failure(ned_main, monkeypatch):
    monkeypatch.setattr(ned_main, "llm_send", _FakeSend([('{"tldr": "cut off', "max_tokens")]))
    res = ned_main.llm_transcript_digest("u", "t", [0])
    assert not res.ok and "max_tokens" in res.error and res.relevance == "Failed"


def test_digest_api_error_is_reported(ned_main, monkeypatch):
    monkeypatch.setattr(ned_main, "llm_send", _FakeSend([RuntimeError("overloaded")]))
    res = ned_main.llm_transcript_digest("u", "t", [0])
    assert "RuntimeError" in res.error and "overloaded" in res.error


def test_digest_without_key_fails_explicitly(monkeypatch):
    import ned.main as m
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    res = m.llm_transcript_digest("u", "t", [0])
    assert res.error == "ANTHROPIC_API_KEY is not set"


def test_long_transcript_is_chunked_not_clipped(ned_main, monkeypatch):
    monkeypatch.setattr(ned_main, "TRANSCRIPT_LLM_MAX_CHARS", 1000)
    monkeypatch.setattr(ned_main, "TRANSCRIPT_CHUNK_CHARS", 800)
    transcript = ("Word word word. " * 150).strip()      # ~2.4k chars -> 3+ parts
    n = len(chunk_text(transcript, 800))
    fake = _FakeSend([f"- note {i}" for i in range(n)] + [json.dumps(_fixture())])
    monkeypatch.setattr(ned_main, "llm_send", fake)
    res = ned_main.llm_transcript_digest("u", transcript, [0])
    assert res.ok and res.parts == n and n >= 3
    assert len(fake.calls) == n + 1
    final = fake.calls[-1]["messages"][0]["content"]
    assert "EXTRACTION NOTES" in final and "- note 0" in final and f"Part {n} of {n}" in final


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------
def _result(**kw) -> TranscriptResult:
    base = dict(
        video_id="hitting-the-buffers", language="English", language_code="en",
        is_generated=True, plain_text="body", timestamped_text="[00:00] body",
        segments=[], title="Hitting the Buffers", channel="The Story of Money",
    )
    base.update(kw)
    return TranscriptResult(**base)


def test_subject_format():
    import ned.main as m
    ok = DigestResult(digest_json=normalise_digest(_fixture()), markdown="x")
    assert m._digest_subject("podcast", "Hitting the Buffers", ok) == \
        "Ned [Medium] Podcast: Hitting the Buffers"
    failed = DigestResult(error="boom")
    assert m._digest_subject("youtube", "T", failed) == "Ned [Digest failed] YouTube: T"


def test_email_shows_only_non_neutral_impact_and_points_to_pdf():
    import ned.main as m
    digest = DigestResult(digest_json=normalise_digest(_fixture()), markdown="x")
    plain, html = m._podcast_email("https://src", _result(), digest)
    assert "CPU" in html and "Kill condition at risk" in html
    assert "MQG" not in html                  # Neutral row left out of the email
    assert "attached PDF" in html and "attached PDF" in plain
    for banned in ("display:flex", "display:grid", "<style", "class="):
        assert banned not in html


def test_email_says_digest_failed_with_reason():
    import ned.main as m
    plain, html = m._transcript_email("https://src", _result(), DigestResult(error="HTTP 529: overloaded"))
    assert "Digest failed: HTTP 529: overloaded" in plain
    assert "Digest failed:</b> HTTP 529: overloaded" in html


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def _html(**kw):
    import datetime as dt
    args = dict(
        kind="podcast", title="Hitting the Buffers", source_url="https://src",
        timestamp=dt.datetime(2026, 9, 25), digest_markdown="", transcript_text="body text.",
    )
    args.update(kw)
    return pdfmod._build_html(**args)


def test_pdf_renders_structured_digest_sections():
    d = normalise_digest(_fixture())
    html = _html(digest_json=d, channel="The Story of Money", provenance="Whisper transcription")
    for s in ("Medium relevance", "What it means for my portfolio", "Kill condition at risk",
              "Pillar P2", "The medium-term lens", "The parallel", "Where it breaks",
              "Sceptic&#x27;s corner", "Companies mentioned", "What I&#x27;m watching next",
              "Full transcript", "The Story of Money", "Whisper transcription"):
        assert s in html, s
    # Kill condition card comes before Supports.
    assert html.index("Kill condition at risk</span>") < html.index("Supports</span>")
    for banned in ("display: flex", "display:flex", "display: grid", "display:grid"):
        assert banned not in html


def test_pdf_empty_impact_says_no_material_impact():
    d = normalise_digest({**_fixture(), "portfolio_impact": []})
    assert "No material impact on current holdings or watchlists." in _html(digest_json=d)


def test_pdf_failure_is_explicit_on_cover_and_body():
    html = _html(digest_error="the reply was cut off at max_tokens (6000)")
    assert "Digest failed</span>" in html
    assert "Digest failed: the reply was cut off" in html


def test_pdf_cover_notes_chunked_analysis():
    html = _html(digest_json=normalise_digest(_fixture()), parts=3)
    assert "Transcript was long; analysed in 3 parts" in html


def test_pdf_markdown_fallback_uses_new_styling():
    html = _html(digest_markdown="1. **TL;DR**: Old style digest.\n\n- a point")
    assert "Unrated relevance" in html and "Old style digest." in html
    assert "<ul class='gold'>" in html


def test_pdf_transcript_timestamps_from_segments():
    segs = [{"text": f"Sentence number {i}.", "start": i * 30.0, "duration": 30.0} for i in range(40)]
    html = _html(segments=segs, transcript_text=" ".join(s["text"] for s in segs))
    assert "<span class='ts'>0:00</span>" in html
    assert "<span class='ts'>3:00</span>" in html


def test_pdf_footer_title_is_css_escaped():
    html = _html(title='He said "buy" \\ sell')
    assert 'He said \\"buy\\" \\\\ sell' in html


def test_sample_pdf_renders_with_weasyprint(tmp_path):
    pytest.importorskip("weasyprint")
    import scripts.ned_sample_transcript_pdf as sample
    out = tmp_path / "sample.pdf"
    assert sample.main(["--backend", "weasyprint", "--out", str(out)]) == 0
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 20_000


# ---------------------------------------------------------------------------
# End to end: runs record the new shape
# ---------------------------------------------------------------------------
def _isolate(monkeypatch, tmp_path, m):
    monkeypatch.setattr(m, "_TRANSCRIPTS_HISTORY_PATH", tmp_path / "docs" / "data" / "transcripts.json")
    monkeypatch.setattr(m, "_TRANSCRIPT_PDFS_DIR", tmp_path / "docs" / "transcripts")
    monkeypatch.setattr(m, "TRANSCRIPTS_DIR", tmp_path / "transcripts")
    monkeypatch.setattr("ned.transcript_pdf.build_transcript_pdf", lambda **kw: None)
    sent: dict = {}
    monkeypatch.setattr(m, "_maybe_email", lambda **kw: sent.update(kw))
    return sent


def test_podcast_run_records_digest_json_and_relevance(ned_main, monkeypatch, tmp_path):
    sent = _isolate(monkeypatch, tmp_path, ned_main)
    monkeypatch.setattr(ned_main, "fetch_podcast_transcript", lambda url: _result())
    monkeypatch.setattr(ned_main, "llm_send", _FakeSend([json.dumps(_fixture())]))
    assert ned_main.run_podcast_digest("https://podcasts.apple.com/x/id1?i=42") == 0

    entry = json.loads((tmp_path / "docs" / "data" / "transcripts.json").read_text(encoding="utf-8"))[0]
    assert entry["title"] == "Hitting the Buffers"
    assert entry["channel"] == "The Story of Money"
    assert entry["portfolio_relevance"] == "Medium"
    assert entry["digest_json"]["portfolio_impact"][0]["ticker"] == "CPU"
    assert entry["digest_markdown"].startswith("1. **TL;DR**")
    assert entry["digest_error"] == ""
    assert sent["subject"] == "Ned [Medium] Podcast: Hitting the Buffers"


def test_youtube_run_uses_oembed_title(ned_main, monkeypatch, tmp_path):
    sent = _isolate(monkeypatch, tmp_path, ned_main)
    monkeypatch.setattr(ned_main, "fetch_transcript", lambda vid, **kw: _result(
        video_id="oay6t8vh7b4", is_generated=False, title="", channel=""))
    monkeypatch.setattr(ned_main, "fetch_video_metadata",
                        lambda vid: {"title": "The Real Title", "channel": "Some Channel"})
    monkeypatch.setattr(ned_main, "llm_send", _FakeSend([json.dumps(_fixture())]))
    assert ned_main.run_transcript_digest("https://youtu.be/oay6t8vh7b4") == 0
    entry = json.loads((tmp_path / "docs" / "data" / "transcripts.json").read_text(encoding="utf-8"))[0]
    assert entry["title"] == "The Real Title" and entry["channel"] == "Some Channel"
    assert sent["subject"] == "Ned [Medium] YouTube: The Real Title"


def test_youtube_run_falls_back_to_video_id_when_oembed_fails(ned_main, monkeypatch, tmp_path):
    sent = _isolate(monkeypatch, tmp_path, ned_main)
    monkeypatch.setattr(ned_main, "fetch_transcript", lambda vid, **kw: _result(
        video_id="oay6t8vh7b4", is_generated=False, title="", channel=""))
    monkeypatch.setattr(ned_main, "fetch_video_metadata", lambda vid: {"title": "", "channel": ""})
    monkeypatch.setattr(ned_main, "llm_send", _FakeSend([RuntimeError("down")]))
    assert ned_main.run_transcript_digest("https://youtu.be/oay6t8vh7b4") == 0
    entry = json.loads((tmp_path / "docs" / "data" / "transcripts.json").read_text(encoding="utf-8"))[0]
    assert entry["title"] == "oay6t8vh7b4"
    assert entry["portfolio_relevance"] == "Failed" and "down" in entry["digest_error"]
    assert sent["subject"] == "Ned [Digest failed] YouTube: oay6t8vh7b4"


def test_fetch_video_metadata_parses_oembed(monkeypatch):
    import io
    import urllib.request
    from ned import youtube_transcript_fetcher as yt

    body = json.dumps({"title": "A Title", "author_name": "A Channel"}).encode("utf-8")

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        return _Resp(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert yt.fetch_video_metadata("oay6t8vh7b4") == {"title": "A Title", "channel": "A Channel"}
    assert seen["url"].startswith("https://www.youtube.com/oembed?url=")

    def boom(req, timeout):
        raise OSError("no network")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert yt.fetch_video_metadata("oay6t8vh7b4") == {"title": "", "channel": ""}
