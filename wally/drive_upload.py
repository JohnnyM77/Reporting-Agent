from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


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
        creds.refresh(Request())
        return build("drive", "v3", credentials=creds)

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


def upload_to_drive(local_path: Path, ticker: str, folder_id: str) -> dict:
    """Upload (or replace) a value-chart xlsx to Google Drive.

    The filename in Drive is the ticker stripped of exchange suffix and file
    extension — e.g. ``NHC.AX`` → ``NHC``.

    Returns a dict:
      {"ok": True,  "url": "...", "file_id": "...", "filename": "..."}
      {"ok": False, "error": "...", "filename": "..."}
    """
    from googleapiclient.http import MediaFileUpload

    drive_filename = ticker.replace(".AX", "").replace(".ax", "").upper()
    safe_name = drive_filename.replace("'", "\\'")

    print(f"[drive] upload_to_drive: {local_path.name} → '{drive_filename}' in folder {folder_id}", flush=True)

    try:
        print("[drive] building service client...", flush=True)
        service = _drive_service()
        print("[drive] service client ready", flush=True)

        query = f"name = '{safe_name}' and '{folder_id}' in parents and trashed = false"
        print(f"[drive] searching: {query}", flush=True)
        found = (
            service.files()
            .list(q=query, spaces="drive", fields="files(id,name)", pageSize=10)
            .execute()
            .get("files", [])
        )
        print(f"[drive] found {len(found)} existing file(s)", flush=True)

        media = MediaFileUpload(str(local_path), mimetype=_XLSX_MIME, resumable=False)

        if found:
            file_id = found[0]["id"]
            print(f"[drive] updating existing file id={file_id}", flush=True)
            updated = (
                service.files()
                .update(fileId=file_id, media_body=media, fields="id")
                .execute()
            )
            file_id = updated.get("id", file_id)
        else:
            metadata: dict = {"name": drive_filename, "parents": [folder_id]}
            print(f"[drive] creating new file '{drive_filename}'", flush=True)
            created = (
                service.files()
                .create(body=metadata, media_body=media, fields="id")
                .execute()
            )
            file_id = created.get("id", "")

        if not file_id:
            raise RuntimeError("Google Drive returned no file id")

        url = f"https://drive.google.com/file/d/{file_id}/view"
        print(f"[drive] upload complete: {url}", flush=True)
        return {"ok": True, "url": url, "file_id": file_id, "filename": drive_filename}

    except Exception as exc:
        print(f"[drive] upload failed for '{drive_filename}': {exc}", flush=True)
        return {"ok": False, "error": str(exc), "filename": drive_filename}


def upload_or_replace_xlsx(local_path: Path, drive_name: str, folder_id: Optional[str] = None) -> str:
    """Compatibility shim — prefer upload_to_drive for new callers."""
    import mimetypes
    from googleapiclient.http import MediaFileUpload

    safe_name = drive_name.replace("'", "\\'")
    service = _drive_service()
    q_parts = [f"name = '{safe_name}'", "trashed = false"]
    if folder_id:
        q_parts.append(f"'{folder_id}' in parents")
    query = " and ".join(q_parts)

    found = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id,name)", pageSize=10)
        .execute()
        .get("files", [])
    )

    mime, _ = mimetypes.guess_type(str(local_path))
    if not mime:
        mime = _XLSX_MIME

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
