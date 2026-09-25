"""Bob-style HTML email and a forwardable PDF for Harry Hindsight.

The email is a glance: one 360px card per report (severity badge, the key
tests as label/value rows, the verdict) plus the JM Watch List triage. The
full analysis goes in a PDF attached to the email, one per report, so it can
be forwarded as a single file.

Email HTML follows Bob's constraints: tables and inline styles only. No
<style>, no class=, no flex or grid, because mail clients strip or mangle
them. The PDF is rendered by weasyprint (same as Bob), which does support a
stylesheet, so the PDF uses one.
"""

from __future__ import annotations

import html as htmlmod
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .render import STATUS_LABEL, _layout, clean
from .schemas import AnalysisOutput, Claim

esc = htmlmod.escape

# Bob's palette for the shell, plus Hindsight's own card colour.
COLOR_BG = "#0B1220"
COLOR_PANEL = "#111B2E"
COLOR_TEXT = "#E5E7EB"
COLOR_CARD = "#FFFFFF"
COLOR_HEADER = "#1E1B4B"
COLOR_HEADER_SUB = "#C7D2FE"
COLOR_ROW_SHADE = "#F3F4F8"
COLOR_LABEL = "#374151"
COLOR_VALUE = "#111827"
COLOR_MUTED = "#6B7280"
COLOR_TRIAGE = "#3B82F6"
EMAIL_FONT = "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Arial, sans-serif"

SEVERITY_COLOURS = {
    "GREEN": ("#15803D", "#FFFFFF"),
    "AMBER": ("#F59E0B", "#0B1220"),
    "RED": ("#B91C1C", "#FFFFFF"),
    "LOLLAPALOOZA": ("#7C3AED", "#FFFFFF"),
}
STATUS_BADGE = {
    "SKIPPED_CAP": ("SKIPPED", "#6B7280", "#FFFFFF"),
    "FAILED_API": ("ANALYSIS FAILED", "#B91C1C", "#FFFFFF"),
    "FAILED_INVALID": ("ANALYSIS FAILED", "#B91C1C", "#FFFFFF"),
    "DRY_RUN": ("DRY RUN", "#6B7280", "#FFFFFF"),
}
TAGLINE = "Looking through Harry's ass has 20:20 vision."

TRIAGE_COLOURS = {
    "WATCH": "#64748B", "PROVOCATE": "#D97706", "ESCALATE": "#B91C1C",
    "FAILED": "#B91C1C", "SKIPPED": "#6B7280", "GATE": "#64748B",
}
CLAIM_PILL = {
    "FACT": ("Fact", "#DCFCE7", "#166534"),
    "INTERPRETATION": ("Interp", "#DBEAFE", "#1E40AF"),
    "MANAGEMENT_CLAIM": ("Mgmt", "#FEF3C7", "#92400E"),
    "AGENT_CLAIM": ("Agent", "#E5E7EB", "#374151"),
    "HINDSIGHT_INFERENCE": ("Hindsight", "#E0E7FF", "#3730A3"),
}
REPORT_NAMES = {
    "SELL_ALERT": "Sell alert",
    "BOB_REVIEW": "Event review",
    "WALLY_REVIEW": "Opportunity review",
    "PORTFOLIO_REVIEW": "Portfolio review",
}


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _t(text: Any) -> str:
    return esc(clean(text))


def ref_label(ref: str) -> str:
    """Short, human label for an evidence ref. URLs become a source name."""
    if ref.startswith(("http://", "https://")):
        host = urlparse(ref).netloc.lower()
        if "asx.com.au" in host:
            return "ASX announcement"
        return host.replace("www.", "") or "source"
    return ref


def _facts(b: dict):
    return b.get("facts")


def position_line(b: dict) -> str:
    f = _facts(b)
    if f is None or not f.avg_cost:
        return ""
    bits = []
    if f.unrealised_pct is not None:
        bits.append(f"{f.unrealised_pct:+.0f}% vs ${f.avg_cost:.2f} avg cost")
    if f.value_weight is not None:
        bits.append(f"{f.value_weight * 100:.1f}% of book")
    return " · ".join(bits)


