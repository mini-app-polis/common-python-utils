"""The job-queue envelope and its message kinds."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from mini_app_polis.api.contract import (
    QUEUE_MESSAGE_VERSION,
    DeejayRunMessage,
    EvaluationIntrospectionMessage,
    EvaluationRepositoryMessage,
    JobMessage,
    QueueMessage,
    TaskAgentResultMessage,
    TaskCreateMessage,
    TaskGroomMessage,
    TaskGroomProposalMessage,
    TranscriptionRunMessage,
    WikiJobMessage,
)

ADAPTER: TypeAdapter[Any] = TypeAdapter(JobMessage)

KINDS = [
    (TranscriptionRunMessage, "transcription.run"),
    (DeejayRunMessage, "deejay.run"),
    (EvaluationRepositoryMessage, "evaluation.repository"),
    (EvaluationIntrospectionMessage, "evaluation.introspection"),
    (WikiJobMessage, "wiki.job"),
    (TaskGroomMessage, "task.groom"),
    (TaskGroomProposalMessage, "task.groom-proposal"),
    (TaskCreateMessage, "task.create"),
    (TaskAgentResultMessage, "task.agent-result"),
]


@pytest.mark.parametrize(("kind", "type_"), KINDS)
def test_each_kind_keeps_its_wire_type_and_version(kind: Any, type_: str) -> None:
    assert issubclass(kind, QueueMessage)
    assert kind.message_type() == type_
    assert kind.VERSION == QUEUE_MESSAGE_VERSION == 1


def test_union_picks_the_kind_by_type() -> None:
    cases: list[tuple[dict[str, Any], type]] = [
        (
            {
                "type": "transcription.run",
                "version": 1,
                "payload": {"mode": "wcs-transcripts", "drive_file_id": "f"},
            },
            TranscriptionRunMessage,
        ),
        (
            {"type": "deejay.run", "version": 1, "payload": {"mode": "m"}},
            DeejayRunMessage,
        ),
        (
            {
                "type": "evaluation.repository",
                "version": 1,
                "payload": {
                    "repo": "r",
                    "ref": "main",
                    "org": "o",
                    "mode": "deterministic",
                    "run_id": "run",
                    "services": [{"id": "r"}],
                    "standards_version": "9.0.0",
                },
            },
            EvaluationRepositoryMessage,
        ),
        (
            {
                "type": "evaluation.introspection",
                "version": 1,
                "payload": {"run_id": "run"},
            },
            EvaluationIntrospectionMessage,
        ),
        (
            {"type": "wiki.job", "version": 1, "payload": {"job": "consolidate"}},
            WikiJobMessage,
        ),
    ]
    for body, kind in cases:
        message = ADAPTER.validate_python(body)
        assert isinstance(message, kind)
        assert message.model_dump(mode="json", exclude_none=True) == body


def test_union_refuses_an_unknown_type() -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python({"type": "nope", "version": 1, "payload": {}})


def test_a_kind_refuses_another_kinds_type() -> None:
    with pytest.raises(ValidationError):
        DeejayRunMessage.model_validate(
            {"type": "transcription.run", "version": 1, "payload": {"mode": "m"}}
        )


def test_a_message_built_in_code_carries_its_type_and_version() -> None:
    message = DeejayRunMessage(payload={"mode": "process-new-files"})  # type: ignore[arg-type]
    assert message.model_dump(mode="json") == {
        "type": "deejay.run",
        "version": 1,
        "payload": {"mode": "process-new-files"},
    }
