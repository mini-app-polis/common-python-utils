"""Contract for the catalog calls deejay-cog makes.

``POST /v1/ingest``, ``POST /v1/live-plays`` and ``POST /v1/spotify/playlists``,
and ``GET /v1/sets`` to see which sets the API already holds.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field


class IngestTrack(BaseModel):
    """TODO: describe this class."""

    play_order: int | None = Field(
        default=None, description="Semantic value for play order."
    )
    play_time: dt.time | None = Field(
        default=None, description="Semantic value for play time."
    )

    label: str | None = Field(default=None, description="Semantic value for label.")
    title: str = Field(..., description="Title value for this record.")
    remix: str | None = Field(default=None, description="Semantic value for remix.")
    artist: str = Field(..., description="Artist name associated with this record.")
    comment: str | None = Field(default=None, description="Semantic value for comment.")

    genre: str | None = Field(default=None, description="Semantic value for genre.")
    bpm: float | None = Field(default=None, description="Semantic value for bpm.")
    release_year: int | None = Field(
        default=None, description="Semantic value for release year."
    )
    length_secs: int | None = Field(
        default=None, description="Semantic value for length secs."
    )

    model_config = ConfigDict(extra="forbid")


class IngestSet(BaseModel):
    """Payload for ingesting one DJ set and its tracks."""

    set_date: dt.date = Field(..., description="Calendar date the set was played.")
    venue: str = Field(..., description="Venue name for the set or play.")
    source_file: str = Field(..., description="Semantic value for source file.")
    tracks: list[IngestTrack] = Field(..., description="Semantic value for tracks.")


class IngestResponseData(BaseModel):
    """Result counters produced by set-ingest operations."""

    set_id: uuid.UUID = Field(..., description="Semantic value for set id.")
    tracks_created: int = Field(..., description="Semantic value for tracks created.")
    catalog_new: int = Field(..., description="Semantic value for catalog new.")
    catalog_updated: int = Field(..., description="Semantic value for catalog updated.")
    catalog_unchanged: int = Field(
        ..., description="Semantic value for catalog unchanged."
    )


class LivePlayIngest(BaseModel):
    """One live-play row accepted by ingest endpoints."""

    played_at: dt.datetime = Field(..., description="Semantic value for played at.")
    title: str = Field(..., description="Title value for this record.")
    artist: str = Field(..., description="Artist name associated with this record.")

    model_config = ConfigDict(extra="forbid")


class LivePlaysIngest(BaseModel):
    """Batch payload for live-play ingest."""

    plays: list[LivePlayIngest] = Field(..., description="Semantic value for plays.")

    model_config = ConfigDict(extra="forbid")


class LivePlaysResponseData(BaseModel):
    """Ingest counters for live-play upsert operations."""

    inserted: int = Field(..., description="Semantic value for inserted.")
    skipped: int = Field(..., description="Semantic value for skipped.")


class SpotifyPlaylistIngest(BaseModel):
    """One Spotify playlist payload accepted for ingest."""

    id: str = Field(
        ..., description="Unique identifier for this spotifyplaylistingest."
    )
    name: str = Field(..., description="Human-readable name.")
    url: str = Field(..., description="Semantic value for url.")
    uri: str = Field(..., description="Semantic value for uri.")
    type: str = Field(default="playlist", description="Semantic value for type.")
    public: bool = Field(default=True, description="Semantic value for public.")
    collaborative: bool = Field(
        default=False, description="Semantic value for collaborative."
    )
    snapshot_id: str | None = Field(
        default=None, description="Semantic value for snapshot id."
    )
    tracks_total: int = Field(default=0, description="Semantic value for tracks total.")
    owner_id: str = Field(
        ..., description="Owner identity associated with this record."
    )
    owner_name: str | None = Field(
        default=None, description="Semantic value for owner name."
    )

    model_config = ConfigDict(extra="forbid")


class SpotifyPlaylistsIngest(BaseModel):
    """Batch payload for Spotify playlist ingest."""

    playlists: list[SpotifyPlaylistIngest] = Field(
        ..., description="Semantic value for playlists."
    )

    model_config = ConfigDict(extra="forbid")


class SpotifyPlaylistsIngestResponse(BaseModel):
    """Ingest counters for Spotify playlist upserts."""

    upserted: int = Field(..., description="Semantic value for upserted.")
    unchanged: int = Field(..., description="Semantic value for unchanged.")


class SetListItem(BaseModel):
    """One set as ``GET /v1/sets`` lists it.

    ``source_file`` is what the cog sent as the set's source file: the
    sheet's base name. It is how a caller tells which of its sheets the API
    already has.
    """

    id: uuid.UUID = Field(..., description="The set's id.")
    set_date: dt.date = Field(..., description="Calendar date the set was played.")
    year: int = Field(..., description="Year of set_date.")
    venue: str = Field(..., description="Venue name for the set.")
    source_file: str | None = Field(
        default=None, description="Source file the set was ingested from."
    )
    track_count: int = Field(default=0, description="Number of tracks in the set.")
