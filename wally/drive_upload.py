from __future__ import annotations

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
    from shared import gdrive

    creds = gdrive.oauth_credentials()
    if creds is not None:
        from google.auth.transport.requests import Request

        try:
            creds.refresh(Request())
            return gdrive.build_service(creds)
        except Exception as e:
            raise RuntimeError(
                f"OAuth refresh token failed: {e}. "
                "The GDRIVE_REFRESH_TOKEN has likely expired. "
                "Regenerate it via Google OAuth consent screen and update the GitHub secret."
            ) from e

    raise RuntimeError(
        "No Drive credentials found. Set GDRIVE_CLIENT_ID + GDRIVE_CLIENT_SECRET + "
        "GDRIVE_REFRESH_TOKEN. (Service account cannot upload to personal Gmail Drive.)"
    )


def upload_to_drive(local_path: Path, ticker: str, folder_id: str) -> dict:
    """Upload (or replace) a value-chart xlsx to Google Drive.

    The filename in Drive is the ticker stripped of exchange suffix and file
    extension — e.g. ``NHC.AX`` → ``NHC``.

    Returns a dict:
      {"ok": True,  "url": "...", "file_id": "...", "filename": "..."}
      {"ok": False, "error": "...", "filename": "..."}
    """
    from shared import gdrive

    drive_filename = ticker.replace(".AX", "").replace(".ax", "").upper()

    print(f"[drive] upload_to_drive: {local_path.name} → '{drive_filename}' in folder {folder_id}", flush=True)

    try:
        print("[drive] building service client...", flush=True)
        service = _drive_service()
        print("[drive] service client ready", flush=True)

        file_id = gdrive.upload_or_replace(
            service, local_path, drive_filename, folder_id, _XLSX_MIME,
            log=lambda m: print(m, flush=True),
        )
        url = gdrive.file_view_url(file_id)
        print(f"[drive] upload complete: {url}", flush=True)
        return {"ok": True, "url": url, "file_id": file_id, "filename": drive_filename}

    except Exception as exc:
        print(f"[drive] upload failed for '{drive_filename}': {exc}", flush=True)
        return {"ok": False, "error": str(exc), "filename": drive_filename}


def upload_or_replace_xlsx(local_path: Path, drive_name: str, folder_id: Optional[str] = None) -> str:
    """Upload or replace *local_path* as *drive_name*; returns the view URL.

    Used by wally/spreadsheet.py and by Sally's value-chart upload
    (sunday-sally/src/main.py). Raises on failure."""
    import mimetypes

    from shared import gdrive

    mime, _ = mimetypes.guess_type(str(local_path))
    file_id = gdrive.upload_or_replace(
        _drive_service(), local_path, drive_name, folder_id, mime or _XLSX_MIME,
    )
    return gdrive.file_view_url(file_id)
