"""Adapters: read what the other agents already wrote. Change nothing.

Every adapter here reads an existing output file (or git history of one).
None of them imports or calls the agent that produced it, so Bob, Sally,
Wally and Theo run exactly as before whether Hindsight exists or not.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import yaml

from .config import REPO_ROOT
from .schemas import HindsightEvent


def bare(ticker: str) -> str:
    t = str(ticker or "").strip().upper()
    return t[:-3] if t.endswith(".AX") else t


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# git helpers (history of Sally's JSON and Theo's thesis files)
# ---------------------------------------------------------------------------


def git_log(rel_path: str, repo_root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """``[(sha, iso_date)]`` newest first. Empty on any git trouble."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--follow", "--format=%H %cs", "--", rel_path],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    rows = []
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2:
            rows.append((parts[0], parts[1]))
    return rows


def git_show(sha: str, rel_path: str, repo_root: Path = REPO_ROOT) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{sha}:{rel_path}"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


# ---------------------------------------------------------------------------
# Sally
# ---------------------------------------------------------------------------


class SallyAdapter:
    REL = "docs/data/sally.json"

    def __init__(self, repo_root: Path = REPO_ROOT, cfg: dict | None = None, history: list[dict] | None = None):
        self.repo_root = repo_root
        self.cfg = (cfg or {}).get("sally", {})
        self._history = history

    def latest(self) -> dict:
        return _load_json(self.repo_root / self.REL)

    def history(self) -> list[dict]:
        """Every distinct Sally run we can see, newest first."""
        if self._history is not None:
            return self._history
        runs: dict[str, dict] = {}
        current = self.latest()
        if current.get("last_run"):
            runs[current["last_run"]] = current
        for sha, _ in git_log(self.REL, self.repo_root):
            text = git_show(sha, self.REL, self.repo_root)
            if not text:
                continue
            try:
                data = json.loads(text)
            except ValueError:
                continue
            runs.setdefault(str(data.get("last_run")), data)
        self._history = [runs[k] for k in sorted(runs, reverse=True)]
        return self._history

    def consecutive_flags(self, ticker: str) -> list[str]:
        """Run dates, newest first, of the unbroken streak of flags on ``ticker``."""
        t = bare(ticker)
        streak = []
        for run in self.history():
            if any(bare(r.get("ticker")) == t for r in run.get("flagged", [])):
                streak.append(str(run.get("last_run")))
            else:
                break
        return streak

    def row(self, ticker: str) -> dict | None:
        for r in self.latest().get("flagged", []):
            if bare(r.get("ticker")) == bare(ticker):
                return r
        return None

    def signal(self, row: dict) -> str:
        """Map Sally's verdict onto SELL / REDUCE / HOLD. Sally never says SELL."""
        verdict = str(row.get("sally_verdict", "")).lower()
        tier = str(row.get("alert_tier", "")).lower()
        if any(w in verdict for w in ("sell", "exit")):
            return "SELL"
        if "trim" in verdict or "reduce" in verdict or "tier 3" in tier:
            return "REDUCE"
        if "stop adding" in verdict or "tier 2" in tier:
            pct = row.get("valuation_percentile")
            dist = row.get("distance_to_high_pct")
            if (
                pct is not None
                and dist is not None
                and float(pct) >= float(self.cfg.get("reduce_percentile", 0.95))
                and float(dist) <= float(self.cfg.get("reduce_max_distance_pct", 3.0))
            ):
                return "REDUCE"
        return "HOLD"

    def _event(self, row: dict, run_date: str, signal: str, source: str, reason: str) -> HindsightEvent:
        t = bare(row.get("ticker"))
        return HindsightEvent(
            source=source,
            event_type="SELL_SIGNAL",
            event_subtype=signal,
            ticker=t,
            priority="HIGH",
            source_report_ref=f"sally:{run_date}:{t}",
            reason=reason,
            payload={"sally_row": row, "sally_run": run_date, "mapped_signal": signal},
        )

    def events(self, holdings: set[str]) -> list[HindsightEvent]:
        data = self.latest()
        run = str(data.get("last_run") or "")
        out = []
        for row in data.get("flagged", []):
            t = bare(row.get("ticker"))
            if t not in holdings:
                continue
            sig = self.signal(row)
            if sig == "HOLD":
                continue
            out.append(self._event(row, run, sig, "SALLY",
                                   f"Sally: {row.get('sally_verdict')} ({row.get('alert_tier')}), mapped to {sig}"))
        return out

    def manual_event(self, ticker: str) -> HindsightEvent:
        data = self.latest()
        run = str(data.get("last_run") or dt.date.today().isoformat())
        row = self.row(ticker) or {"ticker": bare(ticker)}
        if row.get("sally_verdict"):
            sig = self.signal(row)
            reason = (f"Manual sell review requested. Sally's latest: {row.get('sally_verdict')}"
                      f" ({row.get('alert_tier', 'n/a')}), gate maps it to {sig}")
        else:
            sig = "MANUAL"
            reason = f"Manual review requested. Sally has not flagged {bare(ticker)} in her {run} run."
        return self._event(row, run, sig, "MANUAL", reason)


