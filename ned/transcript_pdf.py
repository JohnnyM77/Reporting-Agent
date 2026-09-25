# ned/transcript_pdf.py
#
# Renders a single transcript digest to a self-contained PDF so we can
#   1. attach it to the email (users can forward the whole file to peers),
#   2. link to it from the dashboard as a downloadable artifact.
#
# The PDF is the product; the email only points at it. It is laid out as a
# research note: a navy cover with the relevance call and the TL;DR, then the
# portfolio impact as cards, the medium-term lens, the rest of the analysis,
# and the full transcript as an appendix.
#
# Two backends match this repo's established pattern (see CLAUDE.md):
#   * Windows (self-hosted, YouTube path): Playwright + installed Chrome
#     (channel='chrome'). Chrome is already provisioned on the runner for
#     Slinger's sgx_fetch, so this adds no system deps — just `pip install
#     playwright` at workflow time.
#   * Ubuntu (cloud, podcast path): weasyprint. Pure pip install. Harry
#     Hindsight already runs weasyprint on ubuntu-latest.
#
# Both must render the same document, so the CSS keeps to what both engines
# handle: block layout and tables (no flexbox or grid), system font stacks
# (no web fonts), @page margin boxes for the running footer, :first for the
# full-bleed cover. The page geometry lives in CSS; the Playwright call only
# asks Chrome to honour it (prefer_css_page_size).

from __future__ import annotations

import datetime as dt
import html as htmlmod
import re
from pathlib import Path


class TranscriptPdfError(Exception):
    """PDF backend unavailable or rendering failed."""


# ---------------------------------------------------------------------------
# Palette + type
# ---------------------------------------------------------------------------
NAVY = "#0B1F3A"
GOLD = "#C9A227"
INK = "#1A2233"
SLATE = "#5B6577"
HAIR = "#E3E6EB"
TINT = "#F4F6FA"

EFFECT_COLOURS = {
    "Kill condition at risk": "#B42318",
    "Challenges": "#B7791F",
    "Supports": "#2E7D5B",
    "Neutral": "#8A94A6",
}

# (background, text, border) for the cover's relevance badge.
RELEVANCE_STYLES = {
    "High": (NAVY, GOLD, NAVY),
    "Medium": (GOLD, NAVY, GOLD),
    "Low": ("#FFFFFF", SLATE, "#C4CAD4"),
    "None": ("#FFFFFF", "#8A94A6", HAIR),
    "Unrated": ("#FFFFFF", SLATE, "#C4CAD4"),
    "Failed": ("#B42318", "#FFFFFF", "#B42318"),
}

SERIF = "Georgia, \"Times New Roman\", serif"
SANS = "-apple-system, \"Segoe UI\", Roboto, Helvetica, Arial, sans-serif"

# Seconds between timestamp tags in the transcript appendix.
_TIMESTAMP_EVERY = 180


