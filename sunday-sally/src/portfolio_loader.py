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
    tickers = data.get(source_key, [])
    if not isinstance(tickers, list):
        raise ValueError(f"Invalid portfolio source key '{source_key}' in {source_file}")

    out: list[PortfolioCompany] = []
    for raw in tickers:
        ticker = str(raw).strip().upper()
        if not ticker:
            continue
        out.append(PortfolioCompany(ticker=ticker, exchange_ticker=f"{ticker}{exchange_suffix}"))
    return out
