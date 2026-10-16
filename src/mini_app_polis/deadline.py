"""A deadline for the current run, which the library's retry loops respect.

Why this exists. A Lambda job has a hard ceiling (900 s at most), and a
cog stops its own run some seconds before it so it can report what
happened. The library's retry loops knew nothing of that: one Google call
retries up to eight times with backoff up to 60 s, and with the client's
own ~60 s request timeout that is ~640 s for a single call — enough on its
own to carry a run past the ceiling, where Lambda kills it mid-write and
nothing is reported.

:func:`run_deadline` tells the library when the run must stop. While a
deadline is set, the retry loops this package owns
(:func:`mini_app_polis.google._retry.execute_with_retry`, the Drive copy
loop, Spotify's per-call retries) do two things differently:

- they refuse to *start* an attempt once the deadline has passed, raising
  :class:`RunDeadlineReached` (chained from the previous attempt's error,
  if there was one);
- they refuse to *wait* for a backoff that would end at or past the
  deadline, and re-raise the error that prompted it instead, so the
  caller sees the real failure rather than a timeout it did not ask for.

An attempt already in flight is not interrupted: the deadline is checked
between attempts, never during one. Leave enough headroom between the
deadline and the hard ceiling for one call's own timeout.

Without a deadline, nothing changes: :func:`remaining_seconds` returns
``None`` and every loop behaves as it always has.

Cleanup. Work that must happen *because* the run is out of time — rolling
back a half-finished write, say — would be refused by the very deadline
that triggered it. :func:`without_run_deadline` clears the deadline for its
block so that cleanup runs with the library's ordinary retries, then puts
the deadline back. Keep such blocks short: they spend the headroom left
between the deadline and the hard ceiling.

Scope. The deadline lives in a :class:`contextvars.ContextVar`, so it
belongs to the code running inside the ``with`` block and nothing else:

- :func:`asyncio.to_thread` copies the current context into the worker
  thread, so a Drive or Sheets call made through it (as the cogs and APIs
  do) sees the deadline;
- an :class:`asyncio.Task` created inside the block copies it too;
- a plain :class:`threading.Thread`, or a ``ThreadPoolExecutor`` worker
  started with ``submit``, does **not** — it starts with an empty context.
  Run the target under ``contextvars.copy_context().run`` if it needs the
  deadline.

Usage, in a Lambda handler::

    from mini_app_polis.deadline import RunDeadlineReached, run_deadline

    budget_s = context.get_remaining_time_in_millis() / 1000 - 30
    try:
        with run_deadline(budget_s):
            run(...)
    except RunDeadlineReached:
        ...  # stop cleanly; report a partial run

And for a rollback that has to happen after the deadline::

    from mini_app_polis.deadline import without_run_deadline

    with without_run_deadline():
        drive.delete_file(half_written_id)
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = [
    "RunDeadlineReached",
    "remaining_seconds",
    "run_deadline",
    "without_run_deadline",
]

#: Absolute deadline on the :func:`time.monotonic` clock, or ``None``.
_deadline: ContextVar[float | None] = ContextVar(
    "mini_app_polis_run_deadline", default=None
)


class RunDeadlineReached(TimeoutError):
    """A call would have started, or waited, past the run's deadline.

    A :class:`TimeoutError`, so code that already treats timeouts as
    "stop and report" handles it without change. It is never retried by
    this package's retry loops. The message names what was being done.
    """


@contextmanager
def run_deadline(seconds: float) -> Iterator[None]:
    """Set the run deadline to ``seconds`` from now for the ``with`` block.

    Nesting keeps the earlier of the two deadlines: an inner block can
    tighten the budget but never extend it. The previous deadline (or
    none) is restored when the block exits, however it exits.

    A ``seconds`` of zero or less sets a deadline that has already passed,
    so the first retry-loop attempt inside the block raises
    :class:`RunDeadlineReached`.
    """
    new = time.monotonic() + seconds
    current = _deadline.get()
    token = _deadline.set(new if current is None else min(current, new))
    try:
        yield
    finally:
        _deadline.reset(token)


@contextmanager
def without_run_deadline() -> Iterator[None]:
    """Clear the run deadline for the ``with`` block.

    For cleanup that must run even though the deadline has passed — a
    rollback of a partial write, for instance — and that the library's
    retry loops would otherwise refuse with :class:`RunDeadlineReached`.
    Inside the block :func:`remaining_seconds` returns ``None`` and the
    retry loops behave as they do with no deadline set. The previous
    deadline is restored when the block exits, however it exits.

    A :func:`run_deadline` nested inside starts afresh, since there is no
    earlier deadline to keep: use one to bound the cleanup itself.
    """
    token = _deadline.set(None)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_seconds() -> float | None:
    """Seconds left before the run deadline, or ``None`` when none is set.

    Negative once the deadline has passed.
    """
    deadline = _deadline.get()
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _ensure_time_left(context: str, last_error: BaseException | None = None) -> None:
    """Raise :class:`RunDeadlineReached` if the deadline has passed.

    For retry loops in this package, called before each attempt.
    ``context`` completes "while …"; ``last_error`` is the previous
    attempt's error, which the raised exception is chained from.
    """
    remaining = remaining_seconds()
    if remaining is not None and remaining <= 0:
        raise RunDeadlineReached(
            f"Run deadline reached while {context}; not starting another attempt"
        ) from last_error


def _wait_crosses_deadline(wait_s: float) -> bool:
    """Whether sleeping ``wait_s`` seconds would end at or past the deadline.

    For retry loops in this package, called before a backoff sleep.
    Always ``False`` when no deadline is set.
    """
    remaining = remaining_seconds()
    return remaining is not None and wait_s >= remaining
