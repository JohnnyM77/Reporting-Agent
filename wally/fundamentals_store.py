"""
wally/fundamentals_store.py

Persistent EPS + dividend cache for Wally.
Store: fundamentals/{ticker_slug}.json  e.g. fundamentals/rmd_ax.json
Waterfall: local JSON → Alpha Vantage → FMP → (blank, log warning)
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

_fmp_verified = False  # module-level flag for one-time connectivity check


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


def _fetch_alphavantage(ticker: str) -> tuple[dict, dict]:
    """
    Fetch annual EPS from Alpha Vantage EARNINGS endpoint.
    Returns (eps_by_year, div_by_year) as {str_year: float_cents}.
    AV free tier doesn't include dividends in EARNINGS endpoint so div={}.
    Sleeps 12s after call to respect 5 req/min free tier limit.
    Returns ({}, {}) on any failure.

    NOTE: AV EARNINGS endpoint does not support ASX tickers (.AX suffix)
    on the free tier — skip them entirely and rely on FMP instead.
    """
    if ".AX" in ticker.upper():
        print(f"[fundamentals] Skipping AV for ASX ticker {ticker} — using FMP instead")
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

        # Detect rate limit / error responses
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


def _fetch_fmp(ticker: str) -> tuple[dict, dict]:
    """
    Fetch annual EPS from FMP income-statement endpoint.
    Fetch annual dividends from FMP historical dividend endpoint.
    Returns (eps_by_year, div_by_year) as {str_year: float_cents}.
    Returns ({}, {}) on any failure.
    """
    api_key = os.environ.get("FMP_API_KEY", "").strip()
    if not api_key:
        print(f"[fundamentals] FMP_API_KEY not set, skipping FMP for {ticker}")
        return {}, {}

    eps = {}
    div = {}

    try:
        # EPS from income statement (diluted EPS, annual, up to 15 years)
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

        # Dividends — aggregate all payments within each calendar year
        url2 = (
            f"https://financialmodelingprep.com/api/v3/historical-price-full/"
            f"stock_dividend/{ticker}?apikey={api_key}"
        )
        r2 = requests.get(url2, timeout=20)
        r2.raise_for_status()
        data2 = r2.json()
        yearly_div = defaultdict(float)
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
        "annual_eps_cents": {"2016": 32.1, "2017": 38.4, ...},
        "annual_div_cents": {"2016": 5.8, ...}
      }

    Logic:
      1. One-time FMP connectivity check (first call only)
      2. Load local JSON cache
      3. If not stale -> apply YAML overrides -> return
      4. If stale -> try Alpha Vantage (US only) -> try FMP (always for .AX)
      5. Merge with existing cached data (preserve old years)
      6. Apply YAML overrides (always win)
      7. Save updated cache
      8. Return
    """
    global _fmp_verified
    if not _fmp_verified:
        _fmp_verified = True
        test_eps, _ = _fetch_fmp("RMD.AX")
        if test_eps:
            print(f"[fundamentals] FMP connectivity OK — RMD.AX returned {len(test_eps)} EPS years")
        else:
            print(f"[fundamentals] WARNING: FMP returned nothing for RMD.AX — check FMP_API_KEY secret")

    stored = load(ticker)

    if not is_stale(stored):
        eps = dict(stored.get("annual_eps_cents", {}))
        div = dict(stored.get("annual_div_cents", {}))
        eps, div = _apply_yaml_overrides(ticker, eps, div)
        return {"annual_eps_cents": eps, "annual_div_cents": div}

    print(f"[fundamentals] Cache stale for {ticker} — fetching from APIs...")

    # Start with whatever we already have cached (preserve history)
    eps = dict(stored.get("annual_eps_cents", {}))
    div = dict(stored.get("annual_div_cents", {}))
    sources = dict(stored.get("sources", {}))

    # Try Alpha Vantage first (EPS only, free tier)
    av_eps, _ = _fetch_alphavantage(ticker)
    if av_eps:
        for yr, val in av_eps.items():
            if yr not in eps:  # Only fill gaps, preserve existing values
                eps[yr] = val
                sources[yr] = "alphavantage"

    # Try FMP — always for ASX tickers (.AX), otherwise only to fill gaps
    is_asx = ".AX" in ticker.upper()
    needs_fmp = is_asx or (not av_eps) or (not div)
    if needs_fmp:
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

    # YAML overrides always win (adjusted/underlying EPS)
    eps, div = _apply_yaml_overrides(ticker, eps, div)

    # Save updated cache
    new_data = {
        "ticker": ticker,
        "last_updated": str(datetime.date.today()),
        "annual_eps_cents": eps,
        "annual_div_cents": div,
        "sources": sources,
    }
    save(ticker, new_data)

    return {"annual_eps_cents": eps, "annual_div_cents": div}
