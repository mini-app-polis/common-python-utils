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
    GET  /v1/tasks/groomings/return-rate  how often Ready tasks came back out
    POST /v1/tasks/groomings/jobs       ask for one task to be groomed
    POST /v1/tasks/groomings/proposals  hand a groomer's proposal back
    POST /v1/tasks/groomings/claims     claim one job before grooming it
    POST /v1/tasks/groomings/claims/release  give a claim back after a failure
    POST /v1/tasks/groomings/budget-spent    report a run stopped by the budget

A queue can deliver the same grooming job more than once. The groomer claims
the task and the fingerprint of the content it was sent before it calls the
model, and grooms only if it won the claim, so a second delivery costs
nothing. A run that fails releases its claim, so the next send can groom the
same content; a run that succeeds keeps it, so unchanged content is not
groomed twice.

When a run is stopped because the month's grooming budget is spent, the
groomer reports the stop (the task and its content's fingerprint) to the API,
which stores it apart from the grooming record and returns it on each record
as ``budget_spent_fingerprint``. The sweep sends a task that is not ready
again when that equals the task's current fingerprint, so content the budget
stopped is groomed once there is budget, without waiting for a change. The
API clears the stop when a later claim for that task and content is won.

A grooming can also be lost for good: a run that crashes, or a proposal that
never reaches the board. The task manager records when it sent content for
grooming (``groom_sent_at``) and how many times it has sent that content
without a grooming coming back (``groom_attempts``). Writing the record of an
applied grooming clears both. A send still outstanding after a while is sent
again, up to a limit the task manager sets.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: A task's size. An L task's implementation is to create its subtasks.
GroomingEffort = Literal["S", "M", "L"]

#: Who can do a task, as a tag on it. No tag means the agent may take it.
#: ``cowork``: it needs a working session with the owner (his repositories,
#: the board, consoles, his decisions). ``person``: only the owner can do it
#: (consoles behind his login, accounts, physical checks). Consumers read the
#: names from here, with ``typing.get_args``.
GroomingExecutor = Literal["cowork", "person"]

#: The most children one proposal may split a task into.
GROOMING_MAX_CHILDREN = 25

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


#: The window a return-from-Ready read covers when none is asked for, in days.
RETURN_RATE_DEFAULT_DAYS = 30
#: The longest window a return-from-Ready read covers, in days.
RETURN_RATE_MAX_DAYS = 365


class GroomingReturnRate(BaseModel):
    """How often a task moved to Ready came back out, over the last ``days``.

    ``moved_to_ready`` counts the moves to Ready in the window,
    ``returned`` the tasks sent back out of Ready in it (by a person, or by
    the sweep finding the task no longer ready), and ``ratio`` is
    ``returned / moved_to_ready``: None when nothing moved to Ready. The
    counts come from events recorded as the grooming records change, so a
    window reaching back before the first of them counts only from
    ``counting_since``, which is then later than ``since``.
    """

    days: int = Field(..., ge=1, le=RETURN_RATE_MAX_DAYS)
    since: datetime = Field(..., description="The window's start.")
    counting_since: datetime = Field(
        ...,
        description=(
            "Where the counts start: the window's start, or the first "
            "recorded event when that is later."
        ),
    )
    moved_to_ready: int = Field(..., ge=0)
    returned: int = Field(..., ge=0)
    ratio: float | None = Field(default=None, ge=0)


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
    #: that duplicates or depends on another, and any finished task this
    #: one mentions, its name ending " (done)".
    board: list[str] = Field(default_factory=list, max_length=500)


class GroomingJobAccepted(BaseModel):
    """The job is on the groomer's queue."""

    accepted: bool = True
    message_id: str
    task_gid: str


class GroomingChild(BaseModel):
    """One task a proposal splits its task into: a leaf, ready to be worked.

    It waits on earlier children by their position in the proposal's
    ``children`` (from 0), since their ids are minted only when the task
    manager creates them; on existing tasks and dates, by ``blocked_by``.
    """

    model_config = ConfigDict(extra="forbid")

    #: The title without its id.
    title: str = Field(..., min_length=1, max_length=300)
    done_when: str = Field(..., min_length=1, max_length=5000)
    where: str = Field(..., min_length=1, max_length=1000)
    constraints: str = Field(..., min_length=1, max_length=5000)
    effort: GroomingEffort
    #: Who can do it. Absent means the agent.
    executor: GroomingExecutor | None = None
    #: Earlier children this one waits for, by position.
    blocked_by_siblings: list[int] = Field(
        default_factory=list, max_length=GROOMING_MAX_CHILDREN
    )
    #: Existing task ids and dates it waits for, as the template writes them.
    blocked_by: str | None = Field(default=None, max_length=500)
    #: Why it exists, and links to what is needed.
    body: str = Field(..., min_length=1, max_length=20_000)


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
    #: Who can do the task. Absent means the agent.
    executor: GroomingExecutor | None = None
    #: Why the task exists, and links to what is needed.
    body: str | None = Field(default=None, max_length=20_000)
    questions: list[str] = Field(default_factory=list, max_length=10)
    #: The tasks this one splits into, when it is not a leaf (one
    #: repository, one executor). The task then becomes their compound task.
    children: list[GroomingChild] = Field(
        default_factory=list, max_length=GROOMING_MAX_CHILDREN
    )
    #: Who made it and what it cost, for the record.
    model: str = Field(..., min_length=1, max_length=100)
    cost_usd: float = Field(..., ge=0)

    @model_validator(mode="after")
    def _children_wait_only_on_earlier_siblings(self) -> GroomingProposal:
        for position, child in enumerate(self.children):
            for sibling in child.blocked_by_siblings:
                if not 0 <= sibling < position:
                    raise ValueError(
                        f"child {position} waits on child {sibling}, which is "
                        "not an earlier sibling"
                    )
        return self


class GroomingClaimRequest(BaseModel):
    """Claim one grooming job, or give a claim back.

    A claim is keyed on the task and the fingerprint of the content the job
    was sent for, so the same content sent again in a new message is the
    same claim.
    """

    model_config = ConfigDict(extra="forbid")

    task_gid: str = Field(..., min_length=1, max_length=64, pattern=r"^\d+$")
    fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")


class GroomingClaimResult(BaseModel):
    """Whether the caller won the claim.

    ``claimed`` is false when the claim is already held, by an earlier
    delivery or a run that succeeded. That is an answer, not an error: the
    caller skips the job.
    """

    task_gid: str
    fingerprint: str
    claimed: bool


class GroomingClaimReleased(BaseModel):
    """Whether a held claim was given back.

    ``released`` is false when there was no held claim to give back.
    """

    task_gid: str
    fingerprint: str
    released: bool


class GroomingBudgetSpentRequest(BaseModel):
    """Report a grooming run stopped because the month's budget is spent.

    Keyed like a claim, on the task and the fingerprint of the content the
    job was sent for.
    """

    model_config = ConfigDict(extra="forbid")

    task_gid: str = Field(..., min_length=1, max_length=64, pattern=r"^\d+$")
    fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")


class GroomingBudgetSpentMarked(BaseModel):
    """The stop the API stored."""

    task_gid: str = Field(..., min_length=1, max_length=64, pattern=r"^\d+$")
    fingerprint: str = Field(..., pattern=r"^[0-9a-f]{64}$")


class GroomingProposalAccepted(BaseModel):
    """The proposal is on the task manager's queue."""

    accepted: bool = True
    message_id: str
    task_gid: str
