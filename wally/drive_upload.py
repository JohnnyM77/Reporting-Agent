from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from typing import Optional


def _drive_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        raise RuntimeError("Missing GDRIVE_SERVICE_ACCOUNT_JSON")
    info = json.loads(sa_json)
    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return build("drive", "v3", credentials=creds)


def upload_or_replace_xlsx(local_path: Path, drive_name: str, folder_id: Optional[str] = None) -> str:
    """Upload (or replace if already exists) any file to Google Drive.

    Kept as ``upload_or_replace_xlsx`` for historical reasons; it now works for
    any file type by auto-detecting the MIME type from the file extension.
    """
    from googleapiclient.http import MediaFileUpload

    mime, _ = mimetypes.guess_type(str(local_path))
    if not mime:
        mime = "application/octet-stream"

    service = _drive_service()
    q_parts = [f"name = '{drive_name}'", "trashed = false"]
    if folder_id:
        q_parts.append(f"'{folder_id}' in parents")
    query = " and ".join(q_parts)

    found = service.files().list(q=query, spaces="drive", fields="files(id,name)", pageSize=10).execute().get("files", [])

    media = MediaFileUpload(str(local_path), mimetype=mime, resumable=False)

    if found:
        file_id = found[0]["id"]
        updated = service.files().update(fileId=file_id, media_body=media, fields="id").execute()
        file_id = updated.get("id", file_id)
    else:
        metadata = {"name": drive_name}
        if folder_id:
            metadata["parents"] = [folder_id]
        created = service.files().create(body=metadata, media_body=media, fields="id").execute()
        file_id = created.get("id", "")

    if not file_id:
        raise RuntimeError("Google Drive upload returned no file id")
    return f"https://drive.google.com/file/d/{file_id}/view"
