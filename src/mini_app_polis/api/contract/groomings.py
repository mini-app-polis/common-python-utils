"""Contract for the task board's grooming records.

The task manager sweeps the board and checks every waiting task against the
definition of ready. A grooming record is what it remembers about one task
between sweeps: a fingerprint of the content it last judged, the verdict,
and whether it moved the task to Ready. The fingerprint is what lets a later
sweep tell a task someone has changed from one that is still waiting, so
only a changed task is worth grooming again.

A paid grooming run (a model reading a task and proposing its fields) is
recorded on its own, with its cost, so the month's spend can be capped.

A task that is not ready and has changed is sent for grooming: the task
manager asks for a job, the API queues it for task-groomer-cog, and the
groomer's proposal comes back through the API to the task manager's queue.
The groomer never writes to the board; the task manager applies the
proposal.

    GET  /v1/tasks/groomings            every record
    POST /v1/tasks/groomings            write one record (keyed by task_gid)
    POST /v1/tasks/groomings/runs       record one paid run
    GET  /v1/tasks/groomings/spend      this month's paid runs, in total
    POST /v1/tasks/groomings/jobs       ask for one task to be groomed
    POST /v1/tasks/groomings/proposals  hand a groomer's proposal back
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: A task's size. An L task's implementation is to create its subtasks.
GroomingEffort = Literal["S", "M", "L"]

#: What the last sweep made of a task.
#:
#: ``ready``: it met the definition of ready. ``not_ready``: it did not, and
#: has no open questions. ``questions``: a grooming run asked something only a
#: person can answer, and the task waits on that.
GroomingOutcome = Literal["ready", "not_ready", "questions"]


class _GroomingFields(BaseModel):
    """What the task manager remembers about one task."""

    task_gid: str = Field(..., min_length=1, max_length=64, pattern=r"^\d+$")
    #: The task's id on the board (``MINI-12``), when it has one.
    task_id: str | None = Field(default=None, max_length=64)
    #: sha256 of the content judged: name, body and tags. Not the column, so
    #: moving a task is not a change to its content.
    fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    outcome: GroomingOutcome
    #: Every rule the task failed, as the linter words them.
    problems: list[str] = Field(default_factory=list, max_length=50)
    #: When the task manager moved the task to Ready, while it is still
    #: there. Cleared when the task comes back.
    moved_to_ready_at: datetime | None = None
    #: How often a task the task manager moved to Ready was moved back out:
    #: the measure of whether ready meant ready.
    returned_count: int = Field(default=0, ge=0)
    #: The fingerprint of the content last sent for grooming, or that a
    #: grooming wrote. A task is sent again only once its content differs
    #: from this, so it is groomed once per change and never loops.
    groomed_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class GroomingRecordWrite(_GroomingFields):
    """One task's record, as the task manager writes it."""

    model_config = ConfigDict(extra="forbid")


class GroomingRecord(_GroomingFields):
    """A stored record. Tolerates fields a newer API adds."""

    updated_at: datetime


class GroomingRunCreate(BaseModel):
    """One paid grooming run, for the spend cap."""

    model_config = ConfigDict(extra="forbid")

    task_gid: str = Field(..., min_length=1, max_length=64, pattern=r"^\d+$")
    model: str = Field(..., min_length=1, max_length=100)
    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    cost_usd: float = Field(..., ge=0)


class GroomingRunRecorded(BaseModel):
    """A recorded run."""

    id: str
    task_gid: str
    cost_usd: float
    recorded_at: datetime


class GroomingSpend(BaseModel):
    """The calendar month's paid grooming, so far (UTC)."""

    month: str = Field(..., pattern=r"^\d{4}-\d{2}$")
    runs: int = Field(..., ge=0)
    cost_usd: float = Field(..., ge=0)


class GroomingTask(BaseModel):
    """One task as the groomer reads it: everything it is given about it."""

    model_config = ConfigDict(extra="forbid")

    task_gid: str = Field(..., min_length=1, max_length=64, pattern=r"^\d+$")
    task_id: str | None = Field(default=None, max_length=64)
    name: str = Field(..., min_length=1, max_length=1024)
    #: The body as plain text, template block included.
    notes: str = Field(default="", max_length=65_536)
    tags: list[str] = Field(default_factory=list, max_length=50)
    section: str | None = Field(default=None, max_length=200)
    #: The content's fingerprint when it was sent. A proposal carries it
    #: back, so one made for content that has since changed is not applied.
    fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    #: What the linter found missing.
    problems: list[str] = Field(default_factory=list, max_length=50)
    #: The comments on the task since its last grooming questions, oldest
    #: first: the owner's answers.
    comments: list[str] = Field(default_factory=list, max_length=50)


class GroomingJobRequest(BaseModel):
    """Ask for one task to be groomed."""

    model_config = ConfigDict(extra="forbid")

    task: GroomingTask
    #: The names of the other open tasks on the board, to recognise one
    #: that duplicates or depends on another.
    board: list[str] = Field(default_factory=list, max_length=500)


class GroomingJobAccepted(BaseModel):
    """The job is on the groomer's queue."""

    accepted: bool = True
    message_id: str
    task_gid: str


class GroomingProposal(BaseModel):
    """What a groomer proposes a task should say.

    Every field is the groomer's best reading of the task and the code it
    names. A field it could not settle is left out and asked about in
    ``questions`` instead: a guess in ``done_when`` costs more than a
    question.
    """

    model_config = ConfigDict(extra="forbid")

    task_gid: str = Field(..., min_length=1, max_length=64, pattern=r"^\d+$")
    #: The fingerprint of the content this was made for.
    fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    #: The title without its id.
    title: str = Field(..., min_length=1, max_length=300)
    done_when: str | None = Field(default=None, max_length=5000)
    where: str | None = Field(default=None, max_length=1000)
    constraints: str | None = Field(default=None, max_length=5000)
    effort: GroomingEffort | None = None
    blocked_by: str | None = Field(default=None, max_length=500)
    #: Why the task exists, and links to what is needed.
    body: str | None = Field(default=None, max_length=20_000)
    questions: list[str] = Field(default_factory=list, max_length=10)
    #: Who made it and what it cost, for the record.
    model: str = Field(..., min_length=1, max_length=100)
    cost_usd: float = Field(..., ge=0)


class GroomingProposalAccepted(BaseModel):
    """The proposal is on the task manager's queue."""

    accepted: bool = True
    message_id: str
    task_gid: str
