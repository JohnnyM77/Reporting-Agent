from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
from typing import Optional


def _drive_service():
    """
    Build a Google Drive v3 service client.

    Preference order:
    1. OAuth2 refresh token  (GDRIVE_CLIENT_ID + GDRIVE_CLIENT_SECRET + GDRIVE_REFRESH_TOKEN)
       — files are owned by your personal Google account and count against YOUR quota.
       Use this for personal Gmail Drive folders.
    2. Service account JSON  (GDRIVE_SERVICE_ACCOUNT_JSON)
       — files are owned by the service account which has ZERO storage quota on personal
       Gmail drives. Only works with Google Workspace Shared Drives.
    """
    from googleapiclient.discovery import build

    client_id     = os.environ.get("GDRIVE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET", "").strip()
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN", "").strip()

    if client_id and client_secret and refresh_token:
        # ── OAuth2 user credentials (recommended for personal Gmail Drive) ──
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        # Force a refresh so we have a valid access token before the first call
        creds.refresh(Request())
        return build("drive", "v3", credentials=creds)

    # ── Fallback: service account (only works with Shared Drives / Workspace) ─
    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not sa_json:
        raise RuntimeError(
            "No Drive credentials found. Set GDRIVE_CLIENT_ID + GDRIVE_CLIENT_SECRET + "
            "GDRIVE_REFRESH_TOKEN (recommended) or GDRIVE_SERVICE_ACCOUNT_JSON."
        )
    from google.oauth2.service_account import Credentials as SACredentials
    info = json.loads(sa_json)
    creds = SACredentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
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

    found = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id,name)", pageSize=10)
        .execute()
        .get("files", [])
    )

    media = MediaFileUpload(str(local_path), mimetype=mime, resumable=False)

    if found:
        file_id = found[0]["id"]
        updated = service.files().update(fileId=file_id, media_body=media, fields="id").execute()
        file_id = updated.get("id", file_id)
    else:
        metadata: dict = {"name": drive_name}
        if folder_id:
            metadata["parents"] = [folder_id]
        created = service.files().create(body=metadata, media_body=media, fields="id").execute()
        file_id = created.get("id", "")

    if not file_id:
        raise RuntimeError("Google Drive upload returned no file id")
    return f"https://drive.google.com/file/d/{file_id}/view"
