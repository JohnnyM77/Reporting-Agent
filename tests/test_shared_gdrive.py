"""shared/gdrive.py and each agent's Drive auth policy on top of it.

The Google client libraries are faked in sys.modules; nothing touches the
network."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared import gdrive  # noqa: E402

OAUTH = {"GDRIVE_CLIENT_ID": "cid", "GDRIVE_CLIENT_SECRET": "sec", "GDRIVE_REFRESH_TOKEN": "tok"}
SA = {"GDRIVE_SERVICE_ACCOUNT_JSON": '{"client_email": "sa@x.iam.gserviceaccount.com"}'}
ALL_KEYS = list(OAUTH) + list(SA)


@pytest.fixture
def google(monkeypatch):
    """Fake google.oauth2 / googleapiclient modules; returns the mocks."""
    user_creds = mock.MagicMock(name="UserCredentials")
    sa_cls = mock.MagicMock(name="SACredentials")
    build = mock.MagicMock(return_value="service")
    media = mock.MagicMock(name="MediaFileUpload")
    request = mock.MagicMock(name="Request")
    mods = {
        "google.oauth2.credentials": types.SimpleNamespace(Credentials=user_creds),
        "google.oauth2.service_account": types.SimpleNamespace(Credentials=sa_cls),
        "googleapiclient.discovery": types.SimpleNamespace(build=build),
        "googleapiclient.http": types.SimpleNamespace(MediaFileUpload=media),
        "google.auth.transport.requests": types.SimpleNamespace(Request=request),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    for k in ALL_KEYS:
        monkeypatch.delenv(k, raising=False)
    return types.SimpleNamespace(user_creds=user_creds, sa_cls=sa_cls, build=build, media=media, request=request)


def test_oauth_credentials(google):
    assert gdrive.oauth_credentials(env={}) is None
    assert gdrive.oauth_credentials(env={**OAUTH, "GDRIVE_REFRESH_TOKEN": " "}) is None
    gdrive.oauth_credentials(env=OAUTH)
    kw = google.user_creds.call_args.kwargs
    assert kw["refresh_token"] == "tok" and kw["client_id"] == "cid" and kw["scopes"] == [gdrive.DRIVE_SCOPE]


def test_service_account_info():
    assert gdrive.service_account_info(env={}) is None
    assert gdrive.service_account_info(env=SA)["client_email"].startswith("sa@")
    with pytest.raises(ValueError):
        gdrive.service_account_info(env={"GDRIVE_SERVICE_ACCOUNT_JSON": "{not json"})


def test_build_service_cache_discovery_only_when_given(google):
    gdrive.build_service("creds")
    assert google.build.call_args.kwargs == {"credentials": "creds"}
    gdrive.build_service("creds", cache_discovery=False)
    assert google.build.call_args.kwargs == {"credentials": "creds", "cache_discovery": False}


def _files_service(found):
    svc = mock.MagicMock()
    files = svc.files.return_value
    files.list.return_value.execute.return_value = {"files": found}
    files.create.return_value.execute.return_value = {"id": "new-id"}
    files.update.return_value.execute.return_value = {"id": "upd-id"}
    return svc, files


def test_find_or_create_folder_escapes_and_creates():
    svc, files = _files_service([])
    assert gdrive.find_or_create_folder(svc, "O'Neil", "parent") == "new-id"
    q = files.list.call_args.kwargs["q"]
    assert "name='O\\'Neil'" in q and "'parent' in parents" in q and "trashed=false" in q
    assert files.create.call_args.kwargs["body"] == {"name": "O'Neil", "mimeType": gdrive.FOLDER_MIME, "parents": ["parent"]}

    svc, files = _files_service([{"id": "existing"}])
    assert gdrive.find_or_create_folder(svc, "X") == "existing"
    files.create.assert_not_called()


def test_upload_or_replace_updates_existing_else_creates(google, tmp_path):
    f = tmp_path / "NHC.xlsx"
    f.write_bytes(b"PK")
    svc, files = _files_service([{"id": "old"}])
    assert gdrive.upload_or_replace(svc, f, "NHC", "folder", "application/x") == "upd-id"
    assert files.update.call_args.kwargs["fileId"] == "old"
    assert files.list.call_args.kwargs["q"] == "name = 'NHC' and 'folder' in parents and trashed = false"

    svc, files = _files_service([])
    assert gdrive.upload_or_replace(svc, f, "NHC", None, "application/x") == "new-id"
    assert files.create.call_args.kwargs["body"] == {"name": "NHC"}


# --- per-agent policies --------------------------------------------------------

def test_wally_is_oauth_only_and_refreshes(google, monkeypatch):
    from wally import drive_upload

    with pytest.raises(RuntimeError, match="No Drive credentials"):
        drive_upload._drive_service()
    for k, v in {**SA}.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(RuntimeError, match="No Drive credentials"):  # a service account is not enough
        drive_upload._drive_service()
    for k, v in OAUTH.items():
        monkeypatch.setenv(k, v)
    assert drive_upload._drive_service() == "service"
    google.user_creds.return_value.refresh.assert_called_once()


def test_wally_upload_to_drive_names_file_by_bare_ticker(google, monkeypatch, tmp_path):
    from wally import drive_upload

    for k, v in OAUTH.items():
        monkeypatch.setenv(k, v)
    svc, files = _files_service([])
    google.build.return_value = svc
    f = tmp_path / "nhc.xlsx"
    f.write_bytes(b"PK")
    out = drive_upload.upload_to_drive(f, "NHC.AX", "folder")
    assert out == {"ok": True, "url": "https://drive.google.com/file/d/new-id/view", "file_id": "new-id", "filename": "NHC"}


def test_results_pack_uses_service_account_drive_file_scope(google, monkeypatch):
    from results_pack_agent import gdrive_uploader

    assert gdrive_uploader._drive_service() is None
    for k, v in SA.items():
        monkeypatch.setenv(k, v)
    assert gdrive_uploader._drive_service() == "service"
    assert google.sa_cls.from_service_account_info.call_args.kwargs["scopes"] == [gdrive.DRIVE_FILE_SCOPE]
    monkeypatch.setenv("GDRIVE_SERVICE_ACCOUNT_JSON", "{bad")
    assert gdrive_uploader._drive_service() is None


def test_bob_prefers_oauth_with_cache_discovery_off(google, monkeypatch):
    _pw = types.ModuleType("playwright_fetch")
    _pw.fetch_pdf_with_playwright = None
    sys.modules.setdefault("playwright_fetch", _pw)
    import agent

    for k, v in {**OAUTH, **SA}.items():
        monkeypatch.setenv(k, v)
    assert agent.drive_service() == "service"
    assert google.build.call_args.kwargs["cache_discovery"] is False
    google.sa_cls.from_service_account_info.assert_not_called()
