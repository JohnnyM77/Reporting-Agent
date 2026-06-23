"""
wally/fundamentals_store.py

Persistent EPS + dividend cache for Wally.
Store: fundamentals/{ticker_slug}.json  e.g. fundamentals/rmd_ax.json

Waterfall per market:
  ASX (.AX)  → yfinance only (FMP free tier does not cover ASX)
  US / other → Alpha Vantage annual EPS → FMP (fill gaps + dividends)

Alpha Vantage also supplies:
  - quarterly EPS  (quarterlyEarnings from EARNINGS endpoint)
  - annual dividend (DividendPerShare from OVERVIEW endpoint, US only)

YAML overrides in valuations/ always win over any API data.
"""

import datetime
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

import requests
import yaml


def _slug(ticker: str) -> str:
    return ticker.lower().replace(".", "_")


def _store_path(ticker: str) -> Path:
    return Path("fundamentals") / f"{_slug(ticker)}.json"


def load(ticker: str) -> dict:
    """Load JSON store for ticker. Returns {} if not found or corrupt."""
    path = _store_path(ticker)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[fundamentals] Load error for {ticker}: {e}")
        return {}


def save(ticker: str, data: dict) -> None:
    """Write JSON store for ticker."""
    path = _store_path(ticker)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    print(f"[fundamentals] Saved cache for {ticker}")


def is_stale(data: dict) -> bool:
    """
    Returns True if cache needs refreshing:
    - Empty or missing annual_eps_cents
    - last_updated > 90 days ago
    - Most recent year in annual_eps_cents is > 14 months behind today
    """
    if not data or not data.get("annual_eps_cents"):
        return True
    try:
        last = datetime.date.fromisoformat(data["last_updated"])
        if (datetime.date.today() - last).days > 90:
            return True
    except Exception:
        return True
    eps = data.get("annual_eps_cents", {})
    if eps:
        latest_year = max(int(y) for y in eps.keys())
        current_year = datetime.date.today().year
        months_lag = (current_year - latest_year) * 12
        if months_lag > 14:
            return True
    return False


def _fetch_sws(ticker: str) -> tuple[dict, dict]:
    """
    Fetch EPS + dividends from the local SWS SQLite database.
    DB path: SWS_DB_PATH env var, or default jm_investing_sws.sqlite.
    Ticker is looked up as bare symbol (BHP, not BHP.AX).
    Returns ({}, {}) if DB not available or ticker not found.
    """
    db_path = os.environ.get("SWS_DB_PATH", "jm_investing_sws.sqlite")
    try:
        import sys
        repo_root = str(Path(__file__).resolve().parents[1])
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)
        from investing.sws_reader import SWSReader
        reader = SWSReader(db_path)
        if not reader.is_available():
            return {}, {}
        bare = ticker.upper().replace(".AX", "").replace(".ax", "")
        if bare not in reader.tickers():
            print(f"[fundamentals] SWS: {bare} not in database", flush=True)
            return {}, {}
        eps_cents, div_cents = reader.get_eps_cents_for_fundamentals(bare)
        print(f"[fundamentals] SWS: {bare} → {len(eps_cents)} EPS years, {len(div_cents)} div years", flush=True)
        return eps_cents, div_cents
    except Exception as e:
        print(f"[fundamentals] SWS fetch failed for {ticker}: {e}", flush=True)
        return {}, {}


