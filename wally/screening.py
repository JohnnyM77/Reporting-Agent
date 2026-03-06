from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

from .config import LOW_THRESHOLD_PCT
from .data_fetch import PriceSnapshot


@dataclass
class TickerScreenResult:
    ticker: str
    company_name: str
    current_price: float
    low_52w: float
    high_52w: float
    distance_to_low_pct: float
    below_high_pct: float
    flagged: bool
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def screen_snapshot(snapshot: PriceSnapshot, threshold_pct: float = LOW_THRESHOLD_PCT) -> TickerScreenResult:
    distance_to_low_pct = ((snapshot.current_price - snapshot.low_52w) / snapshot.low_52w) * 100
    below_high_pct = ((snapshot.high_52w - snapshot.current_price) / snapshot.high_52w) * 100 if snapshot.high_52w > 0 else 0.0

    return TickerScreenResult(
        ticker=snapshot.ticker,
        company_name=snapshot.company_name,
        current_price=snapshot.current_price,
        low_52w=snapshot.low_52w,
        high_52w=snapshot.high_52w,
        distance_to_low_pct=distance_to_low_pct,
        below_high_pct=below_high_pct,
        flagged=distance_to_low_pct <= threshold_pct,
    )
