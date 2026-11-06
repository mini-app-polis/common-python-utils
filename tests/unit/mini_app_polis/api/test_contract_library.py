"""The library registry contract: files registered, marked gone, listed, and
catalog entries made for songs that were never played."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from mini_app_polis.api import KaianoApiClient
from mini_app_polis.api.contract import (
    ENDPOINTS_BY_NAME,
    LIBRARY_ABSENT_MAX_FILES,
    LibraryEntryCreate,
    LibraryEntryCreated,
    LibraryFile,
    LibraryFileRegister,
    LibraryFilesAbsentRequest,
    LibraryFilesAbsentResult,
)

_META = {"count": 1, "total": 1, "version": "test"}
_ENTRY = uuid.UUID("3f2b9c1e-7a4d-4e8b-9c1f-2a3b4c5d6e7f")
_ROW = "9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d"


def _register(**overrides: Any) -> LibraryFileRegister:
    base: dict[str, Any] = {
        "drive_file_id": "drive-1",
        "library_root_id": "root-1",
        "path": "House/Temple_Thomas Gold.mp3",
        "identification_status": "identified",
        "catalog_id": _ENTRY,
    }
    base.update(overrides)
    return LibraryFileRegister(**base)


def _file_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": _ROW,
        "drive_file_id": "drive-1",
        "library_root_id": "root-1",
        "path": "House/Temple_Thomas Gold.mp3",
        "size_bytes": 9_000_000,
        "md5": "abc",
        "duration_secs": 215.5,
        "identification_status": "identified",
        "catalog_id": str(_ENTRY),
        "edit_label": None,
        "comment_backup": "cue at 0:32",
        "present": True,
        "trash_requested": False,
        "first_seen": "2026-11-06T10:00:00Z",
        "last_seen": "2026-11-06T10:00:00Z",
        "last_synced": None,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("name", "method", "path", "request_model"),
    [
        (
            "register_library_file",
            "POST",
            "/v1/catalog/library/files",
            LibraryFileRegister,
        ),
        (
            "mark_library_files_absent",
            "POST",
            "/v1/catalog/library/files/absent",
            LibraryFilesAbsentRequest,
        ),
        ("list_library_files", "GET", "/v1/catalog/library/files", None),
        (
            "create_library_entry",
            "POST",
            "/v1/catalog/library/entries",
            LibraryEntryCreate,
        ),
    ],
)
def test_each_library_endpoint_is_under_the_library_scope(
    name: str, method: str, path: str, request_model: Any
) -> None:
    endpoint = ENDPOINTS_BY_NAME[name]
    assert endpoint.method == method
    assert endpoint.path == path
    assert endpoint.request is request_model
    assert endpoint.scope == "catalog.library.write"


def test_an_identified_file_names_its_catalog_entry() -> None:
    with pytest.raises(ValidationError):
        _register(catalog_id=None)
    assert _register().catalog_id == _ENTRY


def test_an_unidentified_file_has_no_catalog_entry() -> None:
    with pytest.raises(ValidationError):
        _register(identification_status="unidentified")
    row = _register(identification_status="unidentified", catalog_id=None)
    assert row.catalog_id is None


def test_a_registration_refuses_unknown_fields_and_bad_values() -> None:
    with pytest.raises(ValidationError):
        _register(present=False)
    with pytest.raises(ValidationError):
        _register(identification_status="maybe")
    with pytest.raises(ValidationError):
        _register(size_bytes=-1)
    with pytest.raises(ValidationError):
        _register(drive_file_id="")


def test_absent_takes_between_one_and_the_most_files() -> None:
    with pytest.raises(ValidationError):
        LibraryFilesAbsentRequest(drive_file_ids=[])
    with pytest.raises(ValidationError):
        LibraryFilesAbsentRequest(drive_file_ids=["x"] * (LIBRARY_ABSENT_MAX_FILES + 1))
    assert LibraryFilesAbsentRequest(drive_file_ids=["x"]).drive_file_ids == ["x"]


def test_a_library_entry_needs_a_title_and_artist_and_refuses_extras() -> None:
    with pytest.raises(ValidationError):
        LibraryEntryCreate(title="", artist="Thomas Gold")
    with pytest.raises(ValidationError):
        LibraryEntryCreate.model_validate(
            {"title": "Temple", "artist": "Thomas Gold", "play_count": 0}
        )
    entry = LibraryEntryCreate(title="Temple", artist="Thomas Gold", bpm=124.0)
    assert entry.bpm == 124.0


def test_a_library_file_round_trips_and_tolerates_new_fields() -> None:
    row = LibraryFile.model_validate(_file_row(something_new="x"))
    assert row.catalog_id == _ENTRY
    assert row.present is True
    assert row.last_synced is None


def _client(
    monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]
) -> tuple[KaianoApiClient, list[tuple[str, str, Any]]]:
    calls: list[tuple[str, str, Any]] = []

    def fake_post(self, path, payload):  # noqa: ARG001
        calls.append(("POST", path, payload))
        return answer

    def fake_get(self, path, params=None):  # noqa: ARG001
        calls.append(("GET", path, params))
        return answer

    monkeypatch.setattr(KaianoApiClient, "post", fake_post)
    monkeypatch.setattr(KaianoApiClient, "get", fake_get)
    return KaianoApiClient(base_url="https://x", api_key="k"), calls


def test_registering_sends_only_the_fields_set(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _client(monkeypatch, {"data": _file_row(), "meta": _META})

    row = client.register_library_file(_register(md5="abc"))

    assert calls == [
        (
            "POST",
            "/v1/catalog/library/files",
            {
                "drive_file_id": "drive-1",
                "library_root_id": "root-1",
                "path": "House/Temple_Thomas Gold.mp3",
                "identification_status": "identified",
                "catalog_id": str(_ENTRY),
                "md5": "abc",
            },
        )
    ]
    assert isinstance(row, LibraryFile)


def test_marking_absent_posts_the_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _client(monkeypatch, {"data": {"marked": 2}, "meta": _META})

    result = client.mark_library_files_absent(
        LibraryFilesAbsentRequest(drive_file_ids=["a", "b"])
    )

    assert calls == [
        ("POST", "/v1/catalog/library/files/absent", {"drive_file_ids": ["a", "b"]})
    ]
    assert result == LibraryFilesAbsentResult(marked=2)


def test_listing_sends_only_the_filters_given(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _client(monkeypatch, {"data": [_file_row()], "meta": _META})

    rows = client.list_library_files(catalog_id=_ENTRY, present=False, limit=10)

    assert calls == [
        (
            "GET",
            "/v1/catalog/library/files",
            {"catalog_id": str(_ENTRY), "present": "false", "limit": 10},
        )
    ]
    assert [r.drive_file_id for r in rows] == ["drive-1"]


def test_listing_with_no_filters_sends_none(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _client(monkeypatch, {"data": [], "meta": _META})

    assert client.list_library_files(identification_status="unidentified") == []
    assert calls == [
        (
            "GET",
            "/v1/catalog/library/files",
            {"identification_status": "unidentified"},
        )
    ]


def test_creating_an_entry_reads_whether_it_was_made(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = {
        "data": {
            "entry": {"id": str(_ENTRY), "title": "Temple", "artist": "Thomas Gold"},
            "created": False,
        },
        "meta": _META,
    }
    client, calls = _client(monkeypatch, answer)

    out = client.create_library_entry(
        LibraryEntryCreate(title="Temple", artist="Thomas Gold")
    )

    assert calls == [
        (
            "POST",
            "/v1/catalog/library/entries",
            {"title": "Temple", "artist": "Thomas Gold"},
        )
    ]
    assert isinstance(out, LibraryEntryCreated)
    assert out.created is False
    assert out.entry.id == _ENTRY