def _css(footer_title: str) -> str:
    return f"""
@page {{
  size: A4;
  margin: 25mm 22mm 25mm 22mm;
  @bottom-center {{
    content: "Ned  |  {footer_title}  |  page " counter(page) " of " counter(pages);
    white-space: pre;
    font-family: {SANS}; font-size: 8pt; color: {SLATE};
    border-top: 0.5pt solid {HAIR};
    padding-top: 3mm;
    vertical-align: top;
    width: 166mm;
  }}
}}
@page :first {{
  margin: 0;
  @bottom-center {{ content: none; border: none; }}
}}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; padding: 0; }}
body {{ font-family: {SANS}; font-size: 10.5pt; line-height: 1.6; color: {INK};
        background: #FFFFFF; }}
a {{ color: {NAVY}; text-decoration: none; }}
p {{ margin: 0 0 3mm 0; }}

/* ---- cover (page 1, full bleed) ---- */
.cover {{ height: 297mm; page-break-after: always; overflow: hidden; }}
.cover-band {{ background: {NAVY}; height: 134mm; padding: 34mm 22mm 0 22mm; }}
.kind {{ font-size: 8pt; letter-spacing: 0.12em; text-transform: uppercase;
         color: {GOLD}; font-weight: 700; }}
.cover h1 {{ font-family: {SERIF}; font-weight: normal; font-size: 28pt;
             line-height: 1.18; color: #FFFFFF; margin: 7mm 0 0 0; max-width: 160mm; }}
.cover .show {{ font-size: 11pt; color: #C9D1DE; margin-top: 5mm; }}
.cover-meta {{ font-size: 8.5pt; color: #9AA6B8; margin-top: 9mm; line-height: 1.7; }}
.cover-meta .sep {{ color: {GOLD}; padding: 0 2mm; }}
.cover-body {{ padding: 16mm 22mm 0 22mm; }}
.badge {{ display: inline-block; font-size: 11pt; font-weight: 700;
          letter-spacing: 0.12em; text-transform: uppercase;
          padding: 2.5mm 5mm; border: 0.75pt solid; border-radius: 1mm; }}
.badge-reason {{ font-size: 11pt; color: {SLATE}; margin-top: 5mm; max-width: 150mm; }}
.pull {{ border-left: 1.2pt solid {GOLD}; padding: 1mm 0 1mm 7mm; margin-top: 14mm;
         max-width: 150mm; font-family: {SERIF}; font-size: 14pt; line-height: 1.55;
         color: {INK}; }}
.pull-label {{ margin-top: 14mm; }}
.cover-fail {{ margin-top: 6mm; max-width: 150mm; color: #B42318; font-size: 10.5pt; }}

/* ---- sections ---- */
.section {{ margin-top: 10mm; }}
.section.first {{ margin-top: 0; }}
.label {{ font-size: 8pt; letter-spacing: 0.12em; text-transform: uppercase;
          color: {GOLD}; font-weight: 700; page-break-after: avoid; }}
h2 {{ font-family: {SERIF}; font-weight: normal; font-size: 16pt; color: {NAVY};
      margin: 1.5mm 0 6mm 0; line-height: 1.3; page-break-after: avoid; }}
h3 {{ font-family: {SERIF}; font-weight: normal; font-size: 12pt; color: {NAVY};
      margin: 7mm 0 2.5mm 0; page-break-after: avoid; }}
.measure {{ max-width: 150mm; }}
.muted {{ color: {SLATE}; }}

ul.gold {{ list-style: none; margin: 0 0 3mm 0; padding: 0; max-width: 150mm; }}
ul.gold li {{ position: relative; padding-left: 6mm; margin: 0 0 2.5mm 0; }}
ul.gold li:before {{ content: ""; position: absolute; left: 0.5mm; top: 2.3mm;
                     width: 1.6mm; height: 1.6mm; background: {GOLD}; }}

/* ---- impact cards ---- */
.card {{ border: 0.5pt solid {HAIR}; border-left: 2pt solid; padding: 4.5mm 5.5mm 4mm 5.5mm;
         margin: 0 0 6mm 0; page-break-inside: avoid; }}
.card table {{ width: 100%; border-collapse: collapse; }}
.card td {{ padding: 0; vertical-align: baseline; }}
.card .ticker {{ font-family: {SERIF}; font-weight: bold; font-size: 13pt; color: {NAVY}; }}
.card .cname {{ color: {SLATE}; font-size: 9.5pt; padding-left: 2mm; }}
.card .right {{ text-align: right; white-space: nowrap; }}
.pill {{ display: inline-block; color: #FFFFFF; font-size: 7pt; font-weight: 700;
         letter-spacing: 0.08em; text-transform: uppercase; padding: 0.8mm 2.4mm;
         border-radius: 3mm; }}
.card .pillar {{ font-size: 8pt; letter-spacing: 0.12em; text-transform: uppercase;
                 color: {SLATE}; margin-top: 2mm; }}
.card .expl {{ margin: 2.5mm 0 0 0; max-width: 150mm; }}
.card .action {{ font-size: 8pt; letter-spacing: 0.12em; text-transform: uppercase;
                 color: {NAVY}; margin-top: 3mm; font-weight: 700; }}
.card .action span {{ color: {SLATE}; font-weight: normal; }}

.callout {{ background: {TINT}; border-left: 2pt solid {GOLD}; padding: 5mm 7mm;
            margin: 8mm 0 0 0; page-break-inside: avoid; }}
.callout .label {{ margin-bottom: 2mm; }}
.callout p {{ font-size: 11pt; margin: 0; max-width: 145mm; }}
.callout.fail {{ background: #FDF1F0; border-left-color: #B42318; color: #7A1A12; }}
.callout.fail .label {{ color: #B42318; }}

/* ---- parallels ---- */
.parallel {{ margin: 0 0 6mm 0; page-break-inside: avoid; max-width: 150mm; }}
.parallel .sub {{ font-size: 8pt; letter-spacing: 0.12em; text-transform: uppercase;
                  color: {SLATE}; margin: 0 0 1mm 0; }}
.parallel .breaks {{ margin-top: 2.5mm; padding-left: 4mm; border-left: 0.5pt solid {HAIR}; }}

/* ---- companies table ---- */
table.companies {{ width: 100%; border-collapse: collapse; font-size: 9.5pt; }}
table.companies th {{ text-align: left; font-size: 8pt; letter-spacing: 0.12em;
                      text-transform: uppercase; color: {SLATE}; font-weight: 700;
                      padding: 0 3mm 2mm 0; border-bottom: 0.5pt solid {HAIR}; }}
table.companies td {{ padding: 2.5mm 3mm 2.5mm 0; border-bottom: 0.5pt solid {HAIR};
                      vertical-align: top; }}
table.companies tr {{ page-break-inside: avoid; }}
table.companies td.tick {{ font-weight: 700; color: {NAVY}; white-space: nowrap; }}

/* ---- checklist ---- */
ul.check {{ list-style: none; margin: 0; padding: 0; max-width: 150mm; }}
ul.check li {{ position: relative; padding-left: 8mm; margin: 0 0 3mm 0; }}
ul.check li:before {{ content: ""; position: absolute; left: 0; top: 1.2mm;
                      width: 3mm; height: 3mm; border: 0.75pt solid {SLATE}; }}

/* ---- markdown fallback ---- */
.md h2 {{ font-size: 13pt; margin: 8mm 0 3mm 0; }}
.md h3 {{ font-size: 11.5pt; margin: 6mm 0 2mm 0; }}
.md p, .md ul {{ max-width: 150mm; }}
.md blockquote {{ margin: 4mm 0; padding: 3mm 5mm; border-left: 1.2pt solid {GOLD};
                  background: {TINT}; }}
.no-digest {{ color: {SLATE}; font-style: italic; }}

/* ---- transcript appendix ---- */
.appendix {{ page-break-before: always; }}
.transcript {{ font-size: 9pt; line-height: 1.55; color: {SLATE}; }}
.transcript p {{ margin: 0 0 2.6mm 0; max-width: 155mm; }}
.ts {{ display: inline-block; font-size: 7pt; color: #8A94A6; border: 0.5pt solid {HAIR};
       border-radius: 1mm; padding: 0 1.2mm; margin-right: 1.5mm; letter-spacing: 0.04em;
       font-variant-numeric: tabular-nums; }}
.source {{ margin-top: 12mm; padding-top: 3mm; border-top: 0.5pt solid {HAIR};
           font-size: 8pt; color: {SLATE}; }}
.source a {{ word-break: break-all; }}
"""