def key_rows(b: dict) -> list[tuple[str, str, str | None]]:
    """``(label, value, colour)`` rows shown on both the card and the PDF."""
    out: AnalysisOutput = b["output"]
    bg, _ = SEVERITY_COLOURS.get(out.severity, ("#374151", "#FFFFFF"))
    rows: list[tuple[str, str, str | None]] = [("Severity", out.severity, bg)]
    if out.thesis_test:
        rows.append(("Thesis", out.thesis_test.classification.title(), None))
    if out.fresh_capital_test:
        f = out.fresh_capital_test
        rows.append(("Would buy it today?", f.would_buy_today + (f" ({clean(f.weight_if_new)})" if f.weight_if_new else ""), None))
    if out.forced_sale_test:
        rows.append(("Want it back if forced out?", out.forced_sale_test.would_rebuy, None))
    lolla = b.get("lolla")
    fired = bool(lolla and lolla.fired and out.severity == "LOLLAPALOOZA")
    rows.append(("Lollapalooza", "YES" if fired else "No", "#7C3AED" if fired else None))
    evidenced = [t.tendency for t in out.munger_scan if t.state in ("OBSERVED EVIDENCE", "CURRENTLY TRIGGERED")]
    rows.append(("Biases with evidence", ", ".join(evidenced) if evidenced else "None evidenced", None))
    pos = position_line(b)
    if pos:
        rows.append(("Position", pos, None))
    return rows


def _source_urls(b: dict) -> list[str]:
    ev = b.get("event")
    urls = []
    if ev is not None and str(ev.source_report_ref).startswith("http"):
        urls.append(ev.source_report_ref)
    return urls


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def _badge(text: str, bg: str, fg: str) -> str:
    return (f'<span style="display:inline-block; background:{bg}; color:{fg}; font-size:10px; font-weight:800;'
            f' letter-spacing:0.8px; padding:3px 7px; border-radius:4px;">{esc(text)}</span>')


def _card(header_html: str, rows_html: str, body_html: str, foot_html: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"'
        ' style="border-collapse:collapse; margin:14px 0;"><tr><td align="center">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="360"'
        f' style="width:100%; max-width:360px; border-collapse:collapse; background:{COLOR_CARD};'
        ' border-radius:10px; overflow:hidden;">'
        f'<tr><td colspan="2" style="background:{COLOR_HEADER}; padding:13px 14px; font-family:{EMAIL_FONT};">'
        f'{header_html}</td></tr>{rows_html}{body_html}{foot_html}'
        '</table></td></tr></table>'
    )


def _row(i: int, label: str, value: str, colour: str | None) -> str:
    bg = COLOR_ROW_SHADE if i % 2 == 0 else COLOR_CARD
    return (
        f'<tr><td style="background:{bg}; padding:10px 14px; font-size:14px; line-height:1.3; color:{COLOR_LABEL};'
        f' font-family:{EMAIL_FONT};">{esc(label)}</td>'
        f'<td align="right" style="background:{bg}; padding:10px 14px; font-size:14px; line-height:1.3;'
        f' color:{colour or COLOR_VALUE}; font-weight:700; font-family:{EMAIL_FONT};">{_t(value)}</td></tr>'
    )


def _link_row(label: str, href: str) -> str:
    return (
        f'<tr><td colspan="2" style="padding:0 14px 10px 14px; font-family:{EMAIL_FONT};">'
        f'<a href="{esc(href)}" style="display:block; padding:10px 12px; background:{COLOR_ROW_SHADE};'
        f' border:1px solid #D9DCE8; border-radius:8px; color:{COLOR_HEADER}; font-size:14px; font-weight:700;'
        f' text-decoration:none;">{esc(label)} &rsaquo;</a></td></tr>'
    )