def _fetch_yfinance(ticker: str) -> tuple[dict, dict]:
    """
    Fetch annual EPS and dividends via yfinance (works for ASX and US).
    Returns (eps_by_year, div_by_year) as {str_year: float_cents}.
    Returns ({}, {}) on any failure.
    """
    try:
        import yfinance as yf
        import pandas as pd

        tk = yf.Ticker(ticker)

        eps: dict = {}
        div: dict = {}

        # Annual EPS from income statement
        try:
            stmt = tk.income_stmt
            if stmt is not None and not stmt.empty:
                for label in ("Diluted EPS", "Basic EPS", "EPS Diluted", "EPS Basic"):
                    if label in stmt.index:
                        row = stmt.loc[label]
                        for col in row.index:
                            try:
                                yr = str(pd.Timestamp(col).year)
                                val = row[col]
                                if val is not None and not pd.isna(val):
                                    eps[yr] = round(float(val) * 100, 2)
                            except Exception:
                                continue
                        if eps:
                            break
        except Exception as e:
            print(f"[fundamentals] yfinance income_stmt failed for {ticker}: {e}")

        # Fallback: trailing EPS from info
        if not eps:
            try:
                info = tk.info or {}
                trailing = info.get("trailingEps")
                if trailing is not None:
                    yr = str(datetime.date.today().year - 1)
                    eps[yr] = round(float(trailing) * 100, 2)
                    print(f"[fundamentals] yfinance using trailingEps fallback for {ticker}")
            except Exception as e:
                print(f"[fundamentals] yfinance info fallback failed for {ticker}: {e}")

        # Annual dividends — aggregate by calendar year
        try:
            divs = tk.dividends
            if divs is not None and not divs.empty:
                yearly: dict = defaultdict(float)
                for ts, amount in divs.items():
                    try:
                        import pandas as pd
                        yr = str(pd.Timestamp(ts).year)
                        yearly[yr] += float(amount) * 100
                    except Exception:
                        continue
                div = {yr: round(total, 2) for yr, total in yearly.items()}
        except Exception as e:
            print(f"[fundamentals] yfinance dividends failed for {ticker}: {e}")

        print(f"[fundamentals] yfinance returned {len(eps)} annual EPS years, {len(div)} div years for {ticker}")
        return eps, div

    except Exception as e:
        print(f"[fundamentals] yfinance fetch failed for {ticker}: {e}")
        return {}, {}


def _fetch_alphavantage(ticker: str) -> tuple[dict, dict]:
    """
    Fetch annual EPS from Alpha Vantage EARNINGS endpoint.
    Returns (eps_by_year, {}) — AV free tier excludes dividends from EARNINGS.
    Sleeps 12s after call to respect 5 req/min free tier limit.
    Returns ({}, {}) on any failure or for ASX tickers.
    """
    if ".AX" in ticker.upper():
        return {}, {}
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
    if not api_key:
        print(f"[fundamentals] ALPHAVANTAGE_API_KEY not set, skipping AV for {ticker}")
        return {}, {}
    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=EARNINGS&symbol={ticker}&apikey={api_key}"
        )
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        payload = r.json()

        if "Note" in payload or "Information" in payload:
            print(f"[fundamentals] AV rate limit or error for {ticker}: "
                  f"{payload.get('Note') or payload.get('Information')}")
            time.sleep(12)
            return {}, {}

        eps = {}
        for entry in payload.get("annualEarnings", []):
            try:
                year = str(entry.get("fiscalDateEnding", ""))[:4]
                val = entry.get("reportedEPS")
                if val and val not in ("None", "null", "") and year:
                    eps[year] = round(float(val) * 100, 2)
            except Exception:
                continue

        print(f"[fundamentals] AV returned {len(eps)} annual EPS years for {ticker}")
        time.sleep(12)
        return eps, {}
    except Exception as e:
        print(f"[fundamentals] AV fetch failed for {ticker}: {e}")
        return {}, {}


def _fetch_alphavantage_quarterly(ticker: str) -> dict:
    """
    Fetch quarterly EPS from Alpha Vantage EARNINGS endpoint.
    Returns {str_quarter: float_cents} e.g. {"2024-Q1": 45.2, ...}.
    Returns {} on any failure or for ASX tickers.
    Note: reuses the same API call as _fetch_alphavantage — call together to avoid double rate-limit sleep.
    """
    if ".AX" in ticker.upper():
        return {}
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
    if not api_key:
        return {}
    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=EARNINGS&symbol={ticker}&apikey={api_key}"
        )
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        payload = r.json()

        if "Note" in payload or "Information" in payload:
            time.sleep(12)
            return {}

        quarterly = {}
        for entry in payload.get("quarterlyEarnings", []):
            try:
                date_str = str(entry.get("fiscalDateEnding", ""))
                val = entry.get("reportedEPS")
                if not date_str or not val or val in ("None", "null", ""):
                    continue
                import datetime as _dt
                d = _dt.date.fromisoformat(date_str)
                quarter = f"{d.year}-Q{(d.month - 1) // 3 + 1}"
                quarterly[quarter] = round(float(val) * 100, 2)
            except Exception:
                continue

        print(f"[fundamentals] AV returned {len(quarterly)} quarterly EPS entries for {ticker}")
        time.sleep(12)
        return quarterly
    except Exception as e:
        print(f"[fundamentals] AV quarterly fetch failed for {ticker}: {e}")
        return {}


