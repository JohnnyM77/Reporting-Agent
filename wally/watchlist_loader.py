from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml


@dataclass
class Watchlist:
    name: str
    tickers: list[str]
    source_path: Path


def _normalize_tickers(values: Iterable[str]) -> list[str]:
    out = []
    for val in values:
        ticker = str(val).strip().upper()
        if ticker:
            out.append(ticker)
    return sorted(set(out))


def load_watchlist(path: str | Path) -> Watchlist:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    if isinstance(data, list):
        name = p.stem.replace("_", " ").title()
        tickers = _normalize_tickers(data)
        return Watchlist(name=name, tickers=tickers, source_path=p)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid watchlist format in {p}")

    tickers = _normalize_tickers(data.get("tickers", []))
    name = str(data.get("name") or p.stem.replace("_", " ").title())
    return Watchlist(name=name, tickers=tickers, source_path=p)
