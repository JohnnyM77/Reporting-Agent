"""
us_fetch.py -- SEC EDGAR filing fetcher for the US Bob agent.

Analog of sgx_fetch.py, but simpler in shape because EDGAR is a free
public JSON/HTML API with none of SGX's TLS-fingerprint / signed-token
plumbing. Runs happily on GitHub-hosted ubuntu-latest.

The one hard rule EDGAR enforces: every request MUST send a
non-empty User-Agent identifying the caller. Missing/blank UAs get an
immediate 403 with an HTML page ("Undeclared Automated Tools ..."), and
too-fast callers get rate-limited to ~10 req/sec. We honour both -- a
0.15s throttle between calls is well inside the limit.

Public API
----------
    fetch_us_filings(
        tickers: List[str],
        lookback_days: int = 400,
        log=print,
    ) -> Dict[str, List[Dict]]

Returns a mapping ticker -> list of filing dicts. Each dict carries the
ASX/SGX-compatible core keys (exchange, ticker, date, time, title, url)
plus US-specific extras (form, accession, cik, items, primary_document,
primary_doc_description). "exchange" defaults to "US" because EDGAR does
NOT reliably ship the listing venue on submissions.json -- we would need
to walk a separate exchanges dataset to disambiguate NASDAQ vs NYSE, and
the value adds nothing to downstream analysis.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests
import yaml


# EDGAR contact header -- their fair-use policy requires it. Override via
# env var when running from another operator; the constant here is what
# CI uses by default.
SEC_USER_AGENT = "Bob the Bot johnmyerscough13@gmail.com"

# Endpoints. `www.sec.gov` hosts the docs; `data.sec.gov` hosts the
# submissions JSON. Both apply the same UA + rate-limit rules.
#
# We use company_tickers_exchange.json (not the more common
# company_tickers.json) because it is materially more accurate for
# currently-listed tickers: it carries the exchange (Nasdaq/NYSE) AND
# it drops delisted duplicates that would otherwise clobber a real
# filer in the ticker->CIK map. company_tickers.json for example has
# SE mapped to CIK 1703399 (a defunct filer) which then 404s on
# submissions, instead of Sea Limited's 1737443 -- that is what took
# down Bob USA run #1. The exchange file has SE mapped to the right
# CIK. `_fetch_all_ticker_maps` keeps the older file as a fallback
# for anything the exchange file misses.
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik10}.json"

# EDGAR asks for ~10 req/sec max; 0.15s = ~6.6 req/sec, comfortably inside.
RATE_LIMIT_SLEEP_SECS = 0.15
HTTP_TIMEOUT_SECS = 30

# NYSE opens at 09:30 America/New_York; the submissions endpoint's dates
# come back as YYYY-MM-DD without a time-of-day, so we use Eastern time
# for the "today" reference too.
NY_TZ = dt.timezone(dt.timedelta(hours=-5))  # EST; DST is close enough for date filtering


def _sec_ua() -> str:
    """Effective UA -- env var beats constant. `.strip()` guards against
    an accidentally-empty env value (Github Actions substitutes missing
    secrets to '') which would otherwise trigger EDGAR's 403 page."""
    return (os.environ.get("SEC_USER_AGENT") or "").strip() or SEC_USER_AGENT


