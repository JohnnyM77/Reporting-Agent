# ned/transcript_pdf.py
#
# Renders a single transcript digest to a self-contained PDF so we can
#   1. attach it to the email (users can forward the whole file to peers),
#   2. link to it from the dashboard as a downloadable artifact.
#
# Two backends match this repo's established pattern (see CLAUDE.md):
#   * Windows (self-hosted, YouTube path): Playwright + installed Chrome
#     (channel='chrome'). Chrome is already provisioned on the runner for
#     Slinger's sgx_fetch, so this adds no system deps — just `pip install
#     playwright` at workflow time.
#   * Ubuntu (cloud, podcast path): weasyprint. Pure pip install. Harry
#     Hindsight already runs weasyprint on ubuntu-latest.
#
# The public entry point is build_transcript_pdf(), which:
#   * builds the HTML,
#   * auto-picks a backend (weasyprint first, then Playwright, then raises),
#   * writes to `output_path`.
#
# The HTML is one document with an @page A4 rule, inline styles only. No
# external assets — everything embeds so the PDF renders identically no
# matter where the browser fetches it from.

from __future__ import annotations

import datetime as dt
import html as htmlmod
import re
from pathlib import Path


class TranscriptPdfError(Exception):
    """PDF backend unavailable or rendering failed."""


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
_CSS = """
@page { size: A4; margin: 22mm 18mm 22mm 18mm;
        @bottom-right { content: counter(page) " / " counter(pages);
                        font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
                        font-size: 9pt; color: #64748b; } }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
       color: #0f172a; margin: 0; font-size: 11pt; line-height: 1.5; }
.cover { background: #0b1220; color: #e2e8f0; padding: 20mm 15mm;
         margin: -22mm -18mm 8mm -18mm; page-break-after: avoid; }
.cover .kind { display: inline-block; font-size: 10pt; font-weight: 800;
               padding: 4px 10px; border-radius: 4px; letter-spacing: 0.6px;
               color: #0b1220; }
.cover .kind.youtube { background: #ef4444; }
.cover .kind.podcast { background: #8b5cf6; }
.cover h1 { font-size: 22pt; margin: 10px 0 8px 0; line-height: 1.2;
            color: #f1f5f9; }
.cover .meta { font-size: 10pt; color: #94a3b8; margin-top: 12px; }
.cover .meta a { color: #60a5fa; word-break: break-all; text-decoration: none; }
.cover .meta a:hover { text-decoration: underline; }
.section-title { font-size: 15pt; font-weight: 700; margin: 18px 0 8px 0;
                 border-bottom: 2px solid #cbd5e1; padding-bottom: 4px;
                 color: #0f172a; page-break-after: avoid; }
.digest h2 { font-size: 13pt; font-weight: 700; margin: 12px 0 6px 0;
             color: #0f172a; page-break-after: avoid; }
.digest h3 { font-size: 12pt; font-weight: 700; margin: 10px 0 4px 0;
             color: #0f172a; page-break-after: avoid; }
.digest p { margin: 6px 0; }
.digest ul, .digest ol { margin: 6px 0; padding-left: 20pt; }
.digest li { margin: 3px 0; }
.digest strong { color: #0f172a; font-weight: 700; }
.digest blockquote { margin: 10px 0; padding: 8px 12px; border-left: 3px solid #cbd5e1;
                    background: #f1f5f9; color: #334155; }
.transcript { white-space: pre-wrap; font-size: 10pt; line-height: 1.55;
              color: #1e293b; }
.transcript p { margin: 6px 0; }
.no-digest { color: #64748b; font-style: italic; }
.footer-note { margin-top: 20mm; font-size: 8pt; color: #94a3b8;
               border-top: 1px solid #e2e8f0; padding-top: 6px; }
"""