# ---------------------------------------------------------------------------
# Bob
# ---------------------------------------------------------------------------


class BobAdapter:
    REL = "docs/data/bob.json"

    def __init__(self, repo_root: Path = REPO_ROOT, data: dict | None = None):
        self.repo_root = repo_root
        self._data = data

    def latest(self) -> dict:
        if self._data is not None:
            return self._data
        return _load_json(self.repo_root / self.REL)

    def items(self) -> list[tuple[str, dict]]:
        """``[(bucket, item)]`` for every item in Bob's latest run."""
        data = self.latest()
        out = []
        for bucket, key in (("HIGH IMPACT", "high_impact"), ("MATERIAL", "material"), ("FYI", "fyi")):
            for item in data.get(key, []) or []:
                out.append((bucket, item))
        # A future Master Engine runner may write its aggregated events; read
        # them if they exist, but do not depend on them.
        run = data.get("last_run") or dt.date.today().isoformat()
        me = self.repo_root / "outputs" / str(run) / "master_investor_events.json"
        if me.is_file():
            payload = _load_json(me)
            for ev in payload.get("events", []) if isinstance(payload, dict) else payload:
                if str(ev.get("agent")) == "bob":
                    bucket = "HIGH IMPACT" if ev.get("priority") in ("CRITICAL", "HIGH") else "MATERIAL"
                    out.append((bucket, {"ticker": bare(ev.get("ticker")), "title": ev.get("headline", ""),
                                         "url": ev.get("asx_url") or "", "summary": ev.get("summary", "")}))
        return out

    @property
    def run_date(self) -> str:
        return str(self.latest().get("last_run") or "")


# ---------------------------------------------------------------------------
# Wally
# ---------------------------------------------------------------------------


class WallyAdapter:
    REL = "docs/data/wally.json"

    def __init__(self, repo_root: Path = REPO_ROOT, data: dict | None = None):
        self.repo_root = repo_root
        self._data = data

    def latest(self) -> dict:
        if self._data is not None:
            return self._data
        return _load_json(self.repo_root / self.REL)

    def rows(self) -> list[dict]:
        """Flagged rows across every watchlist, one per ticker (first list wins)."""
        seen: dict[str, dict] = {}
        for name, wl in (self.latest().get("watchlists") or {}).items():
            for row in wl.get("flagged", []) or []:
                t = bare(row.get("ticker"))
                if t and t not in seen:
                    seen[t] = {**row, "watchlist": name, "bare": t}
        return list(seen.values())

    def row(self, ticker: str) -> dict | None:
        for r in self.rows():
            if r["bare"] == bare(ticker):
                return r
        return None

    @property
    def run_ref(self) -> str:
        return str(self.latest().get("last_run") or "")[:10]


# ---------------------------------------------------------------------------
# Theo (read only, always)
# ---------------------------------------------------------------------------