def _session() -> requests.Session:
    """SEC-friendly requests session. UA is set once here so we don't
    forget it on any individual call. Do NOT set `Host` here -- requests
    picks it per URL from the actual host, and forcing it to www.sec.gov
    on session defaults would break every data.sec.gov call
    (submissions.json 404s the moment its host header is wrong)."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": _sec_ua(),
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    return s


def _throttle() -> None:
    """Sleep between SEC calls to stay inside their fair-use rate."""
    time.sleep(RATE_LIMIT_SLEEP_SECS)


# --- ticker <-> CIK ---------------------------------------------------------

def _company_tickers_cache_path() -> Path:
    """Where we cache company_tickers.json on disk. One file per run keeps
    us from hammering the endpoint on every ticker."""
    return Path("outputs/us/company_tickers.json")


def _fetch_ticker_candidates(
    session: requests.Session,
    log: Callable[[str], None],
) -> Dict[str, List[str]]:
    """Return an uppercase-ticker -> list of zero-padded 10-digit CIKs.

    A list, not a single value, because SEC's ticker files reuse
    symbols across defunct + current filers (e.g. `SE` maps to two
    CIKs in company_tickers.json; only one is Sea Limited). The
    caller tries each candidate until submissions.json responds so
    a stale duplicate can no longer knock us out.

    Priority: company_tickers_exchange.json entries first (higher
    quality -- exchange-aware, drops most defunct filers), then any
    company_tickers.json entries the exchange file didn't cover.
    Within a source, higher CIKs come first (newer filer wins on
    ties, since SEC assigns CIKs monotonically)."""
    cache = _company_tickers_cache_path()
    if cache.exists():
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
            # Old cache shape was ticker -> single CIK string; the new
            # shape is ticker -> list. Migrate on the fly rather than
            # break a cached run.
            if raw and isinstance(next(iter(raw.values()), None), list):
                log(f"[us-fetch] loaded {len(raw)} ticker candidates from cache")
                return raw
        except Exception as exc:
            log(f"[us-fetch] cache read failed ({exc}); refetching")

    # Ordered dict of ticker -> list of CIKs, preserving insertion order
    # so exchange-file candidates come first.
    out: Dict[str, List[str]] = {}

    def _add(ticker: str, cik_int: int) -> None:
        cik10 = f"{cik_int:010d}"
        lst = out.setdefault(ticker, [])
        if cik10 not in lst:
            lst.append(cik10)

    # 1. company_tickers_exchange.json -- structured as
    # {"fields": ["cik", "name", "ticker", "exchange"], "data": [[...], ...]}.
    log("[us-fetch] downloading company_tickers_exchange.json from SEC")
    try:
        r = session.get(SEC_TICKERS_EXCHANGE_URL, timeout=HTTP_TIMEOUT_SECS)
        _throttle()
        if r.status_code == 200:
            payload = r.json()
            fields = [str(f) for f in (payload.get("fields") or [])]
            data = payload.get("data") or []
            try:
                cik_i = fields.index("cik")
                tic_i = fields.index("ticker")
            except ValueError:
                cik_i = tic_i = -1
            if cik_i >= 0 and tic_i >= 0:
                for row in data:
                    if not isinstance(row, list) or len(row) <= max(cik_i, tic_i):
                        continue
                    ticker = str(row[tic_i] or "").strip().upper()
                    cik = row[cik_i]
                    if ticker and cik is not None:
                        try:
                            _add(ticker, int(cik))
                        except (TypeError, ValueError):
                            pass
                log(f"[us-fetch] exchange file contributed {len(out)} tickers")
        else:
            log(f"[us-fetch] exchange file HTTP {r.status_code} -- "
                f"falling back to company_tickers.json only")
    except Exception as exc:
        log(f"[us-fetch] exchange file fetch failed ({exc}) -- "
            f"falling back to company_tickers.json only")

    # 2. company_tickers.json -- older/simpler map, less accurate but a
    # superset of the exchange file for some obscure filers. We append
    # rather than overwrite so exchange candidates stay first.
    log("[us-fetch] downloading company_tickers.json from SEC")
    r = session.get(SEC_TICKERS_URL, timeout=HTTP_TIMEOUT_SECS)
    _throttle()
    if r.status_code != 200:
        # If BOTH files failed and we have nothing, error out. If we
        # got the exchange file only, that's already a solid map.
        if not out:
            raise RuntimeError(
                f"SEC company_tickers.json returned HTTP {r.status_code}: "
                f"{r.text[:200]!r}"
            )
    else:
        rows = r.json()
        # {"0": {"cik_str": 789019, "ticker": "MSFT", "title": "..."}, ...}
        for row in rows.values():
            ticker = str(row.get("ticker", "")).strip().upper()
            cik = row.get("cik_str")
            if not ticker or cik is None:
                continue
            try:
                _add(ticker, int(cik))
            except (TypeError, ValueError):
                pass
    log(f"[us-fetch] resolved {len(out)} tickers "
        f"(candidates per ticker: {sum(len(v) for v in out.values()) / max(len(out), 1):.2f})")

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def resolve_ticker_to_cik(
    ticker: str,
    session: Optional[requests.Session] = None,
    log: Callable[[str], None] = print,
) -> Optional[str]:
    """Public helper -- return zero-padded 10-digit CIK for `ticker`, or
    None if the SEC has no ticker of that name. Case-insensitive.

    Only returns the FIRST candidate; use `_fetch_ticker_candidates`
    directly if you need to try alternates on 404."""
    session = session or _session()
    table = _fetch_ticker_candidates(session, log)
    candidates = table.get(ticker.strip().upper()) or []
    return candidates[0] if candidates else None


# --- submissions -----------------------------------------------------------

def _fetch_submissions(
    cik10: str,
    session: requests.Session,
    log: Callable[[str], None],
) -> Optional[Dict]:
    """Return the parsed submissions JSON for one CIK. None on any error --
    caller handles as 'no filings available for this ticker'."""
    url = SEC_SUBMISSIONS_URL_TEMPLATE.format(cik10=cik10)
    try:
        r = session.get(url, timeout=HTTP_TIMEOUT_SECS)
        _throttle()
    except Exception as exc:
        log(f"[us-fetch] submissions GET failed {url}: "
            f"{exc.__class__.__name__}: {exc}")
        return None
    if r.status_code != 200:
        log(f"[us-fetch] submissions HTTP {r.status_code} for CIK {cik10}: "
            f"{r.text[:200]!r}")
        return None
    try:
        return r.json()
    except Exception as exc:
        log(f"[us-fetch] submissions JSON decode failed for CIK {cik10}: {exc}")
        return None


def _accession_nodash(accession: str) -> str:
    """Strip the dashes from an accession number so it can be used as a
    directory in the Archives URL path.

    "0000789019-25-000017" -> "000078901925000017".
    """
    return (accession or "").replace("-", "")


def _archive_dir_url(cik_int: int, accession_nodash: str) -> str:
    """Directory URL for one accession. Documents live directly under it."""
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/"
    )


def _archive_doc_url(cik_int: int, accession_nodash: str, document: str) -> str:
    return _archive_dir_url(cik_int, accession_nodash) + document


def _normalize_row(
    ticker: str,
    cik10: str,
    idx: int,
    recent: Dict,
    issuer_name: str,
) -> Dict:
    """Turn parallel-array row `idx` from filings.recent into the standard
    announcement dict. Missing fields degrade to empty strings rather
    than KeyErrors -- EDGAR shape is loose enough that not every filing
    fills every column."""
    def _at(key: str, default=""):
        arr = recent.get(key) or []
        if idx < len(arr) and arr[idx] is not None:
            return arr[idx]
        return default

    form = str(_at("form", "")).strip()
    accession = str(_at("accessionNumber", "")).strip()
    filing_date = str(_at("filingDate", "")).strip()
    accepted = str(_at("acceptanceDateTime", "")).strip()
    primary_document = str(_at("primaryDocument", "")).strip()
    primary_desc = str(_at("primaryDocDescription", "")).strip()
    report_date = str(_at("reportDate", "")).strip()
    items = str(_at("items", "")).strip()  # comma-separated list of 8-K items

    cik_int = int(cik10) if cik10 else 0
    accession_nd = _accession_nodash(accession)
    url = _archive_doc_url(cik_int, accession_nd, primary_document) if primary_document else ""

    # Time-of-day comes off acceptanceDateTime (e.g. "2025-07-30T16:05:12.000Z");
    # some old rows only carry a date -- treat those as time-less.
    time_str = ""
    if accepted:
        m = re.match(r"\d{4}-\d{2}-\d{2}T(\d{2}:\d{2})", accepted)
        if m:
            time_str = m.group(1)

    # Title: EDGAR does not ship a headline like ASX/SGX do. Build one
    # from the form and description so downstream classifiers still get
    # something readable in `title`.
    title_parts = [form]
    if primary_desc and primary_desc != form:
        title_parts.append(primary_desc)
    if items:
        title_parts.append(f"Items: {items}")
    title = " -- ".join(title_parts)

    return {
        "exchange": "US",  # NYSE vs NASDAQ not consistently in submissions.json
        "ticker": ticker,
        "date": filing_date,
        "time": time_str,
        "title": title,
        "url": url,
        # US-specific -- keep raw so classifier can pattern-match forms/items.
        "form": form,
        "accession": accession,
        "accession_nodash": accession_nd,
        "cik": cik10,
        "cik_int": cik_int,
        "items": items,
        "primary_document": primary_document,
        "primary_doc_description": primary_desc,
        "issuer_name": issuer_name,
        "report_date": report_date,
        "filing_dir_url": _archive_dir_url(cik_int, accession_nd) if accession_nd else "",
    }


def _within_lookback(row: Dict, lookback_days: int) -> bool:
    """True when the filing's filingDate is within `lookback_days` of now.
    Undated rows are kept (same conservative choice sgx_fetch makes)."""
    if lookback_days <= 0:
        return True
    d = (row.get("date") or "").strip()
    if not d:
        return True
    try:
        filed = dt.date.fromisoformat(d[:10])
    except Exception:
        return True
    return filed >= dt.date.today() - dt.timedelta(days=lookback_days)


# --- public API ------------------------------------------------------------

def fetch_us_filings(
    tickers: List[str],
    lookback_days: int = 400,
    log: Callable[[str], None] = print,
) -> Dict[str, List[Dict]]:
    """Fetch recent SEC filings for each US `ticker`. Returns a mapping
    ticker -> list of filing dicts, newest first, filtered to the last
    `lookback_days`. Missing ticker in the returned dict means we
    couldn't resolve or fetch that ticker (fatal at the caller); an
    empty list means the API returned rows but none fell in the window."""
    tickers = [t.strip().upper() for t in tickers if t and t.strip()]
    if not tickers:
        return {}
    session = _session()

    # Resolve every ticker in one go, then fetch submissions per CIK.
    table = _fetch_ticker_candidates(session, log)
    results: Dict[str, List[Dict]] = {}

    for ticker in tickers:
        candidates = table.get(ticker) or []
        if not candidates:
            log(f"[us-fetch] {ticker}: not found in SEC ticker files")
            continue

        # Try each candidate CIK in priority order until submissions.json
        # returns 200. Stale duplicates (e.g. an old `SE` filer whose CIK
        # 1703399 shadowed Sea Ltd's 1737443) 404 here, so we walk on.
        subs = None
        cik10 = None
        for candidate in candidates:
            log(f"[us-fetch] {ticker}: trying CIK {candidate}")
            got = _fetch_submissions(candidate, session, log)
            if got:
                subs = got
                cik10 = candidate
                break
        if not subs or not cik10:
            log(f"[us-fetch] {ticker}: none of {len(candidates)} candidate "
                f"CIK(s) returned submissions -- giving up on this ticker")
            continue

        issuer_name = str(subs.get("name") or "").strip()
        recent = ((subs.get("filings") or {}).get("recent")) or {}
        # parallel arrays -- length is defined by any populated column
        n = 0
        for key in ("form", "filingDate", "accessionNumber"):
            n = max(n, len(recent.get(key) or []))
        rows: List[Dict] = []
        for i in range(n):
            row = _normalize_row(ticker, cik10, i, recent, issuer_name)
            if _within_lookback(row, lookback_days):
                rows.append(row)
        # Newest first (submissions.json already comes back that way, but
        # some accessions can share a filingDate -- sort defensively).
        rows.sort(key=lambda r: (r.get("date") or "", r.get("accession") or ""),
                  reverse=True)
        log(f"[us-fetch] {ticker}: {len(rows)} filing(s) in last {lookback_days} days "
            f"(CIK {cik10}, issuer {issuer_name!r})")
        results[ticker] = rows

    return results


def select_last_results_set(
    rows: List[Dict],
    log: Callable[[str], None] = print,
) -> Tuple[List[Dict], Optional[str], Optional[str]]:
    """From `rows` (newest first, one ticker), pick the set of filings
    that make up the most recent reported period:

      1. The newest 10-K or 10-Q as the anchor.
      2. Plus the newest 8-K with item 2.02 (Results of Operations)
         that filed within ~45 days of that anchor -- usually the
         earnings press release for the same period.
      3. If no 10-K/10-Q exists in the window, fall back to the newest
         8-K item 2.02 alone.

    Returns (selected_rows, anchor_form, report_date). anchor_form is
    "10-K" / "10-Q" / "8-K" depending on which type led the pick, so the
    caller can seed the LLM's period_type."""
    if not rows:
        return [], None, None

    # Periodic annual/interim reports. 10-K/10-Q for domestic filers;
    # 20-F / 40-F for foreign private issuers (Sea Ltd, most Chinese ADRs,
    # Canadian FPIs). All four anchor the analysis; the /A amendments
    # come with a matching original but a fresh amendment is still the
    # canonical "last results" if it is newer.
    _periodic_forms = {"10-K", "10-K/A", "10-Q", "10-Q/A",
                       "20-F", "20-F/A", "40-F", "40-F/A"}

    def _is_periodic(r: Dict) -> bool:
        return (r.get("form") or "").strip() in _periodic_forms

    def _is_earnings_8k(r: Dict) -> bool:
        if r.get("form") != "8-K":
            return False
        items = (r.get("items") or "").split(",")
        return any(it.strip() == "2.02" for it in items)

    periodic = [r for r in rows if _is_periodic(r)]
    earn_8ks = [r for r in rows if _is_earnings_8k(r)]

    if periodic:
        anchor = periodic[0]
        anchor_form = anchor.get("form")
        report_date = anchor.get("report_date") or anchor.get("date") or ""
        selected: List[Dict] = [anchor]
        # Attach the newest earnings 8-K within 45 days of the anchor,
        # which is usually the press release / non-GAAP framing for the
        # same period. A quarterly 10-Q filed 30-40 days after quarter
        # end is the common case; the 8-K lands within a few weeks of
        # the same quarter.
        anchor_date_str = (anchor.get("date") or "")[:10]
        try:
            anchor_date = dt.date.fromisoformat(anchor_date_str)
        except Exception:
            anchor_date = None
        for r in earn_8ks:
            try:
                d = dt.date.fromisoformat((r.get("date") or "")[:10])
            except Exception:
                continue
            if anchor_date and abs((anchor_date - d).days) <= 45:
                selected.append(r)
                break  # only the nearest 8-K
        log(f"[us-fetch] anchor: {anchor_form} filed {anchor_date_str} "
            f"(+ {len(selected) - 1} companion 8-K)")
        return selected, anchor_form, report_date

    if earn_8ks:
        anchor = earn_8ks[0]
        anchor_form = "8-K"
        report_date = anchor.get("report_date") or anchor.get("date") or ""
        log(f"[us-fetch] anchor: 8-K item 2.02 filed {anchor.get('date')} "
            f"(no 10-K/10-Q in window)")
        return [anchor], anchor_form, report_date

    log("[us-fetch] no 10-K/10-Q/8-K item 2.02 in window -- nothing to analyse")
    return [], None, None


