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


def build_workbook_earnings_history(
    earnings_data: EarningsData,
    div_series: Optional[pd.Series] = None,
    eps_scale: float = 100.0,
) -> list[dict]:
    """Convert Alpha Vantage earnings + dividend data into workbook earnings history format.

    Args:
        earnings_data: EarningsData from fetch_earnings().
        div_series:    yfinance dividends Series (dollar/share per payment, DatetimeIndex).
                       Pass None if no dividend data is available.
        eps_scale:     Multiply reported EPS (in native currency dollars) by this factor
                       to get the "cents" unit used by the workbook (default 100 for USD/AUD).

    Returns:
        List of dicts {date, period, ttm_eps, ttm_div, notes}, sorted oldest-first.
        Only includes entries where a full 4-quarter TTM window is available.
    """
    quarters = [q for q in earnings_data.quarterly if q.reported_eps is not None]
    if len(quarters) < 4:
        return []

    # Normalise dividend series index to tz-naive timestamps for comparison.
    div_ts: Optional[pd.Series] = None
    if div_series is not None and not div_series.empty:
        idx = div_series.index
        if hasattr(idx, "tz") and idx.tz is not None:
            idx = idx.tz_localize(None)
        div_ts = pd.Series(div_series.values, index=pd.DatetimeIndex(idx))

    result = []
    for i in range(len(quarters) - 3):
        window = quarters[i : i + 4]
        eps_vals = [q.reported_eps for q in window if q.reported_eps is not None]
        if len(eps_vals) < 4:
            continue
        ttm_eps_dollars = sum(eps_vals)

        q = quarters[i]  # most-recent quarter in this window (list is most-recent-first, so index i is newest)

        # Use reported_date when valid, otherwise fall back to fiscal_date.
        date_str = (
            q.reported_date
            if (q.reported_date and q.reported_date.strip())
            else q.fiscal_date
        )
        report_ts = pd.Timestamp(date_str)

        # TTM dividends: sum all payments in the 12 months ending at report_ts.
        ttm_div_dollars = 0.0
        if div_ts is not None:
            start = report_ts - pd.DateOffset(years=1)
            mask = (div_ts.index >= start) & (div_ts.index <= report_ts)
            ttm_div_dollars = float(div_ts[mask].sum())

        # Period label from fiscal quarter.
        fiscal_dt = pd.Timestamp(q.fiscal_date)
        quarter_num = (fiscal_dt.month - 1) // 3 + 1
        period = f"Q{quarter_num} FY{fiscal_dt.year}"

        result.append(
            {
                "date": date_str,
                "period": period,
                "ttm_eps": round(ttm_eps_dollars * eps_scale, 1),
                "ttm_div": round(ttm_div_dollars * eps_scale, 1),
                "notes": "alphavantage",
            }
        )

    result.sort(key=lambda e: e["date"])
    return result


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
