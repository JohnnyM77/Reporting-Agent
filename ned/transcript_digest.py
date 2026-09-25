# ned/transcript_digest.py
#
# The single-episode transcript digest: prompt, JSON parsing and rendering.
# The LLM call itself stays in ned/main.py::llm_transcript_digest so it
# shares Ned's client plumbing and call cap; everything here is pure (no
# network, no file I/O) and unit-testable.
#
# The model is asked for strict JSON (DIGEST_JSON_SHAPE). When it slips —
# prose around the object, ```json fences — parse_digest_response recovers
# the object; when there is no object at all, the reply is kept as markdown
# so a run never dies on a formatting slip. A reply cut off at max_tokens is
# never passed off as a clean digest: the caller marks it failed.

from __future__ import annotations

import json
import re
from dataclasses import dataclass

RELEVANCE_LEVELS = ("High", "Medium", "Low", "None")

EFFECT_ORDER = ("Kill condition at risk", "Challenges", "Supports", "Neutral")

SYSTEM_PROMPT = """\
You are Ned, research analyst for JM, an Australian CPA and former CFO who runs a concentrated, value-oriented portfolio. His philosophy is "slow to buy, even slower to sell". He reads cash flow first, distrusts "underlying" numbers, and treats every speaker's story as a hypothesis to test, not a fact. Your job is not to summarise the episode politely. Your job is to tell JM what this episode means for HIS money over the next 3 to 5 years.

Rules:
- Be factual about what was said. Never invent figures. If you are reasoning beyond the transcript, label it as your inference.
- Many episodes are indirect. A history podcast about the 1840s Railway Mania or the 1990s fibre-optic bubble may never mention a listed company but can be highly relevant to today's AI capex cycle. Actively look for these historical analogies, structural parallels and second-order effects, and apply them to present-day markets and to JM's holdings. Say where the analogy holds and where it breaks.
- Think about who captures the value in the theme discussed: builders, suppliers, users, landlords or financiers. Say who gets hurt if it goes wrong.
- Consider the speaker's incentives. A fund manager talking his book is not neutral.
- Only flag a holding or watchlist name as affected if there is a genuine, explainable link. A weak or forced connection is worse than saying "no material impact". Most holdings will usually be unaffected; do not pad the list.
- When the episode touches a thesis pillar, name the pillar ID and say whether the content supports it, challenges it, or brings a kill condition closer.
- Use Australian English. Do not use em dashes. Do not use the words or phrases "delve", "moreover", "furthermore", "testament to" or "navigate the landscape".

JM's holdings, theses and watchlists follow. "Held" means a ticker under HOLDINGS or a thesis with status HELD. "Watchlist" means a name under WATCHLISTS that is not held.

{portfolio_context}
"""

DIGEST_JSON_SHAPE = """\
{
  "title_suggestion": "short human title for the episode",
  "portfolio_relevance": "High" | "Medium" | "Low" | "None",
  "relevance_reason": "one sentence",
  "tldr": "2-3 sentences",
  "key_points": ["5-8 items, each with the specific claim or number"],
  "medium_term_lens": {
    "themes": ["2-4 investable themes the episode speaks to"],
    "historical_parallels": [{"parallel": "the analogy and where it holds", "where_it_breaks": "where it breaks"}],
    "second_order_effects": ["non-obvious consequences over 3-5 years"],
    "winners_and_losers": "short paragraph on where value accrues and who is exposed"
  },
  "portfolio_impact": [
    {
      "ticker": "BXB",
      "name": "Brambles",
      "type": "Held" | "Watchlist",
      "link": "Direct" | "Indirect" | "Thematic",
      "effect": "Supports" | "Challenges" | "Neutral" | "Kill condition at risk",
      "pillar": "P1 or empty",
      "explanation": "1-3 sentences",
      "action": "No action" | "Watch" | "Review thesis" | "Research further"
    }
  ],
  "new_ideas": ["companies or sectors worth researching that JM does not own or watch, with one line why"],
  "companies_mentioned": [{"name": "", "ticker": "", "context": ""}],
  "sceptics_corner": "where the speaker could be wrong, what they conveniently skipped, conflicts of interest",
  "so_what": "2-3 sentence bottom line for JM",
  "watching_next": ["specific things to monitor, e.g. data points, results dates, events"]
}"""

