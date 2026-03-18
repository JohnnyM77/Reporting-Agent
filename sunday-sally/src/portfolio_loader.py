from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class PortfolioCompany:
    ticker: str
    exchange_ticker: str


def _load_sally_excluded_etfs() -> frozenset[str]:
    """Load the shared passive-ETF exclusion list from config/excluded_passive_etfs.yaml.

    Resolves the path relative to the repository root.  Returns an empty
    frozenset if the file is missing so that Sally degrades gracefully in
    test environments.
    """
    # This file lives at sunday-sally/src/portfolio_loader.py,
    # so parents[2] is the repository root.
    candidate = Path(__file__).resolve().parents[2] / "config" / "excluded_passive_etfs.yaml"
    if not candidate.exists():
        return frozenset()
    try:
        data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        raw = data.get("excluded_tickers", [])
        return frozenset(str(t).strip().upper() for t in raw if t)
    except Exception:  # noqa: BLE001
        return frozenset()


# Passive ETFs that Sally must never analyse.
# Bob (announcements agent) does NOT use this exclusion set.
SALLY_EXCLUDED_TICKERS: frozenset[str] = _load_sally_excluded_etfs()


def load_portfolio(source_file: str = "tickers.yaml", source_key: str = "asx", exchange_suffix: str = ".AX") -> list[PortfolioCompany]:
    """Load the same portfolio universe used by Bob (tickers.yaml)."""
    path = Path(source_file)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tickers_data = data.get(source_key)

    if tickers_data is None:
        raise ValueError(f"Portfolio source key '{source_key}' not found in {source_file}")

    # Handle both list format (old) and dict format (new enriched with company names)
    if isinstance(tickers_data, dict):
        tickers = list(tickers_data.keys())
    elif isinstance(tickers_data, list):
        tickers = tickers_data
    else:
        raise ValueError(f"Invalid portfolio source key '{source_key}' in {source_file}: expected list or dict, got {type(tickers_data).__name__}")

    out: list[PortfolioCompany] = []
    for raw in tickers:
        ticker = str(raw).strip().upper()
        if not ticker:
            continue
        exchange_ticker = f"{ticker}{exchange_suffix}"
        if exchange_ticker in SALLY_EXCLUDED_TICKERS:
            print(f"[sally] skipping excluded passive ETF: {exchange_ticker}", flush=True)
            continue
        out.append(PortfolioCompany(ticker=ticker, exchange_ticker=exchange_ticker))
    return out
