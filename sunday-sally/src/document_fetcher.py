from __future__ import annotations

import datetime as dt
from pathlib import Path

import requests


def fetch_asx_announcements(ticker: str, limit: int = 12) -> list[dict]:
    url = f"https://www.asx.com.au/asx/v2/statistics/announcements.do?asxCode={ticker}"
    try:
        resp = requests.get(url, timeout=25)
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
    index_file.write_text(__import__("json").dumps({"saved_at": dt.datetime.utcnow().isoformat(), "announcements": announcements}, indent=2), encoding="utf-8")
    return source_dir
