"""The messages the API puts on a cog's job queue.

Every message is one envelope, ``{type, version, payload}``:
:class:`QueueMessage`. ``type`` says what the message asks for, ``version``
is the schema version of the envelope and its payload, and ``payload`` is
the job. A consumer refuses a version it does not speak rather than
misreading it, which is what makes a producer and consumer safe to deploy
in either order.

Each kind of message is a :class:`QueueMessage` with its own ``type`` and a
typed payload. :data:`JobMessage` is all of them, as a union discriminated
on ``type``; :func:`mini_app_polis.lambda_worker.parse_message` reads one
body as the kinds a consumer accepts.

Each cog has its own queue, so a type a consumer does not accept is a
producer bug, not a message for someone else.
"""

from __future__ import annotations

from typing import Annotated, Any, ClassVar, Generic, Literal, TypeVar

from pydantic import BaseModel, Field

from .agent_runs import AgentRun
from .groomings import GroomingJobRequest, GroomingProposal
from .tasks import TaskCreateRequest, TaskUpdateRequest
from .wcs_wiki import WcsWikiJob

#: The schema version every message kind is on today.
QUEUE_MESSAGE_VERSION = 1

P = TypeVar("P")


class QueueMessage(BaseModel, Generic[P]):
    """One message on a job queue: ``{type, version, payload}``.

    A subclass names one kind: its ``type`` as a ``Literal``, its payload
    model, and the version it speaks as ``VERSION``.
    """

    VERSION: ClassVar[int] = QUEUE_MESSAGE_VERSION

    type: str = Field(..., description="What the message asks for.")
    version: int = Field(
        default=QUEUE_MESSAGE_VERSION,
        description="Schema version of the envelope and its payload.",
    )
    payload: P = Field(..., description="The job.")

    @classmethod
    def message_type(cls) -> str:
        """The ``type`` this kind carries."""
        return str(cls.model_fields["type"].default)


# -- payloads that have no model of their own elsewhere ------------------------


class TranscriptionRunPayload(BaseModel):
    """One run of transcription-cog: one file in one mode, the cleanup
    sweep, or one note written on the site."""

    mode: str = Field(..., description="What to run; this routes the job.")
    drive_file_id: str | None = Field(
        default=None, description="The file, for a mode that takes one."
    )
    note: dict[str, str] | None = Field(
        default=None,
        description="wcs-note: the note, with id, date, instructor, title and text.",
    )


class DeejayRunPayload(BaseModel):
    """One run of deejay-cog's router, in one mode."""

    mode: str = Field(..., description="What to run; this routes the job.")


class EvaluationRepositoryPayload(BaseModel):
    """One repository to evaluate.

    A release-triggered evaluation carries the repository and what the
    caller supplied; one repository within a fleet pass also carries its
    services, its monorepo and the pass's pinned standards version.
    """

    repo: str = Field(..., description="Repository name.")
    ref: str = Field(..., description="Git ref to evaluate.")
    org: str = Field(..., description="GitHub organisation.")
    mode: str = Field(..., description="Which checks run.")
    repo_id: str | None = Field(default=None, description="Registry id.")
    run_id: str | None = Field(default=None, description="The run or pass.")
    services: list[dict[str, Any]] | None = Field(
        default=None, description="Fleet pass: the services the repository carries."
    )
    monorepo: dict[str, Any] | None = Field(
        default=None, description="Fleet pass: the monorepo record."
    )
    standards_version: str | None = Field(
        default=None, description="Fleet pass: the catalog version it pinned."
    )


class EvaluationIntrospectionPayload(BaseModel):
    """The checks scoped to no repository."""

    run_id: str = Field(..., description="This run.")
    pass_run_id: str | None = Field(
        default=None, description="A fleet pass for the checks that grade one."
    )
    standards_version: str | None = Field(
        default=None, description="The catalog version to grade against."
    )


# -- the message kinds -----------------------------------------------------------


class TranscriptionRunMessage(QueueMessage[TranscriptionRunPayload]):
    """``transcription.run`` on transcription-cog's queue."""

    type: Literal["transcription.run"] = "transcription.run"


class DeejayRunMessage(QueueMessage[DeejayRunPayload]):
    """``deejay.run`` on deejay-cog's queue."""

    type: Literal["deejay.run"] = "deejay.run"


class EvaluationRepositoryMessage(QueueMessage[EvaluationRepositoryPayload]):
    """``evaluation.repository`` on evaluator-cog's queue."""

    type: Literal["evaluation.repository"] = "evaluation.repository"


class EvaluationIntrospectionMessage(QueueMessage[EvaluationIntrospectionPayload]):
    """``evaluation.introspection`` on evaluator-cog's queue."""

    type: Literal["evaluation.introspection"] = "evaluation.introspection"


class WikiJobMessage(QueueMessage[WcsWikiJob]):
    """``wiki.job`` on wiki-curator-cog's queue."""

    type: Literal["wiki.job"] = "wiki.job"


class TaskGroomMessage(QueueMessage[GroomingJobRequest]):
    """``task.groom`` on task-groomer-cog's queue."""

    type: Literal["task.groom"] = "task.groom"


class TaskGroomProposalMessage(QueueMessage[GroomingProposal]):
    """``task.groom-proposal`` on task-manager-cog's queue."""

    type: Literal["task.groom-proposal"] = "task.groom-proposal"


class TaskCreateMessage(QueueMessage[TaskCreateRequest]):
    """``task.create`` on task-manager-cog's queue."""

    type: Literal["task.create"] = "task.create"


class TaskUpdateMessage(QueueMessage[TaskUpdateRequest]):
    """``task.update`` on task-manager-cog's queue."""

    type: Literal["task.update"] = "task.update"


class TaskAgentResultMessage(QueueMessage[AgentRun]):
    """``task.agent-result`` on task-manager-cog's queue."""

    type: Literal["task.agent-result"] = "task.agent-result"


class TaskSweepPayload(BaseModel):
    """A scheduled sweep of the board carries nothing."""


class TaskSweepMessage(QueueMessage[TaskSweepPayload]):
    """``task.sweep`` on task-manager-cog's queue, from its own schedule.

    The schedule sends ``{type, version}`` with no payload, so the payload is
    optional here: a kind with nothing to carry may leave it out.
    """

    type: Literal["task.sweep"] = "task.sweep"
    payload: TaskSweepPayload = Field(
        default_factory=TaskSweepPayload, description="Nothing; may be omitted."
    )


#: Every message kind, discriminated on ``type``.
JobMessage = Annotated[
    TranscriptionRunMessage
    | DeejayRunMessage
    | EvaluationRepositoryMessage
    | EvaluationIntrospectionMessage
    | WikiJobMessage
    | TaskGroomMessage
    | TaskGroomProposalMessage
    | TaskCreateMessage
    | TaskUpdateMessage
    | TaskAgentResultMessage
    | TaskSweepMessage,
    Field(discriminator="type"),
]

__all__ = [
    "QUEUE_MESSAGE_VERSION",
    "DeejayRunMessage",
    "DeejayRunPayload",
    "EvaluationIntrospectionMessage",
    "EvaluationIntrospectionPayload",
    "EvaluationRepositoryMessage",
    "EvaluationRepositoryPayload",
    "JobMessage",
    "QueueMessage",
    "TaskAgentResultMessage",
    "TaskCreateMessage",
    "TaskGroomMessage",
    "TaskGroomProposalMessage",
    "TaskSweepMessage",
    "TaskSweepPayload",
    "TaskUpdateMessage",
    "TranscriptionRunMessage",
    "TranscriptionRunPayload",
    "WikiJobMessage",
]
