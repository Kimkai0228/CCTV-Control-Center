"""Google Drive uploads and sharing driven entirely by database settings."""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from typing import Any, Callable

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from sqlalchemy.orm import Session

from .. import models
from ..settings_service import as_int, get_setting

SCOPES = ["https://www.googleapis.com/auth/drive"]
_lock = threading.Lock()
_cache: dict[str, Any] = {"fingerprint": None, "service": None}


class DriveError(RuntimeError):
    pass


class DriveService:
    def __init__(self, service):
        self.service = service

    # ------------------------------------------------------------------
    @staticmethod
    def from_settings(db: Session) -> "DriveService":
        raw = get_setting(db, "google.service_account_json")
        if not raw:
            raise DriveError("Google service account JSON is not configured")
        impersonate = get_setting(db, "google.impersonate_email").strip()
        return DriveService.from_credentials_json(raw, impersonate)

    # ------------------------------------------------------------------
    @staticmethod
    def from_credentials_json(raw: str, impersonate: str = "") -> "DriveService":
        """Build a client from raw service-account JSON.

        Used by remote agents, which receive credentials in the job payload
        instead of maintaining their own Google configuration.
        """
        if not raw:
            raise DriveError("Google service account JSON is not configured")
        try:
            info = json.loads(raw)
        except ValueError as exc:
            raise DriveError(f"Google credential JSON is invalid: {exc}") from exc
        if not isinstance(info, dict) or not info.get("client_email"):
            raise DriveError("Google credential JSON is missing client_email")
        fingerprint = hash((raw, impersonate))
        with _lock:
            if _cache["fingerprint"] == fingerprint and _cache["service"] is not None:
                return DriveService(_cache["service"])
        try:
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
            if impersonate:
                try:
                    creds = creds.with_subject(impersonate)
                except Exception:
                    # Keep the service account itself if delegation is not set up.
                    pass
            service = build("drive", "v3", credentials=creds, cache_discovery=False)
        except Exception as exc:
            raise DriveError(f"Could not authenticate with Google: {exc}") from exc
        with _lock:
            _cache["fingerprint"] = fingerprint
            _cache["service"] = service
        return DriveService(service)

    # ------------------------------------------------------------------
    def test(self) -> tuple[bool, str]:
        try:
            about = self.service.about().get(fields="user(displayName,emailAddress),storageQuota").execute()
            user = about.get("user", {})
            return True, f"Connected as {user.get('emailAddress') or user.get('displayName') or 'service account'}"
        except Exception as exc:
            return False, str(exc)

    def create_folder(self, name: str, parent_id: str) -> dict[str, Any]:
        metadata = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id:
            metadata["parents"] = [parent_id]
        return (
            self.service.files()
            .create(body=metadata, fields="id, webViewLink", supportsAllDrives=True)
            .execute()
        )

    def upload_file(
        self,
        path: str,
        parent_id: str,
        display_name: str | None = None,
        progress: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        media = MediaFileUpload(path, mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024)
        metadata: dict[str, Any] = {"name": display_name or path.split("/")[-1]}
        if parent_id:
            metadata["parents"] = [parent_id]
        request = self.service.files().create(
            body=metadata,
            media_body=media,
            fields="id, webViewLink, size",
            supportsAllDrives=True,
        )
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status and progress:
                progress(int(status.progress() * 100))
        return response

    def grant_access(self, file_id: str, email: str, days: int = 30, notify: bool = True) -> bool:
        permission = {"type": "user", "role": "reader", "emailAddress": email}
        permission_obj = None
        try:
            permission_obj = (
                self.service.permissions()
                .create(
                    fileId=file_id,
                    body=permission,
                    sendNotificationEmail=False,
                    supportsAllDrives=True,
                )
                .execute()
            )
        except Exception as exc:
            if "notify" in str(exc).lower() or "invalidsharingrequest" in str(exc).lower():
                permission_obj = (
                    self.service.permissions()
                    .create(
                        fileId=file_id,
                        body=permission,
                        sendNotificationEmail=notify,
                        supportsAllDrives=True,
                    )
                    .execute()
                )
            else:
                raise
        perm_id = permission_obj.get("id") if permission_obj else None
        if perm_id and days > 0:
            expiry = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
            try:
                self.service.permissions().update(
                    fileId=file_id,
                    permissionId=perm_id,
                    body={"role": "reader", "expirationTime": expiry.isoformat().replace("+00:00", "Z")},
                    supportsAllDrives=True,
                ).execute()
            except Exception:
                pass
        return True


def resolve_folder_id(db: Session, folder_key: str | None, campus: str | None = None) -> str:
    query = db.query(models.DriveFolder).filter(models.DriveFolder.enabled.is_(True))
    if folder_key:
        folder = query.filter(models.DriveFolder.key == folder_key).first()
        if folder:
            return folder.folder_id
    if campus:
        folder = query.filter(models.DriveFolder.campus == campus.lower()).first()
        if folder:
            return folder.folder_id
    return get_setting(db, "google.fallback_folder_id")


def upload_job_files(
    db: Session,
    files: list[dict[str, Any]],
    folder_name: str,
    folder_key: str | None,
    campus: str | None,
    progress: Callable[[int, str], None] | None = None,
    *,
    service: DriveService | None = None,
    parent_id: str | None = None,
    existing_folder_id: str | None = None,
    existing_folder_link: str | None = None,
) -> dict[str, Any]:
    """Upload a list of local files into an incident folder.

    ``service`` and ``parent_id`` let a remote agent upload with credentials
    supplied in its job payload rather than a local database.  When a job is
    retried, ``existing_folder_id`` reuses the folder created by the original
    run so files from the successful cameras stay together.
    """
    if service is None:
        if not get_setting(db, "google.upload_enabled") in {"1", "true", "True", "yes", "on"}:
            raise DriveError("Google Drive upload is disabled in settings")
        service = DriveService.from_settings(db)
    if existing_folder_id:
        folder_id = existing_folder_id
        folder_link = existing_folder_link or ""
    else:
        if parent_id is None:
            parent_id = resolve_folder_id(db, folder_key, campus)
        if not parent_id:
            raise DriveError("No destination Drive folder configured (choose a folder or set a fallback)")

        if progress:
            progress(0, "Creating Google Drive folder")
        folder = service.create_folder(folder_name, parent_id)
        folder_id = folder.get("id", "")
        folder_link = folder.get("webViewLink", "")
    uploaded: list[dict[str, Any]] = []
    total = max(1, len(files))

    for index, item in enumerate(files):
        path = item.get("path")
        if not path:
            continue
        display = item.get("filename") or path.split("/")[-1]

        def file_progress(pct: int, index=index):
            if progress:
                overall = int(((index + pct / 100) / total) * 100)
                progress(overall, f"Uploading {display} ({pct}%)")

        result = service.upload_file(path, folder_id, display, file_progress)
        uploaded.append(
            {
                "filename": display,
                "file_id": result.get("id"),
                "link": result.get("webViewLink"),
                "size": int(result.get("size") or 0),
            }
        )
        if progress:
            progress(int(((index + 1) / total) * 100), f"Uploaded {display}")

    return {
        "folder_id": folder_id,
        "folder_link": folder_link,
        "files": uploaded,
        "parent_id": parent_id,
    }


def validate_credentials_json(raw: str) -> tuple[bool, str]:
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        return False, f"Invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return False, "Credential JSON must be an object"
    missing = [key for key in ("type", "client_email", "private_key", "token_uri") if not payload.get(key)]
    if missing:
        return False, "Missing required fields: " + ", ".join(missing)
    if payload.get("type") != "service_account":
        return False, "Credential must be a service_account JSON key"
    return True, f"Valid service account for {payload.get('client_email')}"


def sanitize_drive_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "", name or "").strip()
    return re.sub(r"\s+", " ", cleaned)[:200] or "CCTV_Request"
