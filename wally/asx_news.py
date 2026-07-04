"""Significant ASX announcement lookup for Wally-flagged tickers.

When Wally flags a watchlist ticker (trading near its 52-week low) this
module fetches the company's ASX announcements over a recent lookback
window (default 60 days, env WALLY_NEWS_LOOKBACK_DAYS) and keeps only the
significant ones:

  * anything ASX has marked price-sensitive, always; plus
  * titles matching high-signal categories (trading halts, capital
    raisings, M&A, guidance changes, results, dividends, board changes,
    contract/regulatory events, ASX queries),

while dropping routine notices (Appendix 3B/3Y, director's interest
notices, substantial-holder filings, AGM paperwork, …).

Retrieval delegates to the repo-root ``asx_fetch`` module — the same
proven 2-stage path (direct HTTP → Playwright) used by Bob.  The import
is lazy so Wally degrades gracefully when bs4/requests are unavailable.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, asdict
from typing import Optional

from .config import NEWS_LOOKBACK_DAYS, NEWS_MAX_ITEMS


def _log(msg: str) -> None:
    print(f"[wally/asx_news] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Significance classification
# ---------------------------------------------------------------------------

# Checked in order — first match wins and provides the category label.
_SIGNIFICANT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("Trading halt / suspension",
     re.compile(r"trading halt|suspension from (official )?quotation|voluntary suspension|reinstatement to (official )?quotation", re.I)),
    ("Capital raising",
     re.compile(r"capital rais|placement|entitlement offer|rights issue|share purchase plan|\bSPP\b|institutional offer|retail offer|convertible note", re.I)),
    ("M&A / takeover",
     re.compile(r"takeover|merger|acquisition|acquires?|scheme of arrangement|scheme booklet|divest|demerger|binding agreement|non[- ]binding (indicative )?(offer|proposal)|sale of business", re.I)),
    ("Guidance / trading update",
     re.compile(r"guidance|profit warning|earnings (update|upgrade|downgrade)|trading update|market update|business update|outlook|impairment|write[- ]?down", re.I)),
    ("Results",
     re.compile(r"full[- ]?year results|half[- ]?year results|[fh]y ?\d{2,4} results?|annual report|preliminary final report|results (presentation|announcement)|quarterly activities|quarterly cash ?flow|appendix 4[cde]", re.I)),
    ("Dividend / capital return",
     re.compile(r"dividend|distribution announcement|capital return|buy[- ]?back", re.I)),
    ("Board / executive change",
     re.compile(r"(ceo|cfo|chief executive|chief financial|managing director|chair(man|person)?) (change|transition|succession|appointment|resignation)|(appointment|resignation|retirement) of (ceo|cfo|chief executive|chief financial|managing director|chair(man|person)?|director)", re.I)),
    ("Contract / regulatory",
     re.compile(r"contract (award|win)|awarded contract|\bFDA\b|\bTGA\b|regulatory approval|clinical trial|class action|\bASIC\b|\bACCC\b|court (decision|ruling|approval)|licence grant", re.I)),
    ("ASX query",
     re.compile(r"asx (price )?query|price query response|aware (query|letter)", re.I)),
]

# Routine notices that stay excluded even when a pattern above would match
# (e.g. "Change of Director's Interest Notice" tripping "director").
# Never applied to announcements ASX has marked price-sensitive.
_ROUTINE_PATTERN = re.compile(
    r"appendix 2a|appendix 3[bghyz]|change of director'?s interest|"
    r"initial director'?s interest|final director'?s interest|"
    r"notification regarding unquoted|application for quotation|"
    r"cleansing (notice|statement)|becoming a substantial holder|"
    r"ceasing to be a substantial holder|change in substantial holding|"
    r"daily (net tangible asset|nta)|proxy form|notice of (annual|extraordinary) general meeting|"
    r"results of (annual|extraordinary) general meeting|agm presentation|"
    r"corporate governance statement|constitution|top 20 holders|"
    r"company secretary|change of (registered office|share registry) address|"
    r"update - dividend/distribution|dividend/distribution - ",
    re.I,
)


def classify_significance(title: str, price_sensitive: Optional[bool] = None) -> Optional[str]:
    """Return a significance category for an announcement, or None if routine.

    Announcements ASX marks price-sensitive are always significant — they
    keep their keyword category when one matches, otherwise the generic
    "Price sensitive" label.  Everything else must clear the routine-noise
    filter and then match a significant-category pattern.
    """
    text = " ".join((title or "").split())
    if not text:
        return None

    category = next((c for c, p in _SIGNIFICANT_PATTERNS if p.search(text)), None)
    if price_sensitive:
        return category or "Price sensitive"
    if _ROUTINE_PATTERN.search(text):
        return None
    return category


# ---------------------------------------------------------------------------
# Ticker helpers
# ---------------------------------------------------------------------------

def asx_code(ticker: str) -> Optional[str]:
    """Return the bare ASX code for a Wally ticker, or None if not ASX.

    Wally's watchlist loader normalises ASX listings to Yahoo-style
    "CODE.AX"; anything without that suffix (US, LSE, TSX, …) has no ASX
    announcements to look up.
    """
    t = (ticker or "").strip().upper()
    if t.endswith(".AX") and len(t) > 3:
        return t[:-3]
    return None


# ---------------------------------------------------------------------------
# News item model
# ---------------------------------------------------------------------------

@dataclass
class NewsItem:
    """One significant ASX announcement for a flagged ticker."""
    ticker: str            # Wally ticker as screened, e.g. "BHP.AX"
    date: str              # DD/MM/YYYY as reported by ASX
    time: str              # may be empty
    title: str
    url: str
    category: str
    price_sensitive: bool

    def to_dict(self) -> dict:
        return asdict(self)

    def sort_date(self) -> dt.date:
        try:
            return dt.datetime.strptime(self.date, "%d/%m/%Y").date()
        except Exception:
            return dt.date.min


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _http_session():
    """Browser-like requests session for the ASX v2 endpoint."""
    import requests

    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Referer": "https://www.asx.com.au/",
    })
    return s


def fetch_significant_news(
    ticker: str,
    lookback_days: Optional[int] = None,
    max_items: Optional[int] = None,
    session=None,
) -> Optional[list[NewsItem]]:
    """Fetch significant ASX announcements for *ticker* over the lookback window.

    Returns None for non-ASX tickers (nothing to look up), otherwise a
    newest-first list of :class:`NewsItem` capped at *max_items* — possibly
    empty when the window holds no significant announcements.  Never raises.
    """
    code = asx_code(ticker)
    if not code:
        return None

    lookback = lookback_days if lookback_days is not None else NEWS_LOOKBACK_DAYS
    cap = max_items if max_items is not None else NEWS_MAX_ITEMS
    to_date = dt.date.today()
    from_date = to_date - dt.timedelta(days=lookback)

    try:
        from asx_fetch import fetch_asx_announcements_html
    except Exception as exc:  # bs4/requests missing — degrade gracefully
        _log(f"announcement fetch layer unavailable ({exc}) — skipping news for {ticker}")
        return []

    try:
        raw = fetch_asx_announcements_html(
            session or _http_session(), code, from_date=from_date, to_date=to_date
        )
    except Exception as exc:
        _log(f"fetch failed for {code}: {exc}")
        return []

    items: list[NewsItem] = []
    for ann in raw:
        title = ann.get("title", "")
        category = classify_significance(title, ann.get("price_sensitive"))
        if not category:
            continue
        items.append(
            NewsItem(
                ticker=ticker.strip().upper(),
                date=ann.get("date", ""),
                time=ann.get("time", ""),
                title=title,
                url=ann.get("url", ""),
                category=category,
                price_sensitive=bool(ann.get("price_sensitive")),
            )
        )

    items.sort(key=lambda i: i.sort_date(), reverse=True)
    dropped = len(raw) - len(items)
    _log(
        f"{ticker}: {len(items)} significant of {len(raw)} announcement(s) "
        f"in last {lookback} days ({dropped} routine filtered)"
    )
    return items[:cap]
