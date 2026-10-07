from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


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
    from mini_app_polis.google.drive import DriveFacade

    svc = FakeDriveService()
    drive = DriveFacade(svc)
    files = drive.list_files("parent")
    assert [f.id for f in files] == ["1", "2", "3"]
    assert files[0].name == "a"
    # two list calls (page 1 + page 2)
    assert len([c for c in svc.calls if c[0] == "list"]) == 2


def test_ensure_folder_uses_cache(monkeypatch):
    from mini_app_polis.google.drive import FOLDER_CACHE, DriveFacade

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
    from mini_app_polis.google.drive import DriveFacade

    monkeypatch.setattr("mini_app_polis.google.drive.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        "mini_app_polis.google.drive.random.uniform", lambda _a, _b: 0.0
    )

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
    from mini_app_polis.google.drive import DriveFacade

    monkeypatch.setattr("mini_app_polis.google.drive.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        "mini_app_polis.google.drive.random.uniform", lambda _a, _b: 0.0
    )

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
    from mini_app_polis.google._retry import RetryConfig
    from mini_app_polis.google.drive import DriveFacade

    # Avoid real sleeps from execute_with_retry() for 5xx
    monkeypatch.setattr("mini_app_polis.google._retry.time.sleep", lambda _s: None)
    monkeypatch.setattr("mini_app_polis.google._retry.random.random", lambda: 0.0)

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
    from mini_app_polis.google.drive import DriveFacade

    svc = FakeDriveService()
    drive = DriveFacade(svc)
    drive.move_file("fileX", new_parent_id="newParent", remove_from_parents=True)

    # update call includes removeParents when old parents exist
    upd = [c for c in svc.calls if c[0] == "update"][-1][1]
    assert upd["addParents"] == "newParent"
    assert "removeParents" in upd


def test_download_file_writes_to_disk(tmp_path: Path):
    from mini_app_polis.google.drive import DriveFacade

    svc = FakeDriveService()
    drive = DriveFacade(svc)
    dest = tmp_path / "out.bin"
    drive.download_file("file1", str(dest))
    assert dest.read_bytes() == b"hello"


def test_upload_and_update_and_rename_and_delete(tmp_path: Path):
    from mini_app_polis.google.drive import DriveFacade

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


def test_trash_file_is_a_metadata_update_not_a_delete():
    """Trashing must not go near files.delete.

    files.delete is the irreversible path and needs organizer/Manager on
    a shared drive; trashing is an edit, which Content manager can do.
    Sending the wrong one is the difference between a sweep that works
    and one that fails every time.
    """
    from mini_app_polis.google.drive import DriveFacade

    svc = FakeDriveService()
    drive = DriveFacade(svc)

    drive.trash_file("fileX")

    ops = [c[0] for c in svc.calls]
    assert ops == ["update"]
    kwargs = svc.calls[0][1]
    assert kwargs["fileId"] == "fileX"
    assert kwargs["body"] == {"trashed": True}
    assert kwargs["supportsAllDrives"] is True


def _m3u_drive(monkeypatch, listing):
    from mini_app_polis.google.drive import DriveFacade

    drive = DriveFacade(FakeDriveService())
    calls = []

    def list_files(parent_id, **kwargs):
        calls.append((parent_id, kwargs))
        if isinstance(listing, Exception):
            raise listing
        return listing

    monkeypatch.setattr(drive, "list_files", list_files)
    return drive, calls


def test_get_all_and_most_recent_m3u_files(monkeypatch):
    from mini_app_polis import config
    from mini_app_polis.google.types import DriveFile

    monkeypatch.setattr(config, "VDJ_HISTORY_FOLDER_ID", "folder")
    drive, calls = _m3u_drive(
        monkeypatch,
        [
            DriveFile(id="1", name="2026-01-01.m3u"),
            DriveFile(id="2", name="2026-01-03.m3u"),
            DriveFile(id="3", name="2026-01-02.M3U"),
            DriveFile(id="4", name="2026-01-04.m3u8"),
            DriveFile(id="5", name="2026-01-05.m3u.bak"),
        ],
    )

    all_files = drive.get_all_m3u_files()
    assert [f["id"] for f in all_files] == ["2", "3", "1"]  # newest-first, .m3u only
    assert drive.get_most_recent_m3u_file() == {"id": "2", "name": "2026-01-03.m3u"}
    assert calls[0][0] == "folder"


