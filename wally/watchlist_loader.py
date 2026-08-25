from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
    # Optional per-ticker target ("buy") prices, keyed by normalised ticker.
    # Empty for plain-list watchlists, so existing lists behave exactly as before.
    target_prices: dict[str, float] = field(default_factory=dict)


def _ticker_from_entry(entry) -> str | None:
    """Normalise one raw entry to a ticker string.

    Handles plain strings, legacy dict {ticker/name}, the TII75 dict
    {symbol, exchange}, and the new target dict {ticker/code/symbol, ...}.
    """
    if isinstance(entry, str):
        t = entry.strip().upper()
        return t or None
    if isinstance(entry, dict):
        symbol = str(
            entry.get("symbol") or entry.get("ticker") or entry.get("code") or ""
        ).strip().upper()
        if not symbol:
            return None
        exchange = str(entry.get("exchange") or "").strip().upper()
        suffix = _EXCHANGE_SUFFIX.get(exchange, "")
        # Only append the suffix if the symbol doesn't already carry one.
        if suffix and not symbol.endswith(suffix):
            return f"{symbol}{suffix}"
        return symbol
    return None


def _coerce_price(val) -> float | None:
    """Coerce a raw target price to a positive float, or None if unusable."""
    if val is None:
        return None
    try:
        price = float(val)
    except (ValueError, TypeError):
        return None
    return price if price > 0 else None


def load_watchlist(path: str | Path, validate_tii75: bool = False) -> Watchlist:
    p = Path(path)
    print(f"[wally] Loading watchlist: {p}", flush=True)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    # Raw ticker items live either at the top level (a bare list) or under
    # data["tickers"] (a mapping-style watchlist).
    if isinstance(data, list):
        raw_items = data
        name = p.stem.replace("_", " ").title()
        top_targets: dict = {}
    elif isinstance(data, dict):
        raw_items = data.get("tickers", []) or []
        name = str(data.get("name") or p.stem.replace("_", " ").title())
        top_targets = data.get("targets") or {}
    else:
        raise ValueError(f"Invalid watchlist format in {p}")

    tickers: list[str] = []
    target_prices: dict[str, float] = {}

    for item in raw_items:
        ticker = _ticker_from_entry(item)
        if not ticker:
            continue
        tickers.append(ticker)
        if isinstance(item, dict):
            price = _coerce_price(item.get("target_price", item.get("buy_price")))
            if price is not None:
                target_prices[ticker] = price

    # Merge any top-level `targets:` map (TICKER -> price).
    if isinstance(top_targets, dict):
        for raw_key, raw_price in top_targets.items():
            key = str(raw_key).strip().upper()
            price = _coerce_price(raw_price)
            if key and price is not None:
                target_prices[key] = price

    # Keep the sorted(set(...)) dedupe on tickers.
    tickers = sorted(set(tickers))

    print(f"[wally] Loaded watchlist '{name}' — {len(tickers)} tickers", flush=True)
    if target_prices:
        print(f"[wally] Target prices set for {len(target_prices)} ticker(s)", flush=True)
    if tickers:
        sample = ", ".join(tickers[:10])
        print(f"[wally] Sample tickers: {sample}", flush=True)

    if validate_tii75:
        _validate_tii75(tickers, p)

    return Watchlist(
        name=name,
        tickers=tickers,
        source_path=p,
        target_prices=target_prices,
    )


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
