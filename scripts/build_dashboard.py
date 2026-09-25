#!/usr/bin/env python3
"""
build_dashboard.py — generates docs/index.html from the agents' docs/data/*.json.
Run from repo root: python scripts/build_dashboard.py

This script owns the I/O: reading docs/data/*.json, keeping the
*_history.json files, writing docs/index.html. The HTML for each agent's
card lives in dashboard/sections/<agent>.py and the page shell in
dashboard/page.py.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

# The Slinger runs this script on the self-hosted Windows runner, where
# sys.stdout defaults to cp1252. Any non-ASCII char in a print() (a `→`
# in a log line, an emoji in a section header) crashes the whole build
# with UnicodeEncodeError, which killed the C07 rerun after slinger.json
# had already been committed but before index.html was rebuilt with the
# Slinger card. Force UTF-8 so console encoding stops being a load-
# bearing bug.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
DATA_DIR = DOCS_DIR / "data"

# Run as a script, sys.path[0] is scripts/; the dashboard package is at the root.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dashboard.page import render_page  # noqa: E402
from dashboard.sections.bob import (  # noqa: E402, F401  (_BADGE_COLOURS/_FIELD_MAP re-exported for tests)
    _BADGE_COLOURS,
    _FIELD_MAP,
    _bob_section,
)
from dashboard.sections.harry import _harry_section  # noqa: E402
from dashboard.sections.ned import _ned_section  # noqa: E402
from dashboard.sections.sally import _sally_section  # noqa: E402
from dashboard.sections.slinger import _slinger_section  # noqa: E402
from dashboard.sections.theo import _theo_section  # noqa: E402
from dashboard.sections.transcripts import _transcripts_section  # noqa: E402
from dashboard.sections.us import _us_section  # noqa: E402
from dashboard.sections.wally import _wally_section  # noqa: E402


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load(name: str) -> dict:
    """Load an agent's dashboard JSON. Reads as UTF-8 explicitly so the
    Windows runner's Python 3.14 (which still defaults `read_text(encoding='utf-8')` to
    cp1252) does not mojibake `¢` -> `Â¢` and cycle differently against
    the ubuntu Publish site runner -- that's what left
    docs/data/slinger_history.json thrashing between platforms and made
    every Publish site run try to "fix" the mojibake."""
    path = DATA_DIR / name
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _load_transcripts() -> list[dict]:
    """docs/data/transcripts.json is a bare JSON array, not an object.

    `_load` returns `{}` on any error and would misread this, so give it its
    own loader that returns [] on error and validates the shape."""
    path = DATA_DIR / "transcripts.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return []
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Last-two-runs history (docs/data/*_history.json)
# ---------------------------------------------------------------------------

def _update_us_history(us: dict) -> list[dict]:
    """Keep the last two distinct Bob USA runs in
    docs/data/us_history.json. Mirrors _update_slinger_history exactly:
    prepend when the new run differs from the top of the history, then
    truncate to two. Returns the history newest first.

    Same UTF-8 read convention as _update_slinger_history to avoid the
    Windows cp1252 cycle -- Bob USA is ubuntu-only right now, but the
    dashboard rebuild that reads this file also runs on other agents'
    runners, and one of those is Windows (Slinger). Consistency
    matters even before it bites."""
    if not us or not us.get("last_run"):
        try:
            existing = json.loads((DATA_DIR / "us_history.json").read_text(encoding="utf-8"))
            return existing if isinstance(existing, list) else []
        except Exception:
            return []

    hist_path = DATA_DIR / "us_history.json"
    history: list[dict] = []
    if hist_path.exists():
        try:
            loaded = json.loads(hist_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                history = [h for h in loaded if isinstance(h, dict)]
        except Exception:
            history = []

    def _sig(d: dict) -> str:
        return json.dumps(
            {k: d.get(k) for k in ("last_run", "silence", "high_impact", "material", "fyi")},
            sort_keys=True,
        )

    if not history or _sig(history[0]) != _sig(us):
        history = [us] + history
    history = history[:2]
    hist_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return history


def _update_slinger_history(slinger: dict) -> list[dict]:
    """Keep the last two distinct Slinger runs in
    docs/data/slinger_history.json.

    Mirrors ``_update_bob_history``. Slinger overwrites slinger.json
    every run, so without this the site would only ever show the most
    recent digest. This prepends the current run when its content
    differs from the top of the history and truncates to two, so a
    plain dashboard rebuild (Theo/Wally/Ned, or an identical re-run)
    never churns the file. Returns the history, newest first."""
    if not slinger or not slinger.get("last_run"):
        # No Slinger data to record; return whatever is already on disk.
        try:
            existing = json.loads((DATA_DIR / "slinger_history.json").read_text(encoding='utf-8'))
            return existing if isinstance(existing, list) else []
        except Exception:
            return []

    hist_path = DATA_DIR / "slinger_history.json"
    history: list[dict] = []
    if hist_path.exists():
        try:
            loaded = json.loads(hist_path.read_text(encoding='utf-8'))
            if isinstance(loaded, list):
                history = [h for h in loaded if isinstance(h, dict)]
        except Exception:
            history = []

    def _sig(d: dict) -> str:
        return json.dumps(
            {k: d.get(k) for k in ("last_run", "silence", "high_impact", "material", "fyi")},
            sort_keys=True,
        )

    if not history or _sig(history[0]) != _sig(slinger):
        history = [slinger] + history
    history = history[:2]
    hist_path.write_text(json.dumps(history, indent=2), encoding='utf-8')
    return history


def _update_bob_history(bob: dict) -> list[dict]:
    """Keep the last two distinct Bob digests in docs/data/bob_history.json.

    Bob overwrites bob.json every run, so without this the site only ever
    shows the most recent digest. This prepends the current run when its
    content differs from the top of the history and truncates to two, so a
    plain dashboard rebuild (Theo/Wally/Ned, or an identical re-run) never
    churns the file. Returns the history, newest first.
    """
    if not bob or not bob.get("last_run"):
        # No Bob data to record; return whatever is already on disk.
        try:
            existing = json.loads((DATA_DIR / "bob_history.json").read_text(encoding='utf-8'))
            return existing if isinstance(existing, list) else []
        except Exception:
            return []

    hist_path = DATA_DIR / "bob_history.json"
    history: list[dict] = []
    if hist_path.exists():
        try:
            loaded = json.loads(hist_path.read_text(encoding='utf-8'))
            if isinstance(loaded, list):
                history = [h for h in loaded if isinstance(h, dict)]
        except Exception:
            history = []

    def _sig(d: dict) -> str:
        return json.dumps(
            {k: d.get(k) for k in ("last_run", "silence", "high_impact", "material", "fyi")},
            sort_keys=True,
        )

    if not history or _sig(history[0]) != _sig(bob):
        history = [bob] + history
    history = history[:2]
    hist_path.write_text(json.dumps(history, indent=2), encoding='utf-8')
    return history


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_dashboard() -> None:
    bob = _load("bob.json")
    bob_history = _update_bob_history(bob)
    slinger = _load("slinger.json")
    slinger_history = _update_slinger_history(slinger)
    us = _load("us.json")
    us_history = _update_us_history(us)
    wally = _load("wally.json")
    sally = _load("sally.json")
    theo = _load("theo.json")
    ned = _load("ned.json")
    harry = _load("harry.json")
    transcripts = _load_transcripts()

    _now_utc = datetime.utcnow()
    generated_at = f"{_now_utc.day} {_now_utc.strftime('%b %Y %H:%M UTC')}"

    # Display order. Adding an agent: one _load above, one line here.
    sections = [
        _bob_section(bob, bob_history),
        _slinger_section(slinger, slinger_history),
        _us_section(us, us_history),
        _wally_section(wally),
        _sally_section(sally),
        _harry_section(harry),
        _theo_section(theo),
        _ned_section(ned),
        _transcripts_section(transcripts),
    ]
    html = render_page(sections, generated_at)

    out = DOCS_DIR / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"[dashboard] Written -> {out}")


if __name__ == "__main__":
    build_dashboard()