def test_m3u_helpers_list_the_folder_they_are_given(monkeypatch):
    from mini_app_polis import config
    from mini_app_polis.google.types import DriveFile

    monkeypatch.setattr(config, "VDJ_HISTORY_FOLDER_ID", "from-config")
    drive, calls = _m3u_drive(monkeypatch, [DriveFile(id="1", name="2026-01-01.m3u")])

    drive.get_all_m3u_files("passed-in")
    drive.get_most_recent_m3u_file(folder_id="passed-in")

    assert [c[0] for c in calls] == ["passed-in", "passed-in"]


def test_m3u_helpers_raise_without_a_folder(monkeypatch):
    from mini_app_polis import config

    monkeypatch.setattr(config, "VDJ_HISTORY_FOLDER_ID", None)
    drive, calls = _m3u_drive(monkeypatch, [])

    with pytest.raises(ValueError, match="VDJ_HISTORY_FOLDER_ID"):
        drive.get_all_m3u_files()
    with pytest.raises(ValueError, match="VDJ_HISTORY_FOLDER_ID"):
        drive.get_most_recent_m3u_file()
    assert calls == []


def test_m3u_helpers_let_a_failed_listing_raise(monkeypatch):
    """A Drive outage must not look like an empty history folder."""
    drive, _ = _m3u_drive(monkeypatch, RuntimeError("503 backendError"))

    with pytest.raises(RuntimeError, match="503"):
        drive.get_all_m3u_files("folder")
    with pytest.raises(RuntimeError, match="503"):
        drive.get_most_recent_m3u_file("folder")


def test_an_empty_history_folder_is_empty_not_an_error(monkeypatch):
    drive, _ = _m3u_drive(monkeypatch, [])

    assert drive.get_all_m3u_files("folder") == []
    assert drive.get_most_recent_m3u_file("folder") is None


def test_download_m3u_file_data_returns_lines(monkeypatch):
    from mini_app_polis.google.drive import DriveFacade

    svc = FakeDriveService()
    drive = DriveFacade(svc)
    lines = drive.download_m3u_file_data("file1")
    assert lines == ["hello"]


# ---------------------------------------------------------------------------
# share_with_readers / app_properties / find_files_by_app_property /
# get_file_name / clear_folder_cache
#
# These use a MagicMock service so every assertion is on the exact call Drive
# received; a fake that silently accepted any kwargs could let them pass
# without the request being right.
# ---------------------------------------------------------------------------


def _mock_service():
    from unittest.mock import MagicMock

    return MagicMock()


def test_share_with_readers_cleans_dedupes_and_validates():
    from unittest.mock import call

    from mini_app_polis.google.drive import DriveFacade, ShareResult

    svc = _mock_service()
    drive = DriveFacade(svc)

    result = drive.share_with_readers(
        "file1",
        [
            "  Alice@Example.com ",
            None,
            "",
            "   ",
            "not-an-email",
            "two@@example.com",
            "spa ce@example.com",
            "nodot@example",
            "bob@example.org",
            "ALICE@example.com",  # duplicate of the first after cleaning
            "bob@example.org",
        ],
    )

    assert result == ShareResult(
        shared=["alice@example.com", "bob@example.org"], failed=[]
    )
    create = svc.permissions.return_value.create
    assert create.call_args_list == [
        call(
            fileId="file1",
            body={
                "role": "reader",
                "type": "user",
                "emailAddress": "alice@example.com",
            },
            sendNotificationEmail=False,
            supportsAllDrives=True,
        ),
        call(
            fileId="file1",
            body={
                "role": "reader",
                "type": "user",
                "emailAddress": "bob@example.org",
            },
            sendNotificationEmail=False,
            supportsAllDrives=True,
        ),
    ]
    assert create.return_value.execute.call_count == 2


def test_share_with_readers_accepts_any_iterable():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    drive = DriveFacade(svc)

    result = drive.share_with_readers("f", (e for e in ["a@b.co"]))

    assert result.shared == ["a@b.co"]
    svc.permissions.return_value.create.assert_called_once()