def report_card(b: dict, pdf_attached: bool, pdf_error: str = "") -> str:
    ticker = b.get("ticker") or "PORTFOLIO"
    kind = REPORT_NAMES.get(b["report_type"], b["report_type"])
    trigger = f'<div style="color:{COLOR_HEADER_SUB}; font-size:12px; margin-top:3px; line-height:1.4;">{_t(b.get("reason", ""))}</div>'
    title = f'<div style="color:#FFFFFF; font-size:17px; font-weight:800; margin-top:7px;">{esc(ticker)} · {esc(kind)}</div>'

    if b["status"] != "OK":
        label, bg, fg = STATUS_BADGE.get(b["status"], (b["status"], "#B91C1C", "#FFFFFF"))
        detail = STATUS_LABEL.get(b["status"], b["status"])
        err = (f'<div style="font-family:monospace; font-size:12px; margin-top:6px; color:#7F1D1D;">{_t(b.get("error", ""))[:500]}</div>'
               if b.get("error") else "")
        body = (f'<tr><td colspan="2" style="padding:12px 14px; background:#FEE2E2; color:#991B1B; font-family:{EMAIL_FONT};'
                f' font-size:14px; line-height:1.45;"><strong>{esc(detail)}.</strong> No analysis exists for this item;'
                f' nothing below is a stand-in for one.{err}</td></tr>')
        if b["status"] == "SKIPPED_CAP":
            body = (f'<tr><td colspan="2" style="padding:12px 14px; background:{COLOR_ROW_SHADE}; color:{COLOR_LABEL};'
                    f' font-family:{EMAIL_FONT}; font-size:14px;">{esc(detail)}. Not an error.</td></tr>')
        pos = position_line(b)
        rows = _row(0, "Position", pos, None) if pos else ""
        return _card(_badge(label, bg, fg) + title + trigger, rows, body, "")

    out: AnalysisOutput = b["output"]
    bg, fg = SEVERITY_COLOURS.get(out.severity, ("#374151", "#FFFFFF"))
    rows = "".join(_row(i, *r) for i, r in enumerate(key_rows(b)))
    verdict = (
        f'<tr><td colspan="2" style="padding:13px 14px; font-family:{EMAIL_FONT}; font-size:15px; line-height:1.5;'
        f' color:{COLOR_VALUE}; border-top:2px solid {COLOR_HEADER};"><strong>{_t(out.verdict)}</strong>'
        f'<div style="margin-top:6px; font-size:14px; color:{COLOR_LABEL};">{_t(out.severity_reason)}</div>'
    )
    for d in out.disagreements:
        verdict += (f'<div style="margin-top:6px; font-size:13px; color:{COLOR_HEADER};">'
                    f'Disagrees with {esc(d.with_agent.title())}: {_t(d.point)}</div>')
    verdict += "</td></tr>"
    if pdf_attached:
        foot = (f'<tr><td colspan="2" style="padding:10px 14px; background:{COLOR_ROW_SHADE}; color:{COLOR_MUTED};'
                f' font-family:{EMAIL_FONT}; font-size:13px;">📎 Full analysis PDF attached to this email</td></tr>')
    else:
        foot = (f'<tr><td colspan="2" style="padding:10px 14px; background:#FEE2E2; color:#991B1B;'
                f' font-family:{EMAIL_FONT}; font-size:13px;"><strong>⚠️ PDF not attached.</strong> {_t(pdf_error)}</td></tr>')
    for url in _source_urls(b):
        foot += _link_row(f"Source: {ref_label(url)}", url)
    return _card(_badge(out.severity, bg, fg) + title + trigger, rows, verdict, foot)


def _section_bar(title: str, colour: str) -> str:
    return (f'<div style="margin:18px 0 6px 0; padding:10px 12px; background:{colour}; color:#0B1220; font-weight:800;'
            f' border-radius:10px; letter-spacing:0.6px;">{esc(title)}</div>')


_TRIAGE_RE = re.compile(r"^(\S+)\s*\|\s*([A-Z ]+?)\s*\|\s*(.*)$")


