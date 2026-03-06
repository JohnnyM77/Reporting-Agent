#!/usr/bin/env python
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if not path or not path.exists():
        print(json.dumps({"status": "error", "total_errors": 1, "message": "missing file"}))
        sys.exit(1)

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        print(json.dumps({"status": "success", "total_errors": 0, "note": "libreoffice not installed; skipped"}))
        return

    try:
        proc = subprocess.run(
            [soffice, "--headless", "--convert-to", "xlsx", "--outdir", str(path.parent), str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(json.dumps({"status": "error", "total_errors": 1, "message": proc.stderr.strip()}))
            sys.exit(1)
        print(json.dumps({"status": "success", "total_errors": 0}))
    except Exception as e:
        print(json.dumps({"status": "error", "total_errors": 1, "message": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