def test_share_with_readers_collects_per_address_failures(as_http_error):
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    boom = as_http_error(status=400, message="invalid sharing request")

    def create(*, fileId, body, sendNotificationEmail, supportsAllDrives):
        req = _Exec(
            (lambda: (_ for _ in ()).throw(boom))
            if body["emailAddress"] == "bad@example.com"
            else (lambda: {"id": "perm"})
        )
        return req

    svc.permissions.return_value.create.side_effect = create
    drive = DriveFacade(svc)

    result = drive.share_with_readers(
        "file1", ["good@example.com", "bad@example.com", "also@example.com"]
    )

    assert result.shared == ["good@example.com", "also@example.com"]
    assert len(result.failed) == 1
    assert result.failed[0][0] == "bad@example.com"
    assert result.failed[0][1] is boom
    # The failure did not stop the address after it.
    assert svc.permissions.return_value.create.call_count == 3


def test_share_with_readers_makes_no_call_when_nothing_valid():
    from mini_app_polis.google.drive import DriveFacade, ShareResult

    svc = _mock_service()
    drive = DriveFacade(svc)

    result = drive.share_with_readers("file1", [None, "", "  ", "nope", "a@b"])

    assert result == ShareResult(shared=[], failed=[])
    svc.permissions.assert_not_called()


def test_share_result_is_frozen():
    import dataclasses

    import pytest

    from mini_app_polis.google.drive import ShareResult

    r = ShareResult(shared=[], failed=[])
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.shared = ["x"]  # type: ignore[misc]


def test_copy_file_sends_app_properties_only_when_given():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    copy = svc.files.return_value.copy
    copy.return_value.execute.return_value = {"id": "new"}
    drive = DriveFacade(svc)

    assert drive.copy_file("src", parent_folder_id="p", name="n") == "new"
    copy.assert_called_once_with(
        fileId="src",
        body={"parents": ["p"], "name": "n"},
        fields="id",
        supportsAllDrives=True,
    )

    copy.reset_mock()
    copy.return_value.execute.return_value = {"id": "new2"}
    assert (
        drive.copy_file(
            "src",
            parent_folder_id="p",
            name="n",
            app_properties={"deejaytoolsSubmissionId": "42"},
        )
        == "new2"
    )
    copy.assert_called_once_with(
        fileId="src",
        body={
            "parents": ["p"],
            "name": "n",
            "appProperties": {"deejaytoolsSubmissionId": "42"},
        },
        fields="id",
        supportsAllDrives=True,
    )


def test_upload_bytes_sends_app_properties_only_when_given():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    create = svc.files.return_value.create
    create.return_value.execute.return_value = {"id": "up"}
    drive = DriveFacade(svc)

    assert (
        drive.upload_bytes(
            parent_id="p", filename="a.mp3", content=b"x", mime_type="audio/mpeg"
        )
        == "up"
    )
    kwargs = create.call_args.kwargs
    assert kwargs["body"] == {"name": "a.mp3", "parents": ["p"]}
    assert kwargs["fields"] == "id"
    assert kwargs["supportsAllDrives"] is True
    assert kwargs["media_body"].mimetype == "audio/mpeg"

    create.reset_mock()
    drive.upload_bytes(
        parent_id="p",
        filename="a.mp3",
        content=b"x",
        mime_type="audio/mpeg",
        app_properties={"k": "v"},
    )
    create.assert_called_once()
    assert create.call_args.kwargs["body"] == {
        "name": "a.mp3",
        "parents": ["p"],
        "appProperties": {"k": "v"},
    }


def test_upload_bytes_is_single_request_unless_resumable():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    create = svc.files.return_value.create
    create.return_value.execute.return_value = {"id": "up"}
    drive = DriveFacade(svc)

    drive.upload_bytes(
        parent_id="p", filename="a.mp3", content=b"x", mime_type="audio/mpeg"
    )
    assert create.call_args.kwargs["media_body"].resumable is False

    create.reset_mock()
    drive.upload_bytes(
        parent_id="p",
        filename="a.mp3",
        content=b"x" * 10,
        mime_type="audio/mpeg",
        resumable=True,
    )
    create.assert_called_once()
    media = create.call_args.kwargs["media_body"]
    assert media.resumable is True


