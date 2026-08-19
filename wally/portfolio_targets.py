"""Load and manage portfolio target data (buy/sell prices, weightings) for watchlists."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class PortfolioTarget:
    """Buy/sell targets and portfolio weighting for a single holding."""
    ticker: str
    company: str
    buy_below: Optional[float]
    sell_above: Optional[float]
    max_weight: Optional[float]
    currency: str = "AUD"


class PortfolioTargets:
    """Container for portfolio targets indexed by ticker."""

    def __init__(self):
        self.targets: dict[str, PortfolioTarget] = {}

    def load_from_file(self, path: str | Path) -> None:
        """Load targets from YAML file in tii_portfolio_targets.yaml format."""
        p = Path(path)
        if not p.exists():
            print(f"[portfolio_targets] File not found: {p}", flush=True)
            return

        print(f"[portfolio_targets] Loading: {p}", flush=True)
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        holdings = data.get("holdings", [])

        for holding in holdings:
            ticker = holding.get("ticker", "").strip().upper()
            if not ticker:
                continue

            target = PortfolioTarget(
                ticker=ticker,
                company=holding.get("company", ""),
                buy_below=self._parse_float(holding.get("buy_below")),
                sell_above=self._parse_float(holding.get("sell_above")),
                max_weight=self._parse_float(holding.get("max_weight")),
                currency=holding.get("currency", "AUD"),
            )
            self.targets[ticker] = target

        print(f"[portfolio_targets] Loaded {len(self.targets)} targets", flush=True)

    def get(self, ticker: str) -> PortfolioTarget | None:
        """Get target for a ticker (case-insensitive)."""
        return self.targets.get(ticker.upper())

    def has_targets(self) -> bool:
        """Check if any targets are loaded."""
        return len(self.targets) > 0

    @staticmethod
    def _parse_float(val) -> Optional[float]:
        """Safely parse float, returning None for None, 'null', or '-'."""
        if val is None or val == '-' or val == 'null':
            return None
        try:
            return float(val)
        except (ValueError, TypeError):
            return None


def load_portfolio_targets(path: str | Path) -> PortfolioTargets:
    """Load portfolio targets from file."""
    targets = PortfolioTargets()
    targets.load_from_file(path)
    return targets
