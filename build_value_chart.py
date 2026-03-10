#!/usr/bin/env python3
"""
scripts/build_value_chart.py
─────────────────────────────
CLI entry point for the value chart builder.
Used by:
  - GitHub Actions / Codex agent (automated)
  - Sunday Sally (triggered on flagged companies)
  - Manual local runs

Usage:
  python scripts/build_value_chart.py NHC.AX
  python scripts/build_value_chart.py ARB.AX --price-csv data/arb_prices.csv
  python scripts/build_value_chart.py NHC.AX --output outputs/NHC_custom.xlsx
  python scripts/build_value_chart.py NHC.AX --drive-folder-id <GDRIVE_FOLDER_ID>

The script reads:
  valuations/<ticker_slug>.yaml   e.g. valuations/nhc_ax.yaml

Reference template:
  outputs/NHC_ASX_Value_Analysis_v3.xlsx  (the example cyclical workbook)

Reference config:
  valuations/nhc_ax.yaml          (full annotated example with all options)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow running from repo root or scripts/ directory
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wally.value_chart_builder import build_value_chart


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build an ASX value chart workbook from a valuations YAML config.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/build_value_chart.py NHC.AX
  python scripts/build_value_chart.py ARB.AX --price-csv data/arb_prices.csv
  python scripts/build_value_chart.py NHC.AX --output outputs/NHC_review.xlsx
  python scripts/build_value_chart.py NHC.AX --drive-folder-id abc123

Config files live in:  valuations/<ticker_lower_with_underscores>.yaml
  e.g. NHC.AX  → valuations/nhc_ax.yaml
       ARB.AX  → valuations/arb_ax.yaml
       CSL.AX  → valuations/csl_ax.yaml

Reference template:    outputs/NHC_ASX_Value_Analysis_v3.xlsx
Reference config:      valuations/nhc_ax.yaml  (fully annotated)
        """,
    )
    parser.add_argument(
        "ticker",
        help="ASX ticker (e.g. NHC.AX) or path to YAML config file.",
    )
    parser.add_argument(
        "--price-csv",
        dest="price_csv",
        help="Path to marketindex daily price CSV (Date as YYYYMMDD, Close column).",
        default=None,
    )
    parser.add_argument(
        "--output",
        dest="output",
        help="Output xlsx path. Defaults to outputs/<TICKER>_ASX_Value_Analysis.xlsx",
        default=None,
    )
    parser.add_argument(
        "--drive-folder-id",
        dest="drive_folder_id",
        help="Google Drive folder ID to upload result. Falls back to env GDRIVE_FOLDER_ID.",
        default=None,
    )

    args = parser.parse_args()

    drive_folder_id = (
        args.drive_folder_id
        or os.environ.get("SALLY_DRIVE_ROOT_FOLDER_ID")
        or os.environ.get("GDRIVE_FOLDER_ID")
    )

    result = build_value_chart(
        ticker_or_config_path=args.ticker,
        output_path=args.output,
        price_csv_path=args.price_csv,
        drive_folder_id=drive_folder_id,
    )

    print(f"[build_value_chart] Done → {result}")


if __name__ == "__main__":
    main()