def test_find_files_by_app_property_query_and_escaping():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    lst = svc.files.return_value.list
    lst.return_value.execute.return_value = {"files": [{"id": "a"}, {"id": "b"}]}
    drive = DriveFacade(svc)

    ids = drive.find_files_by_app_property("fold'er", key="k'ey\\", value="O'Brien\\'s")

    assert ids == ["a", "b"]
    lst.assert_called_once()
    kwargs = lst.call_args.kwargs
    assert kwargs["q"] == (
        "'fold\\'er' in parents"
        " and appProperties has { key='k\\'ey\\\\'"
        " and value='O\\'Brien\\\\\\'s' }"
        " and trashed = false"
    )
    assert kwargs["supportsAllDrives"] is True
    assert kwargs["includeItemsFromAllDrives"] is True
    assert kwargs["pageToken"] is None
    assert "nextPageToken" in kwargs["fields"]


def test_find_files_by_app_property_plain_query():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    lst = svc.files.return_value.list
    lst.return_value.execute.return_value = {"files": []}
    drive = DriveFacade(svc)

    assert drive.find_files_by_app_property("P1", key="sid", value="7") == []
    assert lst.call_args.kwargs["q"] == (
        "'P1' in parents and appProperties has { key='sid' and value='7' }"
        " and trashed = false"
    )


def test_find_files_by_app_property_paginates():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    pages = {
        None: {"files": [{"id": "1"}, {"id": "2"}], "nextPageToken": "t2"},
        "t2": {"files": [{"id": "3"}], "nextPageToken": "t3"},
        "t3": {"files": [{"id": "4"}]},
    }
    seen_tokens: list[str | None] = []

    def list_(**kwargs):
        seen_tokens.append(kwargs["pageToken"])
        return _Exec(lambda: pages[kwargs["pageToken"]])

    svc.files.return_value.list.side_effect = list_
    drive = DriveFacade(svc)

    assert drive.find_files_by_app_property("p", key="k", value="v") == [
        "1",
        "2",
        "3",
        "4",
    ]
    assert seen_tokens == [None, "t2", "t3"]


def test_get_file_name():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    get = svc.files.return_value.get
    get.return_value.execute.return_value = {"name": "Routine.mp3"}
    drive = DriveFacade(svc)

    assert drive.get_file_name("f1") == "Routine.mp3"
    get.assert_called_once_with(fileId="f1", fields="name", supportsAllDrives=True)


def test_clear_folder_cache_empties_cache_and_forces_lookup():
    from mini_app_polis.google.drive import (
        FOLDER_CACHE,
        DriveFacade,
        clear_folder_cache,
    )

    FOLDER_CACHE.clear()
    FOLDER_CACHE["p/Stale"] = "dead-id"

    svc = _mock_service()
    lst = svc.files.return_value.list
    lst.return_value.execute.return_value = {"files": [{"id": "fresh-id"}]}
    drive = DriveFacade(svc)

    # Cached: no API call.
    assert drive.ensure_folder("p", "Stale") == "dead-id"
    lst.assert_not_called()

    clear_folder_cache()
    assert FOLDER_CACHE == {}

    # After clearing, the folder is looked up again.
    assert drive.ensure_folder("p", "Stale") == "fresh-id"
    lst.assert_called_once()
    FOLDER_CACHE.clear()


def test_from_service_account_info_uses_exactly_the_given_scopes(monkeypatch):
    from mini_app_polis.google import drive as drive_mod

    made = {}

    def fake_creds(info, scopes):
        made["info"], made["scopes"] = info, scopes
        return "creds"

    monkeypatch.setattr(
        drive_mod.service_account.Credentials,
        "from_service_account_info",
        staticmethod(fake_creds),
    )
    monkeypatch.setattr(drive_mod, "build_drive_service", lambda creds: f"svc({creds})")

    facade = drive_mod.DriveFacade.from_service_account_info(
        {"client_email": "a@b", "private_key": "k", "token_uri": "t"},
        scopes=("https://www.googleapis.com/auth/drive.file",),
    )

    assert facade.service == "svc(creds)"
    assert made == {
        "info": {"client_email": "a@b", "private_key": "k", "token_uri": "t"},
        "scopes": ["https://www.googleapis.com/auth/drive.file"],
    }


# ---------------------------------------------------------------------------
# Lookup, export, versioning, in-memory download and delete fallbacks.
# MagicMock services again, so assertions are on the exact Drive requests.
# ---------------------------------------------------------------------------