CHUNK_EXTRACTION_PROMPT = """\
This is part {part} of {total} of a long {kind} transcript ("{title}"). Extract dense working notes for a later analyst, not a summary for a reader:
- every specific claim, number, date, name and company mentioned, with who said it;
- the arguments made and the evidence offered for them;
- any historical episode, analogy or comparison drawn;
- anything bearing on markets, industries, capital cycles or listed companies.
Plain bullet points. Australian English, no em dashes. Do not add your own opinions.

--- TRANSCRIPT PART {part} OF {total} ---
{text}"""


@dataclass
class DigestResult:
    """Outcome of one transcript digest. `error` set = the digest failed and
    the email/PDF must say so; `markdown` may still hold raw model output."""

    digest_json: dict | None = None
    markdown: str = ""
    error: str = ""
    parts: int = 1                  # >1 when the transcript was chunked
    parts_failed: int = 0
    model: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.digest_json or self.markdown)

    @property
    def relevance(self) -> str:
        """High/Medium/Low/None from the JSON; "Unrated" for a markdown-only
        reply; "Failed" when the digest failed."""
        if self.error:
            return "Failed"
        if self.digest_json:
            return self.digest_json.get("portfolio_relevance") or "Unrated"
        return "Unrated"


def build_system_prompt(portfolio_context: str) -> str:
    ctx = (portfolio_context or "").strip() or "(Portfolio context unavailable for this run.)"
    return SYSTEM_PROMPT.format(portfolio_context=ctx)


def build_user_prompt(
    *,
    kind: str,
    title: str,
    channel: str,
    source_url: str,
    body: str,
    from_chunk_notes: int = 0,
) -> str:
    kind_label = "YouTube video" if kind == "youtube" else "podcast episode"
    header = [
        f"Episode type: {kind_label}",
        f"Title: {title or '(unknown)'}",
    ]
    if channel:
        header.append(f"{'Channel' if kind == 'youtube' else 'Show'}: {channel}")
    header.append(f"Source: {source_url}")
    if from_chunk_notes:
        body_label = (
            f"--- EXTRACTION NOTES ({from_chunk_notes} parts; the transcript was "
            "too long to send whole, so these notes were taken from it part by part) ---"
        )
    else:
        body_label = "--- TRANSCRIPT ---"
    return (
        "\n".join(header)
        + "\n\nAnalyse this episode for JM. Reply with a single JSON object and "
        "nothing else: no preamble, no code fences. Use exactly this shape "
        "(empty lists are fine where nothing applies):\n\n"
        + DIGEST_JSON_SHAPE
        + "\n\nSort portfolio_impact with \"Kill condition at risk\" first, then "
        "\"Challenges\", then \"Supports\", then the rest; Held before Watchlist "
        "within each.\n\n"
        + body_label
        + "\n"
        + body
    )