# ---------------------------------------------------------------------------
# Markdown fallback (older runs, markdown-only replies, raw failed output)
# ---------------------------------------------------------------------------
def _md_to_html(md: str) -> str:
    """Render digest markdown to a small HTML subset: headings, bullets,
    numbered bold-label sections, blockquotes, paragraphs.

    Deliberately not a general markdown engine; it covers what the old
    digest prompt emitted and what digest_json_to_markdown produces.
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

        m = re.match(r"^(#{2,3})\s+(.+)$", stripped)
        if m:
            _flush_para()
            _close_list()
            tag = "h2" if len(m.group(1)) == 2 else "h3"
            out.append(f"<{tag}>{_inline(m.group(2))}</{tag}>")
            continue

        m = re.match(r"^[-*•]\s+(.+)$", stripped)
        if m:
            _flush_para()
            if not in_list:
                out.append("<ul class='gold'>")
                in_list = True
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        # Numbered items: 1. **TL;DR** — ...  render as h3 + body.
        m = re.match(r"^(\d+)\.\s+(.+)$", stripped)
        if m:
            _flush_para()
            _close_list()
            body = m.group(2)
            m2 = re.match(r"^\*\*([^*]+)\*\*\s*[—\-–:]\s*(.+)$", body)
            if m2:
                out.append(f"<h3>{_inline(m2.group(1))}</h3>")
                out.append(f"<p>{_inline(m2.group(2))}</p>")
            elif re.match(r"^\*\*([^*]+)\*\*\s*:?\s*$", body):
                label = re.match(r"^\*\*([^*]+)\*\*", body).group(1)
                out.append(f"<h3>{_inline(label)}</h3>")
            else:
                out.append(f"<p>{_inline(body)}</p>")
            continue

        if stripped.startswith(">"):
            _flush_para()
            _close_list()
            out.append(f"<blockquote>{_inline(stripped[1:].strip())}</blockquote>")
            continue

        _close_list()
        in_para.append(raw)

    _flush_para()
    _close_list()
    return "\n".join(out)


def _inline(text: str) -> str:
    """HTML-escape then apply **bold** + *italic* + `code` + auto-links."""
    text = htmlmod.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\n]+?)\*(?!\w)", r"<em>\1</em>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"(https?://[^\s<>&]+)", r"<a href='\1'>\1</a>", text)
    return text


def _esc(text) -> str:
    return htmlmod.escape(str(text or ""))


def _tldr_from_markdown(md: str) -> str:
    m = re.search(r"(?is)\*\*TL[;\s]*DR\*\*\s*[—\-–:]\s*(.+?)(?:\n\s*\n|\n\s*\d\.|\Z)", md or "")
    if not m:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[*_`]", "", m.group(1))).strip()


# ---------------------------------------------------------------------------
# Transcript appendix
# ---------------------------------------------------------------------------
def _fmt_ts(seconds: float) -> str:
    s = int(seconds or 0)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


_SENTENCE_END = re.compile(r"[.!?][\"')\]]?(\s|$)")


def _transcript_html(text: str) -> str:
    """Render the raw transcript as paragraph blocks so it reflows cleanly
    in a Chrome print / weasyprint layout (a single 60k-char <p> with
    pre-wrap breaks page-break heuristics). Paragraphs are ~6 sentences,
    capped at ~700 chars for unpunctuated captions."""
    if not (text or "").strip():
        return "<p class='no-digest'>No transcript body was captured.</p>"
    paras: list[str] = []
    for block in re.split(r"\n\s*\n+", text.strip()):
        block = block.strip()
        if not block:
            continue
        current, sentences = "", 0
        for sent in re.split(r"(?<=[.!?])\s+", block):
            if current and (sentences >= 6 or len(current) + len(sent) > 700):
                paras.append(current.strip())
                current, sentences = "", 0
            current += sent + " "
            sentences += 1
        if current.strip():
            paras.append(current.strip())
    return "\n".join(f"<p>{htmlmod.escape(p)}</p>" for p in paras)


def _transcript_html_from_segments(segments: list[dict]) -> str:
    """Paragraphs of ~6 sentences built from timed segments, with a small
    grey time tag on the first paragraph past each _TIMESTAMP_EVERY mark."""
    paras: list[tuple[float | None, str]] = []
    buf: list[str] = []
    buf_start: float | None = None
    sentences = 0
    next_mark = 0.0

    def flush():
        nonlocal buf, buf_start, sentences
        text = " ".join(buf).strip()
        if text:
            paras.append((buf_start, text))
        buf, buf_start, sentences = [], None, 0

    for seg in segments:
        t = " ".join(str(seg.get("text") or "").split())
        if not t:
            continue
        if buf_start is None:
            buf_start = float(seg.get("start") or 0.0)
        buf.append(t)
        sentences += len(_SENTENCE_END.findall(t + " "))
        if sentences >= 6 or sum(len(b) + 1 for b in buf) > 700:
            flush()
    flush()

    out = []
    for start, text in paras:
        tag = ""
        if start is not None and start >= next_mark:
            tag = f"<span class='ts'>{_fmt_ts(start)}</span>"
            while next_mark <= start:
                next_mark += _TIMESTAMP_EVERY
        out.append(f"<p>{tag}{htmlmod.escape(text)}</p>")
    return "\n".join(out)


def _has_timings(segments) -> bool:
    return bool(segments) and any(
        isinstance(s, dict) and s.get("text") and s.get("start") is not None for s in segments
    ) and len(segments) > 1


# ---------------------------------------------------------------------------
# Structured digest sections
# ---------------------------------------------------------------------------
def _section(label: str, heading: str, body: str, first: bool = False) -> str:
    cls = "section first" if first else "section"
    return (
        f"<div class='{cls}'><div class='label'>{_esc(label)}</div>"
        f"<h2>{_esc(heading)}</h2>{body}</div>"
    )


def _bullets(items: list[str]) -> str:
    return "<ul class='gold'>" + "".join(f"<li>{_esc(i)}</li>" for i in items) + "</ul>"


def _impact_card(it: dict) -> str:
    effect = it.get("effect", "Neutral")
    colour = EFFECT_COLOURS.get(effect, EFFECT_COLOURS["Neutral"])
    meta = " &middot; ".join(_esc(x) for x in (it.get("name"), it.get("type"), it.get("link")) if x)
    pillar = (
        f"<div class='pillar'>Pillar {_esc(it['pillar'])}</div>" if it.get("pillar") else ""
    )
    return (
        f"<div class='card' style='border-left-color:{colour}'>"
        "<table><tr>"
        f"<td><span class='ticker'>{_esc(it.get('ticker'))}</span>"
        f"<span class='cname'>{meta}</span></td>"
        f"<td class='right'><span class='pill' style='background:{colour}'>{_esc(effect)}</span></td>"
        "</tr></table>"
        f"{pillar}"
        f"<p class='expl'>{_esc(it.get('explanation'))}</p>"
        f"<div class='action'>Action <span>&middot;</span> {_esc(it.get('action'))}</div>"
        "</div>"
    )


def _digest_sections(d: dict) -> str:
    parts: list[str] = []

    impact = d.get("portfolio_impact") or []
    body = (
        "".join(_impact_card(it) for it in impact)
        if impact else
        "<p class='measure muted'>No material impact on current holdings or watchlists.</p>"
    )
    if d.get("so_what"):
        body += (
            "<div class='callout'><div class='label'>So what</div>"
            f"<p>{_esc(d['so_what'])}</p></div>"
        )
    parts.append(_section("Portfolio", "What it means for my portfolio", body, first=True))

    lens = d.get("medium_term_lens") or {}
    lens_body = ""
    if lens.get("themes"):
        lens_body += "<h3>Themes</h3>" + _bullets(lens["themes"])
    if lens.get("historical_parallels"):
        lens_body += "<h3>Historical parallels</h3>"
        for p in lens["historical_parallels"]:
            lens_body += "<div class='parallel'>"
            if p.get("parallel"):
                lens_body += f"<div class='sub'>The parallel</div><p>{_esc(p['parallel'])}</p>"
            if p.get("where_it_breaks"):
                lens_body += (
                    "<div class='breaks'><div class='sub'>Where it breaks</div>"
                    f"<p>{_esc(p['where_it_breaks'])}</p></div>"
                )
            lens_body += "</div>"
    if lens.get("second_order_effects"):
        lens_body += "<h3>Second-order effects</h3>" + _bullets(lens["second_order_effects"])
    if lens.get("winners_and_losers"):
        lens_body += (
            "<h3>Winners and losers</h3>"
            f"<p class='measure'>{_esc(lens['winners_and_losers'])}</p>"
        )
    if lens_body:
        parts.append(_section("3 to 5 years", "The medium-term lens", lens_body))

    if d.get("key_points"):
        parts.append(_section("The episode", "What was said", _bullets(d["key_points"])))
    if d.get("sceptics_corner"):
        parts.append(_section(
            "Test the story", "Sceptic's corner",
            f"<p class='measure'>{_esc(d['sceptics_corner'])}</p>",
        ))
    if d.get("new_ideas"):
        parts.append(_section("Beyond the portfolio", "New ideas", _bullets(d["new_ideas"])))
    if d.get("companies_mentioned"):
        rows = "".join(
            f"<tr><td>{_esc(c.get('name'))}</td><td class='tick'>{_esc(c.get('ticker'))}</td>"
            f"<td>{_esc(c.get('context'))}</td></tr>"
            for c in d["companies_mentioned"]
        )
        parts.append(_section(
            "Reference", "Companies mentioned",
            "<table class='companies'><thead><tr><th style='width:32%'>Company</th>"
            "<th style='width:14%'>Ticker</th><th>Context</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>",
        ))
    if d.get("watching_next"):
        parts.append(_section(
            "Follow up", "What I'm watching next",
            "<ul class='check'>" + "".join(f"<li>{_esc(w)}</li>" for w in d["watching_next"]) + "</ul>",
        ))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------
def _css_string(text: str) -> str:
    """Escape text for use inside a CSS double-quoted string."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) > 70:
        text = text[:69].rstrip() + "…"
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _duration_from_segments(segments) -> float:
    try:
        last = max(
            float(s.get("start") or 0) + float(s.get("duration") or 0)
            for s in segments if isinstance(s, dict)
        )
        return last
    except (ValueError, TypeError):
        return 0.0


