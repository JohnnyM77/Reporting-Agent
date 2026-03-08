from __future__ import annotations

import json
import os
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_drive_filename(run_folder_root: Path, file_path: Path, run_label: str) -> str:
    """
    Build a flat Drive filename by joining the run_label with the relative
    path parts using double-underscore separators.

    Example:
        root  = /tmp/run
        path  = /tmp/run/ABC/source_docs/announcement_index.json
        label = SundaySally_2026-01-04
        → 'SundaySally_2026-01-04__ABC__source_docs__announcement_index.json'
    """
    rel = file_path.relative_to(run_folder_root)
    return run_label + "__" + "__".join(rel.parts)


def _drive_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build

    raw = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return None
    creds = Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)


def _ensure_folder(service, name: str, parent_id: str | None) -> str:
    """Return the Drive folder ID for `name` under `parent_id`, creating it if needed."""
    clauses = [
        f"name='{name}'",
        "mimeType='application/vnd.google-apps.folder'",
        "trashed=false",
    ]
    if parent_id:
        clauses.append(f"'{parent_id}' in parents")
    found = (
        service.files()
        .list(q=" and ".join(clauses), fields="files(id,name)", pageSize=5)
        .execute()
        .get("files", [])
    )
    if found:
        return found[0]["id"]

    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        body["parents"] = [parent_id]
    created = service.files().create(body=body, fields="id").execute()
    return created["id"]


def _upsert_file(service, local_path: Path, file_name: str, parent_folder_id: str) -> str:
    """
    Upload a file to Drive, replacing the existing file with the same name
    in the same folder if one already exists (upsert semantics).

    Returns the Drive file ID.
    """
    from googleapiclient.http import MediaFileUpload

    q = " and ".join(
        [
            f"name='{file_name}'",
            f"'{parent_folder_id}' in parents",
            "trashed=false",
        ]
    )
    existing = (
        service.files()
        .list(q=q, fields="files(id)", pageSize=5)
        .execute()
        .get("files", [])
    )

    media = MediaFileUpload(str(local_path), resumable=False)
    if existing:
        file_id = existing[0]["id"]
        service.files().update(fileId=file_id, media_body=media, fields="id").execute()
        return file_id
    else:
        body = {"name": file_name, "parents": [parent_folder_id]}
        result = service.files().create(body=body, media_body=media, fields="id").execute()
        return result["id"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def upload_run_folder(
    local_run_folder: Path,
    drive_folder_components: list[str],
    root_folder_id: str | None = None,
) -> str | None:
    """
    Upload every file under `local_run_folder` into a matching Drive folder
    hierarchy defined by `drive_folder_components`, optionally rooted at
    `root_folder_id`.

    Files that already exist (same name, same parent folder) are updated
    in-place so re-runs never accumulate duplicates.

    Returns the Drive folder URL on success, or None if credentials are
    missing or the upload fails entirely.
    """
    service = _drive_service()
    if service is None:
        print("[google_drive_uploader] GDRIVE_SERVICE_ACCOUNT_JSON not set — skipping upload.")
        return None

    # Walk down the target folder path, creating folders as needed
    parent_id = root_folder_id
    for component in drive_folder_components:
        parent_id = _ensure_folder(service, component, parent_id)

    uploaded = 0
    errors = 0
    for path in sorted(local_run_folder.rglob("*")):
        if path.is_dir():
            continue

        rel = path.relative_to(local_run_folder)

        # Ensure any sub-folder hierarchy within the run folder also exists in Drive
        folder_parent = parent_id
        for part in rel.parts[:-1]:
            folder_parent = _ensure_folder(service, part, folder_parent)

        try:
            _upsert_file(service, path, rel.name, folder_parent)
            uploaded += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[google_drive_uploader] WARNING: could not upload {rel} — {exc}")
            errors += 1

    print(
        f"[google_drive_uploader] Upload complete: {uploaded} file(s) uploaded"
        + (f", {errors} error(s) — check logs above" if errors else ".")
    )
    return f"https://drive.google.com/drive/folders/{parent_id}"