def triage_html(lines: list[str]) -> str:
    rows, quiet, extra = [], [], []
    for line in lines:
        m = _TRIAGE_RE.match(line.strip())
        if not m:
            if line.strip():
                extra.append(line.strip())
            continue
        t, status, reason = m.groups()
        if status == "NO CHANGE":
            quiet.append(t)
            continue
        key = status.split()[0]
        colour = TRIAGE_COLOURS.get(key, "#64748B")
        rows.append(
            f'<tr><td style="padding:9px 10px; border-bottom:1px solid #1F2A44; color:{COLOR_TEXT}; font-weight:800;'
            f' font-size:14px; white-space:nowrap; vertical-align:top;">{esc(t)}</td>'
            f'<td style="padding:9px 6px; border-bottom:1px solid #1F2A44; vertical-align:top;">'
            f'{_badge(status, colour, "#FFFFFF")}</td>'
            f'<td style="padding:9px 10px; border-bottom:1px solid #1F2A44; color:{COLOR_TEXT}; font-size:14px;'
            f' line-height:1.4;">{_t(reason)}</td></tr>'
        )
    out = _section_bar("JM WATCH LIST", COLOR_TRIAGE)
    if rows:
        out += (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%"'
                f' style="border-collapse:collapse; background:{COLOR_PANEL}; border-radius:10px;">{"".join(rows)}</table>')
    if quiet:
        out += (f'<div style="margin:8px 0; padding:10px 12px; background:{COLOR_PANEL}; border-radius:10px;'
                f' color:#9CA3AF; font-size:13px; line-height:1.5;">No change ({len(quiet)}): {esc(", ".join(quiet))}</div>')
    for x in extra:
        out += f'<div style="color:{COLOR_TEXT}; font-size:13px; margin:4px 0;">{_t(x)}</div>'
    return out


def build_email_html(summary: dict, pdfs: dict[str, Path], pdf_errors: dict[str, str]) -> str:
    llm = summary.get("llm", {})
    reports = summary["reports"]
    head = (
        f'<div style="padding:18px; background:{COLOR_BG}; color:{COLOR_TEXT}; font-family:{EMAIL_FONT};">'
        f'<div style="font-size:22px; font-weight:900; margin-bottom:6px;">Harry Hindsight</div>'
        f'<div style="opacity:0.9; font-size:14px; font-style:italic; margin-bottom:6px;">{esc(TAGLINE)}</div>'
        f'<div style="opacity:0.9; font-size:14px; margin-bottom:10px;">Chief Sceptic · {esc(summary["date"])}'
        f' · {len(reports)} report(s)</div>'
        f'<div style="opacity:0.75; font-size:12px; margin-bottom:14px;">Model calls {llm.get("calls", 0)}/{llm.get("cap", 0)}'
        f' &nbsp;|&nbsp; ~US${llm.get("cost_usd_estimate", 0):.2f} &nbsp;|&nbsp; Let\'s have this conversation before it\'s hindsight.</div>'
    )
    notes = []
    if summary.get("queued"):
        notes.append(f"Call cap reached: {summary['queued']} item(s) queued for the next run. Skipped, not failed.")
    failed = sum(1 for b in reports if b["status"].startswith("FAILED"))
    if failed:
        notes.append(f"{failed} analysis/analyses FAILED. The real error is on each card.")
    notes += summary.get("notes", [])
    body = "".join(f'<div style="margin:8px 0; padding:10px 12px; background:{COLOR_PANEL}; border-radius:10px;'
                   f' font-size:13px;">{_t(n)}</div>' for n in notes)
    groups = [("SELL_ALERT", "SELL ALERTS", "#F59E0B"), ("BOB_REVIEW", "EVENT REVIEWS", "#F59E0B"),
              ("WALLY_REVIEW", "OPPORTUNITIES", "#10B981"), ("PORTFOLIO_REVIEW", "PORTFOLIO", "#A78BFA")]
    for rt, title, colour in groups:
        mine = [b for b in reports if b["report_type"] == rt]
        if not mine:
            continue
        body += _section_bar(title, colour)
        for b in mine:
            key = b.get("report_id", "")
            body += report_card(b, key in pdfs, pdf_errors.get(key, ""))
    if summary.get("triage"):
        body += triage_html(summary["triage"])
    if summary.get("scorecard"):
        body += _section_bar("QUARTERLY SCORECARD", "#A78BFA")
        body += "".join(f'<div style="font-size:13px; margin:4px 0;">{_t(x)}</div>' for x in summary["scorecard"])
    foot = ('<div style="opacity:0.6; font-size:11px; margin-top:18px;">The Munger scan is a checklist for asking better'
            ' questions, not a diagnosis. Not financial advice.</div></div>')
    return head + body + foot


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

