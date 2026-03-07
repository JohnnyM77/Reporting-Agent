from __future__ import annotations

import json
import os
from pathlib import Path


def _drive_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        return None

    info = json.loads(sa_json)
    # Reuse Bob's exact auth scope pattern.
    scopes = ["https://www.googleapis.com/auth/drive.file"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return build("drive", "v3", credentials=creds)


def _drive_safe_name(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_")


def _build_drive_filename(local_run_folder: Path, path: Path, run_label: str) -> str:
    rel = path.relative_to(local_run_folder)
    rel_flat = "__".join(rel.parts)
    return _drive_safe_name(f"{run_label}__{rel_flat}")


def upload_run_folder(local_run_folder: Path, folder_id: str, run_label: str) -> str | None:
    """Upload run files into one existing Drive folder using Bob's parent-folder pattern."""
    from googleapiclient.http import MediaFileUpload

    service = _drive_service()
    if service is None or not folder_id:
        return None

    for path in local_run_folder.rglob("*"):
        if path.is_dir():
            continue

        drive_name = _build_drive_filename(local_run_folder, path, run_label)
        media = MediaFileUpload(str(path), resumable=False)
        metadata = {"name": drive_name, "parents": [folder_id]}
        service.files().create(body=metadata, media_body=media, fields="id").execute()

    return f"https://drive.google.com/drive/folders/{folder_id}"
