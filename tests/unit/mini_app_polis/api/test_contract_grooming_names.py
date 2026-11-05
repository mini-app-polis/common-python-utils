"""The grooming names still work beside the refining ones.

Grooming was renamed refining. Until every cog has moved, the API serves the
grooming routes and the old names have to keep their wire shape. These tests
go with the names when they are removed.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from mini_app_polis.api import contract
from mini_app_polis.api.contract import (
    ENDPOINTS_BY_NAME,
    NEEDS_REFINING,
    AgentRunResult,
    GroomingRecord,
    GroomingRecordWrite,
    JobMessage,
    RefiningRecord,
    TaskGroomMessage,
    TaskGroomProposalMessage,
    TaskRefineMessage,
    TaskRefineProposalMessage,
    TaskSweepPayload,
    TaskSweepRequest,
)
from mini_app_polis.api.request_sweep import _parser, _request

FINGERPRINT = "a" * 64

GROOMING_TO_REFINING = {
    "list_groomings": "list_refinings",
    "record_grooming": "record_refining",
    "record_grooming_run": "record_refining_run",
    "grooming_spend": "refining_spend",
    "grooming_return_rate": "refining_return_rate",
    "request_grooming": "request_refining",
    "submit_grooming_proposal": "submit_refining_proposal",
    "claim_grooming": "claim_refining",
    "release_grooming_claim": "release_refining_claim",
    "mark_grooming_budget_spent": "mark_refining_budget_spent",
}


@pytest.mark.parametrize(("old", "new"), sorted(GROOMING_TO_REFINING.items()))
def test_each_grooming_route_has_a_refining_twin(old: str, new: str) -> None:
    before, after = ENDPOINTS_BY_NAME[old], ENDPOINTS_BY_NAME[new]
    assert after.method == before.method
    assert after.path == before.path.replace("/groomings", "/refinings")
    assert after.scope == before.scope.replace("groomings", "refinings")


def test_the_grooming_record_keeps_its_wire_names() -> None:
    record = GroomingRecordWrite(
        task_gid="1",
        fingerprint=FINGERPRINT,
        outcome="questions",
        groomed_fingerprint=FINGERPRINT,
        groom_attempts=2,
    )
    body = record.model_dump(mode="json")
    assert body["groomed_fingerprint"] == FINGERPRINT
    assert body["groom_attempts"] == 2
    assert "refined_fingerprint" not in body


def test_the_refining_record_speaks_refining_names() -> None:
    record = RefiningRecord(
        task_gid="1",
        fingerprint=FINGERPRINT,
        outcome="ready",
        refined_fingerprint=FINGERPRINT,
        refine_attempts=1,
        updated_at="2026-11-04T00:00:00Z",
    )
    assert record.refined_fingerprint == FINGERPRINT
    with pytest.raises(ValidationError):
        contract.RefiningRecordWrite(
            task_gid="1",
            fingerprint=FINGERPRINT,
            outcome="ready",
            groomed_fingerprint=FINGERPRINT,
        )
    assert GroomingRecord.model_fields.keys() - RefiningRecord.model_fields.keys() == {
        "groomed_fingerprint",
        "groom_sent_at",
        "groom_attempts",
    }


@pytest.mark.parametrize(
    ("kind", "type_", "payload"),
    [
        (TaskRefineMessage, "task.refine", "job"),
        (TaskGroomMessage, "task.groom", "job"),
        (TaskRefineProposalMessage, "task.refine-proposal", "proposal"),
        (TaskGroomProposalMessage, "task.groom-proposal", "proposal"),
    ],
)
def test_both_names_of_each_message_parse_through_the_union(
    kind: Any, type_: str, payload: str
) -> None:
    bodies = {
        "job": {
            "task": {
                "task_gid": "1",
                "name": "A task",
                "fingerprint": FINGERPRINT,
            }
        },
        "proposal": {
            "task_gid": "1",
            "fingerprint": FINGERPRINT,
            "title": "A task",
            "model": "a-model",
            "cost_usd": 0.1,
        },
    }
    message = TypeAdapter(JobMessage).validate_python(
        {"type": type_, "version": 1, "payload": bodies[payload]}
    )
    assert isinstance(message, kind)


@pytest.mark.parametrize(
    "payload", [{"refine": True}, {"groom": True}, {"refine": True, "groom": False}]
)
def test_either_sweep_flag_asks_for_refining(payload: dict[str, Any]) -> None:
    for model in (TaskSweepPayload, TaskSweepRequest):
        parsed = model.model_validate(payload)
        assert parsed.refine is True
        assert parsed.groom is True


def test_a_sweep_message_with_the_old_flag_refines() -> None:
    message = TypeAdapter(JobMessage).validate_python(
        {"type": "task.sweep", "version": 1, "payload": {"groom": True}}
    )
    assert message.payload.refine is True


def test_request_sweep_takes_the_old_flag() -> None:
    assert _request(_parser().parse_args(["--groom"])).refine is True
    assert _request(_parser().parse_args(["--refine"])).refine is True


@pytest.mark.parametrize("outcome", sorted(NEEDS_REFINING))
def test_either_name_of_needs_refining_asks_its_questions(outcome: str) -> None:
    result = AgentRunResult(
        run_id="00000000-0000-0000-0000-000000000000",
        outcome=outcome,
        questions=["Which one?"],
    )
    assert result.outcome == outcome
    with pytest.raises(ValidationError):
        AgentRunResult(run_id="00000000-0000-0000-0000-000000000000", outcome=outcome)