def _raises(exc):
    def _fn():
        raise exc

    return _fn


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz012345/view",
            "1AbCdEfGhIjKlMnOpQrStUvWxYz012345",
        ),
        (
            "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123-_/edit#gid=0",
            "1AbCdEfGhIjKlMnOpQrStUvWxYz0123-_",
        ),
        ("1AbCdEfGhIjKlMnOpQrStUvWxYz012345", "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"),
        ("https://example.com/short", None),
        ("", None),
    ],
)
def test_extract_drive_file_id(value, expected):
    from mini_app_polis.google.drive import DriveFacade

    assert DriveFacade.extract_drive_file_id(value) == expected


def test_list_files_builds_the_query_from_every_filter():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    lst = svc.files.return_value.list
    lst.return_value.execute.return_value = {"files": []}

    DriveFacade(svc).list_files(
        "parent",
        mime_type="audio/mpeg",
        name_contains="Bob's",
        trashed=True,
        include_folders=False,
    )

    assert lst.call_args.kwargs["q"] == (
        "'parent' in parents"
        " and mimeType != 'application/vnd.google-apps.folder'"
        " and mimeType = 'audio/mpeg'"
        " and name contains 'Bob\\'s'"
        " and trashed = true"
    )


def test_find_file_in_folder_without_mime_type_returns_first_or_none():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    lst = svc.files.return_value.list
    lst.return_value.execute.side_effect = [{"files": [{"id": "f1"}]}, {"files": []}]
    drive = DriveFacade(svc)

    assert drive.find_file_in_folder("p", name="It's") == "f1"
    assert drive.find_file_in_folder("p", name="x") is None
    assert lst.call_args_list[0].kwargs["q"] == (
        "name = 'It\\'s' and 'p' in parents and trashed = false"
    )


def test_ensure_folder_creates_the_folder_when_none_exists(monkeypatch):
    from mini_app_polis.google import drive as drive_mod

    monkeypatch.setattr(drive_mod, "FOLDER_CACHE", {})
    svc = _mock_service()
    files = svc.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    files.create.return_value.execute.return_value = {"id": "new-folder"}
    drive = drive_mod.DriveFacade(svc)

    assert drive.ensure_folder("parent", "Mom's Music") == "new-folder"
    assert drive.ensure_folder("parent", "Mom's Music") == "new-folder"

    files.list.assert_called_once()
    assert files.list.call_args.kwargs["q"] == (
        "'parent' in parents and name = 'Mom\\'s Music' "
        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    files.create.assert_called_once_with(
        body={
            "name": "Mom's Music",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["parent"],
        },
        fields="id",
        supportsAllDrives=True,
    )
    assert drive_mod.FOLDER_CACHE == {"parent/Mom's Music": "new-folder"}


def test_copy_file_raises_when_drive_returns_no_id():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    svc.files.return_value.copy.return_value.execute.return_value = {}

    with pytest.raises(
        RuntimeError, match="did not return an id for source_file_id=src"
    ):
        DriveFacade(svc).copy_file("src")
    # A missing id is not a propagation 404: no retry.
    svc.files.return_value.copy.assert_called_once()


def test_move_file_can_keep_existing_parents():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    files = svc.files.return_value
    files.get.return_value.execute.return_value = {"parents": ["old"]}

    DriveFacade(svc).move_file("f", new_parent_id="new", remove_from_parents=False)

    files.get.assert_called_once_with(
        fileId="f", fields="parents", supportsAllDrives=True
    )
    files.update.assert_called_once_with(
        fileId="f", addParents="new", fields="id, parents", supportsAllDrives=True
    )


def test_move_file_with_no_current_parents_removes_nothing():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    files = svc.files.return_value
    files.get.return_value.execute.return_value = {}

    DriveFacade(svc).move_file("f", new_parent_id="new")

    assert "removeParents" not in files.update.call_args.kwargs


def test_move_file_removes_every_previous_parent():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    files = svc.files.return_value
    files.get.return_value.execute.return_value = {"parents": ["a", "b"]}

    DriveFacade(svc).move_file("f", new_parent_id="new")

    assert files.update.call_args.kwargs["removeParents"] == "a,b"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"\x00bytes", b"\x00bytes"),
        (bytearray(b"ba"), b"ba"),
        ("café", "café".encode()),
    ],
)
def test_export_file_always_returns_bytes(payload, expected):
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    export = svc.files.return_value.export
    export.return_value.execute.return_value = payload

    out = DriveFacade(svc).export_file("doc", mime_type="text/csv")

    assert out == expected
    assert type(out) is bytes
    export.assert_called_once_with(fileId="doc", mimeType="text/csv")


