# ned/news_scanner.py
#
# Scans Google News RSS and static RSS feeds for portfolio company mentions.
# Uses Yahoo Finance news (already in repo) as an additional source.

from __future__ import annotations

import datetime as dt
import re
import urllib.parse
import xml.etree.ElementTree as ET

import requests

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

_RSS_TIMEOUT = 15


def _fetch_rss(url: str) -> list[dict]:
    """Fetch and parse an RSS/Atom feed. Returns list of item dicts."""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=_RSS_TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as exc:
        print(f"[ned/news] RSS fetch failed {url}: {exc}")
        return []

    items = []
    # RSS 2.0
    for item in root.findall(".//item"):
        pub_str = (item.findtext("pubDate") or "").strip()
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "description": (item.findtext("description") or "")[:400].strip(),
            "published_str": pub_str,
            "published": _parse_date(pub_str),
        })
    # Atom
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for entry in root.findall(".//atom:entry", ns):
        pub_str = (entry.findtext("atom:published", namespaces=ns) or "").strip()
        link_el = entry.find("atom:link", ns)
        items.append({
            "title": (entry.findtext("atom:title", namespaces=ns) or "").strip(),
            "link": link_el.get("href", "") if link_el is not None else "",
            "description": (entry.findtext("atom:summary", namespaces=ns) or "")[:400].strip(),
            "published_str": pub_str,
            "published": _parse_date(pub_str),
        })
    return items


def _parse_date(s: str) -> dt.datetime | None:
    """Best-effort parse of RSS/Atom date strings → UTC datetime."""
    if not s:
        return None
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            parsed = dt.datetime.strptime(s, fmt)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed
        except ValueError:
            continue
    return None


def _mentions_company(text: str, ticker: str, company_name: str) -> bool:
    haystack = text.lower()
    if re.search(rf"\b{re.escape(ticker.lower())}\b", haystack):
        return True
    first_word = company_name.split()[0].lower()
    if len(first_word) > 3 and first_word in haystack:
        return True
    return False


def scan_rss_feeds(
    feed_configs: list[dict],
    companies: dict[str, str],   # {TICKER: "Company Name"}
    lookback_hours: int,
    seen_keys: set[str],
) -> list[dict]:
    """
    Scan configured RSS feeds for company mentions.
    Returns list of hit dicts.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lookback_hours)
    hits: list[dict] = []

    for feed in feed_configs:
        feed_type = feed.get("type", "static")
        label = feed.get("label", feed["url"])

        if feed_type == "google_news":
            # One RSS query per company
            for ticker, name in companies.items():
                query = urllib.parse.quote_plus(f'"{name}"')
                url = feed["url"].replace("{query}", query)
                items = _fetch_rss(url)
                for item in items:
                    _check_item(item, ticker, name, label, cutoff, seen_keys, hits, f"gnews|{ticker}|{item['link']}")
        else:
            # Static feed — fetch once, filter by company
            items = _fetch_rss(feed["url"])
            for item in items:
                text = f"{item['title']} {item['description']}"
                for ticker, name in companies.items():
                    if _mentions_company(text, ticker, name):
                        _check_item(item, ticker, name, label, cutoff, seen_keys, hits, f"rss|{ticker}|{item['link']}")

    return hits


def _check_item(
    item: dict,
    ticker: str,
    company_name: str,
    label: str,
    cutoff: dt.datetime,
    seen_keys: set[str],
    hits: list[dict],
    key: str,
) -> None:
    pub = item.get("published")
    if pub and pub < cutoff:
        return
    if key in seen_keys:
        return
    hits.append({
        "source": label,
        "source_type": "rss",
        "tickers": [ticker],
        "title": item["title"],
        "url": item["link"],
        "description": item["description"],
        "published": pub.isoformat() if pub else "",
        "seen_key": key,
    })


def scan_yahoo_finance(
    companies: dict[str, str],
    lookback_hours: int,
    seen_keys: set[str],
) -> list[dict]:
    """
    Pull Yahoo Finance news per ticker using the existing news_context_fetcher.
    Falls back gracefully if unavailable.
    """
    try:
        import sys
        import os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from news_context_fetcher import fetch_news_context
    except ImportError:
        return []

    cutoff_ts = (dt.datetime.utcnow() - dt.timedelta(hours=lookback_hours)).timestamp()
    hits: list[dict] = []

    for ticker, name in companies.items():
        exchange_ticker = f"{ticker}.AX" if ticker not in ("RR.",) else ticker
        items = fetch_news_context(exchange_ticker, max_items=5)
        for item in items:
            pub_ts = item.get("published_at") or 0
            if pub_ts and pub_ts < cutoff_ts:
                continue
            key = f"yf|{ticker}|{item.get('link', '')}"
            if key in seen_keys:
                continue
            hits.append({
                "source": f"Yahoo Finance ({item.get('publisher', '?')})",
                "source_type": "yahoo",
                "tickers": [ticker],
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "description": "",
                "published": str(pub_ts),
                "seen_key": key,
            })

    return hits