def _default_provenance(kind: str) -> str:
    return "YouTube captions" if kind == "youtube" else "Podcast transcript"


def _build_html(
    *,
    kind: str,
    title: str,
    source_url: str,
    timestamp: dt.datetime,
    digest_markdown: str,
    transcript_text: str,
    digest_json: dict | None = None,
    digest_error: str = "",
    channel: str = "",
    provenance: str = "",
    segments: list[dict] | None = None,
    parts: int = 1,
    parts_failed: int = 0,
) -> str:
    kind_label = "YouTube" if kind == "youtube" else "Podcast"
    title = (title or "").strip() or "(untitled)"
    d = digest_json or {}
    provenance = provenance or _default_provenance(kind)

    # ---- cover ----
    meta_bits = [f"Analysed {timestamp.strftime('%d %b %Y')}"]
    duration = _duration_from_segments(segments or [])
    if duration >= 60:
        meta_bits.append(f"{round(duration / 60)} min")
    meta_bits.append(f"{len(transcript_text or ''):,} characters")
    meta_bits.append(provenance)
    sep = "<span class='sep'>&middot;</span>"
    meta_line = sep.join(_esc(b) for b in meta_bits)
    if parts > 1:
        note = f"Transcript was long; analysed in {parts} parts"
        if parts_failed:
            note += f" ({parts_failed} could not be read)"
        meta_line += f"<br>{_esc(note)}"

    if digest_error:
        rel = "Failed"
    elif d:
        rel = d.get("portfolio_relevance") or "Unrated"
    else:
        rel = "Unrated"
    bg, fg, border = RELEVANCE_STYLES.get(rel, RELEVANCE_STYLES["Unrated"])
    badge_text = "Digest failed" if rel == "Failed" else f"{rel} relevance"
    badge = (
        f"<span class='badge' style='background:{bg};color:{fg};border-color:{border}'>"
        f"{_esc(badge_text)}</span>"
    )
    if digest_error:
        reason_html = f"<div class='cover-fail'>Digest failed: {_esc(digest_error)}</div>"
    elif d.get("relevance_reason"):
        reason_html = f"<div class='badge-reason'>{_esc(d['relevance_reason'])}</div>"
    elif not d:
        reason_html = "<div class='badge-reason'>The analysis came back as free text, not the structured format, so it is not rated.</div>"
    else:
        reason_html = ""

    tldr = d.get("tldr") or ("" if digest_error else _tldr_from_markdown(digest_markdown))
    pull = (
        f"<div class='label pull-label'>TL;DR</div><div class='pull' style='margin-top:3mm'>{_esc(tldr)}</div>"
        if tldr else ""
    )
    show = f"<div class='show'>{_esc(channel)}</div>" if channel else ""
    cover = (
        "<div class='cover'>"
        "<div class='cover-band'>"
        f"<div class='kind'>{_esc(kind_label)}</div>"
        f"<h1>{_esc(title)}</h1>{show}"
        f"<div class='cover-meta'>{meta_line}</div>"
        "</div>"
        f"<div class='cover-body'>{badge}{reason_html}{pull}</div>"
        "</div>"
    )

    # ---- analysis ----
    if digest_error:
        body = _section(
            "Analysis", "Digest failed",
            "<div class='callout fail'><div class='label'>What went wrong</div>"
            f"<p>{_esc(digest_error)}</p></div>"
            + (
                "<h3>Raw model output</h3><div class='md'>" + _md_to_html(digest_markdown) + "</div>"
                if digest_markdown.strip() else
                "<p class='measure muted' style='margin-top:6mm'>The full transcript follows.</p>"
            ),
            first=True,
        )
    elif d:
        body = _digest_sections(d)
    else:
        body = _section("Analysis", "Digest", f"<div class='md'>{_md_to_html(digest_markdown)}</div>", first=True)

    # ---- appendix ----
    if _has_timings(segments):
        transcript_html = _transcript_html_from_segments(segments or [])
    else:
        transcript_html = _transcript_html(transcript_text)
    esc_url = _esc(source_url)
    appendix = (
        "<div class='appendix'><div class='label'>Appendix</div><h2>Full transcript</h2>"
        f"<div class='transcript'>{transcript_html}</div>"
        f"<div class='source'>Source: <a href='{esc_url}'>{esc_url}</a><br>"
        f"Transcript: {_esc(provenance)}. Analysis by Ned (Anthropic Claude). "
        "Not financial advice.</div></div>"
    )

    footer_title = _css_string(title)
    return f"""<!doctype html>
<html lang="en-AU"><head>
<meta charset="utf-8">
<title>Ned | {_esc(title)}</title>
<style>{_css(footer_title)}</style>
</head><body>
{cover}
{body}
{appendix}
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
        # If Chrome isn't installed we fall back to bundled Chromium.
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="domcontentloaded")
            # Geometry (A4, margins, the zero-margin cover page, the running
            # footer) comes from the @page rules, so both backends lay the
            # document out the same way.
            page.pdf(
                path=str(out_path),
                prefer_css_page_size=True,
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
    digest_json: dict | None = None,
    digest_error: str = "",
    channel: str = "",
    provenance: str = "",
    segments: list[dict] | None = None,
    parts: int = 1,
    parts_failed: int = 0,
) -> Path:
    """Build the PDF and write it to output_path.

    Renders the structured digest when `digest_json` is given, else the
    markdown digest in the same styling (older runs, markdown-only replies).
    `digest_error` puts an explicit "Digest failed" on the cover and in the
    body instead of an empty analysis.

    Backend selection:
      1. weasyprint if importable (cleanest install on ubuntu-latest).
      2. Playwright + Chrome/Chromium.
      3. Raise TranscriptPdfError.
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
        digest_json=digest_json,
        digest_error=digest_error,
        channel=channel,
        provenance=provenance,
        segments=segments,
        parts=parts,
        parts_failed=parts_failed,
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
