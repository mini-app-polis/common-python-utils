"""The job-queue envelope and its message kinds."""

from __future__ import annotations

import json
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
    RotationIncidentMessage,
    RotationJobMessage,
    RotationSweepMessage,
    TaskAgentResultMessage,
    TaskCreateMessage,
    TaskGroomMessage,
    TaskGroomProposalMessage,
    TaskSweepMessage,
    TaskUpdateMessage,
    TranscriptionRunMessage,
    WikiJobMessage,
)
from mini_app_polis.lambda_worker import parse_message

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
    (TaskUpdateMessage, "task.update"),
    (TaskAgentResultMessage, "task.agent-result"),
    (TaskSweepMessage, "task.sweep"),
    (RotationJobMessage, "rotation.job"),
    (RotationSweepMessage, "rotation.sweep"),
    (RotationIncidentMessage, "rotation.incident"),
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


def test_a_sweep_needs_no_payload() -> None:
    message = ADAPTER.validate_python({"type": "task.sweep", "version": 1})
    assert isinstance(message, TaskSweepMessage)


def _update_body(**payload: Any) -> dict[str, Any]:
    return {"type": "task.update", "version": 1, "payload": payload}


def test_an_update_round_trips_through_the_union() -> None:
    body = _update_body(
        external_id="ci.api-kaianolevine-com#3f2a",
        comment="Healed: the last three runs passed.",
        section="Review",
    )
    message = ADAPTER.validate_python(body)
    assert isinstance(message, TaskUpdateMessage)
    assert message.model_dump(mode="json", exclude_none=True) == body


def test_an_update_may_leave_the_section_out() -> None:
    body = _update_body(external_id="github-issue.r#7", comment="Closed on GitHub.")
    message = ADAPTER.validate_python(body)
    assert isinstance(message, TaskUpdateMessage)
    assert message.payload.section is None


@pytest.mark.parametrize("missing", ["external_id", "comment"])
def test_an_update_refuses_a_payload_without_its_key_or_comment(
    missing: str,
) -> None:
    payload = {"external_id": "ci.r#1", "comment": "c"}
    del payload[missing]
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(_update_body(**payload))


@pytest.mark.parametrize(
    "payload",
    [
        {"external_id": "", "comment": "c"},
        {"external_id": "ci.r#1", "comment": ""},
        {"external_id": "ci.r#1", "comment": "c" * 20_001},
        {"external_id": "x" * 257, "comment": "c"},
        {"external_id": "ci.r#1", "comment": "c", "section": ""},
        {"external_id": "ci.r#1", "comment": "c", "signal": "s"},
    ],
)
def test_an_update_refuses_a_payload_out_of_bounds(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(_update_body(**payload))


@pytest.mark.parametrize("config", ["prd", "dev"])
def test_the_scheduled_sweep_body_is_a_rotation_sweep(config: str) -> None:
    body = {"type": "rotation.sweep", "version": 1, "payload": {"config": config}}
    message = ADAPTER.validate_python(body)
    assert isinstance(message, RotationSweepMessage)
    assert message.payload.config == config
    assert message.model_dump(mode="json") == body


def test_a_rotation_job_round_trips_through_the_union() -> None:
    body = {
        "type": "rotation.job",
        "version": 1,
        "payload": {"name": "TICK_SECRET", "config": "dev", "mode": "compromised"},
    }
    message = ADAPTER.validate_python(body)
    assert isinstance(message, RotationJobMessage)
    assert message.model_dump(mode="json") == body


def test_rotation_messages_built_in_code_carry_their_type_and_version() -> None:
    job = RotationJobMessage(
        payload={"name": "TICK_SECRET", "config": "prd", "mode": "routine"}  # type: ignore[arg-type]
    )
    sweep = RotationSweepMessage(payload={"config": "dev"})  # type: ignore[arg-type]
    assert job.model_dump(mode="json") == {
        "type": "rotation.job",
        "version": 1,
        "payload": {"name": "TICK_SECRET", "config": "prd", "mode": "routine"},
    }
    assert sweep.model_dump(mode="json") == {
        "type": "rotation.sweep",
        "version": 1,
        "payload": {"config": "dev"},
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"config": "dev", "mode": "routine"},
        {"name": "TICK_SECRET", "mode": "routine"},
        {"name": "TICK_SECRET", "config": "dev"},
        {"name": "tick_secret", "config": "dev", "mode": "routine"},
        {"name": "TICK SECRET", "config": "dev", "mode": "routine"},
        {"name": "X" * 129, "config": "dev", "mode": "routine"},
        {"name": "TICK_SECRET", "config": "stg", "mode": "routine"},
        {"name": "TICK_SECRET", "config": "dev", "mode": "scheduled"},
        {"name": "TICK_SECRET", "config": "dev", "mode": "routine", "value": "v"},
    ],
)
def test_a_rotation_job_refuses_a_payload_out_of_shape(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(
            {"type": "rotation.job", "version": 1, "payload": payload}
        )


@pytest.mark.parametrize(
    "body",
    [
        {"type": "rotation.sweep", "version": 1},
        {"type": "rotation.sweep", "version": 1, "payload": {}},
        {"type": "rotation.sweep", "version": 1, "payload": {"config": "stg"}},
        {
            "type": "rotation.sweep",
            "version": 1,
            "payload": {"config": "prd", "name": "TICK_SECRET"},
        },
    ],
)
def test_a_rotation_sweep_refuses_a_body_out_of_shape(body: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(body)


_INCIDENT = "7c2d4e1a-9b3f-4a6c-8d5e-1f2a3b4c5d6e"


def test_an_incident_round_trips_through_the_union() -> None:
    body = {
        "type": "rotation.incident",
        "version": 1,
        "payload": {"incident_id": _INCIDENT},
    }
    message = ADAPTER.validate_python(body)
    assert isinstance(message, RotationIncidentMessage)
    assert message.payload.incident_id == _INCIDENT
    assert message.model_dump(mode="json") == body


def test_an_incident_parses_through_parse_message() -> None:
    body = json.dumps(
        {
            "type": "rotation.incident",
            "version": 1,
            "payload": {"incident_id": _INCIDENT},
        }
    )
    message = parse_message(
        body, RotationJobMessage, RotationSweepMessage, RotationIncidentMessage
    )
    assert isinstance(message, RotationIncidentMessage)


@pytest.mark.parametrize(
    "payload", [{}, {"incident_id": "incident-1"}, {"incident_id": _INCIDENT, "x": 1}]
)
def test_an_incident_refuses_a_payload_out_of_shape(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ADAPTER.validate_python(
            {"type": "rotation.incident", "version": 1, "payload": payload}
        )


def test_an_incident_job_carries_its_id_and_a_plain_job_does_not() -> None:
    incident_job = RotationJobMessage(
        payload={  # type: ignore[arg-type]
            "name": "TICK_SECRET",
            "config": "prd",
            "mode": "compromised",
            "incident_id": _INCIDENT,
        }
    )
    plain = RotationJobMessage(
        payload={"name": "TICK_SECRET", "config": "prd", "mode": "routine"}  # type: ignore[arg-type]
    )
    assert incident_job.model_dump(mode="json")["payload"]["incident_id"] == _INCIDENT
    # A consumer on a release from before incidents forbids the field, so a
    # job with none must not send it, not even as null.
    assert "incident_id" not in plain.model_dump(mode="json")["payload"]
    assert "incident_id" not in json.loads(plain.model_dump_json())["payload"]
