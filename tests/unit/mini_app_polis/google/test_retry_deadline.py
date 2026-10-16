"""execute_with_retry under a run deadline."""

from __future__ import annotations

import types

import pytest

from mini_app_polis import deadline
from mini_app_polis.deadline import RunDeadlineReached, run_deadline


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch):
    """Fake monotonic clock; each sleep advances it. Returns the sleeps."""
    state = {"now": 1000.0}
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        state["now"] += s

    monkeypatch.setattr(
        deadline, "time", types.SimpleNamespace(monotonic=lambda: state["now"])
    )
    monkeypatch.setattr("mini_app_polis.google._retry.time.sleep", sleep)
    # Jitter factor 0.7 + 0.5 * 0.6 = 1.0: waits equal the nominal delay.
    monkeypatch.setattr("mini_app_polis.google._retry.random.random", lambda: 0.5)
    return types.SimpleNamespace(state=state, sleeps=sleeps)


def _always_failing(err: Exception):
    calls: list[int] = []

    def fn():
        calls.append(1)
        raise err

    return fn, calls


def test_no_deadline_keeps_retrying_to_the_limit(clock, as_http_error):
    from mini_app_polis.google._retry import RetryConfig, execute_with_retry

    err = as_http_error(status=503)
    fn, calls = _always_failing(err)
    retry = RetryConfig(max_retries=4, base_delay_s=10, max_delay_s=60)

    with pytest.raises(type(err)):
        execute_with_retry(fn, context="listing files", retry=retry)
    assert len(calls) == 4
    assert clock.sleeps == [10, 20, 40]


def test_stops_before_a_backoff_that_would_cross_the_deadline(clock, as_http_error):
    from mini_app_polis.google._retry import RetryConfig, execute_with_retry

    err = as_http_error(status=503)
    fn, calls = _always_failing(err)
    retry = RetryConfig(max_retries=8, base_delay_s=10, max_delay_s=60)

    # 10 + 20 = 30 s of backoff fit in 35 s; the next (40 s) would not.
    with run_deadline(35), pytest.raises(type(err)) as exc:
        execute_with_retry(fn, context="listing files", retry=retry)
    assert exc.value is err  # the real error, not a timeout
    assert len(calls) == 3
    assert clock.sleeps == [10, 20]


def test_backoff_ending_exactly_at_the_deadline_is_not_taken(clock):
    from mini_app_polis.google._retry import RetryConfig, execute_with_retry

    fn, calls = _always_failing(ConnectionResetError("reset"))
    retry = RetryConfig(max_retries=5, base_delay_s=10, max_delay_s=60)

    with run_deadline(10), pytest.raises(ConnectionResetError):
        execute_with_retry(fn, context="reading sheet", retry=retry)
    assert len(calls) == 1
    assert clock.sleeps == []


def test_refuses_to_start_an_attempt_past_the_deadline(clock):
    from mini_app_polis.google._retry import execute_with_retry

    calls: list[int] = []

    with run_deadline(5):
        clock.state["now"] += 5
        with pytest.raises(RunDeadlineReached, match="while reading sheet"):
            execute_with_retry(lambda: calls.append(1), context="reading sheet")
    assert calls == []


def test_attempt_refused_after_an_error_is_chained_from_it(clock, monkeypatch):
    from mini_app_polis.google import _retry
    from mini_app_polis.google._retry import RetryConfig, execute_with_retry

    err = ConnectionResetError("reset")
    fn, calls = _always_failing(err)

    # The backoff fits, but the attempt it leads to finds the deadline gone
    # (the clock moved on while something else ran).
    def sleep(s: float) -> None:
        clock.state["now"] += 100

    monkeypatch.setattr(_retry.time, "sleep", sleep)
    retry = RetryConfig(max_retries=5, base_delay_s=1, max_delay_s=60)

    with run_deadline(50), pytest.raises(RunDeadlineReached) as exc:
        execute_with_retry(fn, context="reading sheet", retry=retry)
    assert exc.value.__cause__ is err
    assert len(calls) == 1


def test_run_deadline_reached_from_inside_fn_is_not_retried(clock):
    from mini_app_polis.google._retry import (
        execute_with_retry,
        is_retryable_non_http_error,
    )

    assert is_retryable_non_http_error(RunDeadlineReached("x")) is False
    assert is_retryable_non_http_error(TimeoutError("x")) is True

    fn, calls = _always_failing(RunDeadlineReached("inner"))
    with pytest.raises(RunDeadlineReached):
        execute_with_retry(fn, context="nested")
    assert len(calls) == 1
