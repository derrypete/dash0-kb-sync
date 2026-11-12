"""Native Google Docs in-place sync engine using Google Drive API v3."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
import google_auth_httplib2
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload
import httplib2

from dash0_kb_sync.compiler import CompiledBundle
from dash0_kb_sync.config import CHANGELOG_BUCKET, SyncSettings, utc_now_iso
from dash0_kb_sync.db import DriveFileRecord, StateDB

logger = logging.getLogger(__name__)

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
MARKDOWN_MIME = "text/markdown"


class DriveAuthError(RuntimeError):
    """Raised when Google Drive OAuth credentials or tokens are missing or invalid."""


@dataclass
class DriveSyncAction:
    """Outcome of syncing a single compiled Markdown bundle to Google Drive."""

    doc_key: str
    action: str  # 'created' | 'updated' | 'unchanged'
    file_id: str
    web_view_link: str


def _save_token_secure(token_path: Path, creds: Credentials) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass


def load_or_authenticate_credentials(
    settings: SyncSettings,
    *,
    interactive: bool = False,
) -> Credentials:
    """Load persisted `token.json` (refreshing if expired) or run browser OAuth if `interactive=True`."""
    creds: Credentials | None = None
    if settings.token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(
                str(settings.token_path), settings.drive_scopes
            )
        except Exception as exc:
            logger.warning("Failed to parse existing token.json (%s)", exc)
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token_secure(settings.token_path, creds)
            logger.info("Refreshed Google Drive OAuth access token.")
            return creds
        except RefreshError as exc:
            logger.warning("Google OAuth refresh token expired/revoked: %s", exc)
            if not interactive:
                raise DriveAuthError(
                    "Google Drive OAuth token expired or was revoked. "
                    "Re-authenticate interactively with: `python -m dash0_kb_sync auth`"
                ) from exc

    if not interactive:
        raise DriveAuthError(
            f"No valid Google Drive token found at {settings.token_path}. "
            f"Place your Google OAuth Desktop Client JSON at {settings.credentials_path} "
            "and run `python -m dash0_kb_sync auth` once."
        )

    if not settings.credentials_path.exists():
        raise DriveAuthError(
            f"Missing OAuth client secrets file at {settings.credentials_path}.\n"
            "Download an OAuth 2.0 Desktop App client JSON from Google Cloud Console "
            "(APIs & Services -> Credentials) and save it to ./config/credentials.json."
        )

    flow = InstalledAppFlow.from_client_secrets_file(
        str(settings.credentials_path),
        scopes=settings.drive_scopes,
    )
    creds = flow.run_local_server(port=0, open_browser=True)
    _save_token_secure(settings.token_path, creds)
    return creds


class DriveManifest:
    """Dual-persisted (`drive_manifest.json` + SQLite) state mapping `doc_key -> fileId`."""

    def __init__(self, manifest_path: Path, db: StateDB) -> None:
        self.manifest_path = manifest_path
        self.db = db
        self.folder_id: str | None = db.get_meta("drive_folder_id")
        self.folder_url: str | None = db.get_meta("drive_folder_url")
        self.files: dict[str, DriveFileRecord] = db.get_drive_files()
        self._load_json_and_merge()

    def _load_json_and_merge(self) -> None:
        if not self.manifest_path.exists():
            return
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not parse %s: %s", self.manifest_path, exc)
            return

        if isinstance(raw, dict):
            if raw.get("folder_id") and not self.folder_id:
                self.folder_id = str(raw["folder_id"])
            if raw.get("folder_url") and not self.folder_url:
                self.folder_url = str(raw["folder_url"])

            raw_files = raw.get("files") or {}
            if isinstance(raw_files, dict):
                for doc_key, info in raw_files.items():
                    if doc_key not in self.files and isinstance(info, dict) and info.get("file_id"):
                        rec = DriveFileRecord(
                            doc_key=doc_key,
                            file_id=str(info["file_id"]),
                            web_view_link=str(info.get("web_view_link") or ""),
                            sha256=str(info.get("sha256") or ""),
                            char_count=int(info.get("char_count") or 0),
                            word_count=int(info.get("word_count") or 0),
                            page_count=int(info.get("page_count") or 0),
                            last_synced_at=str(info.get("last_synced_at") or ""),
                        )
                        self.files[doc_key] = rec
                        self.db.upsert_drive_file(rec)

    def save(self) -> None:
        if self.folder_id:
            self.db.set_meta("drive_folder_id", self.folder_id)
        if self.folder_url:
            self.db.set_meta("drive_folder_url", self.folder_url)

        payload = {
            "folder_name": "Dash0_Knowledge_Base",
            "folder_id": self.folder_id,
            "folder_url": self.folder_url,
            "updated_at": utc_now_iso(),
            "files": {k: asdict(v) for k, v in sorted(self.files.items())},
        }
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def record_file(self, rec: DriveFileRecord) -> None:
        self.files[rec.doc_key] = rec
        self.db.upsert_drive_file(rec)
        self.save()


RETRYABLE_HTTP_STATUSES = (403, 408, 429, 500, 502, 503, 504)
TRANSIENT_TRANSPORT_ERRORS = (TimeoutError, OSError, httplib2.HttpLib2Error)


def _is_retryable_http_error(exc: HttpError) -> bool:
    status = int(getattr(exc.resp, "status", 0) or 0)
    if status not in RETRYABLE_HTTP_STATUSES:
        return False
    if status == 403:
        # Do not retry permanent permission / disabled-API errors
        msg = str(exc).lower()
        if "insufficientpermissions" in msg or "accessnotconfigured" in msg:
            return False
    return True


class GoogleDriveSyncEngine:
    """Synchronizes compiled Markdown files into native Google Docs in-place."""

    def __init__(self, settings: SyncSettings, db: StateDB, *, interactive: bool = False) -> None:
        self.settings = settings
        self.db = db
        self.creds = load_or_authenticate_credentials(settings, interactive=interactive)
        self.service = self._build_drive_service()
        self.manifest = DriveManifest(settings.drive_manifest_path, db)

    def _build_drive_service(self) -> Any:
        """Build a Google Drive v3 client with a generous socket timeout for large Markdown conversions."""
        raw_http = httplib2.Http(timeout=self.settings.drive_http_timeout_sec)
        authed_http = google_auth_httplib2.AuthorizedHttp(self.creds, http=raw_http)
        return build("drive", "v3", http=authed_http, cache_discovery=False)

    def _reset_drive_service(self) -> None:
        """Discard any half-closed SSL sockets after a timeout or transport error."""
        self.service = self._build_drive_service()

    def _execute_with_backoff(
        self,
        request_factory: Callable[[], Any],
        *,
        description: str = "Drive API call",
        max_attempts: int = 5,
    ) -> dict[str, Any]:
        """Execute a Google Drive API request factory with transport reset & exponential backoff."""
        for attempt in range(1, max_attempts + 1):
            try:
                req = request_factory()
                return req.execute()
            except HttpError as exc:
                status = int(getattr(exc.resp, "status", 0) or 0)
                if _is_retryable_http_error(exc) and attempt < max_attempts:
                    wait_s = min(30.0, 2.0 ** attempt)
                    logger.warning(
                        "%s returned HTTP %s (attempt %d/%d), retrying in %.1fs",
                        description,
                        status,
                        attempt,
                        max_attempts,
                        wait_s,
                    )
                    self._reset_drive_service()
                    time.sleep(wait_s)
                    continue
                raise
            except TRANSIENT_TRANSPORT_ERRORS as exc:
                if attempt < max_attempts:
                    wait_s = min(30.0, 2.0 ** attempt)
                    logger.warning(
                        "%s encountered %s: %s (attempt %d/%d), resetting connection & retrying in %.1fs",
                        description,
                        type(exc).__name__,
                        exc,
                        attempt,
                        max_attempts,
                        wait_s,
                    )
                    self._reset_drive_service()
                    time.sleep(wait_s)
                    continue
                raise
        raise RuntimeError("Unreachable retry loop exit")

    def ensure_drive_folder(self) -> tuple[str, str]:
        """Verify or create the `Dash0_Knowledge_Base` folder in Google Drive."""
        if self.manifest.folder_id:
            try:
                existing = self._execute_with_backoff(
                    lambda: self.service.files().get(
                        fileId=self.manifest.folder_id,
                        fields="id, name, webViewLink, trashed",
                    ),
                    description=f"get folder {self.manifest.folder_id}",
                )
                if not existing.get("trashed"):
                    folder_id = str(existing["id"])
                    folder_url = str(
                        existing.get("webViewLink")
                        or f"https://drive.google.com/drive/folders/{folder_id}"
                    )
                    self.manifest.folder_id = folder_id
                    self.manifest.folder_url = folder_url
                    self.manifest.save()
                    return folder_id, folder_url
            except HttpError as exc:
                logger.warning("Stored Drive folder ID %s not accessible: %s", self.manifest.folder_id, exc)

        # Search for an existing non-trashed folder with the target name
        escaped_name = self.settings.drive_folder_name.replace("'", "\\'")
        query = (
            f"name = '{escaped_name}' and "
            f"mimeType = '{GOOGLE_FOLDER_MIME}' and "
            "trashed = false"
        )
        res = self._execute_with_backoff(
            lambda: self.service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name, webViewLink)",
                pageSize=5,
            ),
            description=f"list folder {self.settings.drive_folder_name}",
        )
        found = res.get("files") or []
        if found:
            folder_id = str(found[0]["id"])
            folder_url = str(
                found[0].get("webViewLink")
                or f"https://drive.google.com/drive/folders/{folder_id}"
            )
        else:
            created = self._execute_with_backoff(
                lambda: self.service.files().create(
                    body={
                        "name": self.settings.drive_folder_name,
                        "mimeType": GOOGLE_FOLDER_MIME,
                    },
                    fields="id, name, webViewLink",
                ),
                description=f"create folder {self.settings.drive_folder_name}",
            )
            folder_id = str(created["id"])
            folder_url = str(
                created.get("webViewLink")
                or f"https://drive.google.com/drive/folders/{folder_id}"
            )
            logger.info("Created Google Drive folder '%s' (%s)", self.settings.drive_folder_name, folder_id)

        self.manifest.folder_id = folder_id
        self.manifest.folder_url = folder_url
        self.manifest.save()
        return folder_id, folder_url

    def _find_existing_doc_in_folder(self, doc_key: str, folder_id: str) -> tuple[str, str] | None:
        """Check if a Google Doc named `doc_key` already exists inside `folder_id`."""
        escaped_key = doc_key.replace("'", "\\'")
        query = (
            f"name = '{escaped_key}' and "
            f"'{folder_id}' in parents and "
            f"mimeType = '{GOOGLE_DOC_MIME}' and "
            "trashed = false"
        )
        res = self._execute_with_backoff(
            lambda: self.service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name, webViewLink)",
                pageSize=5,
            ),
            description=f"lookup doc {doc_key}",
        )
        files = res.get("files") or []
        if not files:
            return None
        fid = str(files[0]["id"])
        link = str(
            files[0].get("webViewLink") or f"https://docs.google.com/document/d/{fid}/edit"
        )
        return fid, link

    def _update_existing_doc(self, bundle: CompiledBundle, file_id: str) -> DriveSyncAction:
        """Upload fresh Markdown to an existing Google Doc ID (`files().update`), preserving its URL."""

        def _make_update_req() -> Any:
            media = MediaFileUpload(
                str(bundle.file_path),
                mimetype=MARKDOWN_MIME,
                resumable=True,
            )
            return self.service.files().update(
                fileId=file_id,
                body={"name": bundle.doc_key},
                media_body=media,
                fields="id, name, webViewLink",
            )

        updated = self._execute_with_backoff(
            _make_update_req,
            description=f"update doc {bundle.doc_key} ({file_id})",
        )
        resolved_id = str(updated["id"])
        web_link = str(
            updated.get("webViewLink")
            or f"https://docs.google.com/document/d/{resolved_id}/edit"
        )
        rec = DriveFileRecord(
            doc_key=bundle.doc_key,
            file_id=resolved_id,
            web_view_link=web_link,
            sha256=bundle.sha256,
            char_count=bundle.char_count,
            word_count=bundle.word_count,
            page_count=bundle.page_count,
            last_synced_at=utc_now_iso(),
        )
        self.manifest.record_file(rec)
        return DriveSyncAction(
            doc_key=bundle.doc_key,
            action="updated",
            file_id=resolved_id,
            web_view_link=web_link,
        )

    def sync_bundle(self, bundle: CompiledBundle, folder_id: str) -> DriveSyncAction:
        """Create or update a native Google Doc in-place, preserving `fileId` across syncs."""
        existing = self.manifest.files.get(bundle.doc_key)

        # If a previously unsplit bucket split into `_Part_1`, reuse the base bucket's fileId in-place!
        if existing is None and bundle.doc_key == f"{bundle.bucket}_Part_1":
            base_rec = self.manifest.files.get(bundle.bucket)
            if base_rec is not None:
                existing = base_rec

        if existing is None:
            discovered = self._find_existing_doc_in_folder(bundle.doc_key, folder_id)
            if discovered is not None:
                fid, link = discovered
                existing = DriveFileRecord(
                    doc_key=bundle.doc_key,
                    file_id=fid,
                    web_view_link=link,
                    sha256="",
                    char_count=0,
                    word_count=0,
                    page_count=0,
                    last_synced_at="",
                )

        is_changelog = bundle.bucket == CHANGELOG_BUCKET

        if existing is not None:
            if not is_changelog:
                raw_file_sha = (
                    hashlib.sha256(bundle.file_path.read_bytes()).hexdigest()
                    if bundle.file_path.exists()
                    else ""
                )
                if existing.sha256 in (bundle.sha256, raw_file_sha) and existing.sha256:
                    if existing.sha256 != bundle.sha256:
                        # Migrate legacy raw-bytes hash to timestamp-invariant bundle hash
                        existing.sha256 = bundle.sha256
                        self.manifest.record_file(existing)
                    return DriveSyncAction(
                        doc_key=bundle.doc_key,
                        action="unchanged",
                        file_id=existing.file_id,
                        web_view_link=existing.web_view_link,
                    )

            try:
                return self._update_existing_doc(bundle, existing.file_id)
            except HttpError as exc:
                # Only fall through to creation if the user explicitly deleted the file (404)
                if getattr(exc.resp, "status", 0) != 404:
                    raise
                logger.warning(
                    "Existing Google Doc %s (%s) returned 404; creating replacement.",
                    bundle.doc_key,
                    existing.file_id,
                )

        # First-time creation of native Google Doc converted from Markdown.
        # If a large conversion times out on the client socket after Google's backend has
        # already created the document, we check the folder before retrying so we adopt
        # its fileId in-place rather than creating a duplicate Google Doc.
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                adopted = self._find_existing_doc_in_folder(bundle.doc_key, folder_id)
                if adopted is not None:
                    adopted_id, _ = adopted
                    logger.info(
                        "Adopted Google Doc %s (%s) created server-side during prior timeout; updating in-place.",
                        bundle.doc_key,
                        adopted_id,
                    )
                    act = self._update_existing_doc(bundle, adopted_id)
                    return DriveSyncAction(
                        doc_key=act.doc_key,
                        action="created",
                        file_id=act.file_id,
                        web_view_link=act.web_view_link,
                    )

            try:
                media = MediaFileUpload(
                    str(bundle.file_path),
                    mimetype=MARKDOWN_MIME,
                    resumable=True,
                )
                created = self.service.files().create(
                    body={
                        "name": bundle.doc_key,
                        "mimeType": GOOGLE_DOC_MIME,
                        "parents": [folder_id],
                    },
                    media_body=media,
                    fields="id, name, webViewLink",
                ).execute()
                break
            except HttpError as exc:
                if _is_retryable_http_error(exc) and attempt < max_attempts:
                    wait_s = min(30.0, 2.0 ** attempt)
                    logger.warning(
                        "create doc %s returned HTTP %s (attempt %d/%d), retrying in %.1fs",
                        bundle.doc_key,
                        getattr(exc.resp, "status", 0),
                        attempt,
                        max_attempts,
                        wait_s,
                    )
                    self._reset_drive_service()
                    time.sleep(wait_s)
                    continue
                raise
            except TRANSIENT_TRANSPORT_ERRORS as exc:
                if attempt < max_attempts:
                    wait_s = min(30.0, 2.0 ** attempt)
                    logger.warning(
                        "create doc %s timed out / transport error (%s: %s, attempt %d/%d), checking Drive & retrying in %.1fs",
                        bundle.doc_key,
                        type(exc).__name__,
                        exc,
                        attempt,
                        max_attempts,
                        wait_s,
                    )
                    self._reset_drive_service()
                    time.sleep(wait_s)
                    continue
                raise

        file_id = str(created["id"])
        web_link = str(
            created.get("webViewLink") or f"https://docs.google.com/document/d/{file_id}/edit"
        )
        rec = DriveFileRecord(
            doc_key=bundle.doc_key,
            file_id=file_id,
            web_view_link=web_link,
            sha256=bundle.sha256,
            char_count=bundle.char_count,
            word_count=bundle.word_count,
            page_count=bundle.page_count,
            last_synced_at=utc_now_iso(),
        )
        self.manifest.record_file(rec)
        return DriveSyncAction(
            doc_key=bundle.doc_key,
            action="created",
            file_id=file_id,
            web_view_link=web_link,
        )

    def sync_all(
        self,
        bundles: list[CompiledBundle],
        on_start: Callable[[CompiledBundle], None] | None = None,
        on_progress: Callable[[DriveSyncAction], None] | None = None,
    ) -> list[DriveSyncAction]:
        """Sync all compiled Markdown bundles in-place into the `Dash0_Knowledge_Base` Drive folder."""
        folder_id, _ = self.ensure_drive_folder()
        actions: list[DriveSyncAction] = []
        for bundle in bundles:
            if on_start:
                on_start(bundle)
            act = self.sync_bundle(bundle, folder_id)
            actions.append(act)
            if on_progress:
                on_progress(act)
        return actions
