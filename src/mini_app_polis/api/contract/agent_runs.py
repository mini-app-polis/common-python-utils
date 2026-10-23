"""Contract for the agent that implements Ready tasks.

The task manager dispatches an agent for a Ready task: a headless Claude
Code run in GitHub Actions, in the repository the task names, which opens a
draft pull request into ``dev``. Each dispatch is one agent run, recorded
here before the run is started. The record is also the lock: a repository
with a run in flight is given no second one.

When the run ends, its workflow posts the result: how it ended, what it
cost, and the pull request it opened. The API records it and queues it for
the task manager, the one writer to the board, which moves the task. A run
that never reports is marked failed by the task manager, and a run that
could not be started is marked failed the same way.

A run that finds its task unclear or undetermined changes nothing and ends
as ``needs_grooming``, with its questions. The task manager sends the task
back to grooming with them; it is not retried.

The month's spend is the sum of the runs' costs, for the monthly cap.

    POST /v1/tasks/agent-runs           record a dispatched run
    GET  /v1/tasks/agent-runs           runs, by status and repository
    GET  /v1/tasks/agent-runs/spend     this month's agent runs, in total
    POST /v1/tasks/agent-runs/failures  mark a dispatched run failed
    POST /v1/tasks/agent-runs/results   a run's result, from its workflow
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: Where a run is. ``dispatched`` until its result or failure is recorded.
AgentRunStatus = Literal[
    "dispatched", "succeeded", "failed", "capped", "needs_grooming"
]

#: How a run ended, as its workflow reports it. ``capped``: it reached its
#: budget limit and stopped with no pull request. ``needs_grooming``: it found
#: the task unclear, changed nothing, and asks its questions.
AgentRunOutcome = Literal["succeeded", "failed", "capped", "needs_grooming"]

_GID = r"^\d+$"
_RUN_ID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_REPOSITORY = r"^[A-Za-z0-9._-]+$"
_URL = r"^https://"


class AgentRunCreate(BaseModel):
    """A run the task manager is about to start."""

    model_config = ConfigDict(extra="forbid")

    task_gid: str = Field(..., min_length=1, max_length=64, pattern=_GID)
    #: The task's id on the board (``MINI-12``), when it has one.
    task_id: str | None = Field(default=None, max_length=64)
    #: The repository the run works in, by name within the organisation.
    repository: str = Field(..., min_length=1, max_length=100, pattern=_REPOSITORY)
    model: str = Field(..., min_length=1, max_length=100)


class AgentRun(BaseModel):
    """A recorded run. Tolerates fields a newer API adds."""

    id: str
    task_gid: str
    task_id: str | None = None
    repository: str
    model: str
    status: AgentRunStatus
    #: What the run cost; None while it is in flight, or when it ended
    #: without reporting.
    cost_usd: float | None = None
    pr_url: str | None = None
    actions_run_url: str | None = None
    #: Why a run failed or was capped, in a sentence.
    reason: str | None = None
    #: What a run that needs grooming asked.
    questions: list[str] = Field(default_factory=list)
    dispatched_at: datetime
    finished_at: datetime | None = None


class AgentRunFailure(BaseModel):
    """The task manager marks a dispatched run failed: it could not be
    started, or it never reported."""

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., pattern=_RUN_ID)
    reason: str = Field(..., min_length=1, max_length=2000)


class AgentRunResult(BaseModel):
    """How a run ended, as its workflow reports it.

    A run that succeeded opened a pull request and names it; one that did
    not opened none. A run that needs grooming asks at least one question;
    no other outcome carries questions.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(..., pattern=_RUN_ID)
    outcome: AgentRunOutcome
    #: What the run cost, from the agent's own report. None when the run
    #: ended before the agent could report it.
    cost_usd: float | None = Field(default=None, ge=0)
    pr_url: str | None = Field(default=None, max_length=500, pattern=_URL)
    actions_run_url: str | None = Field(default=None, max_length=500, pattern=_URL)
    reason: str | None = Field(default=None, max_length=2000)
    #: What the run could not settle, for the owner to answer in grooming.
    questions: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _pull_request_only_on_success(self) -> AgentRunResult:
        if self.outcome == "succeeded" and not self.pr_url:
            raise ValueError("a run that succeeded names its pull request")
        if self.outcome != "succeeded" and self.pr_url:
            raise ValueError(f"a run that ended {self.outcome} opened no pull request")
        return self

    @model_validator(mode="after")
    def _questions_only_when_grooming_is_needed(self) -> AgentRunResult:
        if self.outcome == "needs_grooming" and not self.questions:
            raise ValueError("a run that needs grooming asks its questions")
        if self.outcome != "needs_grooming" and self.questions:
            raise ValueError(f"a run that ended {self.outcome} asks no questions")
        if any(not q.strip() for q in self.questions):
            raise ValueError("a question is not blank")
        return self


class AgentResultAccepted(BaseModel):
    """The result is recorded and on the task manager's queue."""

    accepted: bool = True
    message_id: str
    run_id: str


class AgentSpend(BaseModel):
    """The calendar month's agent runs, so far (UTC)."""

    month: str = Field(..., pattern=r"^\d{4}-\d{2}$")
    runs: int = Field(..., ge=0)
    cost_usd: float = Field(..., ge=0)
