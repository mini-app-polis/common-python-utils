from __future__ import annotations

from pathlib import Path


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeDriveService:
    def __init__(self):
        self.calls = []
        # Preload two pages of list results
        self._list_pages = [
            (
                {
                    "files": [
                        {
                            "id": "1",
                            "name": "a",
                            "mimeType": "t",
                            "modifiedTime": "2020",
                        },
                        {
                            "id": "2",
                            "name": "b",
                            "mimeType": "t",
                            "modifiedTime": "2021",
                        },
                    ],
                    "nextPageToken": "p2",
                },
            ),
            (
                {
                    "files": [
                        {
                            "id": "3",
                            "name": "c",
                            "mimeType": "t",
                            "modifiedTime": "2022",
                        },
                    ],
                },
            ),
        ]
        self._created_folders = {}
        self._parents = {"fileX": ["oldParent"]}
        self._copied_ids = []

    def files(self):
        svc = self

        class _Files:
            def list(self, **params):
                svc.calls.append(("list", params))
                token = params.get("pageToken")
                if token == "p2":
                    return _Exec(lambda: svc._list_pages[1][0])
                return _Exec(lambda: svc._list_pages[0][0])

            def create(
                self, body=None, media_body=None, fields=None, supportsAllDrives=None
            ):
                svc.calls.append(
                    ("create", body, bool(media_body), fields, supportsAllDrives)
                )
                # folder create: return stable id
                name = (body or {}).get("name", "")
                new_id = svc._created_folders.setdefault(
                    name, f"id_{len(svc._created_folders) + 1}"
                )
                return _Exec(lambda: {"id": new_id})

            def copy(self, fileId=None, body=None, fields=None, supportsAllDrives=None):
                svc.calls.append(("copy", fileId, body, fields, supportsAllDrives))
                # allow tests to override by monkeypatching this method
                new_id = f"copy_{fileId}"
                svc._copied_ids.append(new_id)
                return _Exec(lambda: {"id": new_id})

            def get(
                self,
                fileId=None,
                fields=None,
                supportsAllDrives=None,
                includeItemsFromAllDrives=None,
            ):
                svc.calls.append(
                    (
                        "get",
                        fileId,
                        fields,
                        supportsAllDrives,
                        includeItemsFromAllDrives,
                    )
                )
                return _Exec(lambda: {"parents": svc._parents.get(fileId, [])})

            def update(self, **kwargs):
                svc.calls.append(("update", kwargs))
                # apply parent updates for realism
                fid = kwargs.get("fileId")
                if fid and "addParents" in kwargs:
                    svc._parents[fid] = [kwargs["addParents"]]
                return _Exec(
                    lambda: {
                        "id": kwargs.get("fileId"),
                        "parents": svc._parents.get(fid, []),
                    }
                )

            def delete(self, fileId=None, supportsAllDrives=None):
                svc.calls.append(("delete", fileId, supportsAllDrives))
                return _Exec(lambda: {"deleted": True})

            def get_media(self, fileId=None):
                svc.calls.append(("get_media", fileId))
                return {"data": b"hello"}

        return _Files()


def test_list_files_paginates_and_maps_to_types(monkeypatch):
    from kaiano.google.drive import DriveFacade

    svc = FakeDriveService()
    drive = DriveFacade(svc)
    files = drive.list_files("parent")
    assert [f.id for f in files] == ["1", "2", "3"]
    assert files[0].name == "a"
    # two list calls (page 1 + page 2)
    assert len([c for c in svc.calls if c[0] == "list"]) == 2


def test_ensure_folder_uses_cache(monkeypatch):
    from kaiano.google.drive import FOLDER_CACHE, DriveFacade

    FOLDER_CACHE.clear()
    svc = FakeDriveService()
    drive = DriveFacade(svc)

    # First call creates (because our Fake list() always returns files, but folder query includes name exact;
    # we don't simulate that, so just ensure caching behavior)
    fid1 = drive.ensure_folder("p", "MyFolder")
    fid2 = drive.ensure_folder("p", "MyFolder")
    assert fid1 == fid2
    # second call should not add additional list/create calls
    assert fid1 in FOLDER_CACHE.values()


def test_copy_file_retries_on_404_not_found(monkeypatch, as_http_error):
    from kaiano.google.drive import DriveFacade

    monkeypatch.setattr("kaiano.google.drive.time.sleep", lambda _s: None)
    monkeypatch.setattr("kaiano.google.drive.random.uniform", lambda _a, _b: 0.0)

    svc = FakeDriveService()
    original_files = svc.files

    state = {"n": 0}

    def files():
        f = original_files()
        old_copy = f.copy

        def copy(fileId=None, body=None, fields=None, supportsAllDrives=None):
            state["n"] += 1
            if state["n"] < 3:
                return _Exec(
                    lambda: (_ for _ in ()).throw(
                        as_http_error(status=404, message="File not found")
                    )
                )
            return old_copy(
                fileId=fileId,
                body=body,
                fields=fields,
                supportsAllDrives=supportsAllDrives,
            )

        f.copy = copy  # type: ignore[attr-defined]
        return f

    svc.files = files  # type: ignore[assignment]

    drive = DriveFacade(svc)
    new_id = drive.copy_file("abc", parent_folder_id="p", name="new", max_retries=5)
    assert new_id == "copy_abc"


