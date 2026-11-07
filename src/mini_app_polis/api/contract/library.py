"""Contract for the music library registry deejay-cog keeps in the catalog.

The registry has one row per Drive file in the library: where it is, what it
is (size, checksum, duration), the catalog entry it is linked to, and whether
it is still there. A song in the library that has never been played gets a
catalog entry of its own, from the library and with no plays; stats,
summaries and the collection count plays, so those entries are not in them.

Every call here is under ``catalog.library.write``, the scope deejay-cog's
machine holds for the library.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .deejay import CatalogMatch

#: Whether a file's song is known. ``identified`` files are linked to a
#: catalog entry; ``unidentified`` ones are listed for the owner to identify.
LibraryIdentification = Literal["identified", "unidentified"]

#: The most Drive file ids one call may mark not present.
LIBRARY_ABSENT_MAX_FILES = 500


class LibraryFileTags(BaseModel):
    """A library file's own tags as last read, shown to the owner for a file
    the nightly run could not identify."""

    title: str | None = Field(default=None, description="Its title tag.")
    artist: str | None = Field(default=None, description="Its artist tag.")
    genre: str | None = Field(default=None, description="Its genre tag.")
    bpm: str | None = Field(default=None, description="Its BPM tag, as written.")
    year: str | None = Field(default=None, description="Its year tag.")
    label: str | None = Field(default=None, description="Its record label tag.")


class LibraryFileRegister(BaseModel):
    """A library file, registered or brought up to date.

    Keyed by ``drive_file_id``: the first call registers the file. A later
    call writes the required fields, the link and its status, and of the
    optional fields only those it sends. Either way the file is present
    again and its ``last_seen`` is now. ``first_seen`` is set once, by the
    first call.

    An ``identified`` file names its ``catalog_id``; an ``unidentified`` one
    has none.
    """

    drive_file_id: str = Field(..., min_length=1, description="The Drive file's id.")
    library_root_id: str = Field(
        ...,
        min_length=1,
        description="The Drive folder id of the library root the file is under.",
    )
    path: str = Field(
        ...,
        min_length=1,
        description="The file's path under its library root, with its name.",
    )
    size_bytes: int | None = Field(default=None, ge=0, description="Its size.")
    md5: str | None = Field(
        default=None, description="Drive's md5 checksum of its contents."
    )
    duration_secs: float | None = Field(
        default=None, ge=0, description="Its length, in seconds."
    )
    identification_status: LibraryIdentification = Field(
        ..., description="Whether the file's song is known."
    )
    catalog_id: uuid.UUID | None = Field(
        default=None,
        description="The catalog entry it is linked to; set when identified.",
    )
    edit_label: str | None = Field(
        default=None,
        description=(
            "This copy's release label (Clean, Explicit, Radio Edit, No Intro "
            "Edit), shown after the song's title. None: the title alone."
        ),
    )
    comment_backup: str | None = Field(
        default=None,
        description="The file's comment frame, kept as a backup; never written.",
    )
    synced_at: dt.datetime | None = Field(
        default=None,
        description="When the catalog's values were last written to the file.",
    )
    tags: LibraryFileTags | None = Field(
        default=None,
        description="The file's own tags as last read; sent for an unidentified file.",
    )
    identify_note: str | None = Field(
        default=None,
        description=(
            "Why the nightly run could not identify the file, with the "
            "fingerprint's best guess when it had one."
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _linked_when_identified(self) -> LibraryFileRegister:
        identified = self.identification_status == "identified"
        if identified != (self.catalog_id is not None):
            raise ValueError(
                "an identified file names its catalog_id; an unidentified one has none"
            )
        return self


class LibraryFile(BaseModel):
    """One file in the library registry."""

    id: uuid.UUID = Field(..., description="The registry row's id.")
    drive_file_id: str = Field(..., description="The Drive file's id.")
    library_root_id: str = Field(..., description="Its library root's folder id.")
    path: str = Field(..., description="Its path under the library root.")
    size_bytes: int | None = Field(default=None, description="Its size.")
    md5: str | None = Field(default=None, description="Its md5 checksum.")
    duration_secs: float | None = Field(default=None, description="Its length.")
    identification_status: LibraryIdentification = Field(
        ..., description="Whether its song is known."
    )
    catalog_id: uuid.UUID | None = Field(
        default=None, description="The catalog entry it is linked to."
    )
    edit_label: str | None = Field(default=None, description="Its release label.")
    comment_backup: str | None = Field(
        default=None, description="Its comment, as registered."
    )
    present: bool = Field(..., description="Whether it is still in the library.")
    trash_requested: bool = Field(
        ..., description="Whether the owner asked for it to go to Drive's trash."
    )
    first_seen: dt.datetime = Field(..., description="When it was first registered.")
    last_seen: dt.datetime = Field(..., description="When it was last registered.")
    last_synced: dt.datetime | None = Field(
        default=None, description="When the catalog's values were last written to it."
    )
    tags: LibraryFileTags | None = Field(
        default=None, description="Its own tags as last registered."
    )
    identify_note: str | None = Field(
        default=None, description="Why the nightly run could not identify it."
    )


class LibraryFilesAbsentRequest(BaseModel):
    """Files gone from the library: each is marked not present.

    Their rows, links and catalog entries are kept. An id the registry does
    not hold is ignored, so a retried call does no harm.
    """

    drive_file_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=LIBRARY_ABSENT_MAX_FILES,
        description="The Drive ids of the files that are gone.",
    )

    model_config = ConfigDict(extra="forbid")


class LibraryFilesAbsentResult(BaseModel):
    """How many registered files the call marked not present."""

    marked: int = Field(
        ..., ge=0, description="Files that were present and now are not."
    )


class LibraryFilesSynced(BaseModel):
    """Files whose owned fields now carry the catalog's values: each one's
    ``last_synced`` becomes ``synced_at``.

    An id the registry does not hold is ignored, so a retried call does no
    harm.
    """

    drive_file_ids: list[str] = Field(
        ...,
        min_length=1,
        max_length=LIBRARY_ABSENT_MAX_FILES,
        description="The Drive ids of the files written.",
    )
    synced_at: dt.datetime = Field(
        ..., description="When the catalog's values were written to them."
    )

    model_config = ConfigDict(extra="forbid")


class LibraryFilesSyncedResult(BaseModel):
    """How many registered files the call marked synced."""

    marked: int = Field(..., ge=0, description="Files whose last sync was set.")


class LibraryEntryCreate(BaseModel):
    """A catalog entry for a song in the library that has never been played.

    Made only from a confident identification. The entry comes from the
    library and has no plays. When the catalog already has an entry a play
    of the song would be filed under (the set import's own matching, across
    every owner, as ``POST /v1/catalog/lookup`` finds it), that entry is the
    answer and nothing is made.
    """

    title: str = Field(..., min_length=1, description="The song's title.")
    artist: str = Field(..., min_length=1, description="The artist.")
    remix: str | None = Field(
        default=None, description="The version (remix, live, acoustic), if any."
    )
    label: str | None = Field(default=None, description="The record label.")
    genre: str | None = Field(default=None, description="The genre.")
    bpm: float | None = Field(default=None, gt=0, description="Beats per minute.")
    release_year: int | None = Field(
        default=None, ge=1000, le=9999, description="The release year."
    )

    model_config = ConfigDict(extra="forbid")


class LibraryEntryCreated(BaseModel):
    """The library entry, and whether this call made it."""

    entry: CatalogMatch = Field(..., description="The catalog entry.")
    created: bool = Field(
        ..., description="False when an entry for the song already existed."
    )


#: Why files were grouped as copies of one song.
DuplicateReason = Literal[
    "same_md5", "same_catalog_id", "same_title_artist", "same_name"
]

#: The most groups one duplicate report may carry.
DUPLICATE_REPORT_MAX_GROUPS = 2000


class DuplicateMember(BaseModel):
    """One registered file in a duplicate group.

    A file kept on purpose (a different edit, such as a No Intro Edit whose
    length differs, or a different release label, such as Clean against
    Explicit) is shown in its group but can never be sent to the trash
    through it.
    """

    drive_file_id: str = Field(..., min_length=1, description="The Drive file's id.")
    kept_on_purpose: bool = Field(
        default=False, description="A different edit or label: not a duplicate."
    )
    why_kept: str | None = Field(
        default=None, description="Why the file is kept on purpose, when it is."
    )
    bitrate_kbps: int | None = Field(
        default=None, ge=0, description="Its bitrate, when known."
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _why_when_kept(self) -> DuplicateMember:
        if self.kept_on_purpose and not self.why_kept:
            raise ValueError("a file kept on purpose says why")
        return self


class DuplicateGroupReport(BaseModel):
    """Files that look like copies of one song, and why they were grouped.

    ``key`` names the group the same way every run (for example the shared
    md5 or catalog id), so an unchanged library reports the same groups.
    """

    key: str = Field(..., min_length=1, description="The group's stable key.")
    reasons: list[DuplicateReason] = Field(
        ..., min_length=1, description="Why the files were grouped."
    )
    files: list[DuplicateMember] = Field(
        ..., min_length=2, description="The files in the group."
    )

    model_config = ConfigDict(extra="forbid")


class DuplicateReport(BaseModel):
    """Every duplicate group a report run found under one library root.

    The report replaces the open groups the API holds for that root: an open
    group the report does not include is dropped, and groups the owner has
    resolved are kept.
    """

    library_root_id: str = Field(
        ..., min_length=1, description="The library root the groups are under."
    )
    groups: list[DuplicateGroupReport] = Field(
        ...,
        max_length=DUPLICATE_REPORT_MAX_GROUPS,
        description="The groups found; empty when the library has none.",
    )

    model_config = ConfigDict(extra="forbid")


class DuplicateReportResult(BaseModel):
    """What the report changed among the open groups."""

    open_groups: int = Field(..., ge=0, description="Open groups after the report.")
    added: int = Field(..., ge=0, description="Groups the report added.")
    dropped: int = Field(
        ..., ge=0, description="Open groups the report did not include, now dropped."
    )


#: The most catalog entries one display read may ask for.
CATALOG_DISPLAY_MAX_IDS = 200


class CatalogDisplayRequest(BaseModel):
    """Catalog entries whose owned fields a reconcile compares with files."""

    ids: list[uuid.UUID] = Field(
        ...,
        min_length=1,
        max_length=CATALOG_DISPLAY_MAX_IDS,
        description="The catalog entries to read, in the order the answers come back.",
    )

    model_config = ConfigDict(extra="forbid")


class CatalogDisplay(BaseModel):
    """A catalog entry's owned fields as a file should carry them: written out
    by the display rules, so the title includes the version."""

    id: uuid.UUID = Field(..., description="The catalog entry's id.")
    title: str = Field(..., description="The title, with its version.")
    artist: str = Field(..., description="The artist, with any featured artists.")
    release_year: int | None = Field(default=None, description="The release year.")
    genre: str | None = Field(default=None, description="The broad genre.")
    subgenre: str | None = Field(default=None, description="The subgenre.")
    bpm: float | None = Field(default=None, description="BPM, as the rules round it.")
    label: str | None = Field(default=None, description="The record label.")
    changed_at: dt.datetime = Field(
        ...,
        description=(
            "When what is shown last changed: the entry, or the display rules, "
            "whichever is later."
        ),
    )


class CatalogFillUp(BaseModel):
    """A library file's values for fields its catalog entry may leave empty.

    Only fields the entry leaves empty are set; a value the catalog has is
    never overwritten. A genre is placed on the catalog's vocabulary first.
    """

    release_year: int | None = Field(
        default=None, ge=1000, le=9999, description="The file's year."
    )
    genre: str | None = Field(default=None, description="The file's genre.")
    bpm: float | None = Field(default=None, gt=0, description="The file's BPM.")
    label: str | None = Field(default=None, description="The file's label.")

    model_config = ConfigDict(extra="forbid")


class CatalogFillUpResult(BaseModel):
    """The entry after a fill-up, and which fields it filled."""

    entry: CatalogDisplay = Field(..., description="The entry, as it is now.")
    filled: list[str] = Field(
        default_factory=list, description="The fields the call set."
    )


#: The kinds a library reconcile files a difference under (deejay-cog's
#: library_reconcile). ``other`` is approved file by file, never as a kind.
ReconcileKind = Literal[
    "case_punctuation",
    "featured_moved",
    "artist_joiner",
    "version_brackets",
    "genre_remapped",
    "genre_filled",
    "year_filled",
    "year_earlier",
    "year_later",
    "year_differs",
    "bpm_rounding",
    "bpm_differs",
    "label_differs",
    "fill_up",
    "other",
]

#: The fields a reconcile compares (and ``catalog_id``, the file's link tag).
ReconcileField = Literal[
    "title", "artist", "year", "genre", "bpm", "label", "catalog_id"
]

#: The most files one reconcile report may name as compared.
RECONCILE_REPORT_MAX_FILES = 500


class ReconcileDifference(BaseModel):
    """One field where a library file and its catalog entry differ."""

    drive_file_id: str = Field(..., min_length=1, description="The Drive file's id.")
    path: str = Field(..., description="The file's path under the library root.")
    field: ReconcileField = Field(..., description="The field that differs.")
    kind: ReconcileKind = Field(..., description="The kind it is filed under.")
    file_value: str = Field(..., description="The file's value ('' when empty).")
    catalog_value: str = Field(..., description="The catalog's value ('' when empty).")

    model_config = ConfigDict(extra="forbid")


class ReconcileReport(BaseModel):
    """The files one reconcile run compared, and their differences.

    For every file named in ``compared``, the report replaces what the API
    holds: its differences become these, and a compared file with none has
    none left. Files the run did not compare keep theirs.
    """

    run_id: str = Field(..., min_length=1, description="The run that compared them.")
    compared: list[str] = Field(
        ...,
        max_length=RECONCILE_REPORT_MAX_FILES,
        description="The Drive ids of every file the run compared.",
    )
    differences: list[ReconcileDifference] = Field(
        default_factory=list,
        max_length=RECONCILE_REPORT_MAX_FILES * 7,
        description="Their differences; each one's file is in compared.",
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _differences_are_of_compared_files(self) -> ReconcileReport:
        named = set(self.compared)
        stray = sorted({d.drive_file_id for d in self.differences} - named)
        if stray:
            raise ValueError(f"differences for files not in compared: {stray[:5]}")
        return self


class ReconcileReportResult(BaseModel):
    """The open differences after a report."""

    open_differences: int = Field(..., ge=0, description="Differences now held.")
    files_with_differences: int = Field(
        ..., ge=0, description="Files that have at least one."
    )


class ReconcileApprovalsSet(BaseModel):
    """What the owner approved for the reconcile's write runs: whole kinds,
    and ``other`` differences one file and field at a time."""

    kinds: list[ReconcileKind] = Field(
        default_factory=list, description="The approved kinds (never 'other')."
    )
    files: dict[str, list[ReconcileField]] = Field(
        default_factory=dict,
        description="Approved 'other' differences: Drive file id to its fields.",
    )

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _other_is_by_file(self) -> ReconcileApprovalsSet:
        if "other" in self.kinds:
            raise ValueError("'other' is approved file by file, under files")
        return self


class ReconcileApprovals(ReconcileApprovalsSet):
    """The approvals as the API holds them, and who last changed them."""

    updated_at: dt.datetime | None = Field(
        default=None, description="When they last changed; None if never set."
    )
    updated_by: str | None = Field(default=None, description="Who last changed them.")

    # A response: fields this library does not know yet are ignored.
    model_config = ConfigDict(extra="ignore")
