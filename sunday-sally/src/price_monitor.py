from __future__ import annotations

from dataclasses import dataclass

import yfinance as yf


@dataclass
class PriceData:
    ticker: str
    company_name: str
    current_price: float
    high_52w: float
    low_52w: float
    distance_to_high: float
    market_cap: float | None


def fetch_price_data(exchange_ticker: str, raw_ticker: str) -> PriceData | None:
    tk = yf.Ticker(exchange_ticker)
    hist = tk.history(period="1y", interval="1d", auto_adjust=False)
    if hist.empty or "Close" not in hist:
        return None

    close = hist["Close"].dropna()
    if close.empty:
        return None

    high_52w = float(close.max())
    low_52w = float(close.min())
    current = float(close.iloc[-1])
    distance_to_high = (high_52w - current) / high_52w if high_52w > 0 else 1.0

    try:
        info = tk.info or {}
    except Exception:
        info = {}

    return PriceData(
        ticker=raw_ticker,
        company_name=str(info.get("longName") or info.get("shortName") or raw_ticker),
        current_price=current,
        high_52w=high_52w,
        low_52w=low_52w,
        distance_to_high=distance_to_high,
        market_cap=info.get("marketCap"),
    )
