"""Wally's Shortlist — a weekly valuation-aware "best opportunities right now" pass.

Wally's flagged tables are a tripwire (near a 52-week low, or below a per-ticker
buy price). A tripwire is not a verdict: buy prices go stale, and "cheaper than
its own history" can still be absurdly expensive in absolute terms. This module
adds a second stage that ranks the whole watchlist universe on merit and asks an
LLM analyst to pick up to five genuine opportunities, with real reasoning.

Two stages, so it is smart but cheap:

1. ``_opportunity_score`` — a deterministic, valuation-first score over every
   screened row. Absolute-valuation sanity (a hard "still expensive" gate) sits
   above de-rating and buy-price proximity, which are demoted to tie-breakers.
2. ``_rank_with_llm`` — one Claude call over the top candidates that returns a
   ranked shortlist of up to five, and is explicitly allowed to return fewer (or
   none) when nothing is compelling.

The module never raises into Wally's run: no API key, a failed fetch or a bad
LLM reply degrades to a smaller (or empty) shortlist and Wally reports as before.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .data_fetch import ValuationSnapshot, fetch_valuation_snapshot
from .screening import TickerScreenResult

MODEL = os.environ.get("WALLY_SHORTLIST_MODEL", "claude-opus-4-6")
MAX_CANDIDATES = int(os.environ.get("WALLY_SHORTLIST_CANDIDATES", "12"))
MAX_PICKS = int(os.environ.get("WALLY_SHORTLIST_PICKS", "5"))
# forward/trailing PE at or above this is "still expensive" no matter how far
# the price has fallen — the "60x is still 60x" gate.
EXPENSIVE_PE = float(os.environ.get("WALLY_SHORTLIST_EXPENSIVE_PE", "40"))


def _log(msg: str) -> None:
    print(f"[wally/shortlist] {msg}", flush=True)


def _as_fraction(x) -> Optional[float]:
    """Normalise a yield to a decimal fraction.

    yfinance reports dividendYield as a percent (4.5) in some versions and a
    fraction (0.045) in others. Anything above 1.5 can only be a percent (a
    150% yield is not real), so scale it down.
    """
    if not isinstance(x, (int, float)):
        return None
    x = float(x)
    return x / 100.0 if x > 1.5 else x


# ---------------------------------------------------------------------------
# Stage 1 — deterministic opportunity score
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    row: TickerScreenResult
    watchlist: str
    val: Optional[ValuationSnapshot]
    score: float = 0.0
    notes: str = ""


def _opportunity_score(row: TickerScreenResult, val: Optional[ValuationSnapshot]) -> tuple[float, str]:
    """Valuation-first opportunity score. Higher is more interesting.

    Absolute cheapness (forward PE, FCF yield, EV/EBITDA) dominates; de-rating
    and buy-price proximity are tie-breakers; a rich PE caps the whole score so
    a de-rated but still-expensive name cannot float to the top.
    """
    score = 0.0
    notes: list[str] = []

    fwd = val.forward_pe if val else None
    trail = val.trailing_pe if val else None
    pe = fwd if (isinstance(fwd, (int, float)) and fwd > 0) else trail
    ev = val.ev_to_ebitda if val else None
    fcf = _as_fraction(val.fcf_yield) if val else None
    dy = _as_fraction(val.dividend_yield) if val else None

    # --- Valuation (the bulk of the score) ---
    if isinstance(pe, (int, float)) and pe > 0:
        if pe < 12:
            score += 30; notes.append(f"PE {pe:.0f}, cheap")
        elif pe < 18:
            score += 22; notes.append(f"PE {pe:.0f}")
        elif pe < 25:
            score += 12
        elif pe < 35:
            score += 3
        elif pe < 50:
            score -= 12; notes.append(f"PE {pe:.0f}, rich")
        else:
            score -= 30; notes.append(f"PE {pe:.0f}, very expensive")

    if fcf is not None:
        if fcf >= 0.08:
            score += 20; notes.append(f"FCF yield {fcf*100:.0f}%")
        elif fcf >= 0.05:
            score += 12; notes.append(f"FCF yield {fcf*100:.0f}%")
        elif fcf >= 0.03:
            score += 5
        elif fcf < 0:
            score -= 8; notes.append("negative FCF")

    if isinstance(ev, (int, float)) and ev > 0:
        if ev < 8:
            score += 10; notes.append(f"EV/EBITDA {ev:.0f}")
        elif ev < 12:
            score += 5
        elif ev > 20:
            score -= 8; notes.append(f"EV/EBITDA {ev:.0f}")

    if dy is not None:
        if dy >= 0.05:
            score += 8; notes.append(f"yield {dy*100:.1f}%")
        elif dy >= 0.03:
            score += 4

    # --- De-rating / entry timing (tie-breakers) ---
    if row.below_target:
        score += 8; notes.append("below buy price")
    if row.near_low:
        score += 6; notes.append("near 52w low")
    off_high = row.below_high_pct or 0.0
    if off_high >= 40:
        score += 8; notes.append(f"{off_high:.0f}% off high")
    elif off_high >= 25:
        score += 5
    elif off_high >= 15:
        score += 2

    # --- Hard "still expensive" gate ---
    if isinstance(pe, (int, float)) and pe >= EXPENSIVE_PE:
        score = min(score, 5.0)
        notes.append("gate: still expensive")

    if val is None:
        notes.append("no valuation data")

    return score, "; ".join(notes)


def _rank_candidates(
    rows: list[tuple[TickerScreenResult, str]],
    fetch_valuation: Callable[[str], ValuationSnapshot] = fetch_valuation_snapshot,
    max_candidates: int = MAX_CANDIDATES,
) -> list[Candidate]:
    """Score every row (fetching valuation best-effort) and return the top N."""
    cands: list[Candidate] = []
    for row, wl in rows:
        if row.error:
            continue
        val: Optional[ValuationSnapshot] = None
        try:
            val = fetch_valuation(row.ticker)
        except Exception as exc:  # pragma: no cover - network
            _log(f"valuation fetch failed for {row.ticker}: {exc}")
        score, notes = _opportunity_score(row, val)
        cands.append(Candidate(row=row, watchlist=wl, val=val, score=score, notes=notes))
    cands.sort(key=lambda c: -c.score)
    return cands[:max_candidates]


# ---------------------------------------------------------------------------
# Stage 2 — LLM analyst ranking
# ---------------------------------------------------------------------------

@dataclass
class ShortlistPick:
    ticker: str
    company_name: str
    rank: int
    one_liner: str
    thesis: str
    why_now: str
    key_risk: str
    verdict: str
    watchlist: str = ""
    current_price: Optional[float] = None
    forward_pe: Optional[float] = None
    trailing_pe: Optional[float] = None
    ev_ebitda: Optional[float] = None
    fcf_yield: Optional[float] = None
    dividend_yield: Optional[float] = None
    pct_off_high: Optional[float] = None
    target_price: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "rank": self.rank,
            "one_liner": self.one_liner,
            "thesis": self.thesis,
            "why_now": self.why_now,
            "key_risk": self.key_risk,
            "verdict": self.verdict,
            "watchlist": self.watchlist,
            "current_price": self.current_price,
            "forward_pe": self.forward_pe,
            "trailing_pe": self.trailing_pe,
            "ev_ebitda": self.ev_ebitda,
            "fcf_yield": self.fcf_yield,
            "dividend_yield": self.dividend_yield,
            "pct_off_high": self.pct_off_high,
            "target_price": self.target_price,
        }


@dataclass
class ShortlistResult:
    picks: list[ShortlistPick] = field(default_factory=list)
    note: str = ""
    candidates_considered: int = 0
    status: str = "ok"  # ok | skipped | failed

    def to_dict(self) -> dict:
        return {
            "note": self.note,
            "candidates_considered": self.candidates_considered,
            "status": self.status,
            "picks": [p.to_dict() for p in self.picks],
        }


def _fmt(x, pct: bool = False, dp: int = 2) -> str:
    if not isinstance(x, (int, float)):
        return "n/a"
    return f"{x*100:.1f}%" if pct else f"{x:.{dp}f}"


def _candidate_line(i: int, c: Candidate) -> str:
    r, v = c.row, c.val
    pos = None
    if r.high_52w and r.low_52w and r.high_52w > r.low_52w:
        pos = (r.current_price - r.low_52w) / (r.high_52w - r.low_52w) * 100
    parts = [
        f"{i}. {r.ticker} ({r.company_name}) [{c.watchlist}]",
        f"price {_fmt(r.current_price)}",
        f"52w range pos {pos:.0f}%" if pos is not None else "52w pos n/a",
        f"off 52w high {_fmt(r.below_high_pct, dp=0)}%",
        f"fwd PE {_fmt(v.forward_pe, dp=0) if v else 'n/a'}",
        f"trail PE {_fmt(v.trailing_pe, dp=0) if v else 'n/a'}",
        f"EV/EBITDA {_fmt(v.ev_to_ebitda, dp=0) if v else 'n/a'}",
        f"FCF yield {_fmt(_as_fraction(v.fcf_yield), pct=True) if v else 'n/a'}",
        f"div yield {_fmt(_as_fraction(v.dividend_yield), pct=True) if v else 'n/a'}",
    ]
    if r.target_price:
        tag = "below" if r.below_target else "above"
        parts.append(f"buy price {_fmt(r.target_price)} ({tag}, may be stale)")
    return " | ".join(parts)


def _build_prompt(cands: list[Candidate]) -> str:
    lines = "\n".join(_candidate_line(i + 1, c) for i, c in enumerate(cands))
    return f"""You are Wally the Watcher, a disciplined value-and-quality investor. Below is a pre-ranked candidate list drawn from across the watchlists this week (all figures are live).

