"""Tests for mini_app_polis.deadline."""

from __future__ import annotations

import asyncio
import threading
import types

import pytest

from mini_app_polis import deadline
from mini_app_polis.deadline import (
    RunDeadlineReached,
    remaining_seconds,
    run_deadline,
)


class FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(
        deadline, "time", types.SimpleNamespace(monotonic=fake.monotonic)
    )
    return fake


def test_remaining_is_none_without_a_deadline():
    assert remaining_seconds() is None


def test_remaining_counts_down_and_goes_negative(clock):
    with run_deadline(10):
        assert remaining_seconds() == 10
        clock.now += 4
        assert remaining_seconds() == 6
        clock.now += 10
        assert remaining_seconds() == -4


def test_deadline_is_restored_on_exit(clock):
    with run_deadline(10):
        pass
    assert remaining_seconds() is None


def test_deadline_is_restored_when_the_block_raises(clock):
    with pytest.raises(ValueError), run_deadline(10):
        raise ValueError("boom")
    assert remaining_seconds() is None


def test_nesting_keeps_the_earlier_deadline(clock):
    with run_deadline(10):
        with run_deadline(60):  # cannot extend
            assert remaining_seconds() == 10
        with run_deadline(3):  # can tighten
            assert remaining_seconds() == 3
        assert remaining_seconds() == 10  # outer restored
    assert remaining_seconds() is None


def test_run_deadline_reached_is_a_timeout_error():
    assert issubclass(RunDeadlineReached, TimeoutError)


def test_ensure_time_left_is_a_no_op_without_a_deadline():
    deadline._ensure_time_left("doing a thing")


def test_ensure_time_left_raises_past_the_deadline_and_chains(clock):
    cause = ConnectionResetError("reset")
    with run_deadline(5):
        deadline._ensure_time_left("doing a thing")  # time left: no raise
        clock.now += 5
        with pytest.raises(RunDeadlineReached, match="while doing a thing") as exc:
            deadline._ensure_time_left("doing a thing", cause)
    assert exc.value.__cause__ is cause


def test_wait_crosses_deadline(clock):
    assert deadline._wait_crosses_deadline(10_000) is False  # no deadline
    with run_deadline(5):
        assert deadline._wait_crosses_deadline(4.9) is False
        assert deadline._wait_crosses_deadline(5) is True  # ends at it
        assert deadline._wait_crosses_deadline(6) is True


def test_package_exports_the_module_lazily():
    import mini_app_polis

    assert "deadline" in mini_app_polis.__all__
    assert mini_app_polis.deadline is deadline


def test_deadline_propagates_into_asyncio_to_thread():
    async def main() -> float | None:
        with run_deadline(100):
            return await asyncio.to_thread(remaining_seconds)

    seen = asyncio.run(main())
    assert seen is not None and 0 < seen <= 100


def test_deadline_does_not_propagate_into_a_plain_thread():
    seen: list[float | None] = []
    with run_deadline(100):
        t = threading.Thread(target=lambda: seen.append(remaining_seconds()))
        t.start()
        t.join()
    assert seen == [None]