def _fetch_alphavantage_dividends(ticker: str) -> dict:
    """
    Fetch trailing annual dividend from AV OVERVIEW endpoint (DividendPerShare).
    Returns {str_year: float_cents} with the current year's trailing figure.
    Only used for US tickers when no dividend data exists yet.
    Returns {} on any failure or for ASX tickers.
    """
    if ".AX" in ticker.upper():
        return {}
    api_key = os.environ.get("ALPHAVANTAGE_API_KEY", "").strip()
    if not api_key:
        return {}
    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=OVERVIEW&symbol={ticker}&apikey={api_key}"
        )
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        payload = r.json()

        if "Note" in payload or "Information" in payload:
            time.sleep(12)
            return {}

        val = payload.get("DividendPerShare")
        if val and val not in ("None", "null", "", "0", "0.0"):
            yr = str(datetime.date.today().year)
            cents = round(float(val) * 100, 2)
            print(f"[fundamentals] AV OVERVIEW DividendPerShare={val} for {ticker}")
            time.sleep(12)
            return {yr: cents}

        time.sleep(12)
        return {}
    except Exception as e:
        print(f"[fundamentals] AV dividend fetch failed for {ticker}: {e}")
        return {}


def _fetch_fmp(ticker: str) -> tuple[dict, dict]:
    """
    Fetch annual EPS from FMP income-statement endpoint.
    Fetch annual dividends from FMP historical dividend endpoint.
    Returns (eps_by_year, div_by_year) as {str_year: float_cents}.
    Returns ({}, {}) on any failure or for ASX tickers (free tier doesn't cover ASX).
    """
    if ".AX" in ticker.upper():
        return {}, {}

    api_key = os.environ.get("FMP_API_KEY", "").strip()
    if not api_key:
        print(f"[fundamentals] FMP_API_KEY not set, skipping FMP for {ticker}")
        return {}, {}

    eps = {}
    div = {}

    try:
        url = (
            f"https://financialmodelingprep.com/api/v3/income-statement/"
            f"{ticker}?limit=15&apikey={api_key}"
        )
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            for entry in data:
                try:
                    year = str(entry.get("date", ""))[:4]
                    val = entry.get("epsdiluted")
                    if val is not None and year:
                        eps[year] = round(float(val) * 100, 2)
                except Exception:
                    continue
        print(f"[fundamentals] FMP returned {len(eps)} annual EPS years for {ticker}")

        time.sleep(0.5)

        url2 = (
            f"https://financialmodelingprep.com/api/v3/historical-price-full/"
            f"stock_dividend/{ticker}?apikey={api_key}"
        )
        r2 = requests.get(url2, timeout=20)
        r2.raise_for_status()
        data2 = r2.json()
        yearly_div: dict = defaultdict(float)
        for entry in data2.get("historical", []):
            try:
                year = str(entry.get("date", ""))[:4]
                yearly_div[year] += float(entry.get("dividend", 0)) * 100
            except Exception:
                continue
        div = {yr: round(total, 2) for yr, total in yearly_div.items()}
        print(f"[fundamentals] FMP returned {len(div)} dividend years for {ticker}")

    except Exception as e:
        print(f"[fundamentals] FMP fetch failed for {ticker}: {e}")

    return eps, div


def _apply_yaml_overrides(ticker: str, eps: dict, div: dict) -> tuple[dict, dict]:
    """
    Load valuations/{slug}.yaml and apply overrides.
    YAML values always win. Returns updated (eps, div).
    """
    yaml_path = Path("valuations") / f"{_slug(ticker)}.yaml"
    if not yaml_path.exists():
        return eps, div
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        series = data.get("series", {}) or {}
        yaml_eps = series.get("eps", {}) or {}
        yaml_div = series.get("dividend", {}) or {}
        override_count = 0
        for yr, val in yaml_eps.items():
            eps[str(yr)] = float(val)
            override_count += 1
        for yr, val in yaml_div.items():
            div[str(yr)] = float(val)
        if override_count:
            print(f"[fundamentals] Applied {override_count} YAML overrides for {ticker}")
    except Exception as e:
        print(f"[fundamentals] YAML override error for {ticker}: {e}")
    return eps, div


