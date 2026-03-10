from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf


@dataclass
class PriceSnapshot:
    ticker: str
    company_name: str
    current_price: float
    low_52w: float
    high_52w: float


def fetch_price_snapshot(ticker: str) -> Optional[PriceSnapshot]:
    tk = yf.Ticker(ticker)

    info = {}
    try:
        info = tk.info or {}
    except Exception:
        info = {}

    hist = tk.history(period="1y", interval="1d", auto_adjust=False)
    if hist.empty:
        return None

    close = hist["Close"].dropna()
    if close.empty:
        return None

    current_price = float(close.iloc[-1])
    low_52w = float(close.min())
    high_52w = float(close.max())
    name = str(info.get("longName") or info.get("shortName") or ticker)

    if low_52w <= 0:
        return None

    return PriceSnapshot(
        ticker=ticker,
        company_name=name,
        current_price=current_price,
        low_52w=low_52w,
        high_52w=high_52w,
    )


def fetch_price_history_10y_monthly(ticker: str) -> pd.Series:
    hist = yf.Ticker(ticker).history(period="10y", interval="1mo", auto_adjust=False)
    if hist.empty or "Close" not in hist:
        return pd.Series(dtype=float)
    series = hist["Close"].dropna()
    series.index = pd.to_datetime(series.index)
    return series


def fetch_price_history_10y_daily(ticker: str, csv_path: Path) -> Path:
    """Fetch 10-year daily OHLCV history, save to CSV, and return the path.

    The CSV uses YYYYMMDD-formatted dates in the Date column, which matches
    the format expected by generate_asx_value_spreadsheet.
    """
    hist = yf.Ticker(ticker).history(period="10y", interval="1d", auto_adjust=False)
    if hist.empty or "Close" not in hist:
        raise RuntimeError(f"No 10-year daily history available for {ticker}")
    df = hist[["Open", "High", "Low", "Close", "Volume"]].copy()
    df = df.dropna(subset=["Close"])
    df.index = pd.to_datetime(df.index).strftime("%Y%m%d")
    df.index.name = "Date"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path)
    return csv_path
