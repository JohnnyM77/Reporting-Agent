"""Deterministic gates. No LLM call happens unless one of these fires.

All thresholds and keyword rules come from ``config/hindsight.yaml`` so they
can be tuned without touching code.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from pathlib import Path
from typing import Any

from .adapters import BobAdapter, WallyAdapter, bare
from .schemas import HindsightEvent

# ---------------------------------------------------------------------------
# Bob: map buckets to priorities, never rewrite Bob's classifier
# ---------------------------------------------------------------------------

_PCT_NEAR = r"(\d+(?:\.\d+)?)\s*%"


def _text_of(item: dict) -> str:
    bits = [str(item.get("title", ""))]
    analysis = item.get("analysis")
    if isinstance(analysis, dict):
        bits.append(str(analysis.get("summary", "")))
    elif isinstance(analysis, str):
        bits.append(analysis)
    bits.append(str(item.get("summary", "")))
    return " ".join(" ".join(bits).split())


def _raise_is_critical(text: str, bob_cfg: dict) -> bool:
    """A raise is CRITICAL only when the dilution or discount is readable and big."""
    low = text.lower()
    dil = float(bob_cfg.get("dilution_pct_critical", 10.0))
    disc = float(bob_cfg.get("discount_pct_critical", 15.0))
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s*(discount)", low):
        if float(m.group(1)) > disc:
            return True
    for m in re.finditer(r"discount of\s*" + _PCT_NEAR, low):
        if float(m.group(1)) > disc:
            return True
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%\s*(of (the )?(existing |issued )?(shares|capital|share capital)|dilution)", low):
        if float(m.group(1)) > dil:
            return True
    for m in re.finditer(r"dilution of\s*" + _PCT_NEAR, low):
        if float(m.group(1)) > dil:
            return True
    return False


def bob_priority(bucket: str, item: dict, cfg: dict) -> tuple[str, list[str]]:
    """``(priority, matched_rule_names)`` for one Bob item."""
    bob_cfg = cfg.get("bob", {})
    text = _text_of(item)
    matched = []
    for name, patterns in (bob_cfg.get("critical_rules") or {}).items():
        if any(re.search(p, text, re.IGNORECASE) for p in patterns):
            matched.append(name)
    leadership = any(re.search(p, text, re.IGNORECASE) for p in bob_cfg.get("leadership_rules") or [])

    if bucket == "FYI":
        return "LOW", matched
    if bucket == "MATERIAL":
        if leadership:
            return "HIGH", matched + ["LEADERSHIP_CHANGE"]
        return "MEDIUM", matched

    # HIGH IMPACT
    critical = [m for m in matched if m != "DILUTIVE_RAISE"]
    if "DILUTIVE_RAISE" in matched and _raise_is_critical(text, bob_cfg):
        critical.append("DILUTIVE_RAISE")
    if critical:
        return "CRITICAL", matched
    if leadership:
        matched = matched + ["LEADERSHIP_CHANGE"]
    return "HIGH", matched


def bob_events(adapter: BobAdapter, cfg: dict) -> list[HindsightEvent]:
    """Every Bob item as an event with its mapped priority (LOW included)."""
    out = []
    run = adapter.run_date
    for bucket, item in adapter.items():
        t = bare(item.get("ticker"))
        if not t:
            continue
        priority, rules = bob_priority(bucket, item, cfg)
        out.append(
            HindsightEvent(
                source="BOB",
                event_type="HIGH_IMPACT_EVENT",
                event_subtype=",".join(rules) or str(item.get("type") or bucket).upper().replace(" ", "_"),
                ticker=t,
                priority=priority,
                source_report_ref=str(item.get("url") or f"bob:{run}:{t}:{item.get('title', '')[:40]}"),
                reason=f"Bob {bucket}: {' '.join(str(item.get('title', '')).split())[:160]}",
                payload={"bucket": bucket, "item": item, "bob_run": run},
            )
        )
    return out


def bob_triggers(event: HindsightEvent) -> bool:
    """Only HIGH and CRITICAL get a full review. MEDIUM and LOW never do."""
    return event.priority in ("HIGH", "CRITICAL")


# ---------------------------------------------------------------------------
# Wally: only the best ideas
# ---------------------------------------------------------------------------


def opportunity_score(row: dict, repo_root: Path) -> float:
    """0-100 from Wally's own fields. Deterministic, no model call.

    - below target: up to 50, scaled by the discount to target (25% off = full marks)
    - near the 52-week low: up to 30 (0% above the low = full, 10%+ = none)
    - a valuation config exists for the name: 20
    """
    score = 0.0
    if row.get("below_target"):
        disc = abs(float(row.get("distance_to_target_pct") or 0.0))
        score += 25.0 + min(disc, 25.0)
    dist_low = row.get("distance_to_low_pct")
    if dist_low is not None:
        score += max(0.0, 10.0 - float(dist_low)) * 3.0
    t = bare(row.get("ticker")).lower()
    if (repo_root / "valuations" / f"{t}_ax.yaml").is_file():
        score += 20.0
    return round(min(score, 100.0), 1)


def wally_top(adapter: WallyAdapter, cfg: dict, repo_root: Path) -> list[HindsightEvent]:
    wcfg = cfg.get("wally", {})
    top_n = int(wcfg.get("top_n", 3))
    min_score = float(wcfg.get("min_score", 40.0))
    scored = []
    for row in adapter.rows():
        s = opportunity_score(row, repo_root)
        if s >= min_score:
            scored.append((s, row))
    scored.sort(key=lambda x: (-x[0], x[1]["bare"]))
    out = []
    for s, row in scored[:top_n]:
        out.append(
            HindsightEvent(
                source="WALLY",
                event_type="TOP_OPPORTUNITY",
                ticker=row["bare"],
                priority="MEDIUM",
                source_report_ref=f"wally:{adapter.run_ref}:{row['bare']}",
                reason=f"Wally score {s}: below target={row.get('below_target')}, "
                f"{row.get('distance_to_low_pct', 0):.1f}% above 52w low",
                payload={"row": row, "opportunity_score": s},
            )
        )
    return out


# ---------------------------------------------------------------------------
# JM Watch List triage gates
# ---------------------------------------------------------------------------


def file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def triage_gates(
    ticker: str,
    *,
    cfg: dict,
    prices: dict | None,
    baseline: dict,
    bob_today: list[HindsightEvent],
    wally_row: dict | None,
    thesis: Any,
    thesis_ref: str | None,
    valuation_hash: str | None,
) -> tuple[list[str], dict]:
    """Return ``(reasons, new_baseline)``. No reasons means NO CHANGE.

    ``baseline`` is what we stored last time for this ticker. On first sight
    nothing "since last review" fires; we only record the baseline.
    """
    tcfg = cfg.get("triage", {})
    reasons: list[str] = []
    first_sight = not baseline
    new = dict(baseline)

    if prices:
        last, prev = prices.get("last"), prices.get("prev")
        if last and prev:
            move = (last / prev - 1) * 100
            if abs(move) >= float(tcfg.get("daily_move_pct", 7.0)):
                reasons.append(f"daily move {move:+.1f}%")
        ref_price = baseline.get("review_price")
        if last and ref_price and abs((last / ref_price - 1) * 100) >= float(tcfg.get("move_since_review_pct", 15.0)):
            reasons.append(f"{(last / ref_price - 1) * 100:+.1f}% since last Hindsight review")
        if last and not ref_price:
            new["review_price"] = last

    for ev in bob_today:
        if bare(ev.ticker) == ticker and ev.priority in ("MEDIUM", "HIGH", "CRITICAL"):
            reasons.append(f"new Bob {ev.priority} event: {ev.reason[:80]}")

    below = bool(wally_row and wally_row.get("below_target"))
    if not first_sight and baseline.get("below_target") is not None and below != baseline.get("below_target"):
        reasons.append("crossed its Wally target price " + ("(now below)" if below else "(now above)"))
    new["below_target"] = below

    if thesis is not None:
        # A standing STRAINED status is not news; a pillar newly under pressure is.
        strained = sorted(f"{p.id}:{p.status}" for p in thesis.pillars if p.status in ("STRAINED", "BREACHED"))
        fresh = [s for s in strained if s not in (baseline.get("strained") or [])]
        if fresh and not first_sight:
            reasons.append(f"Theo pillar(s) newly strained or breached: {', '.join(fresh)}")
        new["strained"] = strained
    if thesis_ref:
        if not first_sight and baseline.get("thesis_ref") and baseline["thesis_ref"] != thesis_ref:
            reasons.append("new thesis version")
        new["thesis_ref"] = thesis_ref

    if valuation_hash:
        if not first_sight and baseline.get("valuation_hash") and baseline["valuation_hash"] != valuation_hash:
            reasons.append("valuation config changed")
        new["valuation_hash"] = valuation_hash

    return reasons, new


# ---------------------------------------------------------------------------
# Portfolio review schedule
# ---------------------------------------------------------------------------


def portfolio_review_due(today: dt.date, cfg: dict, done: list[str]) -> str | None:
    """Key of the portfolio review due today (``preseason:2026-07`` or
    ``monthly:2026-09``), or None if every due review has already run."""
    pcfg = cfg.get("portfolio_review", {})
    for month, first, last in pcfg.get("pre_season_windows", []) or []:
        if today.month == int(month) and int(first) <= today.day <= int(last):
            key = f"preseason:{today.year}-{int(month):02d}"
            if key not in done:
                return key
    key = f"monthly:{today.year}-{today.month:02d}"
    if today.day >= int(pcfg.get("monthly_day", 1)) and key not in done:
        return key
    return None


_RANK = {
    ("SELL_SIGNAL", "CRITICAL"): 0,
    ("SELL_SIGNAL", "HIGH"): 0,
    ("HIGH_IMPACT_EVENT", "CRITICAL"): 1,
    ("HIGH_IMPACT_EVENT", "HIGH"): 2,
    ("AUTOPSY_DUE", "MEDIUM"): 3,
    ("WATCHLIST_DAILY", "HIGH"): 4,
    ("TOP_OPPORTUNITY", "MEDIUM"): 5,
    ("PORTFOLIO_REVIEW", "MEDIUM"): 6,
}


def sort_events(events: list[HindsightEvent]) -> list[HindsightEvent]:
    """Highest value first: Sally sells, Bob critical, Bob high, autopsies,
    triage escalations, Wally, portfolio review."""
    return sorted(events, key=lambda e: (_RANK.get((e.event_type, e.priority), 9), e.timestamp))
