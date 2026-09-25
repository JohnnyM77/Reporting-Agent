"""Behavioural facts, computed by code, never by the model.

These are the records the bias analysis is allowed to lean on. Every fact
carries an evidence ref the model can cite; a bias claim that cites nothing
from this registry is downgraded by the validator.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from pathlib import Path
from typing import Any

import yaml

_SELL_TARGET_RE = re.compile(r"sell\s+above\s+(?:A?\$|US\$)?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


@dataclasses.dataclass
class Tranche:
    index: int
    date: dt.date | None
    price: float
    weight: float
    open: bool = True
    value_multiple: float | None = None  # value at ledger date / cost


@dataclasses.dataclass
class BehaviourFacts:
    ticker: str
    held: bool = False
    tranches: list[Tranche] = dataclasses.field(default_factory=list)
    avg_cost: float | None = None
    current_price: float | None = None
    price_source: str = ""
    unrealised_pct: float | None = None
    averaging_down_tranches: int = 0
    averaging_down_recent: bool = False
    holding_days: int | None = None
    sell_target: float | None = None
    sell_target_source: str = ""
    above_sell_target: bool = False
    thesis_versions: int = 0
    thesis_amendments: int = 0
    loosened_amendments: int = 0
    days_since_theo_review: int | None = None
    cost_weight: float | None = None
    value_weight: float | None = None
    ledger_as_at: str = ""
    sally_flag_dates: list[str] = dataclasses.field(default_factory=list)
    unanswered_sally_flags: int = 0
    valuation_bands: dict[str, Any] = dataclasses.field(default_factory=dict)
    kill_condition_pressure: list[str] = dataclasses.field(default_factory=list)
    thesis_revised_under_pressure: bool = False

    def hard_triggers(self, cfg: dict) -> list[str]:
        """Section 9/10 hard behavioural triggers. Code decides, not the model."""
        lcfg = cfg.get("lollapalooza", {})
        out = []
        if self.unrealised_pct is not None and self.unrealised_pct <= -float(lcfg.get("down_vs_cost_pct", 20.0)):
            out.append(f"position down {self.unrealised_pct:.1f}% vs average cost")
        if self.averaging_down_recent:
            out.append(f"averaged down within the last {lcfg.get('averaging_down_months', 12)} months")
        if self.thesis_revised_under_pressure:
            out.append("thesis revised after a kill condition came under pressure")
        if self.above_sell_target:
            out.append(f"price above the original sell target of ${self.sell_target:.2f}")
        if self.unanswered_sally_flags >= int(lcfg.get("unanswered_sally_flags", 2)):
            out.append(f"{self.unanswered_sally_flags} consecutive Sally flags with no recorded response")
        return out

    def lines(self) -> list[tuple[str, str]]:
        """``[(ref, text)]`` for the prompt and the report."""
        t = self.ticker
        out: list[tuple[str, str]] = []
        for tr in self.tranches:
            w = f", {tr.weight * 100:.1f}% of capital committed" if tr.weight else ""
            out.append((f"decision:{t}#{tr.index}",
                        f"Buy #{tr.index} on {tr.date} at ${tr.price:.2f}{w}{'' if tr.open else ' (closed)'}"))
        facts = []
        if self.avg_cost:
            facts.append(f"capital-weighted average cost ${self.avg_cost:.2f}")
        if self.current_price:
            facts.append(f"current price ${self.current_price:.2f} ({self.price_source})")
        if self.unrealised_pct is not None:
            facts.append(f"unrealised return vs average cost {self.unrealised_pct:+.1f}% (price only, excludes dividends)")
        facts.append(f"{self.averaging_down_tranches} averaging-down tranche(s) (bought below the previous buy)"
                     + (", at least one in the last 12 months" if self.averaging_down_recent else ", none in the last 12 months"))
        if self.holding_days is not None:
            facts.append(f"held {self.holding_days} days since the first buy")
        if self.sell_target:
            facts.append(f"original sell target ${self.sell_target:.2f} from {self.sell_target_source}; "
                         + ("price is ABOVE it" if self.above_sell_target else "price has not reached it"))
        if self.cost_weight is not None:
            facts.append(f"{self.cost_weight * 100:.1f}% of all capital committed across the ledger")
        if self.value_weight is not None:
            facts.append(f"about {self.value_weight * 100:.1f}% of portfolio value as at {self.ledger_as_at}")
        if self.days_since_theo_review is not None:
            facts.append(f"{self.days_since_theo_review} days since Theo's last review")
        facts.append(f"thesis: {self.thesis_versions} committed version(s), {self.thesis_amendments} recorded amendment(s), "
                     f"{self.loosened_amendments} labelled LOOSENED")
        if self.kill_condition_pressure:
            facts.append("pillars strained or breached: " + ", ".join(self.kill_condition_pressure))
        if self.sally_flag_dates:
            facts.append(f"Sally has flagged it {len(self.sally_flag_dates)} run(s) in a row ({', '.join(self.sally_flag_dates)}); "
                         f"{self.unanswered_sally_flags} without a recorded response or Theo review")
        out.append((f"behaviour:{t}", "; ".join(facts)))
        if self.valuation_bands:
            vb = self.valuation_bands
            out.append((f"valuation:{t}", vb.get("text", "")))
        return out


def valuation_bands(repo_root: Path, ticker: str) -> dict[str, Any]:
    """Buy and sell prices implied by ``valuations/<t>_ax.yaml``, when the
    config carries a normalised EPS. A config, not a fair value."""
    path = repo_root / "valuations" / f"{ticker.lower()}_ax.yaml"
    if not path.is_file():
        return {}
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    eps = cfg.get("norm_eps")
    bm, sm = cfg.get("buy_multiple"), cfg.get("sell_multiple")
    out = {"stock_type": cfg.get("stock_type"), "buy_multiple": bm, "sell_multiple": sm, "norm_eps_cents": eps}
    if eps and bm and sm:
        buy, sell = float(eps) * float(bm) / 100, float(eps) * float(sm) / 100
        out.update(buy_below=round(buy, 2), sell_above=round(sell, 2))
        out["text"] = (f"valuations/{path.name}: {cfg.get('stock_type', '')} config, normalised EPS {eps}c; "
                       f"buy below ${buy:.2f} ({bm}x), sell above ${sell:.2f} ({sm}x)")
    else:
        out["text"] = f"valuations/{path.name}: {cfg.get('stock_type', '')} config, buy {bm}x / sell {sm}x trailing EPS"
    return out


def _sell_target(thesis: Any, positions_row: dict) -> tuple[float | None, str]:
    if positions_row.get("sell_target"):
        return float(positions_row["sell_target"]), "positions.yaml"
    if thesis is None:
        return None, ""
    texts = [s.note for s in getattr(thesis, "sources", [])] + [thesis.valuation_note, thesis.body]
    for text in texts:
        m = _SELL_TARGET_RE.search(text or "")
        if m:
            return float(m.group(1)), f"theses/{thesis.ticker}.md"
    return None, ""


def compute(
    ticker: str,
    *,
    ledger: Any,
    thesis: Any,
    thesis_versions: int,
    current_price: float | None,
    price_source: str,
    sally_streak: list[str],
    answered_after: list[dt.date],
    positions_row: dict | None = None,
    repo_root: Path | None = None,
    today: dt.date | None = None,
    cfg: dict | None = None,
) -> BehaviourFacts:
    today = today or dt.date.today()
    cfg = cfg or {}
    positions_row = positions_row or {}
    f = BehaviourFacts(ticker=ticker, current_price=current_price, price_source=price_source)

    decisions = ledger.decisions(ticker) if ledger else []
    if decisions:
        f.held = any(d.open for d in decisions)
        f.ledger_as_at = ledger.as_at.isoformat() if ledger.as_at else ""
        for d in decisions:
            mult = None
            if d.normalised and d.flows and d.weight:
                last_date = max(w for w, _ in d.flows)
                last = sum(a for w, a in d.flows if w == last_date and a > 0)
                mult = last / d.weight if d.weight else None
            f.tranches.append(Tranche(d.index, d.date, float(d.price or 0.0), float(d.weight or 0.0), d.open, mult))
    elif positions_row.get("tranches"):
        f.held = True
        for i, tr in enumerate(positions_row["tranches"], 1):
            f.tranches.append(Tranche(i, _as_date(tr.get("date")), float(tr.get("price") or 0),
                                      float(tr.get("weight") or 0), True))

    open_tr = [t for t in f.tranches if t.open and t.price > 0]
    if open_tr:
        if all(t.weight > 0 for t in open_tr):
            capital = sum(t.weight for t in open_tr)
            shares = sum(t.weight / t.price for t in open_tr)
            f.avg_cost = capital / shares if shares else None
        else:
            f.avg_cost = sum(t.price for t in open_tr) / len(open_tr)
    dated = sorted([t for t in f.tranches if t.date], key=lambda t: t.date)
    months = int(cfg.get("lollapalooza", {}).get("averaging_down_months", 12))
    for prev, cur in zip(dated, dated[1:]):
        if cur.price < prev.price:
            f.averaging_down_tranches += 1
            if (today - cur.date).days <= months * 30.5:
                f.averaging_down_recent = True
    if dated:
        f.holding_days = (today - dated[0].date).days
    if f.avg_cost and current_price:
        f.unrealised_pct = (current_price / f.avg_cost - 1) * 100

    if ledger and ledger.holdings and f.tranches:
        total_w = sum(h.weight for h in ledger.holdings.values())
        mine = sum(t.weight for t in f.tranches if t.open)
        f.cost_weight = mine / total_w if total_w else None
        values = {}
        for h in ledger.holdings.values():
            v = 0.0
            for d in h.decisions:
                if d.open and d.flows:
                    last_date = max(w for w, _ in d.flows)
                    v += sum(a for w, a in d.flows if w == last_date and a > 0)
            values[h.ticker] = v
        total_v = sum(values.values())
        if total_v and ticker in values:
            f.value_weight = values[ticker] / total_v

    f.sell_target, f.sell_target_source = _sell_target(thesis, positions_row)
    if f.sell_target and current_price:
        f.above_sell_target = current_price > f.sell_target

    f.thesis_versions = thesis_versions
    if thesis is not None:
        f.thesis_amendments = len(thesis.all_amendments)
        f.loosened_amendments = sum(1 for a in thesis.all_amendments if a.loosened)
        last = thesis.last_review
        if last and last.date:
            f.days_since_theo_review = (today - last.date).days
        f.kill_condition_pressure = [p.id for p in thesis.pillars if p.status in ("STRAINED", "BREACHED")]
        # A review that amended a pillar which was strained or breached at the
        # time counts as "revised under pressure".
        for r in thesis.reviews:
            statuses = thesis.pillar_status_at(r)
            if any(statuses.get(a.pillar) in ("STRAINED", "BREACHED") or a.loosened for a in r.amendments):
                f.thesis_revised_under_pressure = True

    f.sally_flag_dates = list(sally_streak)
    unanswered = 0
    for d in sally_streak:
        when = _as_date(d)
        if when and any(a >= when for a in answered_after):
            continue
        unanswered += 1
    f.unanswered_sally_flags = unanswered

    if repo_root:
        f.valuation_bands = valuation_bands(repo_root, ticker)
    return f


def _as_date(v: Any) -> dt.date | None:
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None
