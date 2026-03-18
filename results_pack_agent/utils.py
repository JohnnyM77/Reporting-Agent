# results_pack_agent/utils.py
# Shared utilities: logging, file naming, date helpers.

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path
from typing import Optional


# ── Logging ────────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    """Print a timestamped log line to stdout (always flushed)."""
    ts = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── Date helpers ───────────────────────────────────────────────────────────────

def asx_date_to_prefix(asx_date: str) -> str:
    """Convert ASX DD/MM/YYYY date string to YYMMDD prefix.

    Examples::

        asx_date_to_prefix("18/03/2026") -> "260318"
    """
    d = dt.datetime.strptime(asx_date, "%d/%m/%Y")
    return d.strftime("%y%m%d")


def asx_date_to_iso(asx_date: str) -> str:
    """Convert ASX DD/MM/YYYY to ISO YYYY-MM-DD."""
    d = dt.datetime.strptime(asx_date, "%d/%m/%Y")
    return d.strftime("%Y-%m-%d")


def iso_to_asx_date(iso_date: str) -> str:
    """Convert ISO YYYY-MM-DD to ASX DD/MM/YYYY."""
    d = dt.datetime.strptime(iso_date, "%Y-%m-%d")
    return d.strftime("%d/%m/%Y")


def parse_asx_date(asx_date: str) -> dt.date:
    """Parse ASX DD/MM/YYYY string to a ``datetime.date``."""
    return dt.datetime.strptime(asx_date, "%d/%m/%Y").date()


# ── File naming ────────────────────────────────────────────────────────────────

def safe_filename(name: str) -> str:
    """Replace characters that are awkward in filenames with underscores."""
    return re.sub(r"[^\w.\-]", "_", name)


def make_output_folder(base_dir: Path, folder_name: str) -> Path:
    """Create and return the output folder, creating parents as needed."""
    folder = base_dir / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def http_session():
    """Return a requests.Session with browser-like headers for ASX."""
    import requests

    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": "https://www.asx.com.au/",
        }
    )
    return s
