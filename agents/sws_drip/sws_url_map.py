"""
agents/sws_drip/sws_url_map.py
────────────────────────────────
Builds a {ticker -> (country, sector, slug)} lookup table for ASX stocks by
crawling Simply Wall St's published sitemaps.

Why sitemaps?
  SWS renders search/company pages client-side (the initial HTML is just a
  Next.js shell), and their internal API routes are undocumented and 404 on
  every guess. But every public site publishes sitemaps for crawlers, and
  SWS's sitemaps contain the EXACT canonical stock URLs we need:
      /stocks/au/materials/asx-bhp/bhp-group-shares
  We discover the sitemaps the standards-compliant way (via robots.txt),
  parse out the ASX URLs, and cache the result. No API guessing, no rotating
  keys, no JS rendering — just the structure SWS itself publishes.

The map is cached to data/sws_url_map.json and rebuilt only when missing,
stale, or when a requested ticker is absent (one forced refresh).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Matches a full ASX stock URL and captures country / sector / ticker / slug.
# e.g. https://simplywall.st/stocks/au/materials/asx-bhp/bhp-group-shares
_STOCK_URL_RE = re.compile(
    r"https?://simplywall\.st/stocks/([a-z]{2})/([^/]+)/asx-([a-z0-9]+)/([^/?#<>\s\"']+)",
    re.IGNORECASE,
)

# Pull <loc>...</loc> entries out of a sitemap or sitemap-index XML document.
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)

# Safety limits so a runaway sitemap can never hang the runner.
_MAX_SITEMAPS = 400
_MAX_DEPTH = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _discover_sitemaps(session, base: str) -> list[str]:
    """Read robots.txt and return every Sitemap: URL it advertises."""
    sitemaps: list[str] = []
    try:
        resp = session.get(f"{base}/robots.txt", timeout=20)
        print(f"[sws_url_map] robots.txt -> HTTP {resp.status_code}", flush=True)
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                line = line.strip()
                if line.lower().startswith("sitemap:"):
                    sitemaps.append(line.split(":", 1)[1].strip())
    except Exception as e:
        print(f"[sws_url_map] robots.txt error: {type(e).__name__}: {e}", flush=True)

    # Fallback to the conventional locations if robots.txt advertised nothing.
    if not sitemaps:
        sitemaps = [f"{base}/sitemap.xml", f"{base}/sitemap_index.xml"]
    print(f"[sws_url_map] Discovered {len(sitemaps)} sitemap entrypoint(s)", flush=True)
    return sitemaps


def _looks_like_stock_sitemap(url: str) -> bool:
    """Heuristic to skip sitemaps that obviously hold no company URLs."""
    u = url.lower()
    if not u.endswith(".xml") and ".xml" not in u:
        return True  # unsure — don't skip
    skip = ("news", "article", "blog", "author", "narrative", "user", "portfolio")
    return not any(s in u for s in skip)


def build_map(session, base: str = "https://simplywall.st") -> dict[str, dict]:
    """
    Crawl SWS sitemaps and return {TICKER: {country, sector, slug}} for ASX stocks.

    Uses the provided curl-cffi session (residential IP + Chrome impersonation)
    so Cloudflare lets the sitemap fetches through.
    """
    start = time.time()
    tickers: dict[str, dict] = {}
    queue = [(u, 0) for u in _discover_sitemaps(session, base)]
    seen: set[str] = set()
    fetched = 0

    while queue and fetched < _MAX_SITEMAPS:
        url, depth = queue.pop(0)
        if url in seen or depth > _MAX_DEPTH:
            continue
        seen.add(url)
        if not _looks_like_stock_sitemap(url):
            continue

        try:
            resp = session.get(url, timeout=30)
            fetched += 1
        except Exception as e:
            print(f"[sws_url_map] fetch error {url}: {type(e).__name__}: {e}", flush=True)
            continue
        if resp.status_code != 200:
            continue

        body = resp.text
        is_index = "<sitemapindex" in body[:500].lower()
        locs = _LOC_RE.findall(body)

        if is_index:
            # Child sitemaps — enqueue the stock-looking ones.
            for loc in locs:
                if loc not in seen and _looks_like_stock_sitemap(loc):
                    queue.append((loc, depth + 1))
        else:
            # Leaf sitemap — harvest ASX stock URLs.
            before = len(tickers)
            for loc in locs:
                m = _STOCK_URL_RE.match(loc)
                if m:
                    country, sector, ticker, slug = m.groups()
                    tickers.setdefault(
                        ticker.upper(),
                        {"country": country.lower(), "sector": sector.lower(), "slug": slug},
                    )
            gained = len(tickers) - before
            if gained:
                print(
                    f"[sws_url_map] {url.rsplit('/', 1)[-1]}: +{gained} ASX tickers "
                    f"(total {len(tickers)})",
                    flush=True,
                )

    elapsed = time.time() - start
    print(
        f"[sws_url_map] Built map: {len(tickers)} ASX tickers from {fetched} "
        f"sitemap(s) in {elapsed:.1f}s",
        flush=True,
    )
    return tickers


def load_or_build(
    session,
    cache_path: Path,
    *,
    max_age_days: int = 14,
    force: bool = False,
) -> dict[str, dict]:
    """
    Return the ticker map, using the cached JSON when it is fresh enough.
    Rebuilds (and rewrites the cache) when missing, stale, empty, or forced.
    """
    if not force and cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            built_at = cached.get("_built_at", "")
            tickers = cached.get("tickers", {})
            age_days = _age_days(built_at)
            if tickers and age_days is not None and age_days <= max_age_days:
                print(
                    f"[sws_url_map] Using cached map: {len(tickers)} tickers "
                    f"(age {age_days:.1f}d)",
                    flush=True,
                )
                return tickers
        except Exception as e:
            print(f"[sws_url_map] cache read error: {type(e).__name__}: {e}", flush=True)

    tickers = build_map(session)
    if tickers:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"_built_at": _now_iso(), "tickers": tickers}, indent=0),
            encoding="utf-8",
        )
        print(f"[sws_url_map] Cached map to {cache_path}", flush=True)
    return tickers


def _age_days(built_at: str) -> Optional[float]:
    try:
        dt = datetime.fromisoformat(built_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400
    except Exception:
        return None
