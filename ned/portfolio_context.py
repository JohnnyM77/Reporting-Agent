# ned/portfolio_context.py
#
# Builds the compact "what JM owns and why" block that Ned's single-episode
# transcript digest puts in its system prompt, so the model can say what an
# episode means for *this* portfolio rather than summarising it politely.
#
# Sources, all read-only:
#   tickers.yaml        holdings across asx / lse / sgx / us
#   theses/*.md         YAML frontmatter only: the bet, pillars, kill conditions
#   watchlists/*.yaml   list name + tickers
#
# Fail soft everywhere. A missing or malformed file is logged and skipped;
# the digest still runs on whatever loaded. Context is a bonus, the episode
# digest is the product.
#
# All file reads are explicit UTF-8: the YouTube path runs on the self-hosted
# Windows runner, where Path.read_text() defaults to cp1252 (see CLAUDE.md,
# "Python read_text() on Windows is cp1252 too").

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# Rough cap on the whole block. ~25k chars is ~6k tokens: cheap next to a
# 60-minute transcript, and enough for every thesis kill condition.
DEFAULT_MAX_CHARS = 25_000

# Always treated as ETFs even if tickers.yaml's etf_tickers list goes missing.
_KNOWN_ETFS = {"VAS", "VEU", "VHY"}

# TII75 is a 30-name list of historical compounders: mostly not tradeable
# ideas for JM. Only include it while it stays small.
_TII75_FILE = "tii75_watchlist.yaml"
_TII75_MAX_TICKERS = 40

# the_bet length per trim stage, applied in order until the block fits;
# None drops it. Tickers, pillar claims and kill conditions are never
# trimmed. Pillar evidence is left out altogether: with it the block is
# ~40k chars, and the model needs the claim and the kill condition, not
# the supporting paragraph.
_BET_TRIM_STAGES: tuple[int | None, ...] = (300, 250, 200, 150, 100, None)

_SECTIONS = (("asx", "ASX"), ("lse", "LSE"), ("sgx", "SGX"), ("us", "US"))