def get_fundamentals(ticker: str) -> dict:
    """
    Main entry point called by value_chart_builder.py and spreadsheet.py.

    Returns dict:
      {
        "annual_eps_cents":   {"2016": 32.1, "2017": 38.4, ...},
        "annual_div_cents":   {"2016": 5.8, ...},
        "quarterly_eps_cents": {"2024-Q1": 45.2, ...}   # US tickers only
      }

    Waterfall:
      ASX (.AX) → yfinance (annual EPS + dividends)
      US / other → Alpha Vantage annual EPS + quarterly EPS → FMP (fill gaps + divs)
                   → AV OVERVIEW for dividends if still empty

    YAML series overrides always win.
    """
    stored = load(ticker)

    if not is_stale(stored):
        eps = dict(stored.get("annual_eps_cents", {}))
        div = dict(stored.get("annual_div_cents", {}))
        quarterly = dict(stored.get("quarterly_eps_cents", {}))
        eps, div = _apply_yaml_overrides(ticker, eps, div)
        return {"annual_eps_cents": eps, "annual_div_cents": div, "quarterly_eps_cents": quarterly}

    print(f"[fundamentals] Cache stale for {ticker} — fetching from APIs...")

    eps = dict(stored.get("annual_eps_cents", {}))
    div = dict(stored.get("annual_div_cents", {}))
    quarterly = dict(stored.get("quarterly_eps_cents", {}))
    sources = dict(stored.get("sources", {}))

    is_asx = ".AX" in ticker.upper()

    if is_asx:
        # ASX: SWS database first (richer data), fall back to yfinance
        sws_eps, sws_div = _fetch_sws(ticker)
        if sws_eps:
            for yr, val in sws_eps.items():
                eps[yr] = val
                sources[yr] = "sws"
            for yr, val in sws_div.items():
                div[yr] = val
        else:
            # SWS not available or ticker not in DB — fall back to yfinance
            yf_eps, yf_div = _fetch_yfinance(ticker)
            for yr, val in yf_eps.items():
                if yr not in eps:
                    eps[yr] = val
                    sources[yr] = "yfinance"
            for yr, val in yf_div.items():
                if yr not in div:
                    div[yr] = val
    else:
        # US / other: AV annual first (also grabs quarterly in same call)
        av_eps, _ = _fetch_alphavantage(ticker)
        if av_eps:
            for yr, val in av_eps.items():
                if yr not in eps:
                    eps[yr] = val
                    sources[yr] = "alphavantage"

        # AV quarterly EPS (separate call — rate limit handled inside)
        av_quarterly = _fetch_alphavantage_quarterly(ticker)
        for q, val in av_quarterly.items():
            if q not in quarterly:
                quarterly[q] = val

        # FMP to fill annual EPS gaps and fetch dividends
        if not av_eps or not div:
            fmp_eps, fmp_div = _fetch_fmp(ticker)
            if fmp_eps:
                for yr, val in fmp_eps.items():
                    if yr not in eps:
                        eps[yr] = val
                        sources[yr] = "fmp"
            if fmp_div:
                for yr, val in fmp_div.items():
                    if yr not in div:
                        div[yr] = val

        # AV OVERVIEW for dividends if still empty
        if not div:
            av_div = _fetch_alphavantage_dividends(ticker)
            for yr, val in av_div.items():
                if yr not in div:
                    div[yr] = val

    # YAML overrides always win
    eps, div = _apply_yaml_overrides(ticker, eps, div)

    new_data = {
        "ticker": ticker,
        "last_updated": str(datetime.date.today()),
        "annual_eps_cents": eps,
        "annual_div_cents": div,
        "quarterly_eps_cents": quarterly,
        "sources": sources,
    }
    save(ticker, new_data)

    return {"annual_eps_cents": eps, "annual_div_cents": div, "quarterly_eps_cents": quarterly}
