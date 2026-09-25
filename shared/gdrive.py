# shared/gdrive.py
#
# Google Drive plumbing: credentials from env, service construction, query
# escaping, find-or-create folder, find-then-update-or-create a file.
#
# Drive is not used operationally right now, but the code paths are kept.
# Each agent keeps its own auth *policy*, because the policies differ for
# real reasons:
#
#   Bob (agent.py)        OAuth preferred, service account fallback + warning
#   Wally / Sally charts  OAuth only (wally/drive_upload.py): a service account
#                         has no storage quota in a personal My Drive
#   Results Pack          service account, drive.file scope
#   Sally run folder      service account, drive scope
#
# ...and each agent keeps its own folder layout and filenames. This module
# never decides what gets uploaded, or where.
#
# The Google client libraries are imported inside functions, so an agent
# that never touches Drive doesn't need them installed.

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
FOLDER_MIME = "application/vnd.google-apps.folder"
TOKEN_URI = "https://oauth2.googleapis.com/token"


def _env(env: Optional[Mapping[str, str]], name: str) -> str:
    e = os.environ if env is None else env
    return (e.get(name) or "").strip()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def oauth_configured(env: Optional[Mapping[str, str]] = None) -> bool:
    """True when GDRIVE_CLIENT_ID, _CLIENT_SECRET and _REFRESH_TOKEN are all set."""
    return all(_env(env, n) for n in ("GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN"))


def oauth_credentials(env: Optional[Mapping[str, str]] = None, scopes: Sequence[str] = (DRIVE_SCOPE,)):
    """OAuth2 user credentials from GDRIVE_CLIENT_ID/SECRET/REFRESH_TOKEN, or
    None if any is missing. Files created with these are owned by the human
    user and use their quota, so plain My Drive folders work."""
    if not oauth_configured(env):
        return None
    from google.oauth2.credentials import Credentials

    return Credentials(
        token=None,
        refresh_token=_env(env, "GDRIVE_REFRESH_TOKEN"),
        token_uri=TOKEN_URI,
        client_id=_env(env, "GDRIVE_CLIENT_ID"),
        client_secret=_env(env, "GDRIVE_CLIENT_SECRET"),
        scopes=list(scopes),
    )


def service_account_info(env: Optional[Mapping[str, str]] = None) -> Optional[dict]:
    """GDRIVE_SERVICE_ACCOUNT_JSON parsed, or None if unset. Raises ValueError
    (json.JSONDecodeError) if it is set but isn't valid JSON."""
    raw = _env(env, "GDRIVE_SERVICE_ACCOUNT_JSON")
    return json.loads(raw) if raw else None


def service_account_credentials(info: dict, scopes: Sequence[str] = (DRIVE_SCOPE,)):
    from google.oauth2.service_account import Credentials

    return Credentials.from_service_account_info(info, scopes=list(scopes))


def build_service(credentials: Any, cache_discovery: Optional[bool] = None):
    """The Drive v3 service. *cache_discovery* is passed only when given."""
    from googleapiclient.discovery import build

    kwargs = {} if cache_discovery is None else {"cache_discovery": cache_discovery}
    return build("drive", "v3", credentials=credentials, **kwargs)


# ---------------------------------------------------------------------------
# Files and folders
# ---------------------------------------------------------------------------

def escape_query_value(value: str) -> str:
    """Escape a value for a Drive ``q`` string literal."""
    return value.replace("'", "\\'")


def file_view_url(file_id: str) -> str:
    return f"https://drive.google.com/file/d/{file_id}/view"


def folder_url(folder_id: str) -> str:
    return f"https://drive.google.com/drive/folders/{folder_id}"


def find_or_create_folder(service: Any, name: str, parent_id: Optional[str] = None) -> Optional[str]:
    """The id of folder *name* under *parent_id*, creating it if missing.
    Raises on API errors; the caller decides whether that is fatal."""
    query = f"name='{escape_query_value(name)}' and mimeType='{FOLDER_MIME}' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    files = service.files().list(q=query, fields="files(id,name)").execute().get("files", [])
    if files:
        return files[0]["id"]
    meta: dict = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        meta["parents"] = [parent_id]
    return service.files().create(body=meta, fields="id").execute().get("id")


def upload_or_replace(
    service: Any,
    local_path: Path,
    drive_name: str,
    folder_id: Optional[str],
    mimetype: str,
    log=None,
) -> str:
    """Update the file named *drive_name* in *folder_id* if one exists, else
    create it. Returns the new/updated file id. Raises on failure."""
    from googleapiclient.http import MediaFileUpload

    q_parts = [f"name = '{escape_query_value(drive_name)}'"]
    if folder_id:
        q_parts.append(f"'{folder_id}' in parents")
    q_parts.append("trashed = false")
    query = " and ".join(q_parts)
    if log:
        log(f"[drive] searching: {query}")
    found = (
        service.files()
        .list(q=query, spaces="drive", fields="files(id,name)", pageSize=10)
        .execute()
        .get("files", [])
    )
    if log:
        log(f"[drive] found {len(found)} existing file(s)")

    media = MediaFileUpload(str(local_path), mimetype=mimetype, resumable=False)
    if found:
        file_id = found[0]["id"]
        if log:
            log(f"[drive] updating existing file id={file_id}")
        updated = service.files().update(fileId=file_id, media_body=media, fields="id").execute()
        file_id = updated.get("id", file_id)
    else:
        metadata: dict = {"name": drive_name}
        if folder_id:
            metadata["parents"] = [folder_id]
        if log:
            log(f"[drive] creating new file '{drive_name}'")
        created = service.files().create(body=metadata, media_body=media, fields="id").execute()
        file_id = created.get("id", "")
    if not file_id:
        raise RuntimeError("Google Drive returned no file id")
    return file_id