def _log(msg: str) -> None:
    print(f"[ned/context] {msg}")


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _read_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _frontmatter(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    data = yaml.safe_load(parts[1])
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Loaders: each returns plain data, never raises
# ---------------------------------------------------------------------------
def _load_holdings(root: Path) -> list[tuple[str, list[tuple[str, str, bool]]]]:
    """[(exchange label, [(ticker, name, is_etf), ...]), ...]"""
    path = root / "tickers.yaml"
    try:
        data = _read_yaml(path) or {}
    except FileNotFoundError:
        _log(f"{path.name} not found; no holdings in context")
        return []
    except Exception as exc:
        _log(f"could not parse {path.name}: {exc}")
        return []
    if not isinstance(data, dict):
        _log(f"{path.name} is not a mapping; skipped")
        return []

    etfs = set(_KNOWN_ETFS)
    for t in data.get("etf_tickers") or []:
        etfs.add(str(t).strip().upper())

    out = []
    for key, label in _SECTIONS:
        section = data.get(key)
        if not isinstance(section, dict) or not section:
            continue
        rows = []
        for ticker, name in section.items():
            t = str(ticker).strip()
            rows.append((t, str(name or "").strip(), t.upper() in etfs))
        out.append((label, rows))
    return out


def _load_theses(root: Path) -> list[dict]:
    tdir = root / "theses"
    if not tdir.is_dir():
        _log(f"{tdir.name}/ not found; no theses in context")
        return []
    theses = []
    for path in sorted(tdir.glob("*.md")):
        try:
            fm = _frontmatter(path)
        except Exception as exc:
            _log(f"could not parse {path.name}: {exc}")
            continue
        if not fm:
            _log(f"{path.name} has no YAML frontmatter; skipped")
            continue
        if fm.get("draft") is True:
            continue
        pillars = []
        for p in fm.get("pillars") or []:
            if not isinstance(p, dict):
                continue
            pillars.append({
                "id": str(p.get("id") or "").strip(),
                "claim": " ".join(str(p.get("claim") or "").split()),
                "kill_condition": " ".join(str(p.get("kill_condition") or "").split()),
                "status": str(p.get("status") or "").strip(),
            })
        theses.append({
            "ticker": str(fm.get("ticker") or path.stem).strip(),
            "name": str(fm.get("name") or "").strip(),
            "status": str(fm.get("status") or "").strip(),
            "conviction": str(fm.get("conviction") or "").strip(),
            "horizon": " ".join(str(fm.get("horizon") or "").split()),
            "archetype": str(fm.get("archetype") or "").strip(),
            "the_bet": str(fm.get("the_bet") or ""),
            "pillars": pillars,
        })
    return theses


def _watchlist_ticker(entry) -> str:
    if isinstance(entry, dict):
        raw = entry.get("ticker") or entry.get("symbol") or entry.get("code") or ""
    else:
        raw = entry
    t = str(raw or "").strip()
    if t.upper().endswith(".AX"):
        t = t[:-3]
    return t


def _load_watchlists(root: Path) -> list[tuple[str, list[str]]]:
    wdir = root / "watchlists"
    if not wdir.is_dir():
        _log(f"{wdir.name}/ not found; no watchlists in context")
        return []
    out = []
    for path in sorted(wdir.glob("*.yaml")):
        try:
            data = _read_yaml(path)
        except Exception as exc:
            _log(f"could not parse {path.name}: {exc}")
            continue
        if isinstance(data, dict):
            name = str(data.get("name") or path.stem)
            items = data.get("tickers") or []
        elif isinstance(data, list):
            name, items = path.stem, data
        else:
            _log(f"{path.name} has no ticker list; skipped")
            continue
        tickers = [t for t in (_watchlist_ticker(e) for e in items) if t]
        if path.name == _TII75_FILE and len(tickers) >= _TII75_MAX_TICKERS:
            _log(f"{path.name} has {len(tickers)} tickers; left out of context")
            continue
        if tickers:
            out.append((name, tickers))
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def _render(holdings, theses, watchlists, *, bet_chars: int | None) -> str:
    lines: list[str] = ["JM PORTFOLIO CONTEXT"]

    if holdings:
        lines += ["", "HOLDINGS (tickers.yaml)"]
        for label, rows in holdings:
            items = "; ".join(
                f"{t} {n}" + (" [ETF]" if etf else "") for t, n, etf in rows
            )
            lines.append(f"{label}: {items}")

    if theses:
        lines += ["", "THESES (pillars and kill conditions)"]
        for th in theses:
            head = f"{th['ticker']} {th['name']}".strip()
            meta = " | ".join(
                f"{k} {th[k]}" for k in ("status", "conviction", "archetype") if th[k]
            )
            lines.append(f"- {head} | {meta}" if meta else f"- {head}")
            if th["horizon"]:
                lines.append(f"  Horizon: {th['horizon']}")
            if bet_chars and th["the_bet"].strip():
                lines.append(f"  Bet: {_clip(th['the_bet'], bet_chars)}")
            for p in th["pillars"]:
                status = f" [{p['status']}]" if p["status"] else ""
                lines.append(f"  {p['id']}{status}: {p['claim']}")
                if p["kill_condition"]:
                    lines.append(f"    Kill: {p['kill_condition']}")

    if watchlists:
        lines += ["", "WATCHLISTS (not held)"]
        for name, tickers in watchlists:
            lines.append(f"{name}: {', '.join(tickers)}")

    if len(lines) == 1:
        lines.append("(No portfolio files could be loaded.)")
    return "\n".join(lines)


def load_portfolio_context(
    repo_root: Path | None = None,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Return the compact plain-text portfolio block for the digest prompt.

    Never raises. Shortens each thesis's the_bet until the block fits
    `max_chars`; tickers, pillar claims and kill conditions are never cut.
    """
    root = Path(repo_root) if repo_root is not None else REPO_ROOT
    try:
        holdings = _load_holdings(root)
        theses = _load_theses(root)
        watchlists = _load_watchlists(root)
    except Exception as exc:  # belt and braces: loaders already fail soft
        _log(f"context load failed: {exc}")
        return "JM PORTFOLIO CONTEXT\n(No portfolio files could be loaded.)"

    text = ""
    for bet_chars in _BET_TRIM_STAGES:
        text = _render(holdings, theses, watchlists, bet_chars=bet_chars)
        if len(text) <= max_chars:
            break
    if len(text) > max_chars:
        _log(
            f"context is {len(text):,} chars after trimming, over the "
            f"{max_chars:,} cap; kept whole (tickers and kill conditions are never cut)"
        )
    n_held = sum(len(rows) for _, rows in holdings)
    _log(
        f"Portfolio context: {len(text):,} chars "
        f"({n_held} holdings, {len(theses)} theses, {len(watchlists)} watchlists)"
    )
    return text
