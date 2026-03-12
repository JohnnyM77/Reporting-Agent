"""Alpha Vantage data fetching for Wally and Sally.

Used as a supplement to yfinance when ALPHAVANTAGE_API_KEY is set:
  - Weekly adjusted price history (TIME_SERIES_WEEKLY_ADJUSTED)
  - Quarterly and annual earnings / EPS (EARNINGS)

Free tier limits: 25 requests/day, 5 requests/minute.
ASX tickers use the .AX suffix (e.g. BHP.AX), same as yfinance.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import requests


BASE_URL = "https://www.alphavantage.co/query"
_LAST_CALL: float = 0.0
_MIN_INTERVAL = 12.5  # seconds between calls to stay under 5/min


def _call(params: dict) -> dict:
    """Rate-limited GET to Alpha Vantage."""
    global _LAST_CALL
    elapsed = time.time() - _LAST_CALL
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    resp = requests.get(BASE_URL, params=params, timeout=20)
    _LAST_CALL = time.time()
    resp.raise_for_status()
    data = resp.json()
    if "Note" in data:
        raise RuntimeError(f"Alpha Vantage rate limit hit: {data['Note']}")
    if "Error Message" in data:
        raise RuntimeError(f"Alpha Vantage error: {data['Error Message']}")
    return data


def get_api_key() -> Optional[str]:
    return os.environ.get("ALPHAVANTAGE_API_KEY") or None


# ---------------------------------------------------------------------------
# Weekly prices
# ---------------------------------------------------------------------------

def fetch_weekly_prices(ticker: str, api_key: str) -> pd.DataFrame:
    """Return a DataFrame of weekly adjusted prices for *ticker*.

    Columns: open, high, low, close, adjusted_close, volume, dividend
    Index: pd.DatetimeIndex (weekly, most-recent last)
    Raises RuntimeError if the ticker is not found or the call fails.
    """
    data = _call({
        "function": "TIME_SERIES_WEEKLY_ADJUSTED",
        "symbol": ticker,
        "apikey": api_key,
        "datatype": "json",
    })
    series = data.get("Weekly Adjusted Time Series")
    if not series:
        raise RuntimeError(f"No weekly data returned for {ticker}")

    rows = []
    for date_str, vals in series.items():
        rows.append({
            "date": pd.to_datetime(date_str),
            "open": float(vals["1. open"]),
            "high": float(vals["2. high"]),
            "low": float(vals["3. low"]),
            "close": float(vals["4. close"]),
            "adjusted_close": float(vals["5. adjusted close"]),
            "volume": int(vals["6. volume"]),
            "dividend": float(vals["7. dividend amount"]),
        })

    df = pd.DataFrame(rows).set_index("date").sort_index()
    return df


def fetch_weekly_close_series(ticker: str, api_key: str) -> pd.Series:
    """Convenience wrapper — returns just the adjusted weekly close as a Series."""
    df = fetch_weekly_prices(ticker, api_key)
    return df["adjusted_close"]


# ---------------------------------------------------------------------------
# Earnings
# ---------------------------------------------------------------------------

@dataclass
class QuarterlyEarning:
    fiscal_date: str          # e.g. "2024-09-30"
    reported_date: str        # e.g. "2024-10-25"
    reported_eps: Optional[float]
    estimated_eps: Optional[float]
    surprise: Optional[float]
    surprise_pct: Optional[float]


@dataclass
class AnnualEarning:
    fiscal_date: str
    reported_eps: Optional[float]


@dataclass
class EarningsData:
    ticker: str
    annual: list[AnnualEarning]
    quarterly: list[QuarterlyEarning]

    @property
    def latest_quarterly(self) -> Optional[QuarterlyEarning]:
        return self.quarterly[0] if self.quarterly else None

    @property
    def trailing_four_quarters_eps(self) -> Optional[float]:
        """Sum of reported EPS for the last 4 quarters (TTM EPS)."""
        eps_vals = [q.reported_eps for q in self.quarterly[:4] if q.reported_eps is not None]
        if len(eps_vals) < 4:
            return None
        return sum(eps_vals)


def fetch_earnings(ticker: str, api_key: str) -> EarningsData:
    """Fetch annual and quarterly earnings for *ticker* from Alpha Vantage.

    Returns an EarningsData object with .annual and .quarterly lists,
    most-recent first.
    Raises RuntimeError if the ticker is not found or the call fails.
    """
    data = _call({
        "function": "EARNINGS",
        "symbol": ticker,
        "apikey": api_key,
    })

    if "annualEarnings" not in data:
        raise RuntimeError(f"No earnings data returned for {ticker}")

    def _f(val: str) -> Optional[float]:
        try:
            return float(val)
        except (TypeError, ValueError):
            return None

    annual = [
        AnnualEarning(
            fiscal_date=e["fiscalDateEnding"],
            reported_eps=_f(e.get("reportedEPS")),
        )
        for e in data.get("annualEarnings", [])
    ]

    quarterly = [
        QuarterlyEarning(
            fiscal_date=e["fiscalDateEnding"],
            reported_date=e.get("reportedDate", ""),
            reported_eps=_f(e.get("reportedEPS")),
            estimated_eps=_f(e.get("estimatedEPS")),
            surprise=_f(e.get("surprise")),
            surprise_pct=_f(e.get("surprisePercentage")),
        )
        for e in data.get("quarterlyEarnings", [])
    ]

    return EarningsData(ticker=ticker, annual=annual, quarterly=quarterly)
