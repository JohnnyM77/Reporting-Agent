from __future__ import annotations

import requests


def fetch_news_context(exchange_ticker: str, max_items: int = 8) -> list[dict]:
    endpoint = f"https://query1.finance.yahoo.com/v1/finance/search?q={exchange_ticker}"
    try:
        r = requests.get(endpoint, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return []

    news = data.get("news", []) if isinstance(data, dict) else []
    out = []
    for item in news[:max_items]:
        out.append({
            "title": item.get("title"),
            "publisher": item.get("publisher"),
            "link": item.get("link"),
            "published_at": item.get("providerPublishTime"),
        })
    return out