# ---------------------------------------------------------------------------
# Chunking for very long transcripts
# ---------------------------------------------------------------------------
def chunk_text(text: str, size: int) -> list[str]:
    """Split text into pieces of at most ~size chars, preferring paragraph,
    then sentence, boundaries. Never drops characters beyond whitespace."""
    text = text or ""
    if size <= 0 or len(text) <= size:
        return [text]
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[start:end]
            floor = int(len(window) * 0.6)
            cut = window.rfind("\n\n", floor)
            if cut == -1:
                m = None
                for m in re.finditer(r"[.!?]\s", window[floor:]):
                    pass
                cut = floor + m.end() if m else -1
            if cut > 0:
                end = start + cut
        chunks.append(text[start:end].strip())
        start = end
    return [c for c in chunks if c]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?\s*```\s*$", re.S)


def _strip_fences(text: str) -> str:
    m = _FENCE_RE.match(text or "")
    return m.group(1) if m else (text or "")


def _try_load(text: str) -> dict | None:
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _as_list(v) -> list:
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def _as_str(v) -> str:
    return "" if v is None else str(v).strip()


def _pick(value: str, allowed: tuple[str, ...], default: str) -> str:
    v = _as_str(value).lower()
    for a in allowed:
        if v == a.lower():
            return a
    return default


def sort_portfolio_impact(items: list[dict]) -> list[dict]:
    """Kill condition at risk, Challenges, Supports, then the rest;
    Held before Watchlist within each. Stable otherwise."""
    def key(it: dict):
        eff = it.get("effect", "")
        eff_rank = EFFECT_ORDER.index(eff) if eff in EFFECT_ORDER else len(EFFECT_ORDER)
        type_rank = 0 if it.get("type") == "Held" else 1
        return (eff_rank, type_rank)
    return sorted(items, key=key)


def normalise_digest(obj: dict) -> dict:
    """Coerce a parsed reply into the documented shape, so renderers can
    index without guarding every field."""
    lens = obj.get("medium_term_lens") if isinstance(obj.get("medium_term_lens"), dict) else {}

    parallels = []
    for p in _as_list(lens.get("historical_parallels")):
        if isinstance(p, dict):
            par = _as_str(p.get("parallel") or p.get("analogy"))
            brk = _as_str(p.get("where_it_breaks") or p.get("breaks"))
        else:
            # Tolerate the flat-string form: "analogy ... Where it breaks: ..."
            par, brk = _as_str(p), ""
            m = re.search(r"(?i)\bwhere it breaks\b\s*[:.\-]?\s*", par)
            if m:
                par, brk = par[: m.start()].strip(), par[m.end():].strip()
        if par or brk:
            parallels.append({"parallel": par, "where_it_breaks": brk})

    impact = []
    for it in _as_list(obj.get("portfolio_impact")):
        if not isinstance(it, dict):
            continue
        ticker = _as_str(it.get("ticker"))
        if not ticker and not _as_str(it.get("name")):
            continue
        impact.append({
            "ticker": ticker,
            "name": _as_str(it.get("name")),
            "type": _pick(it.get("type"), ("Held", "Watchlist"), "Watchlist"),
            "link": _pick(it.get("link"), ("Direct", "Indirect", "Thematic"), "Thematic"),
            "effect": _pick(it.get("effect"), EFFECT_ORDER, "Neutral"),
            "pillar": _as_str(it.get("pillar")),
            "explanation": _as_str(it.get("explanation")),
            "action": _pick(
                it.get("action"),
                ("No action", "Watch", "Review thesis", "Research further"),
                "No action",
            ),
        })

    companies = []
    for c in _as_list(obj.get("companies_mentioned")):
        if isinstance(c, dict):
            row = {k: _as_str(c.get(k)) for k in ("name", "ticker", "context")}
        else:
            row = {"name": _as_str(c), "ticker": "", "context": ""}
        if any(row.values()):
            companies.append(row)

    return {
        "title_suggestion": _as_str(obj.get("title_suggestion")),
        "portfolio_relevance": _pick(obj.get("portfolio_relevance"), RELEVANCE_LEVELS, "Low"),
        "relevance_reason": _as_str(obj.get("relevance_reason")),
        "tldr": _as_str(obj.get("tldr")),
        "key_points": [_as_str(x) for x in _as_list(obj.get("key_points")) if _as_str(x)],
        "medium_term_lens": {
            "themes": [_as_str(x) for x in _as_list(lens.get("themes")) if _as_str(x)],
            "historical_parallels": parallels,
            "second_order_effects": [
                _as_str(x) for x in _as_list(lens.get("second_order_effects")) if _as_str(x)
            ],
            "winners_and_losers": _as_str(lens.get("winners_and_losers")),
        },
        "portfolio_impact": sort_portfolio_impact(impact),
        "new_ideas": [_as_str(x) for x in _as_list(obj.get("new_ideas")) if _as_str(x)],
        "companies_mentioned": companies,
        "sceptics_corner": _as_str(obj.get("sceptics_corner")),
        "so_what": _as_str(obj.get("so_what")),
        "watching_next": [_as_str(x) for x in _as_list(obj.get("watching_next")) if _as_str(x)],
    }


def parse_digest_response(text: str) -> tuple[dict | None, str]:
    """Return (digest_json, markdown).

    Tries a straight parse, then with ```json fences stripped, then the
    outermost {...} span. On success the JSON is normalised and a markdown
    rendering is produced from it. On failure digest_json is None and the
    reply itself is returned as the markdown.
    """
    raw = (text or "").strip()
    candidates = [raw, _strip_fences(raw).strip()]
    start, end = raw.find("{"), raw.rfind("}")
    if 0 <= start < end:
        candidates.append(raw[start : end + 1])
    for cand in candidates:
        obj = _try_load(cand)
        if obj is not None:
            digest = normalise_digest(obj)
            return digest, digest_json_to_markdown(digest)
    return None, raw


# ---------------------------------------------------------------------------
# Markdown rendering (dashboard summary + plain-text email + old-style PDF)
# ---------------------------------------------------------------------------
def digest_json_to_markdown(d: dict) -> str:
    """Render the digest JSON as markdown. The TL;DR stays first in the
    `1. **TL;DR**: ...` form the dashboard card already extracts."""
    out: list[str] = []
    out.append(f"1. **TL;DR**: {d.get('tldr', '')}".rstrip())
    out.append("")
    rel = d.get("portfolio_relevance", "")
    reason = d.get("relevance_reason", "")
    out.append(f"**Portfolio relevance:** {rel}" + (f". {reason}" if reason else ""))
    out.append("")

    out.append("## What it means for my portfolio")
    impact = d.get("portfolio_impact") or []
    if impact:
        for it in impact:
            pillar = f" {it['pillar']}" if it.get("pillar") else ""
            out.append(
                f"- **{it['ticker']}** {it['name']} ({it['type']}, {it['link']}): "
                f"{it['effect']}{pillar}. {it['explanation']} Action: {it['action']}."
            )
    else:
        out.append("No material impact on current holdings or watchlists.")
    out.append("")
    if d.get("so_what"):
        out += ["## So what", d["so_what"], ""]

    lens = d.get("medium_term_lens") or {}
    if any(lens.get(k) for k in ("themes", "historical_parallels", "second_order_effects", "winners_and_losers")):
        out.append("## The medium-term lens")
        if lens.get("themes"):
            out.append("**Themes**")
            out += [f"- {t}" for t in lens["themes"]]
            out.append("")
        if lens.get("historical_parallels"):
            out.append("**Historical parallels**")
            for p in lens["historical_parallels"]:
                line = f"- {p['parallel']}"
                if p.get("where_it_breaks"):
                    line += f" Where it breaks: {p['where_it_breaks']}"
                out.append(line)
            out.append("")
        if lens.get("second_order_effects"):
            out.append("**Second-order effects**")
            out += [f"- {t}" for t in lens["second_order_effects"]]
            out.append("")
        if lens.get("winners_and_losers"):
            out += ["**Winners and losers**", lens["winners_and_losers"], ""]

    if d.get("key_points"):
        out.append("## What was said")
        out += [f"- {k}" for k in d["key_points"]]
        out.append("")
    if d.get("sceptics_corner"):
        out += ["## Sceptic's corner", d["sceptics_corner"], ""]
    if d.get("new_ideas"):
        out.append("## New ideas")
        out += [f"- {k}" for k in d["new_ideas"]]
        out.append("")
    if d.get("companies_mentioned"):
        out.append("## Companies mentioned")
        for c in d["companies_mentioned"]:
            tick = f" ({c['ticker']})" if c.get("ticker") else ""
            ctx = f": {c['context']}" if c.get("context") else ""
            out.append(f"- {c['name']}{tick}{ctx}")
        out.append("")
    if d.get("watching_next"):
        out.append("## What I'm watching next")
        out += [f"- {k}" for k in d["watching_next"]]
        out.append("")
    return "\n".join(out).strip() + "\n"
