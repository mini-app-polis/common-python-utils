"""Contract for the run requests watcher-cog makes.

``POST /v1/deejay/runs`` and ``POST /v1/transcription/runs``. Both answer 202
when they enqueue and 200 when the request is deduplicated, with the same body.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DriveFileRef(BaseModel):
    """One Drive file a watcher saw, for the API to claim before dispatching.

    ``revision`` is the file's modifiedTime, sent only for a folder whose
    files are edited in place and never leave; it makes the claim one per
    version rather than one per file. See services/dispatch_claims.py.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=256, description="Drive file id.")
    revision: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description=(
            "The file's modifiedTime, for a file edited in place. Absent for "
            "a drained inbox, where the file being present is the work."
        ),
    )


class DeejayRunRequest(BaseModel):
    """Ask deejay-cog to run one of its router modes.

    The body is what watcher-cog used to pass to Prefect as flow-run
    parameters, unchanged. An unknown mode is a 422 here rather than a
    message the cog would dead-letter seventeen minutes later.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["process-new-files", "ingest-live-history"] = Field(
        ...,
        description=(
            "Which deejay-cog flow to run. Mirrors DeejayMode in deejay-cog; "
            "the cog refuses a mode it does not recognise."
        ),
    )
    drive_files: list[DriveFileRef] | None = Field(
        None,
        min_length=1,
        max_length=500,
        description=(
            "The files in the watched folder. When present, each is claimed "
            "and the sweep is enqueued only if at least one claim is new or "
            "renewed; otherwise the request is deduplicated. When absent the "
            "sweep is enqueued unconditionally, which is an operator's run."
        ),
    )


class DeejayRunAccepted(BaseModel):
    """The acknowledgement. Not a result — nothing has run yet."""

    accepted: bool = Field(
        True,
        description=(
            "The work is on the queue — this request's message, or, when "
            "deduplicated, an earlier one's."
        ),
    )
    message_id: str = Field(
        "",
        description=(
            "The queue message this request became. What the API can "
            "honestly say it did, and the handle for tracing the job."
        ),
    )
    mode: str = Field(..., description="Flow that will run.")
    deduplicated: bool = Field(
        False,
        description=(
            "Nothing was enqueued because every file named is already "
            "claimed by an earlier request. ``message_id`` is that "
            "request's job, when it has reached the queue."
        ),
    )


class TranscriptionRunRequest(BaseModel):
    """Ask transcription-cog to process one Drive file.

    ``mode`` is what watcher-cog used to pass to Prefect as a flow-run
    parameter, unchanged. ``drive_file_id`` is new: watcher names each file
    it saw change, because one file is what fits in one Lambda invocation.
    The retention sweep, ``voicenotes-cleanup``, works on the archive and
    names no file. Either mistake is a 422 here rather than a message the
    cog would dead-letter an hour later.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["wcs-transcripts", "voicenotes", "voicenotes-cleanup"] = Field(
        ...,
        description=(
            "Which transcription-cog pipeline to run. Mirrors MODES in "
            "transcription-cog's worker; the cog refuses a mode it does not "
            "recognise."
        ),
    )
    drive_file_id: str | None = Field(
        None,
        min_length=1,
        max_length=256,
        description=(
            "The Drive file to process. Required for wcs-transcripts and "
            "voicenotes; refused for voicenotes-cleanup."
        ),
    )

    @model_validator(mode="after")
    def _file_matches_mode(self) -> TranscriptionRunRequest:
        if self.mode == "voicenotes-cleanup":
            # A file id here would be silently ignored, and a caller that
            # believes it said something it did not is the thing
            # extra="forbid" exists for.
            if self.drive_file_id:
                raise ValueError("voicenotes-cleanup takes no drive_file_id")
        elif not self.drive_file_id:
            raise ValueError(f"{self.mode} needs a drive_file_id")
        return self


class TranscriptionRunAccepted(BaseModel):
    """The acknowledgement. Not a result — nothing has run yet."""

    accepted: bool = Field(
        True,
        description=(
            "The work is on the queue — this request's message, or, when "
            "deduplicated, an earlier one's."
        ),
    )
    message_id: str = Field(
        "",
        description=(
            "The queue message this request became. What the API can "
            "honestly say it did, and the handle for tracing the job."
        ),
    )
    mode: str = Field(..., description="Pipeline that will run.")
    drive_file_id: str | None = Field(
        None, description="The file; absent for the retention sweep."
    )
    deduplicated: bool = Field(
        False,
        description=(
            "Nothing was enqueued because every file named is already "
            "claimed by an earlier request. ``message_id`` is that "
            "request's job, when it has reached the queue."
        ),
    )