# --- CLI -------------------------------------------------------------------

def _load_us_tickers_from_yaml(path: Path) -> List[str]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    us = data.get("us") or {}
    if not isinstance(us, dict):
        return []
    return sorted(k for k in us.keys() if isinstance(k, str))


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Fetch SEC EDGAR filings.")
    parser.add_argument(
        "--tickers", default=None,
        help="Comma-separated US symbols. Defaults to tickers.yaml us: section.",
    )
    parser.add_argument(
        "--lookback-days", type=int, default=400,
        help="Filter to filings from the last N days (default 400).",
    )
    parser.add_argument(
        "--out", default="outputs/us/filings.json",
        help="Output JSON path.",
    )
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = _load_us_tickers_from_yaml(Path("tickers.yaml"))
    if not tickers:
        print("[us-fetch] no tickers to fetch (--tickers or tickers.yaml us: "
              "section required)", file=sys.stderr)
        return 1

    print(f"[us-fetch] fetching {len(tickers)} ticker(s): {', '.join(tickers)}")
    results = fetch_us_filings(tickers, lookback_days=args.lookback_days)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"tickers": tickers, "results": results},
                   indent=2, default=str),
        encoding="utf-8",
    )
    total = sum(len(v) for v in results.values())
    print(f"[us-fetch] wrote {out} ({total} filing(s) across "
          f"{sum(1 for v in results.values() if v)} ticker(s) with rows)")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
