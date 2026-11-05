"""The grooming names of :mod:`.refinings`, kept while any cog still uses them.

Grooming was renamed refining. The API serves the ``/v1/tasks/groomings``
routes and their ``tasks.groomings.*`` scopes beside the refining ones until
every cog has moved, and these are the names those routes speak. Everything
here is the refining model under its old name, except the record, whose
fields kept their grooming names on the wire (``groomed_fingerprint``,
``groom_sent_at``, ``groom_attempts``). Removed once every cog has moved.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from .refinings import (
    REFINING_MAX_CHILDREN,
    RETURN_RATE_DEFAULT_DAYS,
    RETURN_RATE_MAX_DAYS,
    RefiningBudgetSpentMarked,
    RefiningBudgetSpentRequest,
    RefiningChild,
    RefiningClaimReleased,
    RefiningClaimRequest,
    RefiningClaimResult,
    RefiningEffort,
    RefiningExecutor,
    RefiningJobAccepted,
    RefiningJobRequest,
    RefiningOutcome,
    RefiningProposal,
    RefiningProposalAccepted,
    RefiningReturnRate,
    RefiningRunCreate,
    RefiningRunRecorded,
    RefiningSpend,
    RefiningTask,
)

GroomingEffort = RefiningEffort
GroomingExecutor = RefiningExecutor
GROOMING_MAX_CHILDREN = REFINING_MAX_CHILDREN
GroomingOutcome = RefiningOutcome


class _GroomingFields(BaseModel):
    """What the task manager remembers about one task."""

    task_gid: str = Field(..., min_length=1, max_length=64, pattern=r"^\d+$")
    #: The task's id on the board (a prefix and a number), when it has one.
    task_id: str | None = Field(default=None, max_length=64)
    #: sha256 of the content judged: name, body and tags. Not the column, so
    #: moving a task is not a change to its content.
    fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    outcome: RefiningOutcome
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
    #: When the content in ``groomed_fingerprint`` was last sent for grooming,
    #: while no grooming of it has come back. None once one has, or when the
    #: content was never sent.
    groom_sent_at: datetime | None = None
    #: How many times that content has been sent with no grooming coming back.
    #: 0 once one has.
    groom_attempts: int = Field(default=0, ge=0)


class GroomingRecordWrite(_GroomingFields):
    """One task's record, as the task manager writes it."""

    model_config = ConfigDict(extra="forbid")


class GroomingRecord(_GroomingFields):
    """A stored record. Tolerates fields a newer API adds."""

    updated_at: datetime
    #: The fingerprint of the content whose last grooming run was stopped by
    #: the spent budget, until a later claim for it is won. The sweep never
    #: writes it: the groomer reports it, and the API keeps it apart from the
    #: record.
    budget_spent_fingerprint: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


GroomingRunCreate = RefiningRunCreate
GroomingRunRecorded = RefiningRunRecorded
GroomingSpend = RefiningSpend
GroomingReturnRate = RefiningReturnRate
GroomingTask = RefiningTask
GroomingJobRequest = RefiningJobRequest
GroomingJobAccepted = RefiningJobAccepted
GroomingChild = RefiningChild
GroomingProposal = RefiningProposal
GroomingClaimRequest = RefiningClaimRequest
GroomingClaimResult = RefiningClaimResult
GroomingClaimReleased = RefiningClaimReleased
GroomingBudgetSpentRequest = RefiningBudgetSpentRequest
GroomingBudgetSpentMarked = RefiningBudgetSpentMarked
GroomingProposalAccepted = RefiningProposalAccepted

__all__ = [
    "GROOMING_MAX_CHILDREN",
    "RETURN_RATE_DEFAULT_DAYS",
    "RETURN_RATE_MAX_DAYS",
    "GroomingBudgetSpentMarked",
    "GroomingBudgetSpentRequest",
    "GroomingChild",
    "GroomingClaimReleased",
    "GroomingClaimRequest",
    "GroomingClaimResult",
    "GroomingEffort",
    "GroomingExecutor",
    "GroomingJobAccepted",
    "GroomingJobRequest",
    "GroomingOutcome",
    "GroomingProposal",
    "GroomingProposalAccepted",
    "GroomingRecord",
    "GroomingRecordWrite",
    "GroomingReturnRate",
    "GroomingRunCreate",
    "GroomingRunRecorded",
    "GroomingSpend",
    "GroomingTask",
]
