# tests/test_ned_transcript_pdf.py
#
# Tests for Ned's transcript-to-PDF renderer.
#
# Backend calls (weasyprint / playwright) are stubbed so the tests don't
# depend on either library being installed, and the fallback ordering is
# exercised via the stubs directly. HTML/markdown rendering is exercised
# against real text so future changes to the digest shape don't silently
# break the layout.

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ned import transcript_pdf as pdfmod


# ---------------------------------------------------------------------------
# Markdown -> HTML helpers
# ---------------------------------------------------------------------------
def test_md_to_html_empty_returns_note():
    """An empty digest should not vanish the page — the transcript still
    matters. Render a small note instead so the reader sees why the digest
    section is thin."""
    out = pdfmod._md_to_html("")
    assert "unavailable" in out.lower()


def test_md_to_html_renders_bold_bullets_headings():
    md = """1. **TL;DR** — A short summary here.

## Key numbers

- **Revenue** grew 12%
- EBIT margin fell to 8%
- `net_debt` was up
"""
    html = pdfmod._md_to_html(md)
    # Numbered TL;DR gets promoted to h3+p, not left as "1. **TL;DR**"
    assert "<h3>TL;DR</h3>" in html
    assert "<h2>Key numbers</h2>" in html
    assert "<ul>" in html and "</ul>" in html
    # Inline markdown transforms
    assert "<strong>Revenue</strong>" in html
    assert "<code>net_debt</code>" in html
    # No stray markdown markers left over
    assert "**" not in html


def test_md_inline_autolink_and_italic():
    html = pdfmod._md_to_html("Regular text with *emphasis* and https://example.com/foo?q=1 as a link.")
    assert "<em>emphasis</em>" in html
    assert "href='https://example.com/foo?q=1'" in html


