"""
investing/sws_reader.py
────────────────────────
Thin adapter layer for reading the JM Investing SQLite database.
Used by Wally (fundamentals_store.py) and Sally as an alternative to live API calls.

Typical usage:
    from investing.sws_reader import SWSReader
    reader = SWSReader()          # uses DB_PATH env var or default path
    eps = reader.get_eps_history("BHP")
    forecasts = reader.get_eps_forecasts("WTC")
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional

# Default path — override with SWS_DB_PATH env var or pass db_path= explicitly
_DEFAULT_DB = Path(os.environ.get("SWS_DB_PATH", "jm_investing_sws.sqlite"))


class SWSReader:
    """Read-only access to the JM Investing SQLite database."""

    def __init__(self, db_path: Optional[Path | str] = None):
        self._path = Path(db_path) if db_path else _DEFAULT_DB
        self._con: Optional[sqlite3.Connection] = None

    def _connect(self) -> sqlite3.Connection:
        if self._con is None:
            if not self._path.exists():
                raise FileNotFoundError(
                    f"SWS database not found at {self._path}. "
                    "Run import_sws_csv.py first, or set SWS_DB_PATH."
                )
            self._con = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
            self._con.row_factory = sqlite3.Row
        return self._con

    def close(self) -> None:
        if self._con:
            self._con.close()
            self._con = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ── Public API ────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Return True if the database exists and is readable."""
        return self._path.exists()

    def tickers(self) -> list[str]:
        """List all tickers in the database."""
        con = self._connect()
        return [r[0] for r in con.execute("SELECT ticker FROM companies ORDER BY ticker")]

    def get_scalar(self, ticker: str, metric_name: str) -> Optional[float]:
        """Return a single scalar metric value, or None if not found."""
        con = self._connect()
        row = con.execute(
            "SELECT value FROM sws_scalar_metrics WHERE ticker = ? AND metric_name = ?",
            (ticker, metric_name),
        ).fetchone()
        return row[0] if row else None

    def get_scalars(self, ticker: str) -> dict[str, float]:
        """Return all scalar metrics for a ticker as {metric_name: value}."""
        con = self._connect()
        rows = con.execute(
            "SELECT metric_name, value FROM sws_scalar_metrics WHERE ticker = ?",
            (ticker,),
        ).fetchall()
        return {r["metric_name"]: r["value"] for r in rows if r["value"] is not None}

    def get_timeseries(self, ticker: str, metric_name: str) -> list[dict]:
        """
        Return a timeseries for (ticker, metric_name).

        Each item: {"date_utc": "YYYY-MM-DD", "value": float, "date_unix": int}
        """
        con = self._connect()
        rows = con.execute(
            """SELECT date_utc, value, date_unix
               FROM sws_timeseries
               WHERE ticker = ? AND metric_name = ?
               ORDER BY date_unix""",
            (ticker, metric_name),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_eps_history(self, ticker: str) -> dict[str, float]:
        """
        Return historical annual EPS as {year: eps_dollars}.
        Source: past_earnings_per_share_ltm_history (offset-based, up to 10 years).
        """
        con = self._connect()
        rows = con.execute(
            "SELECT report_year, eps FROM v_eps_history WHERE ticker = ? ORDER BY report_year",
            (ticker,),
        ).fetchall()
        return {str(r["report_year"]): r["eps"] for r in rows if r["eps"] is not None}

    def get_revenue_history(self, ticker: str) -> dict[str, float]:
        """Return historical annual revenue as {year: value}."""
        con = self._connect()
        rows = con.execute(
            "SELECT report_year, revenue FROM v_revenue_history WHERE ticker = ? ORDER BY report_year",
            (ticker,),
        ).fetchall()
        return {str(r["report_year"]): r["revenue"] for r in rows if r["revenue"] is not None}

    def get_net_income_history(self, ticker: str) -> dict[str, float]:
        """Return historical annual net income as {year: value}."""
        con = self._connect()
        rows = con.execute(
            "SELECT report_year, net_income FROM v_net_income_history WHERE ticker = ? ORDER BY report_year",
            (ticker,),
        ).fetchall()
        return {str(r["report_year"]): r["net_income"] for r in rows if r["net_income"] is not None}

    def get_fundamentals_10y(self, ticker: str) -> list[dict]:
        """Return up to 10 years of revenue, net_income, eps as list of dicts."""
        con = self._connect()
        rows = con.execute(
            """SELECT report_year, revenue, net_income, eps
               FROM v_company_fundamentals_10y WHERE ticker = ? ORDER BY report_year""",
            (ticker,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_snapshot(self, ticker: str) -> dict:
        """Return the latest company snapshot (revenue, net_income, eps, dividend, roe, d/e)."""
        con = self._connect()
        row = con.execute(
            "SELECT * FROM v_latest_company_snapshot WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        return dict(row) if row else {}

    def get_eps_forecasts(self, ticker: str) -> dict[str, dict[str, Optional[float]]]:
        """
        Return analyst EPS forecasts per date: {date_utc: {consensus, high, low}}.
        """
        con = self._connect()
        rows = con.execute(
            """SELECT metric_name, date_utc, value
               FROM sws_timeseries
               WHERE ticker = ?
                 AND metric_name IN (
                     'future_merged_future_earnings_per_share',
                     'future_merged_future_earnings_per_share_high',
                     'future_merged_future_earnings_per_share_low'
                 )
               ORDER BY date_unix""",
            (ticker,),
        ).fetchall()
        result: dict[str, dict] = {}
        for r in rows:
            dt = r["date_utc"]
            if dt not in result:
                result[dt] = {"consensus": None, "high": None, "low": None}
            key = r["metric_name"]
            if key == "future_merged_future_earnings_per_share":
                result[dt]["consensus"] = r["value"]
            elif key == "future_merged_future_earnings_per_share_high":
                result[dt]["high"] = r["value"]
            elif key == "future_merged_future_earnings_per_share_low":
                result[dt]["low"] = r["value"]
        return result

    def get_dividend_history(self, ticker: str) -> dict[str, float]:
        """
        Return annual dividend totals aggregated by calendar year: {year: total_div}.
        """
        rows = self.get_timeseries(ticker, "dividend_historical_dividend_payments")
        by_year: dict[str, float] = {}
        for r in rows:
            if r["value"] is None:
                continue
            yr = r["date_utc"][:4]
            by_year[yr] = by_year.get(yr, 0.0) + r["value"]
        return {yr: round(v, 4) for yr, v in by_year.items()}

    def get_eps_cents_for_fundamentals(self, ticker: str) -> tuple[dict, dict]:
        """
        Returns (annual_eps_cents, annual_div_cents) compatible with
        fundamentals_store.get_fundamentals() return format.

        EPS from SWS is in dollars; this returns cents (×100).
        """
        eps_dollars = self.get_eps_history(ticker)
        annual_eps_cents = {yr: round(v * 100, 2) for yr, v in eps_dollars.items() if v is not None}

        div_dollars = self.get_dividend_history(ticker)
        annual_div_cents = {yr: round(v * 100, 2) for yr, v in div_dollars.items()}

        return annual_eps_cents, annual_div_cents