PDF_CSS = """
@page { size: A4; margin: 16mm 15mm 18mm 15mm;
  @bottom-right { content: "Page " counter(page) " of " counter(pages); font-family: 'Segoe UI', Roboto, Arial, sans-serif; font-size: 8pt; color: #6B7280; }
  @bottom-left { content: "Harry Hindsight"; font-family: 'Segoe UI', Roboto, Arial, sans-serif; font-size: 8pt; color: #6B7280; } }
body { font-family: 'Segoe UI', Roboto, Arial, sans-serif; font-size: 10pt; line-height: 1.5; color: #1F2937; }
.cover { background: #1E1B4B; color: #fff; padding: 14pt 16pt; border-radius: 6pt; }
.cover .kicker { font-size: 8pt; letter-spacing: 2pt; color: #C7D2FE; font-weight: 700; }
.cover h1 { font-size: 22pt; margin: 4pt 0 2pt 0; }
.cover .trigger { color: #C7D2FE; font-size: 9.5pt; }
.pill { display: inline-block; padding: 2pt 7pt; border-radius: 4pt; font-size: 8pt; font-weight: 800; letter-spacing: 0.6pt; }
table.keys { width: 100%; border-collapse: collapse; margin: 12pt 0; }
table.keys td { padding: 6pt 9pt; border-bottom: 1px solid #E5E7EB; }
table.keys td.v { text-align: right; font-weight: 700; }
table.keys tr:nth-child(odd) td { background: #F3F4F8; }
.verdict { border-left: 4pt solid #1E1B4B; background: #EEF2FF; padding: 9pt 12pt; margin: 10pt 0 4pt 0; page-break-inside: avoid; }
.verdict .big { font-size: 12pt; font-weight: 800; color: #1E1B4B; }
h2 { font-size: 12pt; color: #1E1B4B; border-bottom: 2px solid #C7D2FE; padding-bottom: 2pt; margin: 16pt 0 6pt 0; page-break-after: avoid; }
ul.claims { list-style: none; padding: 0; margin: 0; }
ul.claims li { margin: 0 0 6pt 0; padding-left: 0; }
.tag { display: inline-block; font-size: 7pt; font-weight: 800; padding: 1pt 5pt; border-radius: 3pt; margin-right: 4pt; vertical-align: 1pt; }
.refs { color: #6B7280; font-size: 8pt; }
.refs a { color: #4338CA; text-decoration: none; }
table.grid { width: 100%; border-collapse: collapse; font-size: 8.8pt; margin: 6pt 0; page-break-inside: auto; }
table.grid th { background: #1E1B4B; color: #fff; text-align: left; padding: 5pt 6pt; }
table.grid td { border: 1px solid #E5E7EB; padding: 5pt 6pt; vertical-align: top; }
table.grid tr { page-break-inside: avoid; }
.box { border: 1px solid #E5E7EB; border-radius: 5pt; padding: 8pt 11pt; margin: 6pt 0; page-break-inside: avoid; }
.box.lolla { border-color: #7C3AED; background: #F5F3FF; }
.muted { color: #6B7280; font-size: 8.5pt; }
.small { font-size: 8pt; color: #6B7280; }
.disclaimer { margin-top: 18pt; font-size: 8pt; color: #6B7280; border-top: 1px solid #E5E7EB; padding-top: 6pt; }
"""


def _heading(h: str) -> str:
    """'SALLY'S CASE' -> "Sally's case" (str.title() gives "Sally'S Case")."""
    h = h.strip()
    return h[:1].upper() + h[1:].lower()


