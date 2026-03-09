from __future__ import annotations

import json
import mimetypes
import os
import re
from pathlib import Path


def _log(msg: str) -> None:
    print(f"[google_drive_uploader] {msg}", flush=True)


def _drive_service() -> tuple:
    """
    Build a Drive API service client for the service account.
    Uses the full `drive` scope — `drive.file` is too restrictive for
    folders created by a human user and shared with the service account.

    Returns (service | None, error_message | None).
    """
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        return None, "GDRIVE_SERVICE_ACCOUNT_JSON secret is not set"

    try:
        info = json.loads(sa_json)
    except json.JSONDecodeError as exc:
        return None, f"GDRIVE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}"

    sa_email = info.get("client_email", "<unknown>")
    _log(f"Service account: {sa_email}")

    try:
        creds = Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/drive"]
        )
        service = build("drive", "v3", credentials=creds)
        return service, None
    except Exception as exc:
        return None, f"Failed to build Drive service: {exc}"


def _build_drive_filename(run_folder_root: Path, file_path: Path, run_label: str) -> str:
    """Flatten a nested path into a flat Drive filename using __ separators.

    Example:
        root  = /data/outputs/2026/2026-03-10 Weekly Review
        path  = .../2026-03-10 Weekly Review/BHP/memo.md
        label = SundaySally_2026-03-10
        →  SundaySally_2026-03-10__BHP__memo.md
    """
    rel = file_path.relative_to(run_folder_root)
    safe_parts = [re.sub(r"[^\w.\-]", "_", part) for part in rel.parts]
    return run_label + "__" + "__".join(safe_parts)


def _upload_file(service, local_path: Path, drive_filename: str, folder_id: str) -> str:
    """Upload one file. Returns the Drive view URL or raises on failure."""
    from googleapiclient.http import MediaFileUpload

    mime, _ = mimetypes.guess_type(str(local_path))
    if not mime:
        mime = "application/octet-stream"

    file_metadata = {"name": drive_filename, "parents": [folder_id]}
    media = MediaFileUpload(str(local_path), mimetype=mime, resumable=False)
    created = (
        service.files()
        .create(
            body=file_metadata,
            media_body=media,
            fields="id",
            supportsAllDrives=True,  # works for both My Drive and Shared Drives
        )
        .execute()
    )
    file_id = created.get("id") or ""
    if not file_id:
        raise RuntimeError("Drive API returned no file ID")
    return f"https://drive.google.com/file/d/{file_id}/view"


def upload_run_folder(
    local_run_folder: Path,
    drive_folder_components: list[str],
    root_folder_id: str | None = None,
) -> tuple[str | None, str]:
    """
    Upload every file in local_run_folder flat into the Google Drive folder
    identified by root_folder_id.

    Files are named with a run-date prefix so multiple weeks never collide:
        SundaySally_2026-03-10__BHP__memo.md

    Returns (drive_folder_url | None, status_message).
    The status_message is always populated and is safe to put in the email
    so you can see what happened without opening CI logs.
    Never raises — Drive upload is non-fatal.
    """
    if not root_folder_id:
        msg = "Drive upload skipped — GDRIVE_FOLDER_ID secret is not set."
        _log(msg)
        return None, msg

    service, err = _drive_service()
    if service is None:
        msg = f"Drive upload skipped — {err}."
        _log(msg)
        return None, msg

    # Extract date for run label e.g. "SundaySally_2026-03-10"
    run_label = "SundaySally"
    if drive_folder_components:
        date_match = re.search(r"\d{4}-\d{2}-\d{2}", drive_folder_components[-1])
        if date_match:
            run_label = f"SundaySally_{date_match.group()}"

    _log(f"Target folder ID: {root_folder_id}")

    files_to_upload = sorted(p for p in local_run_folder.rglob("*") if p.is_file())
    if not files_to_upload:
        msg = "Drive upload skipped — output folder is empty."
        _log(msg)
        return None, msg

    _log(f"Uploading {len(files_to_upload)} file(s)…")

    uploaded = 0
    errors = 0
    first_error: str = ""

    for path in files_to_upload:
        drive_filename = _build_drive_filename(local_run_folder, path, run_label)
        try:
            _upload_file(service, path, drive_filename, root_folder_id)
            _log(f"  OK  {drive_filename}")
            uploaded += 1
        except Exception as exc:
            err_str = str(exc)
            _log(f"  FAIL {path.name} — {err_str}")
            if not first_error:
                first_error = err_str
            errors += 1

    folder_url = f"https://drive.google.com/drive/folders/{root_folder_id}"

    if uploaded == 0:
        hint = ""
        if "403" in first_error or "forbidden" in first_error.lower():
            hint = (
                " Hint: share the Drive folder with the service account email "
                "shown above and give it Editor access."
            )
        elif "404" in first_error or "not found" in first_error.lower():
            hint = " Hint: check that GDRIVE_FOLDER_ID is the correct folder ID."
        msg = (
            f"Drive upload FAILED — 0/{len(files_to_upload)} files uploaded."
            f" First error: {first_error}.{hint}"
        )
        _log(msg)
        return None, msg

    msg = (
        f"Drive upload OK — {uploaded}/{len(files_to_upload)} file(s) → {folder_url}"
        + (f" ({errors} error(s) also logged above)." if errors else ".")
    )
    _log(msg)
    return folder_url, msg
