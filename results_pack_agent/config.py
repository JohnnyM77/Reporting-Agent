# results_pack_agent/config.py
# Constants and configuration for the Results Pack Agent.
# All secrets are loaded from environment variables at runtime.

from __future__ import annotations

import os
from pathlib import Path

# ── Output roots ──────────────────────────────────────────────────────────────
# Resolved relative to the repo root so the agent works from any cwd.
_REPO_ROOT = Path(__file__).parent.parent
OUTPUT_ROOT = _REPO_ROOT / "outputs" / "results_pack"

# ── ASX API ───────────────────────────────────────────────────────────────────
# The Results Pack Agent uses the shared asx_fetch module (Bob's proven path).
# The endpoint URL is defined in asx_fetch.ASX_V2_URL; it is not duplicated here.

# Months of history to scan when searching for the latest result day.
ASX_HISTORY_MONTHS = 6

# HTTP timeouts (seconds)
HTTP_TIMEOUT_SECS = 30
PDF_DOWNLOAD_TIMEOUT_SECS = 30

# PDF size cap per document sent to Claude (~20 MB)
MAX_PDF_BYTES = 20 * 1024 * 1024

# Maximum PDFs to send per Claude request
MAX_PDFS_PER_CALL = 10

# ── Claude ────────────────────────────────────────────────────────────────────
CLAUDE_DEFAULT_MODEL = os.environ.get("MODEL_NAME", "claude-opus-4-5-20251101")
CLAUDE_MAX_TOKENS = 8192

# ── Google Drive ──────────────────────────────────────────────────────────────
# Root folder ID for results pack uploads.
# Falls back to the same folder Bob uses if RESULTS_PACK_GDRIVE_FOLDER_ID is
# not explicitly set.
GDRIVE_FOLDER_ID: str | None = (
    os.environ.get("RESULTS_PACK_GDRIVE_FOLDER_ID")
    or os.environ.get("GDRIVE_FOLDER_ID")
)

# Parent path inside Drive: "Earnings Reports/<TICKER>/<folder>"
GDRIVE_PARENT_PATH = "Earnings Reports"

# ── Wally / valuation ─────────────────────────────────────────────────────────
# Path to the valuations YAML configs (wally convention)
VALUATIONS_DIR = _REPO_ROOT / "valuations"

# ── Result-day detection ──────────────────────────────────────────────────────
# Keywords that identify the *primary* HY/FY results trigger announcement.
RESULT_DAY_TRIGGER_KEYWORDS: list[str] = [
    "half year results",
    "half-year results",
    "full year results",
    "full-year results",
    "fy results",
    "hy results",
    "h1 results",
    "interim results",
    "appendix 4d",
    "appendix 4e",
    "results announcement",
    "1h fy",
    "1hfy",
    # Common patterns for company-prefixed results titles (e.g. "NHC Half Year Results")
    "half year result",
    "full year result",
    # "FY26 Results", "FY2026 Results", etc.
    "fy26 results",
    "fy25 results",
    "fy24 results",
    "fy2026 results",
    "fy2025 results",
    "fy2024 results",
    # "1HFY26", "2HFY26" style
    "2h fy",
    "2hfy",
    # Financial results or earnings release
    "financial results",
    "earnings release",
    "preliminary final report",
]

# Keywords that indicate a same-day document worth including in the pack
# (but not strong enough to act as the primary trigger on their own).
PACK_INCLUDE_KEYWORDS: list[str] = [
    "half year",
    "half-year",
    "full year",
    "annual report",
    "interim financial report",
    "half-year financial report",
    "full year financial report",
    "financial report",
    "investor presentation",
    "results presentation",
    "management presentation",
    "dividend",
    "distribution",
    "fy ",
    "appendix 4d",
    "appendix 4e",
    "preliminary final",
    "earnings release",
]

# These titles are excluded even if other keywords match.
HARD_NO_KEYWORDS: list[str] = [
    "transcript",
    "webcast",
    "conference call",
]
