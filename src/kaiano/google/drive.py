import io
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Any

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

from kaiano import config
from kaiano import logger as logger_mod

from ._retry import RetryConfig, execute_with_retry
from .types import DriveFile

log = logger_mod.get_logger()
FOLDER_CACHE = {}

_DRIVE_ID_RE = re.compile(r"[-\w]{25,}")
_VERSION_RE = re.compile(r"_v(\d+)$")


@dataclass(frozen=True)
class DownloadedFile:
    file_id: str
    name: str
    mime_type: str
    data: bytes


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

    def __init__(self, service: Any, retry: RetryConfig | None = None):
        self._service = service
        self._retry = retry or RetryConfig()

    @property
    def service(self) -> Any:
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

    def list_files(
        self,
        parent_id: str,
        *,
        mime_type: str | None = None,
        name_contains: str | None = None,
        trashed: bool = False,
        include_folders: bool = True,
    ) -> list[DriveFile]:
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
                lambda page_token=page_token: _call(page_token),
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
    ) -> str:
        """Copy a Drive file.

        This method includes a small retry loop to handle Drive propagation delay where a
        just-created file may temporarily return a 404 "File not found" on copy.

        Backwards compatible with the previous signature; callers can still pass
        `parent_folder_id` and `name` as before.
        """

        body: dict[str, Any] = {}
        if parent_folder_id:
            body["parents"] = [parent_folder_id]
        if name:
            body["name"] = name

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

    def get_all_m3u_files(self) -> list[dict]:
        """Return all VirtualDJ history .m3u files (newest-first).

        Matches legacy `kaiano.m3u_parsing.get_all_m3u_files(drive_service)`.

        Returns a list of dicts containing at least: {"id": str, "name": str}.
        Sorting is by filename. With date-prefixed filenames (YYYY-MM-DD.m3u), this
        yields correct chronological order.
        """

        if not getattr(config, "VDJ_HISTORY_FOLDER_ID", None):
            log.critical("VDJ_HISTORY_FOLDER_ID is not set in config.")
            return []

        try:
            files = self.list_files(
                config.VDJ_HISTORY_FOLDER_ID,
                name_contains=".m3u",
                trashed=False,
                include_folders=False,
            )

            files.sort(key=lambda f: f.name or "")
            files = list(reversed(files))

            return [{"id": f.id, "name": f.name} for f in files]
        except Exception as e:
            log.error(f"Failed to list .m3u files: {e}")
            return []

    def get_most_recent_m3u_file(self) -> dict | None:
        """Return the most recent .m3u file.

        Matches legacy `kaiano.m3u_parsing.get_most_recent_m3u_file(drive_service)`.

        Returns a dict with keys: {"id", "name"} or None.
        """

        if not getattr(config, "VDJ_HISTORY_FOLDER_ID", None):
            log.critical("VDJ_HISTORY_FOLDER_ID is not set in config.")
            return None

        try:
            files = self.list_files(
                config.VDJ_HISTORY_FOLDER_ID,
                name_contains=".m3u",
                trashed=False,
                include_folders=False,
            )

            if not files:
                return None

            files.sort(key=lambda f: f.name or "")
            f = files[-1]
            return {"id": f.id, "name": f.name}
        except Exception as e:
            log.error(f"Failed to find most recent .m3u file: {e}")
            return None

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
    ) -> str:
        """Upload a new Drive file from bytes and return its file ID."""

        media = MediaIoBaseUpload(
            io.BytesIO(content), mimetype=mime_type, resumable=False
        )
        created = execute_with_retry(
            lambda: (
                self._service.files()
                .create(
                    body={"name": filename, "parents": [parent_id]},
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
