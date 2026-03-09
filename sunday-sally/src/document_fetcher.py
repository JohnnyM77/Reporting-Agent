from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import requests

# ASX JSON API — returns proper JSON, no HTML parsing needed.
# The old v2/statistics/announcements.do endpoint returns HTML, not JSON.
_ASX_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
}


def fetch_asx_announcements(ticker: str, limit: int = 12) -> list[dict]:
    url = (
        f"https://www.asx.com.au/asx/1/company/{ticker}"
        f"/announcements?count={limit}&market=ASX"
    )
    try:
        resp = requests.get(url, timeout=25, headers=_ASX_HEADERS)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return []

    rows = payload.get("data", []) if isinstance(payload, dict) else []
    out = []
    for row in rows[:limit]:
        out.append(
            {
                "headline": row.get("header"),
                "released_at": row.get("document_release_date"),
                "url": row.get("url"),
                "market_sensitive": row.get("market_sensitive"),
            }
        )
    return out


def save_source_documents(ticker: str, run_folder: Path, announcements: list[dict]) -> Path:
    source_dir = run_folder / ticker / "source_docs"
    source_dir.mkdir(parents=True, exist_ok=True)
    index_file = source_dir / "announcement_index.json"
    index_file.write_text(
        json.dumps(
            {
                "saved_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "announcements": announcements,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return source_dir