def _md_to_html(md: str) -> str:
    """Render the digest markdown to a small HTML subset — same shape
    ned/main.py::_digest_markdown_to_html already handles for the email,
    plus H2/H3 headings for the PDF's larger canvas.

    Deliberately not a general markdown engine. What we need to render is
    what RESULTS_HYFY_PROMPT/llm_transcript_digest emits: a numbered
    outline, bold labels, and bullet lists.
    """
    if not md:
        return "<p class='no-digest'>LLM digest unavailable — the raw transcript follows.</p>"

    lines = md.splitlines()
    out: list[str] = []
    in_list = False
    in_para: list[str] = []

    def _flush_para():
        if in_para:
            text = " ".join(s.strip() for s in in_para)
            out.append(f"<p>{_inline(text)}</p>")
            in_para.clear()

    def _close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            _flush_para()
            _close_list()
            continue

        # Headings: ## Foo, ### Bar
        m = re.match(r"^(#{2,3})\s+(.+)$", stripped)
        if m:
            _flush_para()
            _close_list()
            level = len(m.group(1))
            tag = "h2" if level == 2 else "h3"
            out.append(f"<{tag}>{_inline(m.group(2))}</{tag}>")
            continue

        # Bullets
        m = re.match(r"^[-*•]\s+(.+)$", stripped)
        if m:
            _flush_para()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        # Numbered items: 1. **TL;DR** — ...  render as h3 + body.
        m = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if m:
            _flush_para()
            _close_list()
            body = m.group(2)
            # If the body has a bold label followed by an em/en-dash, split.
            m2 = re.match(r"^\*\*([^*]+)\*\*\s*[—\-–:]\s*(.+)$", body)
            if m2:
                out.append(f"<h3>{_inline(m2.group(1))}</h3>")
                out.append(f"<p>{_inline(m2.group(2))}</p>")
            elif re.match(r"^\*\*([^*]+)\*\*\s*$", body):
                # Just a bold header for the section, list follows.
                label = re.match(r"^\*\*([^*]+)\*\*\s*$", body).group(1)
                out.append(f"<h3>{_inline(label)}</h3>")
            else:
                out.append(f"<p>{_inline(body)}</p>")
            continue

        # Blockquote
        if stripped.startswith(">"):
            _flush_para()
            _close_list()
            out.append(f"<blockquote>{_inline(stripped[1:].strip())}</blockquote>")
            continue

        # Regular paragraph text
        _close_list()
        in_para.append(raw)

    _flush_para()
    _close_list()
    return "\n".join(out)


def _inline(text: str) -> str:
    """HTML-escape then apply **bold** + *italic* + `code`."""
    text = htmlmod.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?!\w)", r"<em>\1</em>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # Inline auto-links so the source URL stays clickable in the PDF.
    text = re.sub(
        r"(https?://[^\s<>&]+)",
        r"<a href='\1' style='color:#2563eb;text-decoration:none;'>\1</a>",
        text,
    )
    return text


def _transcript_html(text: str) -> str:
    """Render the raw transcript as paragraph blocks so it reflows cleanly
    in a Chrome print / weasyprint layout (a single 60k-char <p> with
    pre-wrap breaks page-break heuristics)."""
    if not text.strip():
        return "<p class='no-digest'>No transcript body was captured.</p>"
    # Paragraphs: split on double newlines when present, else on ~600 chars
    # near sentence boundaries so the flow stays readable.
    paras: list[str] = []
    for block in re.split(r"\n\s*\n+", text.strip()):
        block = block.strip()
        if not block:
            continue
        if len(block) < 800:
            paras.append(block)
            continue
        # Chunk long blocks at sentence boundaries.
        current = ""
        for sent in re.split(r"(?<=[.!?])\s+", block):
            if len(current) + len(sent) > 700:
                if current:
                    paras.append(current.strip())
                current = sent + " "
            else:
                current += sent + " "
        if current.strip():
            paras.append(current.strip())
    return "\n".join(f"<p>{htmlmod.escape(p)}</p>" for p in paras)


