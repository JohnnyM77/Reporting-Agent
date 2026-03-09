from __future__ import annotations

import json
import os
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Internal helpers — identical pattern to Bob's drive_service()
# ---------------------------------------------------------------------------

def _drive_service():
    """
    Build a Drive API service using the same service account secret Bob uses.
    Uses the full drive scope so uploads work for user-owned folders shared
    with the service account (drive.file is too restrictive for those).
    """
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        return None
    info = json.loads(sa_json)
    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return build("drive", "v3", credentials=creds)


def _build_drive_filename(run_folder_root: Path, file_path: Path, run_label: str) -> str:
    """
    Flatten a nested file path into a single Drive filename using __ separators.

    Example:
        root  = /tmp/run
        path  = /tmp/run/BHP/memo.md
        label = SundaySally_2026-03-08
        →  SundaySally_2026-03-08__BHP__memo.md
    """
    rel = file_path.relative_to(run_folder_root)
    # Replace any characters that are awkward in Drive filenames
    safe_parts = [re.sub(r"[^\w.\-]", "_", part) for part in rel.parts]
    return run_label + "__" + "__".join(safe_parts)


def _upload_file(service, local_path: Path, drive_filename: str, folder_id: str) -> str:
    """
    Upload one file into folder_id — same approach as Bob's upload_to_drive().
    Returns the Drive file URL, or empty string on failure.
    """
    from googleapiclient.http import MediaFileUpload

    file_metadata = {"name": drive_filename, "parents": [folder_id]}
    media = MediaFileUpload(str(local_path), resumable=False)
    created = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id")
        .execute()
    )
    file_id = created.get("id") or ""
    if not file_id:
        return ""
    return f"https://drive.google.com/file/d/{file_id}/view"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_run_folder(
    local_run_folder: Path,
    drive_folder_components: list[str],
    root_folder_id: str | None = None,
) -> str | None:
    """
    Upload every file in local_run_folder into the Google Drive folder
    identified by root_folder_id (the GDRIVE_FOLDER_ID secret — same one
    Bob uses).

    Files are uploaded flat with descriptive names so Drive doesn't need
    subfolder creation. The run date is included in every filename so
    multiple weeks never overwrite each other.

    Returns the Drive folder URL on success, None if Drive is not configured
    or the upload fails. Never raises — Drive upload is non-fatal.
    """
    if not root_folder_id:
        print(
            "[google_drive_uploader] GDRIVE_FOLDER_ID is not set — "
            "skipping Drive upload. Sally will still send email."
        )
        return None

    try:
        service = _drive_service()
    except Exception as exc:
        print(f"[google_drive_uploader] Could not build Drive service — skipping upload: {exc}")
        return None

    if service is None:
        print("[google_drive_uploader] GDRIVE_SERVICE_ACCOUNT_JSON not set — skipping upload.")
        return None

    # Build a run label from the folder components e.g. "SundaySally_2026-03-08"
    # drive_folder_components looks like ["Investing", "Sunday Sally", "2026", "2026-03-08 Weekly Review"]
    # We use the last component (the dated one) as the label prefix
    run_label = "SundaySally"
    if drive_folder_components:
        last = drive_folder_components[-1]
        # Extract just the date portion if present e.g. "2026-03-08 Weekly Review" → "2026-03-08"
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", last)
        if date_match:
            run_label = f"SundaySally_{date_match.group()}"

    uploaded = 0
    errors = 0
    last_url = f"https://drive.google.com/drive/folders/{root_folder_id}"

    for path in sorted(local_run_folder.rglob("*")):
        if path.is_dir():
            continue
        drive_filename = _build_drive_filename(local_run_folder, path, run_label)
        try:
            url = _upload_file(service, path, drive_filename, root_folder_id)
            if url:
                uploaded += 1
                last_url = url
            else:
                print(f"[google_drive_uploader] WARNING: upload returned no ID for {path.name}")
                errors += 1
        except Exception as exc:
            print(f"[google_drive_uploader] WARNING: could not upload {path.name} — {exc}")
            errors += 1

    print(
        f"[google_drive_uploader] Upload complete: {uploaded} file(s) uploaded"
        + (f", {errors} error(s)" if errors else ".")
    )

    if uploaded == 0:
        print("[google_drive_uploader] No files were uploaded successfully — not returning Drive link.")
        return None

    # Return a link to the folder itself (same pattern Bob uses for folder links)
    return f"https://drive.google.com/drive/folders/{root_folder_id}"