class TheoAdapter:
    def __init__(self, repo_root: Path = REPO_ROOT):
        self.repo_root = repo_root
        self._map = None

    def theses(self) -> dict:
        if self._map is None:
            from theo import thesis as theo_thesis

            self._map = theo_thesis.load_map(self.repo_root / "theses")
        return self._map

    def get(self, ticker: str):
        return self.theses().get(bare(ticker))

    def versions(self, ticker: str) -> list[dict]:
        """Every committed version of the thesis, oldest first.

        ``[{sha, date, thesis}]``. Needs full git history (fetch-depth: 0);
        a shallow clone returns only what it has.
        """
        from theo import thesis as theo_thesis

        rel = f"theses/{bare(ticker)}.md"
        out = []
        for sha, date in reversed(git_log(rel, self.repo_root)):
            text = git_show(sha, rel, self.repo_root)
            if not text:
                continue
            try:
                data, body = theo_thesis.split_frontmatter(text)
                out.append({"sha": sha, "date": date, "thesis": theo_thesis.from_dict(data, body)})
            except Exception:  # a malformed historic version is skipped, not fatal
                continue
        return out


# ---------------------------------------------------------------------------
# Portfolio and watchlist
# ---------------------------------------------------------------------------


class PortfolioAdapter:
    """Holdings from ``tickers.yaml``; positions from the decision log.

    Decision log precedence: ``HINDSIGHT_DECISION_LOG`` env, then
    ``<store>/data/JM_Decision_Level_IRR.xlsx``, then ``data/decisions.json``
    (the money-free export Theo reads in CI), then ``<store>/positions.yaml``.
    """

    def __init__(self, repo_root: Path = REPO_ROOT, store_root: Path | None = None):
        self.repo_root = repo_root
        self.store_root = store_root
        self._ledger = None

    def holdings(self) -> dict[str, str]:
        data = yaml.safe_load((self.repo_root / "tickers.yaml").read_text(encoding="utf-8")) or {}
        etfs = {bare(t) for t in data.get("etf_tickers", []) or []}
        out = {}
        for key in ("asx", "lse", "sgx", "us"):
            for t, name in (data.get(key) or {}).items():
                t = bare(str(t).rstrip("."))
                if t not in etfs:
                    out[t] = str(name)
        return out

    def ledger(self):
        if self._ledger is not None:
            return self._ledger
        from theo import ledger as theo_ledger

        candidates = []
        if os.environ.get("HINDSIGHT_DECISION_LOG"):
            candidates.append(Path(os.environ["HINDSIGHT_DECISION_LOG"]))
        if self.store_root:
            candidates.append(self.store_root / "data" / "JM_Decision_Level_IRR.xlsx")
        candidates.append(self.repo_root / "data" / "decisions.json")
        for path in candidates:
            if path.is_file():
                led = theo_ledger.load(path)
                if led:
                    self._ledger = led
                    return led
        self._ledger = theo_ledger.Ledger()
        return self._ledger

    def positions_yaml(self) -> dict:
        if not self.store_root:
            return {}
        path = self.store_root / "positions.yaml"
        if not path.is_file():
            return {}
        return (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("positions", {}) or {}


class WatchlistAdapter:
    REL = "watchlists/jm_watchlist.yaml"

    def __init__(self, repo_root: Path = REPO_ROOT, wally: WallyAdapter | None = None):
        self.repo_root = repo_root
        self.wally = wally or WallyAdapter(repo_root)

    def tickers(self) -> list[str]:
        data = yaml.safe_load((self.repo_root / self.REL).read_text(encoding="utf-8")) or {}
        out = []
        for entry in data.get("tickers", []) or []:
            t = entry.get("ticker") if isinstance(entry, dict) else entry
            if t and bare(t) not in out:
                out.append(bare(t))
        return out

    def target(self, ticker: str) -> float | None:
        row = self.wally.row(ticker)
        return row.get("target_price") if row else None
