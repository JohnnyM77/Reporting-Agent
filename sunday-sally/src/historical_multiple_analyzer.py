from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import yfinance as yf


@dataclass
class HistoricalValuationSummary:
    pe_3y_avg: float | None
    pe_5y_avg: float | None
    pe_10y_avg: float | None
    ev_ebitda_avg: float | None
    valuation_percentile: float | None
    notes: list[str]


def _approx_pe_series(exchange_ticker: str) -> pd.Series:
    tk = yf.Ticker(exchange_ticker)
    hist = tk.history(period="10y", interval="1mo", auto_adjust=False)
    if hist.empty or "Close" not in hist:
        return pd.Series(dtype=float)

    try:
        eps = tk.info.get("trailingEps")
    except Exception:
        eps = None

    if not isinstance(eps, (int, float)) or eps <= 0:
        return pd.Series(dtype=float)

    pe = (hist["Close"] / float(eps)).dropna()
    pe.index = pd.to_datetime(pe.index)
    return pe


def summarize_history(exchange_ticker: str, current_trailing_pe: float | None, current_ev_ebitda: float | None) -> HistoricalValuationSummary:
    notes: list[str] = []
    pe_series = _approx_pe_series(exchange_ticker)

    def _window_avg(years: int) -> float | None:
        if pe_series.empty:
            return None
        cutoff = pe_series.index.max() - pd.DateOffset(years=years)
        sub = pe_series[pe_series.index >= cutoff]
        return float(sub.mean()) if not sub.empty else None

    pe_3 = _window_avg(3)
    pe_5 = _window_avg(5)
    pe_10 = _window_avg(10)

    percentile = None
    if current_trailing_pe and not pe_series.empty:
        percentile = float((pe_series <= current_trailing_pe).mean())

    if pe_series.empty:
        notes.append("Historical PE unavailable or trailing EPS not positive.")
    if current_ev_ebitda is None:
        notes.append("EV/EBITDA context unavailable from source feed.")

    ev_avg = current_ev_ebitda
    return HistoricalValuationSummary(
        pe_3y_avg=pe_3,
        pe_5y_avg=pe_5,
        pe_10y_avg=pe_10,
        ev_ebitda_avg=ev_avg,
        valuation_percentile=percentile,
        notes=notes,
    )


def valuation_ratio(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline in (None, 0):
        return None
    return float(current / baseline)


def percentile_bucket(percentile: float | None) -> str:
    if percentile is None:
        return "unknown"
    if percentile >= 0.9:
        return "top_10_percent"
    if percentile >= 0.8:
        return "top_20_percent"
    if percentile >= 0.5:
        return "median_or_above"
    return "below_average"
