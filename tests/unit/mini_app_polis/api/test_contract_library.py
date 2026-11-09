"""The library registry contract: files registered, marked gone, listed, and
catalog entries made for songs that were never played."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from mini_app_polis.api import KaianoApiClient
from mini_app_polis.api.contract import (
    CATALOG_DISPLAY_MAX_IDS,
    DUPLICATE_REPORT_MAX_GROUPS,
    ENDPOINTS_BY_NAME,
    LIBRARY_ABSENT_MAX_FILES,
    RECONCILE_REPORT_MAX_FILES,
    CatalogDisplay,
    CatalogDisplayRequest,
    CatalogFillUp,
    CatalogFillUpResult,
    DuplicateGroupReport,
    DuplicateMember,
    DuplicateReport,
    DuplicateReportResult,
    LibraryEntryCreate,
    LibraryEntryCreated,
    LibraryFile,
    LibraryFileRegister,
    LibraryFilesAbsentRequest,
    LibraryFilesAbsentResult,
    LibraryFilesSynced,
    LibraryFilesSyncedResult,
    LibraryFileTags,
    ReconcileApprovalsSet,
    ReconcileDifference,
    ReconcileReport,
    ReconcileReportResult,
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
    assert entry.preview is False


def test_a_library_entry_preview_is_sent_only_when_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = {
        "data": {
            "entry": {"id": str(_ENTRY), "title": "Temple", "artist": "Thomas Gold"},
            "created": False,
            "previewed": True,
        },
        "meta": _META,
    }
    client, calls = _client(monkeypatch, answer)

    out = client.create_library_entry(
        LibraryEntryCreate(title="Temple", artist="Thomas Gold", preview=True)
    )

    assert calls == [
        (
            "POST",
            "/v1/catalog/library/entries",
            {"title": "Temple", "artist": "Thomas Gold", "preview": True},
        )
    ]
    assert out.previewed is True
    assert out.created is False


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


def test_listing_after_a_row_sends_its_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _client(monkeypatch, {"data": [], "meta": _META})
    last = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

    assert client.list_library_files(after=last, limit=200) == []
    assert calls == [
        (
            "GET",
            "/v1/catalog/library/files",
            {"limit": 200, "after": str(last)},
        )
    ]


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
    assert out.previewed is False


# ── The reconcile's display read and fill-up ──────────────────────────────


def test_the_display_read_and_fill_up_are_under_the_library_scope() -> None:
    display = ENDPOINTS_BY_NAME["read_catalog_display"]
    assert (display.method, display.path) == ("POST", "/v1/catalog/display")
    assert display.request is CatalogDisplayRequest
    fill = ENDPOINTS_BY_NAME["fill_up_catalog_entry"]
    assert (fill.method, fill.path) == ("POST", "/v1/catalog/{catalog_id}/fill-up")
    assert fill.request is CatalogFillUp
    assert {display.scope, fill.scope} == {"catalog.library.write"}


def test_a_display_read_takes_between_one_and_the_most_ids() -> None:
    with pytest.raises(ValidationError):
        CatalogDisplayRequest(ids=[])
    with pytest.raises(ValidationError):
        CatalogDisplayRequest(ids=[_ENTRY] * (CATALOG_DISPLAY_MAX_IDS + 1))
    assert CatalogDisplayRequest(ids=[_ENTRY]).ids == [_ENTRY]


def test_a_fill_up_refuses_fields_it_does_not_own() -> None:
    with pytest.raises(ValidationError):
        CatalogFillUp.model_validate({"title": "Temple"})
    with pytest.raises(ValidationError):
        CatalogFillUp(release_year=19)
    assert CatalogFillUp(genre="House").genre == "House"


def _display(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": str(_ENTRY),
        "title": "Temple (Niko The Kid Remix)",
        "artist": "Thomas Gold",
        "release_year": 2019,
        "genre": "Dance/Electronic",
        "subgenre": "House",
        "bpm": 124.0,
        "label": "Armada",
        "changed_at": "2026-11-06T10:00:00Z",
    }
    row.update(overrides)
    return row


def test_the_client_reads_displays_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _client(monkeypatch, {"data": [_display(), None], "meta": _META})
    other = uuid.UUID("00000000-0000-4000-8000-000000000000")

    found, missing = client.read_catalog_display(
        CatalogDisplayRequest(ids=[_ENTRY, other])
    )

    assert calls == [
        ("POST", "/v1/catalog/display", {"ids": [str(_ENTRY), str(other)]})
    ]
    assert isinstance(found, CatalogDisplay) and found.subgenre == "House"
    assert missing is None


def test_the_client_fills_up_one_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _client(
        monkeypatch,
        {"data": {"entry": _display(), "filled": ["genre"]}, "meta": _META},
    )

    result = client.fill_up_catalog_entry(_ENTRY, CatalogFillUp(genre="House"))

    assert calls == [("POST", f"/v1/catalog/{_ENTRY}/fill-up", {"genre": "House"})]
    assert isinstance(result, CatalogFillUpResult)
    assert result.filled == ["genre"]


def test_the_sync_endpoints_are_under_the_library_scope() -> None:
    listing = ENDPOINTS_BY_NAME["list_library_files_needing_sync"]
    assert (listing.method, listing.path, listing.request) == (
        "GET",
        "/v1/catalog/library/files/needing-sync",
        None,
    )
    marking = ENDPOINTS_BY_NAME["mark_library_files_synced"]
    assert (marking.method, marking.path, marking.request) == (
        "POST",
        "/v1/catalog/library/files/synced",
        LibraryFilesSynced,
    )
    assert listing.scope == marking.scope == "catalog.library.write"


def test_synced_takes_between_one_and_the_most_files_and_a_time() -> None:
    when = dt.datetime(2026, 11, 7, tzinfo=dt.UTC)
    with pytest.raises(ValidationError):
        LibraryFilesSynced(drive_file_ids=[], synced_at=when)
    with pytest.raises(ValidationError):
        LibraryFilesSynced(
            drive_file_ids=["x"] * (LIBRARY_ABSENT_MAX_FILES + 1), synced_at=when
        )
    with pytest.raises(ValidationError):
        LibraryFilesSynced.model_validate({"drive_file_ids": ["a"]})


def test_listing_needing_sync_sends_only_the_arguments_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, calls = _client(monkeypatch, {"data": [_file_row()], "meta": _META})

    rows = client.list_library_files_needing_sync(library_root_id="root", limit=50)

    assert calls == [
        (
            "GET",
            "/v1/catalog/library/files/needing-sync",
            {"library_root_id": "root", "limit": 50},
        )
    ]
    assert [r.drive_file_id for r in rows] == ["drive-1"]


def test_marking_synced_posts_the_ids_and_the_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, calls = _client(monkeypatch, {"data": {"marked": 1}, "meta": _META})
    when = dt.datetime(2026, 11, 7, 4, 41, tzinfo=dt.UTC)

    result = client.mark_library_files_synced(
        LibraryFilesSynced(drive_file_ids=["a"], synced_at=when)
    )

    assert calls == [
        (
            "POST",
            "/v1/catalog/library/files/synced",
            {"drive_file_ids": ["a"], "synced_at": "2026-11-07T04:41:00Z"},
        )
    ]
    assert result == LibraryFilesSyncedResult(marked=1)


def _group(key: str = "same_md5:abc") -> dict[str, Any]:
    return {
        "key": key,
        "reasons": ["same_md5"],
        "files": [
            {"drive_file_id": "a"},
            {
                "drive_file_id": "b",
                "kept_on_purpose": True,
                "why_kept": "No Intro Edit: 12 s shorter",
            },
        ],
    }


def test_the_duplicate_report_is_under_the_library_scope() -> None:
    endpoint = ENDPOINTS_BY_NAME["report_duplicate_groups"]
    assert (endpoint.method, endpoint.path, endpoint.request) == (
        "POST",
        "/v1/catalog/library/duplicates",
        DuplicateReport,
    )
    assert endpoint.scope == "catalog.library.write"


def test_a_group_has_two_files_a_reason_and_says_why_a_file_is_kept() -> None:
    DuplicateGroupReport.model_validate(_group())
    with pytest.raises(ValidationError):
        DuplicateGroupReport.model_validate(
            {**_group(), "files": [{"drive_file_id": "a"}]}
        )
    with pytest.raises(ValidationError):
        DuplicateGroupReport.model_validate({**_group(), "reasons": []})
    with pytest.raises(ValidationError):
        DuplicateGroupReport.model_validate({**_group(), "reasons": ["vibes"]})
    with pytest.raises(ValidationError):
        DuplicateMember(drive_file_id="a", kept_on_purpose=True)


def test_a_report_may_be_empty_and_has_a_ceiling() -> None:
    assert DuplicateReport(library_root_id="root", groups=[]).groups == []
    with pytest.raises(ValidationError):
        DuplicateReport.model_validate(
            {
                "library_root_id": "root",
                "groups": [
                    _group(f"k{n}") for n in range(DUPLICATE_REPORT_MAX_GROUPS + 1)
                ],
            }
        )


def test_reporting_posts_the_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _client(
        monkeypatch,
        {"data": {"open_groups": 1, "added": 1, "dropped": 0}, "meta": _META},
    )

    result = client.report_duplicate_groups(
        DuplicateReport.model_validate(
            {"library_root_id": "root", "groups": [_group()]}
        )
    )

    [(method, path, body)] = calls
    assert (method, path) == ("POST", "/v1/catalog/library/duplicates")
    assert body["library_root_id"] == "root"
    assert body["groups"][0]["files"][1] == {
        "drive_file_id": "b",
        "kept_on_purpose": True,
        "why_kept": "No Intro Edit: 12 s shorter",
    }
    assert result == DuplicateReportResult(open_groups=1, added=1, dropped=0)


def test_listing_can_ask_for_the_files_to_trash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, calls = _client(monkeypatch, {"data": [], "meta": _META})

    client.list_library_files(trash_requested=True, present=True)

    assert calls == [
        (
            "GET",
            "/v1/catalog/library/files",
            {"present": "true", "trash_requested": "true"},
        )
    ]


# ── Tags and the identify note, for the owner's unidentified page ────────


def test_an_unidentified_file_may_carry_its_tags_and_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, calls = _client(
        monkeypatch,
        {
            "data": _file_row(
                identification_status="unidentified",
                catalog_id=None,
                tags={"title": "Temple", "artist": "T. Gold"},
                identify_note="fingerprint: no confident match",
            ),
            "meta": _META,
        },
    )

    row = client.register_library_file(
        _register(
            identification_status="unidentified",
            catalog_id=None,
            tags=LibraryFileTags(title="Temple", artist="T. Gold", bpm="124"),
            identify_note="fingerprint: no confident match",
        )
    )

    sent = calls[0][2]
    assert sent["tags"] == {"title": "Temple", "artist": "T. Gold", "bpm": "124"}
    assert sent["identify_note"] == "fingerprint: no confident match"
    assert row.tags == LibraryFileTags(title="Temple", artist="T. Gold")
    assert row.identify_note == "fingerprint: no confident match"


def test_tags_answered_with_a_field_this_version_lacks_still_read() -> None:
    row = LibraryFile.model_validate(_file_row(tags={"title": "x", "mood": "warm"}))
    assert row.tags is not None and row.tags.title == "x"


def test_a_row_without_tags_reads_as_none() -> None:
    row = LibraryFile.model_validate(_file_row())
    assert row.tags is None and row.identify_note is None


def _difference(file_id: str = "a", kind: str = "other") -> dict[str, Any]:
    return {
        "drive_file_id": file_id,
        "path": "Pop/song.mp3",
        "field": "title",
        "kind": kind,
        "file_value": "Song",
        "catalog_value": "Song (Remix)",
    }


def test_the_reconcile_endpoints_are_under_the_library_scope() -> None:
    expected = {
        "report_reconcile_differences": (
            "POST",
            "/v1/catalog/library/reconcile/differences",
            ReconcileReport,
        ),
        "read_reconcile_approvals": (
            "GET",
            "/v1/catalog/library/reconcile/approvals",
            None,
        ),
        "seed_reconcile_approvals": (
            "POST",
            "/v1/catalog/library/reconcile/approvals/seed",
            ReconcileApprovalsSet,
        ),
    }
    for name, shape in expected.items():
        endpoint = ENDPOINTS_BY_NAME[name]
        assert (endpoint.method, endpoint.path, endpoint.request) == shape
        assert endpoint.scope == "catalog.library.write"


def test_a_reconcile_report_names_every_file_it_has_differences_for() -> None:
    ReconcileReport.model_validate(
        {"run_id": "r", "compared": ["a", "b"], "differences": [_difference()]}
    )
    with pytest.raises(ValidationError):
        ReconcileReport.model_validate(
            {"run_id": "r", "compared": ["b"], "differences": [_difference()]}
        )
    with pytest.raises(ValidationError):
        ReconcileDifference.model_validate({**_difference(), "kind": "vibes"})
    with pytest.raises(ValidationError):
        ReconcileReport.model_validate(
            {
                "run_id": "r",
                "compared": [f"f{n}" for n in range(RECONCILE_REPORT_MAX_FILES + 1)],
            }
        )


def test_other_is_approved_file_by_file_not_as_a_kind() -> None:
    ReconcileApprovalsSet(kinds=["fill_up"], files={"a": ["title", "catalog_id"]})
    with pytest.raises(ValidationError):
        ReconcileApprovalsSet.model_validate({"kinds": ["other"]})
    with pytest.raises(ValidationError):
        ReconcileApprovalsSet.model_validate({"files": {"a": ["subgenre"]}})


def test_reporting_differences_posts_them(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _client(
        monkeypatch,
        {"data": {"open_differences": 1, "files_with_differences": 1}, "meta": _META},
    )

    result = client.report_reconcile_differences(
        ReconcileReport.model_validate(
            {"run_id": "r", "compared": ["a"], "differences": [_difference()]}
        )
    )

    [(method, path, body)] = calls
    assert (method, path) == ("POST", "/v1/catalog/library/reconcile/differences")
    assert body["differences"][0]["catalog_value"] == "Song (Remix)"
    assert result == ReconcileReportResult(open_differences=1, files_with_differences=1)


def test_reading_approvals_gets_them(monkeypatch: pytest.MonkeyPatch) -> None:
    client, calls = _client(
        monkeypatch,
        {
            "data": {
                "kinds": ["fill_up"],
                "files": {"a": ["title"]},
                "updated_at": "2026-11-07T21:00:00Z",
                "updated_by": "owner",
            },
            "meta": _META,
        },
    )

    approvals = client.read_reconcile_approvals()

    [(method, path, _)] = calls
    assert (method, path) == ("GET", "/v1/catalog/library/reconcile/approvals")
    assert approvals.kinds == ["fill_up"]
    assert approvals.files == {"a": ["title"]}