Pick the {MAX_PICKS} BEST opportunities to look at right now, ALL THINGS CONSIDERED. This is not a distance-to-buy-price list and not a 52-week-low list:

- Judge absolute valuation, not just cheapness versus a stock's own history. Something that fell from 80x to 60x earnings is still expensive — do not reward it for falling.
- Treat any "buy price" as a weak, possibly-stale signal (some were set long ago). Trading below an old buy price is not itself a reason to buy.
- Prefer quality businesses at a genuinely sensible price, real free-cash-flow yields, and a clear reason it is interesting NOW.
- You may return FEWER than {MAX_PICKS} — even zero — if nothing is genuinely compelling. Do not pad the list to hit a number. Quality over quantity.

Candidates:
{lines}

Return ONLY a JSON array (no prose, no code fence), best first, each element:
{{"ticker": "...", "rank": 1, "one_liner": "one punchy sentence", "thesis": "2-3 sentences: why it's a good business at a sensible price", "why_now": "1-2 sentences: what makes it timely", "key_risk": "the single biggest risk", "verdict": "one of: Strong buy candidate | Worth a look | Watch closely"}}

If nothing qualifies, return []."""


def _parse_picks(text: str) -> list[dict]:
    """Extract the JSON array from the model reply; tolerant of prose/fences."""
    if not text:
        return []
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    try:
        data = json.loads(t)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    m = re.search(r"\[.*\]", t, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list):
                return data
        except Exception:
            return []
    return []


def _rank_with_llm(cands: list[Candidate], llm_send=None, make_client=None) -> ShortlistResult:
    injected = llm_send is not None and make_client is not None
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not injected and not api_key:
        _log("ANTHROPIC_API_KEY not set — skipping shortlist ranking.")
        return ShortlistResult(status="skipped", note="AI ranking unavailable (no API key).",
                               candidates_considered=len(cands))
    if not injected:
        from shared.llm import make_client as _mk, send as _send
        make_client, llm_send = _mk, _send
    try:
        client = make_client(api_key)
        resp = llm_send(
            client,
            model=MODEL,
            max_tokens=8000,
            stream=True,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": _build_prompt(cands)}],
        )
    except Exception as exc:
        _log(f"LLM ranking failed: {exc}")
        return ShortlistResult(status="failed", note=f"AI ranking failed: {exc}",
                               candidates_considered=len(cands))

    raw = _parse_picks(getattr(resp, "text", "") or "")
    by_ticker = {c.row.ticker.upper(): c for c in cands}
    picks: list[ShortlistPick] = []
    for i, item in enumerate(raw[:MAX_PICKS]):
        tk = str(item.get("ticker", "")).strip().upper()
        c = by_ticker.get(tk)
        if not c:
            continue
        r, v = c.row, c.val
        picks.append(ShortlistPick(
            ticker=r.ticker,
            company_name=r.company_name,
            rank=int(item.get("rank", i + 1) or i + 1),
            one_liner=str(item.get("one_liner", "")).strip(),
            thesis=str(item.get("thesis", "")).strip(),
            why_now=str(item.get("why_now", "")).strip(),
            key_risk=str(item.get("key_risk", "")).strip(),
            verdict=str(item.get("verdict", "")).strip(),
            watchlist=c.watchlist,
            current_price=r.current_price,
            forward_pe=v.forward_pe if v else None,
            trailing_pe=v.trailing_pe if v else None,
            ev_ebitda=v.ev_to_ebitda if v else None,
            fcf_yield=_as_fraction(v.fcf_yield) if v else None,
            dividend_yield=_as_fraction(v.dividend_yield) if v else None,
            pct_off_high=r.below_high_pct,
            target_price=r.target_price,
        ))
    picks.sort(key=lambda p: p.rank)
    for n, p in enumerate(picks, 1):
        p.rank = n
    note = (
        f"Ranked from {len(cands)} candidates across the watchlists."
        if picks
        else "Nothing screens as a compelling buy this week — sitting on hands."
    )
    return ShortlistResult(picks=picks, note=note, candidates_considered=len(cands), status="ok")


def build_shortlist(
    rows: list[tuple[TickerScreenResult, str]],
    fetch_valuation: Callable[[str], ValuationSnapshot] = fetch_valuation_snapshot,
    llm_send=None,
    make_client=None,
) -> ShortlistResult:
    """Full pipeline: score every row, then LLM-rank the top candidates."""
    if not rows:
        return ShortlistResult(status="ok", note="No tickers to consider.", candidates_considered=0)
    cands = _rank_candidates(rows, fetch_valuation=fetch_valuation)
    _log(f"scored {len(rows)} rows, {len(cands)} candidates to the analyst")
    return _rank_with_llm(cands, llm_send=llm_send, make_client=make_client)


# ---------------------------------------------------------------------------
# Rendering — email (table-only, inline styles) and dashboard dict
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _metric(label: str, value: str) -> str:
    return (
        f"<td style='padding:2px 10px 2px 0;font-size:12px;color:#64748b;white-space:nowrap'>"
        f"{label} <span style='color:#1F2D4E;font-weight:600'>{value}</span></td>"
    )


def render_email_html(result: ShortlistResult, run_date: str) -> str:
    """Wally's Shortlist block for the top of the combined email. Table-only."""
    header = (
        "<h2 style='margin:0 0 4px'>Wally's Shortlist</h2>"
        f"<p style='margin:0 0 12px;color:#475569'>Up to {MAX_PICKS} to look at this week, all things considered "
        f"&mdash; {_esc(run_date)}</p>"
    )
    if not result.picks:
        return (
            "<div style='border:1px solid #1F2D4E;border-radius:8px;padding:16px;margin-bottom:20px'>"
            + header
            + f"<p style='margin:0;color:#334155'><strong>{_esc(result.note)}</strong></p></div>"
        )

    cards = []
    for p in result.picks:
        vpe = _fmt(p.forward_pe, dp=0) if p.forward_pe else _fmt(p.trailing_pe, dp=0)
        metrics = (
            "<table cellpadding='0' cellspacing='0'><tr>"
            + _metric("Price", _fmt(p.current_price))
            + _metric("Fwd PE", vpe)
            + _metric("EV/EBITDA", _fmt(p.ev_ebitda, dp=0) if p.ev_ebitda else "n/a")
            + _metric("FCF yld", _fmt(p.fcf_yield, pct=True) if p.fcf_yield is not None else "n/a")
            + _metric("Div yld", _fmt(p.dividend_yield, pct=True) if p.dividend_yield is not None else "n/a")
            + "</tr></table>"
        )
        cards.append(
            "<div style='border:1px solid #cbd5e1;border-left:4px solid #1F2D4E;border-radius:6px;"
            "padding:12px 14px;margin-bottom:10px'>"
            f"<div style='font-size:15px'><strong>{p.rank}. {_esc(p.ticker)} &mdash; {_esc(p.company_name)}</strong>"
            f"<span style='color:#64748b;font-size:12px'> &nbsp;[{_esc(p.watchlist)}]</span>"
            f"<span style='float:right;background:#1F2D4E;color:#fff;font-size:11px;padding:2px 8px;"
            f"border-radius:10px'>{_esc(p.verdict)}</span></div>"
            f"<div style='font-style:italic;color:#334155;margin:6px 0'>{_esc(p.one_liner)}</div>"
            f"{metrics}"
            f"<div style='font-size:13px;color:#1e293b;margin-top:8px'>{_esc(p.thesis)}</div>"
            f"<div style='font-size:13px;color:#166534;margin-top:6px'><strong>Why now:</strong> {_esc(p.why_now)}</div>"
            f"<div style='font-size:13px;color:#9a3412;margin-top:4px'><strong>Key risk:</strong> {_esc(p.key_risk)}</div>"
            "</div>"
        )
    return (
        "<div style='border:1px solid #1F2D4E;border-radius:8px;padding:16px;margin-bottom:22px'>"
        + header
        + "".join(cards)
        + f"<p style='margin:8px 0 0;color:#94a3b8;font-size:11px'>{_esc(result.note)}</p></div>"
    )