def test_export_google_doc_as_text_exports_plain_text_and_replaces_bad_bytes():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    export = svc.files.return_value.export
    export.return_value.execute.return_value = b"Hello \xff world"

    assert DriveFacade(svc).export_google_doc_as_text("doc") == "Hello � world"
    export.assert_called_once_with(fileId="doc", mimeType="text/plain")


def test_find_or_create_spreadsheet_returns_an_existing_one():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    files = svc.files.return_value
    files.list.return_value.execute.return_value = {"files": [{"id": "sheet-1"}]}

    assert (
        DriveFacade(svc).find_or_create_spreadsheet(parent_folder_id="p", name="Log")
        == "sheet-1"
    )
    assert files.list.call_args.kwargs["q"].endswith(
        "and mimeType = 'application/vnd.google-apps.spreadsheet'"
    )
    files.create.assert_not_called()


def test_find_or_create_spreadsheet_creates_one_in_the_folder():
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    files = svc.files.return_value
    files.list.return_value.execute.return_value = {"files": []}
    files.create.return_value.execute.return_value = {"id": "sheet-new"}

    assert (
        DriveFacade(svc).find_or_create_spreadsheet(parent_folder_id="p", name="Log")
        == "sheet-new"
    )
    files.create.assert_called_once_with(
        body={
            "name": "Log",
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": ["p"],
        },
        fields="id",
        supportsAllDrives=True,
    )


def test_download_m3u_file_data_returns_no_lines_when_the_download_fails(
    monkeypatch, as_http_error
):
    from mini_app_polis.google import drive as drive_mod

    log = MagicMock()
    monkeypatch.setattr(drive_mod, "log", log)
    svc = _mock_service()
    svc.files.return_value.get_media.side_effect = as_http_error(
        status=404, message="File not found"
    )

    assert drive_mod.DriveFacade(svc).download_m3u_file_data("gone") == []
    (msg,) = log.error.call_args.args
    assert "gone" in msg and "File not found" in msg


def test_download_m3u_file_data_returns_no_lines_for_undecodable_content(
    monkeypatch,
):
    from mini_app_polis.google import drive as drive_mod

    monkeypatch.setattr(drive_mod, "log", MagicMock())
    svc = _mock_service()
    svc.files.return_value.get_media.return_value = {"data": b"\xff\xfe"}

    assert drive_mod.DriveFacade(svc).download_m3u_file_data("f") == []


def _versioned_drive(names):
    from mini_app_polis.google.drive import DriveFacade

    svc = _mock_service()
    lst = svc.files.return_value.list
    lst.return_value.execute.return_value = {"files": [{"name": n} for n in names]}
    return DriveFacade(svc), lst


def test_resolve_versioned_filename_returns_the_requested_version_when_free():
    drive, lst = _versioned_drive([])

    assert drive.resolve_versioned_filename(
        parent_folder_id="p", desired_filename="Track_v1.mp3"
    ) == ("Track_v1.mp3", 1)
    assert lst.call_args.kwargs["q"] == (
        "'p' in parents and trashed=false and name contains 'Track_v'"
    )


def test_resolve_versioned_filename_skips_every_used_version():
    drive, _ = _versioned_drive(
        [
            "track_v1.MP3",  # case-insensitive match
            "Track_v2.mp3",
            "Track_v4.mp3",
            "Track_v3.wav",  # other extension: not a clash
            "OtherTrack_v3.mp3",  # different base
        ]
    )

    assert drive.resolve_versioned_filename(
        parent_folder_id="p", desired_filename="Track_v1.mp3"
    ) == ("Track_v3.mp3", 3)


def test_resolve_versioned_filename_starts_from_the_requested_version():
    drive, _ = _versioned_drive(["Track_v1.mp3"])

    assert drive.resolve_versioned_filename(
        parent_folder_id="p", desired_filename="Track_v5.mp3"
    ) == ("Track_v5.mp3", 5)