def _pill(text: str, bg: str, fg: str) -> str:
    return f'<span class="pill" style="background:{bg}; color:{fg};">{esc(text)}</span>'


def _refs_html(refs: list[str]) -> str:
    if not refs:
        return ""
    bits = []
    for r in refs:
        if r.startswith(("http://", "https://")):
            bits.append(f'<a href="{esc(r)}">{esc(ref_label(r))}</a>')
        else:
            bits.append(esc(r))
    return f' <span class="refs">({", ".join(bits)})</span>'


def _claim_li(c: Claim) -> str:
    label, bg, fg = CLAIM_PILL.get(c.claim_type, ("", "#E5E7EB", "#374151"))
    return f'<li><span class="tag" style="background:{bg}; color:{fg};">{label}</span>{_t(c.text)}{_refs_html(c.source_refs)}</li>'


def _claims(out: AnalysisOutput, key: str) -> str:
    for k, claims in out.sections.items():
        if k.strip().upper() == key:
            return '<ul class="claims">' + "".join(_claim_li(c) for c in claims) + "</ul>" if claims else '<p class="muted">Nothing recorded.</p>'
    return '<p class="muted">Section missing.</p>'


def _bullets(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{_t(i)}</li>" for i in items) + "</ul>" if items else '<p class="muted">None.</p>'


def _powers_html(out: AnalysisOutput) -> str:
    if not out.seven_powers:
        return '<p class="muted">No assessment returned.</p>'

    def cell(cs: list[Claim]) -> str:
        return "<br>".join(f"{_t(c.text)}{_refs_html(c.source_refs)}" for c in cs) or "&nbsp;"

    rows = "".join(
        f"<tr><td><strong>{esc(p.power)}</strong><br><span class='small'>{esc(p.status)} · {esc(p.confidence)} confidence</span></td>"
        f"<td>{cell(p.benefit_evidence)}</td><td>{cell(p.barrier_evidence)}</td><td>{cell(p.evidence_against)}</td></tr>"
        for p in out.seven_powers)
    intro = f"<p>{_t(out.seven_powers_change)}</p>" if out.seven_powers_change else ""
    return intro + ("<table class='grid'><tr><th>Power</th><th>Benefit</th><th>Barrier</th><th>Against</th></tr>"
                    f"{rows}</table>")


def _munger_html(out: AnalysisOutput) -> str:
    shown = [t for t in out.munger_scan if t.state not in ("NOT ASSESSABLE", "NOT EVIDENT")]
    rows = "".join(
        f"<tr><td><strong>{esc(t.tendency)}</strong><br><span class='small'>{esc(t.state)}</span></td>"
        f"<td>{esc(t.direction)}</td>"
        f"<td>{'<br>'.join(_t(e) for e in t.evidence_for) or '&nbsp;'}{_refs_html(t.evidence_refs)}</td>"
        f"<td>{'<br>'.join(_t(e) for e in t.evidence_against) or '&nbsp;'}</td>"
        f"<td><em>{_t(t.antidote_question)}</em></td></tr>"
        for t in shown)
    quiet = [t.tendency for t in out.munger_scan if t not in shown]
    html = ("<table class='grid'><tr><th>Tendency</th><th>Pushes toward</th><th>Evidence for</th><th>Against</th>"
            f"<th>Ask</th></tr>{rows}</table>") if rows else '<p class="muted">No tendencies flagged.</p>'
    if quiet:
        html += f'<p class="small">Not evident or not assessable: {esc(", ".join(quiet))}</p>'
    return html + '<p class="small">A checklist for asking better questions, not a diagnosis.</p>'


def _lolla_html(out: AnalysisOutput, lolla: Any) -> str:
    if lolla is None:
        return '<p class="muted">Not run.</p>'
    if lolla.fired and out.severity == "LOLLAPALOOZA":
        return (f'<div class="box lolla"><strong>LOLLAPALOOZA ALERT.</strong> {esc(", ".join(lolla.tendencies))} all push toward '
                f'{esc(lolla.direction)}.<br>Hard triggers: {_t("; ".join(lolla.hard_triggers))}'
                + (f"<br>{_t(out.lollapalooza_explanation)}" if out.lollapalooza_explanation else "") + "</div>")
    html = f'<div class="box">No Lollapalooza. Gate: {_t("; ".join(lolla.failures) or "passed but not claimed")}.'
    if lolla.hard_triggers:
        html += f"<br>Hard behavioural triggers present: {_t('; '.join(lolla.hard_triggers))}"
    return html + "</div>"


def _thesis_html(out: AnalysisOutput, tc: Any) -> str:
    html = ""
    if out.thesis_test:
        html += f"<p><strong>{esc(out.thesis_test.classification.title())}.</strong> {_t(out.thesis_test.reason)}</p>"
        if out.thesis_test.evidence_changed_or_thesis_changed:
            html += f"<p>{_t(out.thesis_test.evidence_changed_or_thesis_changed)}</p>"
    if tc is not None:
        html += '<ul class="claims">' + "".join(
            f'<li><span class="tag" style="background:#DCFCE7; color:#166534;">Fact</span>{_t(x)}'
            f' <span class="refs">(thesis history)</span></li>' for x in tc.lines()) + "</ul>"
    return html or '<p class="muted">No thesis test.</p>'


def build_pdf_html(b: dict, date: str) -> str:
    out: AnalysisOutput = b["output"]
    ticker = b.get("ticker") or "Portfolio"
    kind = REPORT_NAMES.get(b["report_type"], b["report_type"])
    bg, fg = SEVERITY_COLOURS.get(out.severity, ("#374151", "#FFFFFF"))
    keys = "".join(
        f"<tr><td>{esc(label)}</td><td class='v' style='color:{colour or '#111827'};'>{_t(value)}</td></tr>"
        for label, value, colour in key_rows(b))
    parts = [
        f"<!doctype html><html><head><meta charset='utf-8'><title>{esc(ticker)} {esc(kind)}</title>"
        f"<style>{PDF_CSS}</style></head><body>",
        f"<div class='cover'><div class='kicker'>HARRY HINDSIGHT · {esc(date)}</div>"
        f"<h1>{esc(ticker)} · {esc(kind)}</h1>{_pill(out.severity, bg, fg)}"
        f"<div class='trigger' style='margin-top:6pt;'>{_t(b.get('reason', ''))}</div>"
        f"<div class='trigger' style='margin-top:4pt; font-style:italic;'>{esc(TAGLINE)}</div></div>",
        f"<table class='keys'>{keys}</table>",
        f"<div class='verdict'><div class='big'>{_t(out.verdict)}</div><div>{_t(out.severity_reason)}</div>",
    ]
    for d in out.disagreements:
        parts.append(f"<div style='margin-top:4pt;'><strong>Disagrees with {esc(d.with_agent.title())}:</strong> {_t(d.point)}</div>")
    parts.append("</div>")

    for heading, kind_ in _layout(b["report_type"]):
        if kind_ == "verdict":
            continue
        parts.append(f"<h2>{esc(_heading(heading), quote=False)}</h2>")
        if kind_ == "sec":
            parts.append(_claims(out, heading))
        elif kind_ == "sec+thesis":
            parts.append(_claims(out, heading) + _thesis_html(out, b.get("tc")))
        elif kind_ == "counter":
            parts.append(_claims(out, heading))
            parts.append(f"<div class='box'><strong>Strongest opposing case.</strong> {_t(out.strongest_opposing_case)}"
                         + ("".join(f"<br>What would tell them apart: {_t(d)}" for d in out.distinguishing_evidence)) + "</div>")
        elif kind_ == "thesis":
            parts.append(_thesis_html(out, b.get("tc")))
        elif kind_ == "powers":
            parts.append(_powers_html(out))
        elif kind_ == "munger":
            parts.append(_munger_html(out))
        elif kind_ == "lolla":
            parts.append(_lolla_html(out, b.get("lolla")))
        elif kind_ == "fresh":
            f = out.fresh_capital_test
            parts.append(f"<p><strong>{esc(f.would_buy_today)}</strong>" + (f", at {_t(f.weight_if_new)}" if f.weight_if_new else "")
                         + f". {_t(f.reasoning)}</p>" + ("<p><em>Holding is being judged by a lower standard than buying.</em></p>"
                                                         if f.holding_held_to_lower_standard else "") if f else '<p class="muted">Not run.</p>')
        elif kind_ == "forced":
            f = out.forced_sale_test
            parts.append(f"<p><strong>{esc(f.would_rebuy)}</strong>. {_t(f.conviction_or_attachment)} {_t(f.reasoning)}</p>"
                         if f else '<p class="muted">Not run.</p>')
        elif kind_ == "tax":
            parts.append(f"<p>{_t(out.tax_note) or 'None given.'}</p>"
                         + (f"<p><strong>Breakeven check.</strong> {_t(out.breakeven_check)}</p>" if out.breakeven_check else ""))
        elif kind_ == "mind":
            parts.append(_bullets(out.what_would_change_my_mind))
        elif kind_ == "questions":
            parts.append(_bullets(out.unanswered_questions))
            if out.questions_for_theo:
                parts.append("<p><strong>Questions for Theo</strong></p>" + _bullets(out.questions_for_theo))
        elif kind_ == "believe":
            parts.append("<p><strong>Evidence for</strong></p>" + _claims(out, "EVIDENCE FOR")
                         + "<p><strong>Evidence against</strong></p>" + _claims(out, "EVIDENCE AGAINST")
                         + f"<div class='box'><strong>Strongest opposing case.</strong> {_t(out.strongest_opposing_case)}</div>")

    if b["report_type"] != "SELL_ALERT" and out.questions_for_theo:
        parts.append("<h2>Questions for Theo</h2>" + _bullets(out.questions_for_theo))
    parts.append("<h2>Predictions for the autopsy</h2>")
    if out.warnings:
        rows = "".join(
            f"<tr><td>{esc(w.severity)}</td><td>{_t(w.text)}</td><td>{_t(p.metric)}</td><td>{esc(p.direction)} {_t(p.threshold)}</td>"
            f"<td>{esc(p.check_date)}</td></tr>" for w in out.warnings for p in w.predictions)
        parts.append("<table class='grid'><tr><th>Sev</th><th>Warning</th><th>Metric</th><th>Test</th><th>Check</th></tr>"
                     f"{rows}</table>")
    else:
        parts.append('<p class="muted">No warnings raised.</p>')
    if b.get("validation_notes"):
        parts.append("<h2>Validator notes</h2><ul class='small'>" + "".join(f"<li>{_t(n)}</li>" for n in b["validation_notes"]) + "</ul>")
    refs = b.get("refs", {})
    if refs:
        items = "".join(
            f"<li><strong>{esc(ref_label(k))}</strong>" + (f" <a href='{esc(k)}'>link</a>" if k.startswith("http") else "")
            + f": {_t(v)}</li>" for k, v in refs.items())
        parts.append(f"<h2>Evidence refs</h2><ul class='small'>{items}</ul>")
    parts.append(f"<div class='disclaimer'>{esc(TAGLINE)} Harry Hindsight reads Bob, Sally, Wally and Theo, and asks whether we are wrong while "
                 "it is still foresight. Claim tags: Fact (cited), Interp (inference), Mgmt (what management says), Agent "
                 "(another agent's conclusion), Hindsight (own synthesis). The Munger scan is a checklist for asking better "
                 "questions, not a diagnosis. Not financial advice.</div></body></html>")
    return "".join(parts)


def pdf_filename(b: dict, date: str) -> str:
    t = b.get("ticker") or "Portfolio"
    kind = REPORT_NAMES.get(b["report_type"], b["report_type"]).replace(" ", "_")
    return f"Hindsight_{t}_{kind}_{date}.pdf"


def render_pdf(html: str, path: Path) -> Path:
    """Render with weasyprint (same engine as Bob). Raises ImportError if it is missing."""
    from weasyprint import HTML

    path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html).write_pdf(str(path))
    return path
