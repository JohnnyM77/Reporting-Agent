from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class PortfolioCompany:
    ticker: str
    exchange_ticker: str


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
        out.append(PortfolioCompany(ticker=ticker, exchange_ticker=f"{ticker}{exchange_suffix}"))
    return out