def test_copy_file_raises_after_exhausting_retries(monkeypatch, as_http_error):
    from kaiano.google.drive import DriveFacade

    monkeypatch.setattr("kaiano.google.drive.time.sleep", lambda _s: None)
    monkeypatch.setattr("kaiano.google.drive.random.uniform", lambda _a, _b: 0.0)

    svc = FakeDriveService()
    original_files = svc.files

    def files():
        f = original_files()

        def copy(fileId=None, body=None, fields=None, supportsAllDrives=None):
            return _Exec(
                lambda: (_ for _ in ()).throw(
                    as_http_error(status=404, message="not found")
                )
            )

        f.copy = copy  # type: ignore[attr-defined]
        return f

    svc.files = files  # type: ignore[assignment]
    drive = DriveFacade(svc)

    try:
        drive.copy_file("abc", max_retries=2)
    except RuntimeError as e:
        assert "after 2 attempts" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_copy_file_re_raises_non_404_http_error(monkeypatch, as_http_error):
    from kaiano.google._retry import RetryConfig
    from kaiano.google.drive import DriveFacade

    # Avoid real sleeps from execute_with_retry() for 5xx
    monkeypatch.setattr("kaiano.google._retry.time.sleep", lambda _s: None)
    monkeypatch.setattr("kaiano.google._retry.random.random", lambda: 0.0)

    svc = FakeDriveService()
    original_files = svc.files

    def files():
        f = original_files()

        def copy(fileId=None, body=None, fields=None, supportsAllDrives=None):
            return _Exec(
                lambda: (_ for _ in ()).throw(as_http_error(status=500, message="boom"))
            )

        f.copy = copy  # type: ignore[attr-defined]
        return f

    svc.files = files  # type: ignore[assignment]
    drive = DriveFacade(
        svc, retry=RetryConfig(max_retries=1, base_delay_s=0.0, max_delay_s=0.0)
    )

    try:
        drive.copy_file("abc", max_retries=2)
    except Exception as e:
        assert "boom" in str(e)
    else:
        raise AssertionError("expected HttpError")


def test_move_file_updates_parents(monkeypatch):
    from kaiano.google.drive import DriveFacade

    svc = FakeDriveService()
    drive = DriveFacade(svc)
    drive.move_file("fileX", new_parent_id="newParent", remove_from_parents=True)

    # update call includes removeParents when old parents exist
    upd = [c for c in svc.calls if c[0] == "update"][-1][1]
    assert upd["addParents"] == "newParent"
    assert "removeParents" in upd


def test_download_file_writes_to_disk(tmp_path: Path):
    from kaiano.google.drive import DriveFacade

    svc = FakeDriveService()
    drive = DriveFacade(svc)
    dest = tmp_path / "out.bin"
    drive.download_file("file1", str(dest))
    assert dest.read_bytes() == b"hello"


def test_upload_and_update_and_rename_and_delete(tmp_path: Path):
    from kaiano.google.drive import DriveFacade

    svc = FakeDriveService()
    drive = DriveFacade(svc)

    p = tmp_path / "x.csv"
    p.write_text("a,b")

    fid = drive.upload_file(str(p), parent_id="p", dest_name="x", mime_type="text/csv")
    assert fid.startswith("id_")

    drive.update_file("fileX", str(p))
    drive.rename_file("fileX", "newname")
    drive.delete_file("fileX")

    ops = [c[0] for c in svc.calls]
    assert "create" in ops
    assert "update" in ops
    assert "delete" in ops


def test_get_all_and_most_recent_m3u_files(monkeypatch):
    from kaiano import config
    from kaiano.google.drive import DriveFacade
    from kaiano.google.types import DriveFile

    svc = FakeDriveService()
    drive = DriveFacade(svc)

    # Patch config and list_files to return date-prefixed names
    config.VDJ_HISTORY_FOLDER_ID = "folder"

    def list_files(parent_id, **kwargs):
        return [
            DriveFile(id="1", name="2026-01-01.m3u"),
            DriveFile(id="2", name="2026-01-03.m3u"),
            DriveFile(id="3", name="2026-01-02.m3u"),
        ]

    monkeypatch.setattr(drive, "list_files", list_files)

    all_files = drive.get_all_m3u_files()
    assert [f["id"] for f in all_files] == ["2", "3", "1"]  # newest-first
    most_recent = drive.get_most_recent_m3u_file()
    assert most_recent == {"id": "2", "name": "2026-01-03.m3u"}


def test_get_m3u_helpers_handle_missing_config(monkeypatch):
    from kaiano import config
    from kaiano.google.drive import DriveFacade

    config.VDJ_HISTORY_FOLDER_ID = None
    svc = FakeDriveService()
    drive = DriveFacade(svc)

    assert drive.get_all_m3u_files() == []
    assert drive.get_most_recent_m3u_file() is None


def test_download_m3u_file_data_returns_lines(monkeypatch):
    from kaiano.google.drive import DriveFacade

    svc = FakeDriveService()
    drive = DriveFacade(svc)
    lines = drive.download_m3u_file_data("file1")
    assert lines == ["hello"]
