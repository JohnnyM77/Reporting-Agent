#!/usr/bin/env python3
"""Test Drive upload — uploads a tiny synthetic xlsx and verifies the return dict."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wally.drive_upload import upload_to_drive


def main() -> None:
    folder_id = os.environ.get("GDRIVE_FOLDER_ID", "").strip()
    if not folder_id:
        print("GDRIVE_FOLDER_ID not set — cannot run test")
        sys.exit(1)

    tmp = Path("/tmp/test_wally_drive.xlsx")
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active["A1"] = "Drive upload test"  # type: ignore[index]
        wb.save(tmp)
        print(f"Created test workbook: {tmp}")
    except ImportError:
        tmp.write_bytes(b"PK\x03\x04" + b"\x00" * 22)
        print(f"openpyxl not available — wrote minimal placeholder: {tmp}")

    result = upload_to_drive(tmp, "TESTUPLOAD.AX", folder_id)
    print(f"Result: {result}")

    if result["ok"]:
        print(f"SUCCESS — file '{result['filename']}' at {result['url']}")
    else:
        print(f"FAILED — {result['error']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
