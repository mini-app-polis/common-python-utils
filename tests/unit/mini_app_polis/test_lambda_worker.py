"""Tests for mini_app_polis.lambda_worker."""

from __future__ import annotations

import json
import sys
import time
from typing import Any

import pytest

import mini_app_polis
from mini_app_polis.api.contract import (
    DeejayRunMessage,
    TaskGroomMessage,
    TranscriptionRunMessage,
)
from mini_app_polis.deadline import remaining_seconds
from mini_app_polis.lambda_worker import (
    RunOutOfTime,
    UnprocessableMessage,
    all_failed,
    deadline,
    parse_message,
    process_records,
)


class FakeContext:
    def __init__(self, remaining_ms: float) -> None:
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> float:
        return self.remaining_ms


def _body(**overrides: Any) -> str:
    message: dict[str, Any] = {
        "type": "transcription.run",
        "version": 1,
        "payload": {"mode": "voicenotes", "drive_file_id": "f1"},
    }
    message.update(overrides)
    return json.dumps(message)


# -- deadline ------------------------------------------------------------------


def test_deadline_raises_run_out_of_time_before_the_timeout() -> None:
    started = time.monotonic()
    with (
        pytest.raises(RunOutOfTime),
        deadline(FakeContext(1300), margin_seconds=1),
    ):
        time.sleep(5)
    assert time.monotonic() - started < 2


def test_run_out_of_time_is_not_an_exception() -> None:
    """A flow's ``except Exception`` must not swallow it."""
    assert not issubclass(RunOutOfTime, Exception)
    with pytest.raises(RunOutOfTime), deadline(FakeContext(1200), margin_seconds=1):
        try:
            time.sleep(5)
        except Exception:  # noqa: BLE001 — what a flow might write
            pytest.fail("RunOutOfTime was caught as an Exception")


def test_no_deadline_without_remaining_time() -> None:
    with deadline(object()):
        assert remaining_seconds() is None


def test_no_time_left_raises_before_the_run() -> None:
    with pytest.raises(RunOutOfTime), deadline(FakeContext(500), margin_seconds=1):
        pytest.fail("the run started with no time left")


def test_deadline_sets_the_shared_run_deadline_with_headroom() -> None:
    with deadline(FakeContext(100_000), margin_seconds=30, request_headroom_seconds=60):
        left = remaining_seconds()
        assert left is not None
        assert 9 < left <= 10
    assert remaining_seconds() is None


def test_shared_run_deadline_clamped_at_zero() -> None:
    with deadline(FakeContext(40_000), margin_seconds=30, request_headroom_seconds=60):
        left = remaining_seconds()
        assert left is not None
        assert left <= 0


def test_deadline_clears_the_alarm() -> None:
    import signal

    with deadline(FakeContext(100_000), margin_seconds=1):
        pass
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


# -- parse_message ---------------------------------------------------------------


def test_parses_a_message_of_an_accepted_kind() -> None:
    message = parse_message(_body(), TranscriptionRunMessage)
    assert isinstance(message, TranscriptionRunMessage)
    assert message.payload.mode == "voicenotes"
    assert message.payload.drive_file_id == "f1"


def test_parses_bytes() -> None:
    message = parse_message(_body().encode(), TranscriptionRunMessage)
    assert message.type == "transcription.run"


def test_picks_the_kind_by_type() -> None:
    body = json.dumps({"type": "deejay.run", "version": 1, "payload": {"mode": "m"}})
    message = parse_message(body, TranscriptionRunMessage, DeejayRunMessage)
    assert isinstance(message, DeejayRunMessage)


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ("not json", "not JSON"),
        ("[1, 2]", "not an object"),
        (_body(version=2), "version 2"),
        (_body(version=None), "version None"),
        (_body(type="deejay.run"), "unknown message type"),
        (_body(payload=None), "no payload object"),
        (_body(payload=["mode"]), "no payload object"),
        (_body(payload={"drive_file_id": "f1"}), "not a valid transcription.run"),
    ],
)
def test_unprocessable_messages(body: str, says: str) -> None:
    with pytest.raises(UnprocessableMessage, match=says):
        parse_message(body, TranscriptionRunMessage)


def test_missing_payload_is_unprocessable() -> None:
    body = json.dumps({"type": "transcription.run", "version": 1})
    with pytest.raises(UnprocessableMessage, match="no payload object"):
        parse_message(body, TranscriptionRunMessage)


def test_needs_a_kind() -> None:
    with pytest.raises(ValueError, match="at least one"):
        parse_message(_body())


# -- process_records -------------------------------------------------------------


def _event(*ids: str) -> dict[str, Any]:
    return {"Records": [{"messageId": i, "body": i} for i in ids]}


def test_names_every_failing_record_once_and_no_successes() -> None:
    def handle(record: dict[str, Any]) -> None:
        body = record["body"]
        if body == "bad":
            raise UnprocessableMessage("bad")
        if body == "late":
            raise RunOutOfTime("late")
        if body == "boom":
            raise RuntimeError("boom")

    result = process_records(_event("ok1", "bad", "late", "ok2", "boom"), handle)
    assert result == {
        "batchItemFailures": [
            {"itemIdentifier": "bad"},
            {"itemIdentifier": "late"},
            {"itemIdentifier": "boom"},
        ]
    }


def test_all_successes_name_nothing() -> None:
    assert process_records(_event("a", "b"), lambda _record: None) == {
        "batchItemFailures": []
    }


def test_unprocessable_is_reported_and_a_broken_report_does_not_raise() -> None:
    told: list[str] = []

    def handle(record: dict[str, Any]) -> None:
        raise UnprocessableMessage(record["body"])

    def on_unprocessable(exc: UnprocessableMessage, record: dict[str, Any]) -> None:
        told.append(str(exc))
        raise RuntimeError("the report failed too")

    result = process_records(_event("x"), handle, on_unprocessable=on_unprocessable)
    assert told == ["x"]
    assert result == {"batchItemFailures": [{"itemIdentifier": "x"}]}


@pytest.mark.parametrize("event", [None, "nope", {}, {"Records": "nope"}, [1, 2]])
def test_handler_never_raises_on_a_malformed_event(event: Any) -> None:
    assert process_records(event, lambda _record: None) == {"batchItemFailures": []}


def test_all_failed_names_every_record() -> None:
    assert all_failed(_event("a", "b")) == {
        "batchItemFailures": [{"itemIdentifier": "a"}, {"itemIdentifier": "b"}]
    }
    assert all_failed(None) == {"batchItemFailures": []}


# -- the module ------------------------------------------------------------------


def test_reachable_from_the_package() -> None:
    assert "lambda_worker" in mini_app_polis.__all__
    assert mini_app_polis.lambda_worker.parse_message is parse_message


def test_imports_no_google_or_aws_client() -> None:
    import subprocess

    code = (
        "import sys, mini_app_polis.lambda_worker; "
        "bad = [m for m in sys.modules if m.split('.')[0] in "
        "('googleapiclient', 'boto3', 'botocore')]; "
        "print(','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


def test_grooming_kind_carries_its_contract_model() -> None:
    assert TaskGroomMessage.message_type() == "task.groom"
