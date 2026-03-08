from __future__ import annotations

import requests

# Yahoo Finance rejects bare requests without a browser-like User-Agent.
_YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


def fetch_news_context(exchange_ticker: str, max_items: int = 8) -> list[dict]:
    endpoint = f"https://query1.finance.yahoo.com/v1/finance/search?q={exchange_ticker}"
    try:
        r = requests.get(endpoint, headers=_YF_HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:
        print(f"[news_context_fetcher] Could not fetch news for {exchange_ticker}: {exc}")
        return []

    news = data.get("news", []) if isinstance(data, dict) else []
    out = []
    for item in news[:max_items]:
        out.append(
            {
                "title": item.get("title"),
                "publisher": item.get("publisher"),
                "link": item.get("link"),
                "published_at": item.get("providerPublishTime"),
            }
        )
    return out
