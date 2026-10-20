"""Contract for asking the task manager to put a task on the board.

``POST /v1/tasks``. A source hands over a *draft*: what it knows about the
work, in the same shape whatever the source is. Everything that is the same
for every task — the board's template block, the task id, tags, section and
assignee — is the task manager's to add, so no source carries a copy of it.

The request is answered 202 once the job is on the queue. Nothing has been
created yet; the task manager does that afterwards, and its run report names
the task.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Where a task comes from. Each source owns one id prefix on the board, or
#: brings its own id; the task manager holds which is which.
TaskSource = Literal["voicenote", "github-issue"]

#: Sources whose tasks carry an id the source already has (an issue's
#: ``repo#n``), rather than one the task manager mints.
GIVEN_ID_SOURCES: frozenset[str] = frozenset({"github-issue"})


class TaskSection(BaseModel):
    """One labelled block of the task's body, rendered below the prose."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1, max_length=80)
    text: str = Field(..., min_length=1, max_length=20_000)


class TaskLink(BaseModel):
    """A link the task should carry: the audio, the issue."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=200)
    url: str = Field(..., min_length=8, max_length=2048, pattern=r"^https://")


class TaskDraft(BaseModel):
    """What a source knows about one piece of work.

    ``title`` may be blank: a voice note can be all filler. The task manager
    then names the task for review instead of refusing it, so the work is
    not lost to a validation error. ``labels`` are tag names; they are
    ignored when ``needs_review`` is set, which applies the review tag alone.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field("", max_length=500)
    where: str | None = Field(
        None,
        max_length=200,
        description="The system, repo or place the work happens in.",
    )
    body: str | None = Field(
        None,
        max_length=20_000,
        description="Prose describing the work, rendered below the template.",
    )
    sections: list[TaskSection] = Field(default_factory=list, max_length=20)
    links: list[TaskLink] = Field(default_factory=list, max_length=20)
    labels: list[str] = Field(default_factory=list, max_length=20)
    needs_review: bool = False
    due_on: date | None = None

    @model_validator(mode="after")
    def _labels_are_names(self) -> TaskDraft:
        for label in self.labels:
            if not label.strip() or len(label) > 50:
                raise ValueError("each label is a tag name of 1-50 characters")
        return self


class TaskCreateRequest(BaseModel):
    """Ask for one task.

    ``external_id`` is the source's own key for the work, prefixed with the
    source (``voicenote.<drive file id>``). The task manager looks it up
    before creating anything, so asking twice makes one task.
    """

    model_config = ConfigDict(extra="forbid")

    source: TaskSource
    external_id: str = Field(..., min_length=1, max_length=256)
    id: str | None = Field(
        None,
        min_length=1,
        max_length=120,
        description=(
            "The task's id, for a source that brings its own (an issue's "
            "repo#n). Refused for a source whose ids the task manager mints."
        ),
    )
    draft: TaskDraft

    @model_validator(mode="after")
    def _id_and_key_match_source(self) -> TaskCreateRequest:
        if not self.external_id.startswith(f"{self.source}."):
            raise ValueError(f"external_id must start with {self.source}.")
        if self.source in GIVEN_ID_SOURCES:
            if not self.id:
                raise ValueError(f"{self.source} needs an id")
        elif self.id:
            raise ValueError(f"{self.source} ids are minted; send no id")
        return self


class TaskCreateAccepted(BaseModel):
    """The acknowledgement. Not a result — no task exists yet."""

    accepted: bool = Field(True, description="The job is on the queue.")
    message_id: str = Field(
        "", description="The queue message this request became, for tracing."
    )
    source: str = Field(..., description="The source the task came from.")
    external_id: str = Field(..., description="The source's key for the work.")
