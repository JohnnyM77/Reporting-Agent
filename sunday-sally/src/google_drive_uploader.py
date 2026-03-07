from __future__ import annotations

import json
import os
from pathlib import Path


def _drive_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    raw = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds)


def _ensure_folder(service, name: str, parent_id: str | None) -> str:
    clauses = [f"name='{name}'", "mimeType='application/vnd.google-apps.folder'", "trashed=false"]
    if parent_id:
        clauses.append(f"'{parent_id}' in parents")
    found = service.files().list(q=" and ".join(clauses), fields="files(id,name)", pageSize=5).execute().get("files", [])
    if found:
        return found[0]["id"]

    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        body["parents"] = [parent_id]
    created = service.files().create(body=body, fields="id").execute()
    return created["id"]


def upload_run_folder(local_run_folder: Path, drive_folder_components: list[str], root_folder_id: str | None = None) -> str | None:
    from googleapiclient.http import MediaFileUpload

    service = _drive_service()
    if service is None:
        return None

    parent_id = root_folder_id
    for component in drive_folder_components:
        parent_id = _ensure_folder(service, component, parent_id)

    for path in local_run_folder.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(local_run_folder)
        folder_parent = parent_id
        for part in rel.parts[:-1]:
            folder_parent = _ensure_folder(service, part, folder_parent)

        media = MediaFileUpload(str(path), resumable=False)
        service.files().create(body={"name": rel.name, "parents": [folder_parent]}, media_body=media).execute()

    return f"https://drive.google.com/drive/folders/{parent_id}"
