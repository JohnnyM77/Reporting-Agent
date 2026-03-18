# results_pack_agent/models.py
# Data classes shared across the results pack agent.

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Announcement:
    """A single ASX announcement item."""

    ticker: str
    title: str
    date: str           # DD/MM/YYYY (ASX format)
    time: str
    url: str
    pdf_url: Optional[str] = None
    pdf_bytes: Optional[bytes] = None
    pdf_path: Optional[str] = None   # local path after saving to disk


@dataclass
class ResultPack:
    """The full set of announcements that constitute a result day."""

    ticker: str
    company_name: str
    result_date: str    # DD/MM/YYYY
    result_type: str    # "HY" or "FY"
    announcements: List[Announcement] = field(default_factory=list)

    # ------------------------------------------------------------------ helpers

    @property
    def date_prefix(self) -> str:
        """Return YYMMDD prefix for file/folder naming."""
        d = dt.datetime.strptime(self.result_date, "%d/%m/%Y")
        return d.strftime("%y%m%d")

    @property
    def folder_name(self) -> str:
        return f"{self.date_prefix}-{self.ticker}-{self.result_type}-Results-Pack"

    @property
    def file_prefix(self) -> str:
        return f"{self.date_prefix}-{self.ticker}-{self.result_type}"

    @property
    def pdfs_downloaded(self) -> int:
        return sum(1 for a in self.announcements if a.pdf_bytes is not None)


@dataclass
class RunSummary:
    """Final run summary returned to the caller / printed to stdout."""

    ticker: str
    result_date: str
    result_type: str
    pdfs_downloaded: int
    prompts_run: List[str]
    local_folder: str
    drive_folder_url: Optional[str]
    valuation_path: Optional[str]
    artifacts: Dict[str, str] = field(default_factory=dict)
    # Set to a structured failure code when the run did not complete normally.
    # Examples: NO_ANNOUNCEMENTS_FOUND, TICKER_VALID_BUT_NO_MATCHING_DATE,
    #           NO_RESULT_PACK_FOUND, PDF_DOWNLOAD_PARTIAL, CLAUDE_ANALYSIS_FAILED
    failure_reason: Optional[str] = None
    # Human-readable message accompanying the failure code
    failure_message: Optional[str] = None
    # Nearest result-day dates found (populated on near-miss date failures)
    nearest_dates: List[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """Return True when the run completed without a failure."""
        return self.failure_reason is None

    def print_summary(self) -> None:
        """Print a clean run summary to stdout."""
        print("\n" + "=" * 60)
        print("  Results Pack Agent — Run Summary")
        print("=" * 60)
        print(f"  Ticker        : {self.ticker}")
        print(f"  Result date   : {self.result_date}")
        print(f"  Result type   : {self.result_type}")
        if self.failure_reason:
            print(f"  Status        : FAILED ({self.failure_reason})")
            if self.failure_message:
                print(f"  Reason        : {self.failure_message}")
            if self.nearest_dates:
                print("  Nearest dates :")
                for d in self.nearest_dates:
                    print(f"    - {d}")
        else:
            print(f"  PDFs downloaded : {self.pdfs_downloaded}")
            print(f"  Prompts run   : {', '.join(self.prompts_run) or 'none'}")
            print(f"  Local folder  : {self.local_folder}")
            if self.drive_folder_url:
                print(f"  Google Drive  : {self.drive_folder_url}")
            if self.valuation_path:
                print(f"  Valuation     : {self.valuation_path}")
            if self.artifacts:
                print("  Artifacts:")
                for name, path in self.artifacts.items():
                    print(f"    {name}: {path}")
        print("=" * 60 + "\n")
