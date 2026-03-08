from __future__ import annotations

import datetime as dt
from pathlib import Path

import requests

# Match the User-Agent + Referer pattern Bob uses so the ASX API doesn't block us.
_ASX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.asx.com.au/",
}


def fetch_asx_announcements(ticker: str, limit: int = 12) -> list[dict]:
    url = (
        f"https://www.asx.com.au/asx/v2/statistics/announcements.do"
        f"?asxCode={ticker}&by=asxCode&period=M6&timeframe=D"
    )
    try:
        resp = requests.get(url, headers=_ASX_HEADERS, timeout=25)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        print(f"[document_fetcher] Could not fetch ASX announcements for {ticker}: {exc}")
        return []

    rows = payload.get("data", []) if isinstance(payload, dict) else []
    out = []
    for row in rows[:limit]:
        out.append(
            {
                "headline": row.get("header"),
                "released_at": row.get("releasedDate"),
                "url": row.get("url"),
                "pdf": row.get("documentKey"),
            }
        )
    return out


def save_source_documents(ticker: str, run_folder: Path, announcements: list[dict]) -> Path:
    source_dir = run_folder / ticker / "source_docs"
    source_dir.mkdir(parents=True, exist_ok=True)
    index_file = source_dir / "announcement_index.json"
    index_file.write_text(
        __import__("json").dumps(
            {
                "saved_at": dt.datetime.utcnow().isoformat(),
                "announcements": announcements,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return source_dir
