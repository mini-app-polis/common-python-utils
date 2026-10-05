import io
import os
import random
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from google.oauth2 import service_account
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

import mini_app_polis.config as config
from mini_app_polis import logger as logger_mod

from ._auth import build_drive_service
from ._retry import RetryConfig, execute_with_retry
from .types import DriveFile

log = logger_mod.get_logger()
FOLDER_CACHE: dict[str, str] = {}

_DRIVE_ID_RE = re.compile(r"[-\w]{25,}")
_VERSION_RE = re.compile(r"_v(\d+)$")
# Same pattern as deejaytools-api's shareDriveFileWithUsers, so addresses the
# old service would have shared are exactly the ones this shares.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def clear_folder_cache() -> None:
    """Empty the process-wide folder-id cache used by ``ensure_folder``.

    ``ensure_folder`` caches ``parent/name -> folder id`` for the life of the
    process. If a cached folder is deleted or trashed out from under it, every
    later copy or upload into that folder fails against a stale id. Callers
    that see such a failure clear the cache so the next ``ensure_folder``
    looks the folder up (or recreates it) instead of reusing the dead id.
    """
    FOLDER_CACHE.clear()


def _escape_query_value(value: str) -> str:
    """Escape a value for a single-quoted Drive query string literal.

    Backslashes first, then quotes: escaping quotes first would double the
    backslash that the quote escape just introduced.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


@dataclass(frozen=True)
class DownloadedFile:
    """Represent an in-memory file download and its Drive metadata."""

    file_id: str
    name: str
    mime_type: str
    data: bytes


@dataclass(frozen=True)
class ShareResult:
    """Outcome of :meth:`DriveFacade.share_with_readers`.

    ``shared`` holds the cleaned addresses that were granted access, in the
    order they were attempted. ``failed`` pairs each address that could not
    be shared with the exception Drive raised for it. One bad address never
    stops the rest from being shared.
    """

    shared: list[str]
    failed: list[tuple[str, Exception]]


class DriveFacade:
    """Small, stable wrapper around the Google Drive API.

    External code should generally access this through `GoogleAPI.drive`.
    """

    @staticmethod
    def extract_drive_file_id(url_or_id: str) -> str | None:
        """Extract a Drive file id from a URL or return the id if already provided."""
        if not url_or_id:
            return None
        m = _DRIVE_ID_RE.search(url_or_id)
        return m.group(0) if m else None

    @classmethod
    def from_service_account_info(
        cls,
        info: Mapping[str, Any],
        *,
        scopes: Sequence[str],
        retry: RetryConfig | None = None,
    ) -> "DriveFacade":
        """A facade acting as the service account described by ``info``.

        ``info`` is the service-account JSON as a mapping (at least
        ``client_email``, ``private_key`` and ``token_uri``). ``scopes`` is
        required rather than defaulted: a caller that should only touch the
        files it created asks for ``drive.file`` and gets nothing wider.
        For callers whose credentials do not arrive as a JSON blob in
        ``GOOGLE_CREDENTIALS_JSON`` (see :class:`~mini_app_polis.google.GoogleAPI`).
        """
        creds = service_account.Credentials.from_service_account_info(
            dict(info), scopes=list(scopes)
        )
        return cls(build_drive_service(creds), retry=retry)

    def __init__(self, service: Any, retry: RetryConfig | None = None):
        self._service = service
        self._retry = retry or RetryConfig()

    @property
    def service(self) -> Any:
        """Return the underlying Google Drive service client."""
        return self._service

    def find_file_in_folder(
        self,
        parent_folder_id: str,
        *,
        name: str,
        mime_type: str | None = None,
    ) -> str | None:
        """Find a non-trashed file by exact name in a folder. Return file id or None."""

        safe_name = name.replace("'", "\\'")
        q = f"name = '{safe_name}' and '{parent_folder_id}' in parents and trashed = false"
        if mime_type:
            safe_mime = mime_type.replace("'", "\\'")
            q += f" and mimeType = '{safe_mime}'"

        resp = execute_with_retry(
            lambda: (
                self._service.files()
                .list(
                    q=q,
                    spaces="drive",
                    fields="files(id, name)",
                    pageSize=10,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            ),
            context=f"finding file '{name}' under {parent_folder_id}",
            retry=self._retry,
        )
        files = resp.get("files") or []
        return files[0]["id"] if files else None

    def find_files_by_app_property(
        self, parent_folder_id: str, *, key: str, value: str
    ) -> list[str]:
        """Return ids of non-trashed files in a folder tagged ``key=value``.

        Matches on Drive ``appProperties`` (as set by ``copy_file`` /
        ``upload_bytes``), which is the reliable way to find a file this app
        created: names can collide or be edited by people, an app property
        cannot be seen or changed outside this OAuth client. Returns every
        match across all pages, so a caller can detect and clean up
        duplicates left by an earlier partial failure.
        """

        q = (
            f"'{_escape_query_value(parent_folder_id)}' in parents"
            f" and appProperties has {{ key='{_escape_query_value(key)}'"
            f" and value='{_escape_query_value(value)}' }}"
            " and trashed = false"
        )

        def _call(page_token: str | None):
            return (
                self._service.files()
                .list(
                    q=q,
                    spaces="drive",
                    fields="nextPageToken, files(id)",
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )

        ids: list[str] = []
        page_token: str | None = None
        while True:
            resp = execute_with_retry(
                partial(_call, page_token),
                context=(
                    f"finding files with appProperty {key}={value} "
                    f"under {parent_folder_id}"
                ),
                retry=self._retry,
            )
            ids.extend(f["id"] for f in resp.get("files") or [] if f.get("id"))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids

    def get_file_name(self, file_id: str) -> str:
        """Return a Drive file's current name.

        Reads it from Drive rather than trusting a stored copy, because people
        rename files in the Drive UI and a cached name goes stale silently.
        """

        meta = execute_with_retry(
            lambda: (
                self._service.files()
                .get(fileId=file_id, fields="name", supportsAllDrives=True)
                .execute()
            ),
            context=f"getting name for file {file_id}",
            retry=self._retry,
        )
        return meta["name"]

    def share_with_readers(
        self, file_id: str, emails: Iterable[str | None]
    ) -> ShareResult:
        """Grant reader access on a file to each valid address, without emailing.

        Mirrors deejaytools-api's ``shareDriveFileWithUsers``: each address is
        trimmed and lowercased, empties and anything that does not look like
        an address are dropped, and duplicates are removed keeping first-seen
        order. ``sendNotificationEmail=False`` because the recipients are told
        through the app itself; Drive's own share email would be a second,
        unbranded message.

        Failures are per address and are collected, never raised: one address
        Drive rejects (no Google account, domain policy) must not stop the
        others from being shared. If nothing survives cleaning, no API call
        is made.
        """

        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in emails:
            email = (raw or "").strip().lower()
            if not email or not _EMAIL_RE.match(email) or email in seen:
                continue
            seen.add(email)
            cleaned.append(email)

        def _call(email: str):
            return (
                self._service.permissions()
                .create(
                    fileId=file_id,
                    body={"role": "reader", "type": "user", "emailAddress": email},
                    sendNotificationEmail=False,
                    supportsAllDrives=True,
                )
                .execute()
            )

        shared: list[str] = []
        failed: list[tuple[str, Exception]] = []
        for email in cleaned:
            try:
                execute_with_retry(
                    partial(_call, email),
                    context=f"sharing file {file_id} with {email}",
                    retry=self._retry,
                )
                shared.append(email)
            except Exception as e:
                log.warning("Failed to share file %s with %s: %s", file_id, email, e)
                failed.append((email, e))

        return ShareResult(shared=shared, failed=failed)

    def list_files(
        self,
        parent_id: str,
        *,
        mime_type: str | None = None,
        name_contains: str | None = None,
        trashed: bool = False,
        include_folders: bool = True,
    ) -> list[DriveFile]:
        """List Drive files in a folder with optional filtering."""
        query = f"'{parent_id}' in parents"
        if not include_folders:
            query += " and mimeType != 'application/vnd.google-apps.folder'"
        if mime_type:
            query += f" and mimeType = '{mime_type}'"
        if name_contains:
            safe_name_contains = name_contains.replace("'", "\\'")
            query += f" and name contains '{safe_name_contains}'"
        query += f" and trashed = {str(trashed).lower()}"

        def _call(page_token: str | None):
            params = {
                "q": query,
                "fields": "nextPageToken, files(id, name, mimeType, modifiedTime)",
                "pageToken": page_token,
                "orderBy": "modifiedTime desc",
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
                "spaces": "drive",
            }
            return self._service.files().list(**params).execute()

        files: list[DriveFile] = []
        page_token: str | None = None
        while True:
            result = execute_with_retry(
                partial(_call, page_token),
                context=f"listing files in folder {parent_id}",
                retry=self._retry,
            )
            for f in result.get("files", []):
                files.append(
                    DriveFile(
                        id=f.get("id", ""),
                        name=f.get("name", ""),
                        mime_type=f.get("mimeType"),
                        modified_time=f.get("modifiedTime"),
                    )
                )
            page_token = result.get("nextPageToken")
            if not page_token:
                break
        return files

    def ensure_folder(self, parent_id: str, name: str) -> str:
        """Return an existing child folder ID or create the folder if missing."""
        cache_key = f"{parent_id}/{name}"
        if cache_key in FOLDER_CACHE:
            return FOLDER_CACHE[cache_key]

        safe_name = name.replace("'", "\\'")
        query = (
            f"'{parent_id}' in parents and name = '{safe_name}' "
            "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )

        resp = execute_with_retry(
            lambda: (
                self._service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="files(id, name)",
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            ),
            context=f"finding folder '{name}' under {parent_id}",
            retry=self._retry,
        )
        folders = resp.get("files", [])
        if folders:
            folder_id = folders[0]["id"]
        else:
            folder_metadata = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            created = execute_with_retry(
                lambda: (
                    self._service.files()
                    .create(
                        body=folder_metadata,
                        fields="id",
                        supportsAllDrives=True,
                    )
                    .execute()
                ),
                context=f"creating folder '{name}' under {parent_id}",
                retry=self._retry,
            )
            folder_id = created["id"]

        FOLDER_CACHE[cache_key] = folder_id
        return folder_id

    def copy_file(
        self,
        file_id: str,
        *,
        parent_folder_id: str | None = None,
        name: str | None = None,
        max_retries: int = 5,
        app_properties: dict[str, str] | None = None,
    ) -> str:
        """Copy a Drive file.

        This method includes a small retry loop to handle Drive propagation delay where a
        just-created file may temporarily return a 404 "File not found" on copy.

        Backwards compatible with the previous signature; callers can still pass
        `parent_folder_id` and `name` as before.

        ``app_properties`` is stored on the copy as Drive ``appProperties``
        (private to this app's OAuth client). Tagging a copy with a stable key
        lets a caller find it again with :meth:`find_files_by_app_property`
        after a partial failure, rather than matching on a name that may
        collide or have been changed.
        """

        body: dict[str, Any] = {}
        if parent_folder_id:
            body["parents"] = [parent_folder_id]
        if name:
            body["name"] = name
        if app_properties is not None:
            body["appProperties"] = dict(app_properties)

        delay = 1.0
        for attempt in range(max_retries):
            try:
                log.info(
                    f"📄 Copying file {file_id} → '{name if name else '(same name)'}' (attempt {attempt + 1}/{max_retries})"
                )
                copied = execute_with_retry(
                    lambda: (
                        self._service.files()
                        .copy(
                            fileId=file_id,
                            body=body,
                            fields="id",
                            supportsAllDrives=True,
                        )
                        .execute()
                    ),
                    context=f"copying file {file_id}",
                    retry=self._retry,
                )
                new_file_id = copied.get("id")
                if not new_file_id:
                    raise RuntimeError(
                        f"Drive copy did not return an id for source_file_id={file_id}"
                    )
                log.info(f"✅ File copied successfully: {new_file_id}")
                return new_file_id

            except HttpError as e:
                status = getattr(e.resp, "status", None)
                if status == 404 and "not found" in str(e).lower():
                    wait = delay + random.uniform(0, 0.5)
                    log.warning(
                        f"⚠️ File {file_id} not yet visible, retrying in {wait:.1f}s (attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(wait)
                    delay *= 2
                    continue
                raise

        raise RuntimeError(
            f"Failed to copy file {file_id} after {max_retries} attempts"
        )

    def move_file(
        self, file_id: str, *, new_parent_id: str, remove_from_parents: bool = True
    ) -> None:
        """Move a Drive file into a target folder and optionally detach old parents."""
        file_meta = execute_with_retry(
            lambda: (
                self._service.files()
                .get(
                    fileId=file_id,
                    fields="parents",
                    supportsAllDrives=True,
                )
                .execute()
            ),
            context=f"getting parents for file {file_id}",
            retry=self._retry,
        )
        previous_parents = ",".join(file_meta.get("parents", []))

        kwargs = {
            "fileId": file_id,
            "addParents": new_parent_id,
            "fields": "id, parents",
            "supportsAllDrives": True,
        }
        if remove_from_parents and previous_parents:
            kwargs["removeParents"] = previous_parents

        execute_with_retry(
            lambda: self._service.files().update(**kwargs).execute(),
            context=f"moving file {file_id} to folder {new_parent_id}",
            retry=self._retry,
        )

    def download_file(self, file_id: str, destination_path: str) -> None:
        """Download a Drive file to a local filesystem path."""
        # Chunked downloads happen client-side, but the initial request creation can fail.
        request = execute_with_retry(
            lambda: self._service.files().get_media(fileId=file_id),
            context=f"creating download request for file {file_id}",
            retry=self._retry,
        )
        with io.FileIO(destination_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _status, done = downloader.next_chunk()

    def export_file(self, file_id: str, *, mime_type: str) -> bytes:
        """Export a Google Workspace file (Docs/Sheets/Slides) as bytes.

        Uses Drive `files.export`, which only works for Google-native formats.
        """

        data = execute_with_retry(
            lambda: (
                self._service.files()
                .export(fileId=file_id, mimeType=mime_type)
                .execute()
            ),
            context=f"exporting file {file_id} as {mime_type}",
            retry=self._retry,
        )
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        return str(data).encode("utf-8")

    def export_google_doc_as_text(self, file_id: str) -> str:
        """Export a Google Doc as plain text."""

        return self.export_file(file_id, mime_type="text/plain").decode(
            "utf-8", errors="replace"
        )

    def upload_file(
        self,
        filepath: str,
        *,
        parent_id: str,
        dest_name: str | None = None,
        mime_type: str | None = None,
    ) -> str:
        """Upload a local file to Drive and return the created file ID."""
        upload_name = dest_name or os.path.basename(filepath)
        file_metadata = {"name": upload_name, "parents": [parent_id]}
        media = MediaFileUpload(filepath, mimetype=mime_type, resumable=True)
        created = execute_with_retry(
            lambda: (
                self._service.files()
                .create(
                    body=file_metadata,
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            ),
            context=f"uploading file {upload_name} to folder {parent_id}",
            retry=self._retry,
        )
        return created["id"]

    def update_file(
        self,
        file_id: str,
        filepath: str,
        *,
        mime_type: str | None = None,
    ) -> None:
        """Upload a new version of an existing Drive file (in-place update)."""

        media = MediaFileUpload(filepath, mimetype=mime_type, resumable=True)

        execute_with_retry(
            lambda: (
                self._service.files()
                .update(
                    fileId=file_id,
                    media_body=media,
                    supportsAllDrives=True,
                )
                .execute()
            ),
            context=f"updating file {file_id} from {os.path.basename(filepath)}",
            retry=self._retry,
        )

    def rename_file(self, file_id: str, new_name: str) -> None:
        """Rename a Drive file."""

        execute_with_retry(
            lambda: (
                self._service.files()
                .update(
                    fileId=file_id,
                    body={"name": new_name},
                    supportsAllDrives=True,
                )
                .execute()
            ),
            context=f"renaming file {file_id} to {new_name}",
            retry=self._retry,
        )

    def upload_csv_as_google_sheet(
        self,
        filepath: str,
        *,
        parent_id: str,
        dest_name: str | None = None,
    ) -> str:
        """Upload a CSV and convert it to a Google Sheet in the destination folder."""

        upload_name = dest_name or os.path.basename(filepath)
        file_metadata = {
            "name": upload_name,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": [parent_id],
        }

        # Upload the CSV but request Drive to convert it to a spreadsheet.
        media = MediaFileUpload(filepath, mimetype="text/csv", resumable=True)

        created = execute_with_retry(
            lambda: (
                self._service.files()
                .create(
                    body=file_metadata,
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            ),
            context=f"uploading CSV as Google Sheet {upload_name} to folder {parent_id}",
            retry=self._retry,
        )

        return created["id"]

    def find_or_create_spreadsheet(self, *, parent_folder_id: str, name: str) -> str:
        """Find an existing spreadsheet by exact name in a folder, or create it."""

        mime = "application/vnd.google-apps.spreadsheet"
        found = self.find_file_in_folder(
            parent_folder_id,
            name=name,
            mime_type=mime,
        )
        if found:
            return found
        return self.create_spreadsheet_in_folder(name, parent_folder_id)

    def get_all_subfolders(self, parent_folder_id: str) -> list[DriveFile]:
        """Return all immediate subfolders of a parent folder (newest-first by modifiedTime)."""

        return self.list_files(
            parent_folder_id,
            mime_type="application/vnd.google-apps.folder",
            trashed=False,
            include_folders=True,
        )

    def get_files_in_folder(
        self, folder_id: str, *, include_folders: bool = True
    ) -> list[DriveFile]:
        """Return all immediate children in a folder (newest-first by modifiedTime)."""

        return self.list_files(
            folder_id,
            trashed=False,
            include_folders=include_folders,
        )

    def delete_file(self, file_id: str) -> None:
        """Permanently delete a file from Google Drive.

        Use with care. This should only be called after a successful end-to-end process.

        Permission note: this is the irreversible path and Drive gates it
        tightly. In My Drive it requires ownership of the file; on a
        shared drive it requires the *organizer* role (the Manager
        access level), which is strictly more than the Content manager
        level that can otherwise add, move and trash content. A caller
        without it fails on every attempt while its moves keep
        succeeding, which reads as an intermittent bug and is not one.
        Prefer :meth:`trash_file` unless the deletion genuinely must be
        unrecoverable.
        """

        execute_with_retry(
            lambda: (
                self._service.files()
                .delete(fileId=file_id, supportsAllDrives=True)
                .execute()
            ),
            context=f"deleting file {file_id}",
            retry=self._retry,
        )

    def trash_file(self, file_id: str) -> None:
        """Move a file to the trash. Recoverable, unlike :meth:`delete_file`.

        This is a metadata update rather than a deletion, so it is
        available to any caller that can edit the file — on a shared
        drive that includes the Content manager role, which cannot call
        :meth:`delete_file` at all.

        Trashed files stop appearing in listings (this module's
        ``list_files`` filters ``trashed = false``), and a shared drive
        empties its own trash automatically after 30 days, so storage is
        still reclaimed without anything being destroyed on the spot. A
        retention sweep with a bug therefore costs a restore rather than
        the data.
        """

        execute_with_retry(
            lambda: (
                self._service.files()
                .update(
                    fileId=file_id,
                    body={"trashed": True},
                    supportsAllDrives=True,
                )
                .execute()
            ),
            context=f"trashing file {file_id}",
            retry=self._retry,
        )

    def get_all_m3u_files(self, folder_id: str | None = None) -> list[dict]:
        """Return the VirtualDJ history ``.m3u`` files, newest first.

        ``folder_id`` is the history folder; it defaults to
        ``config.VDJ_HISTORY_FOLDER_ID``. Returns a list of
        ``{"id": str, "name": str}``. Only names ending in ``.m3u`` count, so
        ``.m3u8`` playlists and the like are left out. Sorted by name:
        VirtualDJ names each history file ``YYYY-MM-DD.m3u``, so the newest
        is first.

        Raises when there is no folder to list, and lets a failed listing
        raise. Both used to be logged and answered with ``[]``, which a
        caller could not tell from an empty folder: a Drive outage read as
        "no history yet", and a scheduled run reported an idle tick instead
        of a failure. An empty list now means the folder really is empty.
        """
        folder = self._history_folder(folder_id)
        files = [
            f
            for f in self.list_files(
                folder,
                name_contains=".m3u",
                trashed=False,
                include_folders=False,
            )
            if (f.name or "").lower().endswith(".m3u")
        ]
        files.sort(key=lambda f: f.name or "", reverse=True)
        return [{"id": f.id, "name": f.name} for f in files]

    def get_most_recent_m3u_file(self, folder_id: str | None = None) -> dict | None:
        """Return the newest history ``.m3u`` file, or None if there is none.

        Same folder, filtering, ordering and errors as
        :meth:`get_all_m3u_files`: None means the folder has no history
        file, never that it could not be read.
        """
        files = self.get_all_m3u_files(folder_id)
        return files[0] if files else None

    @staticmethod
    def _history_folder(folder_id: str | None) -> str:
        """The history folder to list: the argument, else the config value."""
        folder = folder_id or getattr(config, "VDJ_HISTORY_FOLDER_ID", None)
        if not folder:
            raise ValueError(
                "No VirtualDJ history folder: pass folder_id or set "
                "VDJ_HISTORY_FOLDER_ID."
            )
        return folder

    def download_m3u_file_data(
        self, file_id: str, *, encoding: str = "utf-8"
    ) -> list[str]:
        """Download a .m3u file and return its lines."""

        try:
            # The initial request creation can fail; download itself is chunked.
            request = execute_with_retry(
                lambda: self._service.files().get_media(fileId=file_id),
                context=f"creating download request for file {file_id}",
                retry=self._retry,
            )

            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _status, done = downloader.next_chunk()

            return fh.getvalue().decode(encoding).splitlines()
        except Exception as e:
            log.error(f"Failed to download .m3u file with ID {file_id}: {e}")
            return []

    def create_spreadsheet_in_folder(self, name: str, folder_id: str) -> str:
        """Create a Google Sheet in the given Drive folder and return its file ID."""
        body = {
            "name": name,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": [folder_id],
        }

        created = execute_with_retry(
            lambda: (
                self._service.files()
                .create(
                    body=body,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            ),
            context=f"creating spreadsheet '{name}' in folder {folder_id}",
            retry=self._retry,
        )

        return created["id"]

    def resolve_versioned_filename(
        self,
        *,
        parent_folder_id: str,
        desired_filename: str,
    ) -> tuple[str, int]:
        """Return (available_filename, version).

        Requires desired filename end with _vN before extension (e.g. Track_v1.mp3).
        Scans existing filenames in the destination folder and returns the next
        available version.
        """
        if "." in desired_filename:
            base, ext = desired_filename.rsplit(".", 1)
            ext = "." + ext
        else:
            base, ext = desired_filename, ""

        m = _VERSION_RE.search(base)
        if not m:
            raise ValueError(
                "desired_filename must include a _vN suffix before extension (e.g. _v1)"
            )

        base_root = base[: m.start()]
        start_version = int(m.group(1))
        base_root_lc = base_root.lower()
        ext_lc = ext.lower()

        # Fetch existing files with same prefix in the destination folder.
        safe_root = (base_root + "_v").replace("'", "\\'")
        q = (
            f"'{parent_folder_id}' in parents and trashed=false "
            f"and name contains '{safe_root}'"
        )

        resp = execute_with_retry(
            lambda: (
                self._service.files()
                .list(
                    q=q,
                    spaces="drive",
                    fields="files(name)",
                    pageSize=1000,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            ),
            context=f"resolving versioned filename in folder {parent_folder_id}",
            retry=self._retry,
        )

        used_versions: set[int] = set()
        for f in resp.get("files", []):
            name = f.get("name", "")
            name_lc = name.lower()

            if ext_lc and not name_lc.endswith(ext_lc):
                continue

            stem = name_lc[: -len(ext_lc)] if ext_lc else name_lc
            if not stem.startswith(base_root_lc):
                continue

            m2 = _VERSION_RE.search(stem)
            if m2:
                used_versions.add(int(m2.group(1)))

        v = start_version
        while v in used_versions:
            v += 1

        return f"{base_root}_v{v}{ext}", v

    def download_file_bytes(self, file_id: str) -> DownloadedFile:
        """Download a Drive file into memory and return (metadata + bytes)."""

        meta = execute_with_retry(
            lambda: (
                self._service.files()
                .get(
                    fileId=file_id,
                    fields="id,name,mimeType",
                    supportsAllDrives=True,
                )
                .execute()
            ),
            context=f"getting metadata for file {file_id}",
            retry=self._retry,
        )

        request = execute_with_retry(
            lambda: self._service.files().get_media(
                fileId=file_id, supportsAllDrives=True
            ),
            context=f"creating download request for file {file_id}",
            retry=self._retry,
        )

        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()

        return DownloadedFile(
            file_id=file_id,
            name=meta.get("name", ""),
            mime_type=meta.get("mimeType") or "application/octet-stream",
            data=fh.getvalue(),
        )

    def upload_bytes(
        self,
        *,
        parent_id: str,
        filename: str,
        content: bytes,
        mime_type: str,
        app_properties: dict[str, str] | None = None,
        resumable: bool = False,
    ) -> str:
        """Upload a new Drive file from bytes and return its file ID.

        ``app_properties``, when given, is stored on the new file as Drive
        ``appProperties`` so the file can be found again by key with
        :meth:`find_files_by_app_property` (see :meth:`copy_file`).

        ``resumable`` sends the content as a resumable upload, in chunks.
        Google documents the default single-request upload for files up to
        5 MB; pass ``resumable=True`` for anything that may be larger (audio,
        video). ``execute()`` drives every chunk, so the call still returns
        only once the file exists.
        """

        media = MediaIoBaseUpload(
            io.BytesIO(content), mimetype=mime_type, resumable=resumable
        )
        body: dict[str, Any] = {"name": filename, "parents": [parent_id]}
        if app_properties is not None:
            body["appProperties"] = dict(app_properties)
        created = execute_with_retry(
            lambda: (
                self._service.files()
                .create(
                    body=body,
                    media_body=media,
                    fields="id",
                    supportsAllDrives=True,
                )
                .execute()
            ),
            context=f"uploading bytes file {filename} to folder {parent_id}",
            retry=self._retry,
        )
        return created["id"]

    def delete_file_with_fallback(
        self,
        file_id: str,
        *,
        fallback_remove_parent_id: str | None = None,
        quarantine_folder_name: str = "RoutineMusicHandler_ProcessedOriginals",
        quarantine_parent_id: str = "root",
    ) -> None:
        """Delete a Drive file with fallbacks for common permission constraints.

        Behavior:
        1) If capabilities allow, try hard delete.
        2) If hard delete fails and capabilities allow, try trash.
        3) If delete/trash are not permitted (common for non-owner writers), optionally move the file
           out of the intake folder into a quarantine folder (default: My Drive root/quarantine_folder_name).
        """

        # Detect delete/trash permissions up front.
        try:
            caps_meta = execute_with_retry(
                lambda: (
                    self._service.files()
                    .get(
                        fileId=file_id,
                        fields="capabilities(canDelete,canTrash)",
                        supportsAllDrives=True,
                    )
                    .execute()
                ),
                context=f"reading capabilities for file {file_id}",
                retry=self._retry,
            )
            caps = caps_meta.get("capabilities") or {}
            can_delete = bool(caps.get("canDelete"))
            can_trash = bool(caps.get("canTrash"))
        except Exception as e:
            log.debug(
                "Failed to read capabilities; will attempt delete/trash anyway: file_id=%s err=%s",
                file_id,
                e,
            )
            can_delete = True
            can_trash = True

        skip_delete_trash = (not can_delete) and (not can_trash)
        if skip_delete_trash:
            log.info(
                "Skipping delete/trash due to capabilities: file_id=%s canDelete=%s canTrash=%s",
                file_id,
                can_delete,
                can_trash,
            )

        # 1) Hard delete
        if not skip_delete_trash and can_delete:
            try:
                execute_with_retry(
                    lambda: (
                        self._service.files()
                        .delete(fileId=file_id, supportsAllDrives=True)
                        .execute()
                    ),
                    context=f"deleting file {file_id}",
                    retry=self._retry,
                )
                return
            except Exception as e:
                log.warning("Hard delete failed: file_id=%s err=%s", file_id, e)

        # 2) Trash
        if not skip_delete_trash and can_trash:
            try:
                execute_with_retry(
                    lambda: (
                        self._service.files()
                        .update(
                            fileId=file_id,
                            body={"trashed": True},
                            supportsAllDrives=True,
                        )
                        .execute()
                    ),
                    context=f"trashing file {file_id}",
                    retry=self._retry,
                )
                log.info("Trashed file: file_id=%s", file_id)
                return
            except Exception as e:
                log.warning("Trash failed: file_id=%s err=%s", file_id, e)

        # 3) Fallback: move to quarantine
        if fallback_remove_parent_id:
            quarantine_folder_id = self.ensure_folder(
                quarantine_parent_id, quarantine_folder_name
            )

            try:
                meta = execute_with_retry(
                    lambda: (
                        self._service.files()
                        .get(
                            fileId=file_id, fields="id,parents", supportsAllDrives=True
                        )
                        .execute()
                    ),
                    context=f"getting parents for file {file_id}",
                    retry=self._retry,
                )
                current_parents = meta.get("parents") or []
            except Exception as e:
                log.warning(
                    "Failed to fetch parents before move fallback: file_id=%s err=%s",
                    file_id,
                    e,
                )
                current_parents = []

            remove_parents: list[str] = []
            if current_parents and fallback_remove_parent_id in current_parents:
                remove_parents = [fallback_remove_parent_id]
            elif current_parents:
                remove_parents = list(current_parents)

            remove_str = ",".join(remove_parents) if remove_parents else ""

            try:
                execute_with_retry(
                    lambda: (
                        self._service.files()
                        .update(
                            fileId=file_id,
                            addParents=quarantine_folder_id,
                            removeParents=remove_str,
                            fields="id,parents",
                            supportsAllDrives=True,
                        )
                        .execute()
                    ),
                    context=f"moving file {file_id} to quarantine folder",
                    retry=self._retry,
                )
                log.info(
                    "Moved original to quarantine folder: file_id=%s quarantine_folder_id=%s removed_parents=%s",
                    file_id,
                    quarantine_folder_id,
                    remove_str or "<none>",
                )
                return
            except Exception as e:
                log.warning(
                    "Move-to-quarantine fallback failed: file_id=%s quarantine_folder_id=%s removed_parents=%s err=%s",
                    file_id,
                    quarantine_folder_id,
                    remove_str or "<none>",
                    e,
                )

        raise PermissionError(
            f"Unable to delete or trash Drive file {file_id}. See logs for permissions/capabilities snapshot."
        )
