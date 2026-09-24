# results_pack_agent/gdrive_uploader.py
# Google Drive upload for the Results Pack Agent.
# Uploads the entire run folder into Drive under a structured path:
#   Earnings Reports/<TICKER>/<YYMMDD-TICKER-HY-Results-Pack>/

from __future__ import annotations

from pathlib import Path
from typing import Optional

from shared import gdrive

from .utils import log


def _drive_service():
    """Build a Drive API v3 service from the service account secret."""
    try:
        info = gdrive.service_account_info()
        if info is None:
            return None
        creds = gdrive.service_account_credentials(info, scopes=[gdrive.DRIVE_FILE_SCOPE])
        return gdrive.build_service(creds)
    except Exception as exc:
        log(f"[gdrive_uploader] Could not build Drive service: {exc}")
        return None


def _find_or_create_folder(
    service,
    name: str,
    parent_id: Optional[str] = None,
) -> Optional[str]:
    """Return the Drive folder ID for *name* under *parent_id*, creating it if needed."""
    try:
        return gdrive.find_or_create_folder(service, name, parent_id)
    except Exception as exc:
        log(f"[gdrive_uploader] Could not find/create folder '{name}': {exc}")
        return None


def _upload_file(
    service,
    local_path: Path,
    drive_name: str,
    folder_id: str,
) -> str:
    """Upload one file to Drive and return its share URL."""
    try:
        from googleapiclient.http import MediaFileUpload  # type: ignore[import]

        file_meta = {"name": drive_name, "parents": [folder_id]}
        media = MediaFileUpload(str(local_path), resumable=False)
        created = service.files().create(
            body=file_meta, media_body=media, fields="id"
        ).execute()
        file_id = created.get("id", "")
        if file_id:
            return gdrive.file_view_url(file_id)
    except Exception as exc:
        log(f"[gdrive_uploader] Upload failed for {local_path.name}: {exc}")
    return ""


def upload_results_pack(
    local_folder: Path,
    ticker: str,
    folder_name: str,
    root_folder_id: Optional[str] = None,
    dry_run: bool = False,
) -> Optional[str]:
    """Upload all files in *local_folder* to Google Drive.

    Drive folder structure::

        <root_folder_id>/
          Earnings Reports/
            <TICKER>/
              <folder_name>/   ← e.g. 260318-NHC-HY-Results-Pack
                *.pdf
                *.md
                *.json
                *.xlsx

    Returns the Drive folder URL for the run folder, or ``None`` if Drive
    is not configured or the upload fails.  Never raises.
    """
    if not root_folder_id:
        log("[gdrive_uploader] GDRIVE_FOLDER_ID not set — skipping Drive upload.")
        return None

    if dry_run:
        log(f"[gdrive_uploader] [DRY-RUN] Would upload {local_folder} to Drive.")
        return gdrive.folder_url(root_folder_id)

    service = _drive_service()
    if service is None:
        log("[gdrive_uploader] Drive service unavailable — skipping upload.")
        return None

    # Build nested folder: root → Earnings Reports → TICKER → folder_name
    er_id = _find_or_create_folder(service, "Earnings Reports", parent_id=root_folder_id)
    if not er_id:
        return None
    ticker_id = _find_or_create_folder(service, ticker, parent_id=er_id)
    if not ticker_id:
        return None
    run_id = _find_or_create_folder(service, folder_name, parent_id=ticker_id)
    if not run_id:
        return None

    uploaded = 0
    errors = 0
    for path in sorted(local_folder.rglob("*")):
        if path.is_dir():
            continue
        url = _upload_file(service, path, path.name, run_id)
        if url:
            uploaded += 1
            log(f"[gdrive_uploader] ✓ {path.name}")
        else:
            errors += 1

    log(
        f"[gdrive_uploader] Upload complete: {uploaded} file(s)"
        + (f", {errors} error(s)" if errors else ".")
    )
    return gdrive.folder_url(run_id)