def test_resolve_versioned_filename_without_an_extension():
    drive, _ = _versioned_drive(["Notes_v1", "Notes_v1.txt"])

    assert drive.resolve_versioned_filename(
        parent_folder_id="p", desired_filename="Notes_v1"
    ) == ("Notes_v2", 2)


def test_resolve_versioned_filename_escapes_quotes_in_the_query():
    drive, lst = _versioned_drive([])

    drive.resolve_versioned_filename(
        parent_folder_id="p", desired_filename="Rock'n Roll_v1.mp3"
    )

    assert "name contains 'Rock\\'n Roll_v'" in lst.call_args.kwargs["q"]


def test_resolve_versioned_filename_requires_a_version_suffix():
    drive, lst = _versioned_drive([])

    with pytest.raises(ValueError, match="_vN suffix"):
        drive.resolve_versioned_filename(
            parent_folder_id="p", desired_filename="Track.mp3"
        )
    lst.assert_not_called()


class _ChunkedDownload:
    """Stand-in for MediaIoBaseDownload that writes the request in two chunks."""

    def __init__(self, fh, request):
        self._fh = fh
        self._chunks = [request["data"][:2], request["data"][2:]]

    def next_chunk(self):
        self._fh.write(self._chunks.pop(0))
        return None, not self._chunks


def test_download_file_bytes_returns_metadata_and_every_chunk(monkeypatch):
    from mini_app_polis.google import drive as drive_mod

    monkeypatch.setattr(drive_mod, "MediaIoBaseDownload", _ChunkedDownload)
    svc = _mock_service()
    files = svc.files.return_value
    files.get.return_value.execute.return_value = {
        "id": "f",
        "name": "song.mp3",
        "mimeType": "audio/mpeg",
    }
    files.get_media.return_value = {"data": b"ID3abc"}

    out = drive_mod.DriveFacade(svc).download_file_bytes("f")

    assert out == drive_mod.DownloadedFile(
        file_id="f", name="song.mp3", mime_type="audio/mpeg", data=b"ID3abc"
    )
    files.get.assert_called_once_with(
        fileId="f", fields="id,name,mimeType", supportsAllDrives=True
    )
    files.get_media.assert_called_once_with(fileId="f", supportsAllDrives=True)


def test_download_file_bytes_defaults_missing_metadata(monkeypatch):
    from mini_app_polis.google import drive as drive_mod

    monkeypatch.setattr(drive_mod, "MediaIoBaseDownload", _ChunkedDownload)
    svc = _mock_service()
    svc.files.return_value.get.return_value.execute.return_value = {"mimeType": None}
    svc.files.return_value.get_media.return_value = {"data": b""}

    out = drive_mod.DriveFacade(svc).download_file_bytes("f")

    assert (out.name, out.mime_type, out.data) == ("", "application/octet-stream", b"")


# --- delete_file_with_fallback ---------------------------------------------


def _delete_drive(monkeypatch, *, caps=None, parents=None):
    """A DriveFacade whose files().get answers by the ``fields`` requested.

    ``caps`` / ``parents`` are either the value to return or an exception to
    raise for the capabilities read and the parents read respectively.
    """
    from mini_app_polis.google import drive as drive_mod

    monkeypatch.setattr(drive_mod, "FOLDER_CACHE", {})
    monkeypatch.setattr(drive_mod, "log", MagicMock())
    svc = _mock_service()
    files = svc.files.return_value

    def get(*, fileId, fields, supportsAllDrives):
        assert supportsAllDrives is True
        answer = caps if fields.startswith("capabilities") else parents
        if isinstance(answer, Exception):
            return _Exec(_raises(answer))
        return _Exec(lambda: answer)

    files.get.side_effect = get
    # ensure_folder: the quarantine folder already exists.
    files.list.return_value.execute.return_value = {"files": [{"id": "quarantine"}]}
    return drive_mod.DriveFacade(svc), files


def test_delete_with_fallback_hard_deletes_when_allowed(monkeypatch):
    drive, files = _delete_drive(
        monkeypatch, caps={"capabilities": {"canDelete": True, "canTrash": True}}
    )

    drive.delete_file_with_fallback("f", fallback_remove_parent_id="intake")

    files.delete.assert_called_once_with(fileId="f", supportsAllDrives=True)
    files.update.assert_not_called()


