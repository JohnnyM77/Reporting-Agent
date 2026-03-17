from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf


@dataclass
class ValuationSnapshot:
    """Current valuation metrics for a ticker fetched via yfinance.

    All fields may be None when the data is unavailable.
    Ratios (trailing_pe, forward_pe, ev_to_ebitda, price_to_sales) are plain
    multiples (e.g. 25.0 means 25×). fcf_yield and dividend_yield are
    expressed as decimals (e.g. 0.04 means 4%).
    """

    trailing_pe: Optional[float]
    forward_pe: Optional[float]
    ev_to_ebitda: Optional[float]
    price_to_sales: Optional[float]
    fcf_yield: Optional[float]
    dividend_yield: Optional[float]


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
    except Exception as e:
        print(f"[wally/data_fetch] Failed to get info for {ticker}: {e}", flush=True)
        info = {}

    try:
        hist = tk.history(period="1y", interval="1d", auto_adjust=False)
    except Exception as e:
        print(f"[wally/data_fetch] Failed to fetch 1y history for {ticker}: {e}", flush=True)
        return None
    
    if hist.empty:
        print(f"[wally/data_fetch] Empty history returned for {ticker}", flush=True)
        return None

    close = hist["Close"].dropna()
    if close.empty:
        print(f"[wally/data_fetch] No Close prices available for {ticker}", flush=True)
        return None

    current_price = float(close.iloc[-1])
    low_52w = float(close.min())
    high_52w = float(close.max())
    name = str(info.get("longName") or info.get("shortName") or ticker)

    if low_52w <= 0:
        print(f"[wally/data_fetch] Invalid 52-week low ({low_52w}) for {ticker}", flush=True)
        return None

    print(f"[wally/data_fetch] Successfully fetched {ticker}: current=${current_price:.2f}, 52w low=${low_52w:.2f}, 52w high=${high_52w:.2f}", flush=True)
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


def fetch_valuation_snapshot(ticker: str) -> ValuationSnapshot:
    """Fetch current valuation metrics for a ticker via yfinance.

    Works for any exchange (ASX, NYSE, NASDAQ, TSE, etc.).
    Returns a ValuationSnapshot with None values for any unavailable metric.
    """
    tk = yf.Ticker(ticker)
    try:
        info = tk.info or {}
    except Exception:
        info = {}

    market_cap = info.get("marketCap")
    fcf = info.get("freeCashflow")
    fcf_yield = (
        fcf / market_cap
        if (
            isinstance(fcf, (int, float))
            and isinstance(market_cap, (int, float))
            and market_cap
        )
        else None
    )

    return ValuationSnapshot(
        trailing_pe=info.get("trailingPE"),
        forward_pe=info.get("forwardPE"),
        ev_to_ebitda=info.get("enterpriseToEbitda"),
        price_to_sales=info.get("priceToSalesTrailing12Months"),
        fcf_yield=fcf_yield,
        dividend_yield=info.get("dividendYield"),
    )