def _build_html(
    *,
    kind: str,
    title: str,
    source_url: str,
    timestamp: dt.datetime,
    digest_markdown: str,
    transcript_text: str,
) -> str:
    kind_label = "YouTube" if kind == "youtube" else "Podcast"
    kind_class = "youtube" if kind == "youtube" else "podcast"
    ts_str = timestamp.strftime("%d %b %Y · %H:%M UTC")
    esc_title = htmlmod.escape(title or "(untitled)")
    esc_url = htmlmod.escape(source_url)
    digest_html = _md_to_html(digest_markdown)
    transcript_html = _transcript_html(transcript_text)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>Ned Transcript · {esc_title}</title>
<style>{_CSS}</style>
</head><body>
  <div class="cover">
    <span class="kind {kind_class}">{kind_label}</span>
    <h1>{esc_title}</h1>
    <div class="meta">Generated by Ned the News Agent · {ts_str}<br>
      Source: <a href="{esc_url}">{esc_url}</a>
    </div>
  </div>

  <div class="section-title">Digest</div>
  <div class="digest">{digest_html}</div>

  <div class="section-title" style="page-break-before: always;">Full transcript</div>
  <div class="transcript">{transcript_html}</div>

  <div class="footer-note">
    Transcript produced by
    { "the show's own <podcast:transcript>" if kind == "podcast" else "YouTube captions" }
    or Whisper (fallback). Digest generated by Anthropic Claude.
    JohnnyM77/Reporting-Agent
  </div>
</body></html>
"""


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
def _render_with_weasyprint(html: str, out_path: Path) -> None:
    from weasyprint import HTML  # local import so ubuntu-only workflows install it lazily

    HTML(string=html).write_pdf(str(out_path))


def _render_with_playwright(html: str, out_path: Path) -> None:
    from playwright.sync_api import sync_playwright  # local import

    with sync_playwright() as p:
        # `channel='chrome'` uses the installed Chrome that's already on the
        # self-hosted Windows runner (per CLAUDE.md — provisioned for SGX).
        # If Chrome isn't installed we fall back to bundled Chromium, which
        # ubuntu workflows can pre-install with `playwright install chromium`.
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="domcontentloaded")
            page.pdf(
                path=str(out_path),
                format="A4",
                margin={"top": "22mm", "bottom": "22mm", "left": "18mm", "right": "18mm"},
                print_background=True,
                display_header_footer=False,
            )
        finally:
            browser.close()


def build_transcript_pdf(
    *,
    kind: str,
    title: str,
    source_url: str,
    digest_markdown: str,
    transcript_text: str,
    output_path: Path,
    timestamp: dt.datetime | None = None,
) -> Path:
    """Build the PDF and write it to output_path.

    Backend selection:
      1. weasyprint if importable (cleanest install on ubuntu-latest).
      2. Playwright + Chrome/Chromium.
      3. Raise TranscriptPdfError.

    Never emits an image-only or empty PDF — a rendering exception from
    either backend bubbles up as TranscriptPdfError so callers can log
    and move on without pretending the file exists.
    """
    if kind not in ("youtube", "podcast"):
        raise ValueError(f"unknown kind: {kind!r}")

    ts = timestamp or dt.datetime.utcnow()
    html = _build_html(
        kind=kind,
        title=title,
        source_url=source_url,
        timestamp=ts,
        digest_markdown=digest_markdown,
        transcript_text=transcript_text,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    for name, fn in (("weasyprint", _render_with_weasyprint),
                     ("playwright", _render_with_playwright)):
        try:
            fn(html, output_path)
            if output_path.exists() and output_path.stat().st_size > 512:
                return output_path
            errors.append(f"{name}: produced no output or file too small")
        except ImportError as exc:
            errors.append(f"{name}: not installed ({exc})")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")

    raise TranscriptPdfError(
        "Could not render transcript PDF; tried: " + "; ".join(errors)
    )
