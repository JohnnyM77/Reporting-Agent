from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

# Canonical members that must be present in the TII75 watchlist.
_TII75_CANONICAL_COUNT = 30
_TII75_REQUIRED_TICKERS = {"POOL", "FICO", "CPRT", "2914.T"}

# Map exchange names to Yahoo Finance ticker suffixes.
_EXCHANGE_SUFFIX: dict[str, str] = {
    "ASX": ".AX",
    "LSE": ".L",
    "TSX": ".TO",
    "NASDAQ": "",
    "NYSE": "",
    "EURONEXT": ".AS",
    "NZX": ".NZ",
}


@dataclass
class Watchlist:
    name: str
    tickers: list[str]
    source_path: Path


def _normalize_ticker_entry(entry) -> str | None:
    """Handle plain strings, legacy dict {ticker/name}, and new dict {symbol, exchange}."""
    if isinstance(entry, str):
        t = entry.strip().upper()
        return t if t else None
    if isinstance(entry, dict):
        # Support both 'symbol' (new format) and 'ticker' (legacy TII75 format).
        symbol = str(entry.get("symbol") or entry.get("ticker") or "").strip().upper()
        if not symbol:
            return None
        exchange = str(entry.get("exchange") or "").strip().upper()
        suffix = _EXCHANGE_SUFFIX.get(exchange, "")
        # Only append suffix if the symbol doesn't already have one.
        if suffix and not symbol.endswith(suffix):
            return f"{symbol}{suffix}"
        return symbol
    return None


def _normalize_tickers(values: Iterable) -> list[str]:
    out = []
    for val in values:
        t = _normalize_ticker_entry(val)
        if t:
            out.append(t)
    seen: set[str] = set()
    deduped: list[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped


def _validate_tii75(tickers: list[str], source_path: Path) -> None:
    """Validate the TII75 canonical list and log errors; raises on failure."""
    errors: list[str] = []
    if len(tickers) != _TII75_CANONICAL_COUNT:
        errors.append(
            f"[wally] ERROR: TII75 canonical watchlist should contain "
            f"{_TII75_CANONICAL_COUNT} tickers but loaded {len(tickers)}"
        )
    ticker_set = set(tickers)
    for required in sorted(_TII75_REQUIRED_TICKERS):
        if required not in ticker_set:
            errors.append(
                f"[wally] ERROR: TII75 watchlist missing expected ticker {required}"
            )
    for msg in errors:
        print(msg, flush=True)
    if errors:
        raise ValueError(
            f"TII75 watchlist loaded from {source_path} failed canonical validation "
            f"({len(errors)} error(s) — see logs above)"
        )


def load_watchlist(path: str | Path, validate_tii75: bool = False) -> Watchlist:
    p = Path(path)
    print(f"[wally] Loading watchlist: {p}", flush=True)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    if isinstance(data, list):
        name = p.stem.replace("_", " ").title()
        tickers = _normalize_tickers(data)
    elif isinstance(data, dict):
        tickers = _normalize_tickers(data.get("tickers", []))
        name = str(data.get("name") or p.stem.replace("_", " ").title())
    else:
        raise ValueError(f"Invalid watchlist format in {p}")

    print(f"[wally] Loaded watchlist '{name}' — {len(tickers)} tickers", flush=True)
    if tickers:
        sample = ", ".join(tickers[:10])
        print(f"[wally] Sample tickers: {sample}", flush=True)

    if validate_tii75:
        _validate_tii75(tickers, p)

    return Watchlist(name=name, tickers=tickers, source_path=p)
