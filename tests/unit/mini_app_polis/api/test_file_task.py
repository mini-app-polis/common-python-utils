"""``file-task``: a working session files a task through POST /v1/tasks."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from mini_app_polis.api import file_task
from mini_app_polis.api.contract import TaskCreateAccepted, TaskCreateRequest
from mini_app_polis.api.errors import ApiUnavailable, KaianoApiError


class FakeApi:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[TaskCreateRequest] = []

    def request_task(self, payload: TaskCreateRequest) -> TaskCreateAccepted:
        self.sent.append(payload)
        if self.error:
            raise self.error
        return TaskCreateAccepted(
            message_id="m-1", source=payload.source, external_id=payload.external_id
        )


def test_flags_make_a_manual_draft_and_print_the_answer(capsys) -> None:
    api = FakeApi()
    code = file_task.main(
        [
            "--key",
            "split-the-vault",
            "--title",
            "Split the vault",
            "--where",
            "infra",
            "--effort",
            "M",
            "--executor",
            "cowork",
            "--body",
            "Why.",
        ],
        client=api,  # type: ignore[arg-type]
    )
    assert code == 0
    (sent,) = api.sent
    assert sent.source == "manual"
    assert sent.external_id == "manual.split-the-vault"
    assert sent.id is None
    assert sent.draft.title == "Split the vault"
    assert sent.draft.labels == ["cowork"]
    assert json.loads(capsys.readouterr().out) == {
        "external_id": "manual.split-the-vault",
        "accepted": True,
        "message_id": "m-1",
    }


def test_an_agent_task_carries_no_executor_label() -> None:
    api = FakeApi()
    file_task.main(["--key", "k", "--title", "t", "--executor", "agent"], client=api)  # type: ignore[arg-type]
    file_task.main(["--key", "k2", "--title", "t"], client=api)  # type: ignore[arg-type]
    assert [s.draft.labels for s in api.sent] == [[], []]


def test_a_draft_can_come_as_json(tmp_path: Path) -> None:
    path = tmp_path / "draft.json"
    path.write_text(json.dumps({"title": "From JSON", "labels": ["person"]}))
    api = FakeApi()
    assert file_task.main(["--key", "k", "--json", str(path)], client=api) == 0  # type: ignore[arg-type]
    assert api.sent[0].draft.title == "From JSON"
    assert api.sent[0].draft.labels == ["person"]


def test_a_draft_can_come_on_standard_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO('{"title": "Piped"}'))
    api = FakeApi()
    assert file_task.main(["--key", "k", "--json", "-"], client=api) == 0  # type: ignore[arg-type]
    assert api.sent[0].draft.title == "Piped"


def test_the_key_is_required() -> None:
    with pytest.raises(SystemExit) as exit_:
        file_task.main(["--title", "t"], client=FakeApi())  # type: ignore[arg-type]
    assert exit_.value.code == 2


def test_a_draft_the_contract_refuses_files_nothing(capsys) -> None:
    api = FakeApi()
    code = file_task.main(["--key", "k", "--title", "x" * 501], client=api)  # type: ignore[arg-type]
    assert code == 2
    assert api.sent == []
    assert "the draft was refused" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error",
    [
        KaianoApiError(403, "forbidden: source manual\ntrace", "/v1/tasks"),
        ApiUnavailable(0, "Connection failed", "/v1/tasks"),
        KaianoApiError(0, "No API key: set this machine's key", ""),
    ],
)
def test_a_refused_request_says_why_in_one_line(capsys, error) -> None:
    code = file_task.main(
        ["--key", "k", "--title", "t"],
        client=FakeApi(error),  # type: ignore[arg-type]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("file-task: manual.k not filed:")
    assert err.count("\n") == 1


def test_the_session_files_as_its_own_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[str | None] = []

    def from_env(machine_name=None):
        built.append(machine_name)
        return FakeApi()

    monkeypatch.setattr(file_task.KaianoApiClient, "from_env", staticmethod(from_env))
    assert file_task.main(["--key", "k", "--title", "t"]) == 0
    assert built == ["claude-session"]