def test_md_html_escapes_dangerous_input():
    """User-supplied episode content flows through this renderer; a
    malicious title or a stray `<script>` in the transcript must not
    survive as executable HTML."""
    html = pdfmod._md_to_html("Nothing wrong here: <script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_transcript_html_paragraph_chunks_long_blocks():
    """A single 60k-char transcript blob shouldn't page as one giant <p>
    (breaks page-break heuristics). Long blocks split at sentence
    boundaries so pages flow."""
    long = ("Sentence one. " * 200)  # ~2800 chars, well past the 800-char split
    html = pdfmod._transcript_html(long)
    # More than one <p> means it was chunked
    assert html.count("<p>") >= 3


def test_transcript_html_empty_returns_note():
    assert "No transcript" in pdfmod._transcript_html("")


# ---------------------------------------------------------------------------
# Full HTML skeleton
# ---------------------------------------------------------------------------
def test_full_html_carries_expected_regions():
    import datetime as dt
    html = pdfmod._build_html(
        kind="podcast",
        title="Motley Fool Money — Mailbag",
        source_url="https://podcasts.apple.com/x/id1?i=42",
        timestamp=dt.datetime(2026, 9, 24, 6, 15),
        digest_markdown="1. **TL;DR** — Great episode.\n",
        transcript_text="raw transcript body",
    )
    assert "<!doctype html>" in html
    assert "Motley Fool Money" in html
    assert "Podcast" in html and "podcast" in html   # badge class + label
    assert "https://podcasts.apple.com/x/id1?i=42" in html
    assert "24 Sep 2026" in html
    # Provenance note reflects the kind.
    assert "podcast:transcript" in html


def test_full_html_youtube_variant_uses_youtube_note():
    import datetime as dt
    html = pdfmod._build_html(
        kind="youtube",
        title="oay6t8vh7b4",
        source_url="https://www.youtube.com/watch?v=oay6t8vh7b4",
        timestamp=dt.datetime.utcnow(),
        digest_markdown="",
        transcript_text="body",
    )
    assert "YouTube" in html
    assert "YouTube captions" in html


# ---------------------------------------------------------------------------
# build_transcript_pdf backend fallback
# ---------------------------------------------------------------------------
def _make_backend(monkeypatch, name: str, action):
    """Replace one backend with a stub. `action` receives (html, path) and
    can write, raise, or leave the file alone."""
    monkeypatch.setattr(pdfmod, f"_render_with_{name}", action)


def test_uses_weasyprint_when_it_writes_a_file(monkeypatch, tmp_path):
    called: list[str] = []

    def weasy(html, out):
        called.append("weasy")
        out.write_bytes(b"%PDF-1.4\n" + b"x" * 1024)  # >512 bytes so it's accepted

    def playwright(html, out):
        called.append("playwright")
        pytest.fail("playwright should not have been tried when weasyprint succeeded")

    _make_backend(monkeypatch, "weasyprint", weasy)
    _make_backend(monkeypatch, "playwright", playwright)

    out = pdfmod.build_transcript_pdf(
        kind="podcast", title="t", source_url="u",
        digest_markdown="", transcript_text="body body",
        output_path=tmp_path / "out.pdf",
    )
    assert called == ["weasy"]
    assert out.exists() and out.stat().st_size > 512


def test_falls_back_to_playwright_when_weasyprint_missing(monkeypatch, tmp_path):
    def weasy(html, out):
        raise ImportError("weasyprint not installed")

    def playwright(html, out):
        out.write_bytes(b"%PDF-1.4\n" + b"y" * 2048)

    _make_backend(monkeypatch, "weasyprint", weasy)
    _make_backend(monkeypatch, "playwright", playwright)

    out = pdfmod.build_transcript_pdf(
        kind="youtube", title="vid", source_url="u",
        digest_markdown="", transcript_text="body",
        output_path=tmp_path / "out.pdf",
    )
    assert out.stat().st_size > 512


def test_falls_back_to_playwright_when_weasyprint_crashes(monkeypatch, tmp_path):
    def weasy(html, out):
        raise RuntimeError("libpango missing")

    def playwright(html, out):
        out.write_bytes(b"%PDF-1.4\n" + b"z" * 4096)

    _make_backend(monkeypatch, "weasyprint", weasy)
    _make_backend(monkeypatch, "playwright", playwright)

    pdfmod.build_transcript_pdf(
        kind="podcast", title="t", source_url="u",
        digest_markdown="", transcript_text="body",
        output_path=tmp_path / "out.pdf",
    )


def test_raises_when_no_backend_works(monkeypatch, tmp_path):
    def weasy(html, out):
        raise ImportError("no weasyprint")

    def playwright(html, out):
        raise RuntimeError("chrome not found")

    _make_backend(monkeypatch, "weasyprint", weasy)
    _make_backend(monkeypatch, "playwright", playwright)

    with pytest.raises(pdfmod.TranscriptPdfError) as ei:
        pdfmod.build_transcript_pdf(
            kind="podcast", title="t", source_url="u",
            digest_markdown="", transcript_text="body",
            output_path=tmp_path / "out.pdf",
        )
    msg = str(ei.value)
    assert "weasyprint" in msg and "playwright" in msg


def test_rejects_tiny_backend_output_as_failure(monkeypatch, tmp_path):
    """A backend that silently writes an empty or truncated file should
    not be treated as success — the fallback must still run."""
    def weasy(html, out):
        out.write_bytes(b"tiny")  # under the 512-byte guard

    def playwright(html, out):
        out.write_bytes(b"%PDF-1.4\n" + b"w" * 1024)

    _make_backend(monkeypatch, "weasyprint", weasy)
    _make_backend(monkeypatch, "playwright", playwright)

    out = pdfmod.build_transcript_pdf(
        kind="podcast", title="t", source_url="u",
        digest_markdown="", transcript_text="body",
        output_path=tmp_path / "out.pdf",
    )
    assert out.stat().st_size > 512


def test_rejects_unknown_kind():
    with pytest.raises(ValueError):
        pdfmod.build_transcript_pdf(
            kind="tiktok", title="t", source_url="u",
            digest_markdown="", transcript_text="body",
            output_path=Path("/tmp/x.pdf"),
        )
