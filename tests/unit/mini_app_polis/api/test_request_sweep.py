"""``request-sweep``: a working session asks for a sweep through POST /v1/tasks/sweeps."""

from __future__ import annotations

import json

import pytest

from mini_app_polis.api import request_sweep
from mini_app_polis.api.contract import (
    TaskSweepAccepted,
    TaskSweepMessage,
    TaskSweepRequest,
)
from mini_app_polis.api.errors import ApiUnavailable, KaianoApiError


class FakeApi:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[TaskSweepRequest] = []

    def request_sweep(self, payload: TaskSweepRequest) -> TaskSweepAccepted:
        self.sent.append(payload)
        if self.error:
            raise self.error
        return TaskSweepAccepted(message_id="m-1")


def _body(request: TaskSweepRequest) -> dict[str, object]:
    """What the typed client sends for ``request``."""
    return request.model_dump(mode="json", exclude_unset=True, by_alias=True)


def test_no_flags_sends_an_empty_body_and_prints_the_answer(capsys) -> None:
    api = FakeApi()
    assert request_sweep.main([], client=api) == 0  # type: ignore[arg-type]
    (sent,) = api.sent
    assert _body(sent) == {}
    assert json.loads(capsys.readouterr().out) == {
        "accepted": True,
        "message_id": "m-1",
        "groom": False,
        "agents": False,
    }


@pytest.mark.parametrize(
    ("argv", "body"),
    [
        (["--groom"], {"groom": True}),
        (["--agents"], {"agents": True}),
        (["--groom", "--agents"], {"groom": True, "agents": True}),
    ],
)
def test_each_flag_is_sent_and_only_the_flags_given(argv, body) -> None:
    api = FakeApi()
    assert request_sweep.main(argv, client=api) == 0  # type: ignore[arg-type]
    assert _body(api.sent[0]) == body


@pytest.mark.parametrize(
    "error",
    [
        KaianoApiError(
            403, "forbidden: tasks.sweeps.create\ntrace", "/v1/tasks/sweeps"
        ),
        ApiUnavailable(0, "Connection failed", "/v1/tasks/sweeps"),
        KaianoApiError(0, "No API key: set this machine's key", ""),
    ],
)
def test_a_refused_request_says_why_in_one_line(capsys, error) -> None:
    code = request_sweep.main(["--groom"], client=FakeApi(error))  # type: ignore[arg-type]
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("request-sweep: no sweep queued:")
    assert captured.err.count("\n") == 1


def test_the_session_asks_as_its_own_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[str | None] = []

    def from_env(machine_name=None):
        built.append(machine_name)
        return FakeApi()

    monkeypatch.setattr(
        request_sweep.KaianoApiClient, "from_env", staticmethod(from_env)
    )
    assert request_sweep.main(["--agents"]) == 0
    assert built == ["claude-session"]


def test_a_sweep_request_refuses_unknown_fields() -> None:
    with pytest.raises(ValueError):
        TaskSweepRequest.model_validate({"groom": True, "limit": 50})


def test_a_scheduled_sweep_message_parses_with_no_flags() -> None:
    message = TaskSweepMessage.model_validate({"type": "task.sweep", "version": 1})
    assert (message.payload.groom, message.payload.agents) == (False, False)


def test_a_flagged_sweep_message_carries_its_flags() -> None:
    message = TaskSweepMessage.model_validate(
        {"type": "task.sweep", "version": 1, "payload": {"groom": True}}
    )
    assert (message.payload.groom, message.payload.agents) == (True, False)


def test_a_sweep_message_tolerates_a_flag_it_does_not_know() -> None:
    message = TaskSweepMessage.model_validate(
        {"type": "task.sweep", "version": 1, "payload": {"agents": True, "new": 1}}
    )
    assert message.payload.agents is True
