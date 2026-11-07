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