def test_delete_with_fallback_trashes_when_only_trash_is_allowed(monkeypatch):
    drive, files = _delete_drive(
        monkeypatch, caps={"capabilities": {"canDelete": False, "canTrash": True}}
    )

    drive.delete_file_with_fallback("f")

    files.delete.assert_not_called()
    files.update.assert_called_once_with(
        fileId="f", body={"trashed": True}, supportsAllDrives=True
    )


def test_delete_with_fallback_trashes_when_the_hard_delete_fails(
    monkeypatch, as_http_error
):
    drive, files = _delete_drive(
        monkeypatch, caps={"capabilities": {"canDelete": True, "canTrash": True}}
    )
    files.delete.return_value.execute.side_effect = as_http_error(
        status=403, message="insufficientFilePermissions"
    )

    drive.delete_file_with_fallback("f")

    files.delete.assert_called_once()
    assert files.update.call_args.kwargs["body"] == {"trashed": True}


def test_delete_with_fallback_tries_delete_when_capabilities_cannot_be_read(
    monkeypatch, as_http_error
):
    drive, files = _delete_drive(
        monkeypatch, caps=as_http_error(status=403, message="forbidden")
    )

    drive.delete_file_with_fallback("f")

    files.delete.assert_called_once_with(fileId="f", supportsAllDrives=True)


def test_delete_with_fallback_moves_to_quarantine_detaching_only_the_intake_folder(
    monkeypatch,
):
    drive, files = _delete_drive(
        monkeypatch,
        caps={"capabilities": {"canDelete": False, "canTrash": False}},
        parents={"id": "f", "parents": ["intake", "elsewhere"]},
    )

    drive.delete_file_with_fallback(
        "f",
        fallback_remove_parent_id="intake",
        quarantine_folder_name="Q",
        quarantine_parent_id="qroot",
    )

    files.delete.assert_not_called()
    assert "name = 'Q'" in files.list.call_args.kwargs["q"]
    assert "'qroot' in parents" in files.list.call_args.kwargs["q"]
    files.update.assert_called_once_with(
        fileId="f",
        addParents="quarantine",
        removeParents="intake",
        fields="id,parents",
        supportsAllDrives=True,
    )


def test_delete_with_fallback_detaches_every_parent_when_intake_is_not_one(
    monkeypatch,
):
    drive, files = _delete_drive(
        monkeypatch,
        caps={"capabilities": {}},
        parents={"parents": ["a", "b"]},
    )

    drive.delete_file_with_fallback("f", fallback_remove_parent_id="intake")

    assert files.update.call_args.kwargs["removeParents"] == "a,b"


def test_delete_with_fallback_still_moves_when_parents_cannot_be_read(
    monkeypatch, as_http_error
):
    drive, files = _delete_drive(
        monkeypatch,
        caps={"capabilities": None},
        parents=as_http_error(status=404, message="not found"),
    )

    drive.delete_file_with_fallback("f", fallback_remove_parent_id="intake")

    assert files.update.call_args.kwargs["addParents"] == "quarantine"
    assert files.update.call_args.kwargs["removeParents"] == ""


def test_delete_with_fallback_raises_when_nothing_is_permitted_and_no_fallback(
    monkeypatch,
):
    drive, files = _delete_drive(
        monkeypatch, caps={"capabilities": {"canDelete": False, "canTrash": False}}
    )

    with pytest.raises(PermissionError, match="Unable to delete or trash Drive file f"):
        drive.delete_file_with_fallback("f")
    files.delete.assert_not_called()
    files.update.assert_not_called()


def test_delete_with_fallback_raises_when_every_path_fails(monkeypatch, as_http_error):
    drive, files = _delete_drive(
        monkeypatch,
        caps={"capabilities": {"canDelete": True, "canTrash": True}},
        parents={"parents": ["intake"]},
    )
    denied = as_http_error(status=403, message="insufficientFilePermissions")
    files.delete.return_value.execute.side_effect = denied
    files.update.return_value.execute.side_effect = denied

    with pytest.raises(PermissionError):
        drive.delete_file_with_fallback("f", fallback_remove_parent_id="intake")

    # Delete, then trash, then the quarantine move were all attempted.
    files.delete.assert_called_once()
    assert [c.kwargs.get("body") for c in files.update.call_args_list] == [
        {"trashed": True},
        None,
    ]
    assert files.update.call_args.kwargs["addParents"] == "quarantine"
