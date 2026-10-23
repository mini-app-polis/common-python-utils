"""What every Lambda queue consumer in the fleet does the same way.

A cog behind an SQS queue does four things that are not its own work:

- **Stops before the timeout.** Lambda kills a timed-out invocation
  outright: no ``finally``, no report, and the handler returns nothing, so
  the message comes back with nobody told why. :func:`deadline` raises
  :class:`RunOutOfTime` a margin early, inside the run, so it travels the
  ordinary failure path: the run report sends, and the record is named for
  redelivery.
- **Reads its message.** :func:`parse_message` reads one body as the
  message kinds the consumer accepts
  (:mod:`mini_app_polis.api.contract.queue`), or raises
  :class:`UnprocessableMessage`.
- **Names its failures.** :func:`process_records` runs each record and
  returns ``{"batchItemFailures": [...]}``, naming every record that failed
  once. A record left out is deleted from the queue; one named comes back,
  or goes to the dead-letter queue when its receives are spent.
- **Never raises from the handler.** A handler that raises fails the whole
  batch, the records that succeeded included.

Nothing here imports googleapiclient or boto: a cog that uses the shared
Google retry loops passes its own request timeout as
``request_headroom_seconds``.

Usage::

    from mini_app_polis.api.contract import TranscriptionRunMessage
    from mini_app_polis.lambda_worker import (
        deadline,
        parse_message,
        process_records,
    )

    def lambda_handler(event, context):
        def handle(record):
            message = parse_message(record["body"], TranscriptionRunMessage)
            with deadline(context):
                run(message.payload, run_id=record["messageId"])

        return process_records(event, handle)
"""

from __future__ import annotations

import json
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from typing import Any, TypeVar

from pydantic import ValidationError

from mini_app_polis import logger as logger_mod
from mini_app_polis.api.contract.queue import QueueMessage
from mini_app_polis.deadline import run_deadline

__all__ = [
    "DEFAULT_DEADLINE_MARGIN_SECONDS",
    "RunOutOfTime",
    "UnprocessableMessage",
    "all_failed",
    "deadline",
    "parse_message",
    "process_records",
]

log = logger_mod.get_logger()

#: How long before the function's timeout a run is stopped by default, so
#: that it can still send its report and the handler can still return.
DEFAULT_DEADLINE_MARGIN_SECONDS = 30.0


class RunOutOfTime(BaseException):  # noqa: N818 — named for what happened
    """The run was about to be killed by the function's timeout.

    A :class:`BaseException`, so a flow's ``except Exception`` cannot
    swallow it and carry on into the timeout it was raised to prevent.
    :func:`mini_app_polis.pipeline_status.run_report` catches
    ``BaseException``, so the run's report still says what it had done.
    """


class UnprocessableMessage(RuntimeError):
    """The message cannot be handled by this consumer, ever."""


@contextmanager
def deadline(
    context: Any,
    *,
    margin_seconds: float = DEFAULT_DEADLINE_MARGIN_SECONDS,
    request_headroom_seconds: float = 0.0,
) -> Iterator[None]:
    """Raise :class:`RunOutOfTime` ``margin_seconds`` before the timeout.

    SIGALRM, because a run is blocking I/O on the main thread, which is
    where Lambda runs the handler and where the signal is delivered. A
    context without ``get_remaining_time_in_millis`` (a test, a local call)
    gets no deadline.

    Inside, the shared run deadline (:func:`mini_app_polis.deadline.run_deadline`)
    is set ``request_headroom_seconds`` before that stop. The library's
    retry loops check it between attempts, never during one, so an attempt
    started just before it can run one request's timeout; pass the longest
    request the run makes, and that attempt still ends before the stop.
    Clamped at zero: with less time left than the margin and the headroom,
    the library starts no more requests and the run fails the ordinary way.
    """
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if remaining is None:
        yield
        return
    seconds = remaining() / 1000 - margin_seconds
    if seconds <= 0:
        raise RunOutOfTime("no time left in this invocation to start a run")

    def _expire(signum: int, frame: Any) -> None:  # noqa: ARG001
        raise RunOutOfTime(f"stopped {margin_seconds:g}s before the function timeout")

    previous = signal.signal(signal.SIGALRM, _expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        with run_deadline(max(0.0, seconds - request_headroom_seconds)):
            yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


M = TypeVar("M", bound=QueueMessage[Any])


def parse_message(body: str | bytes, *kinds: type[M]) -> M:
    """Read ``body`` as one of ``kinds``. Raises :class:`UnprocessableMessage`.

    In order: the body is JSON, it is an object, its version is one this
    consumer speaks, its type is one of ``kinds``, it carries a payload
    object, and the payload is valid for its kind.
    """
    if not kinds:
        raise ValueError("parse_message needs at least one message kind")
    try:
        message = json.loads(body)
    except ValueError as exc:
        raise UnprocessableMessage(f"body is not JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise UnprocessableMessage("body is not an object")

    version = message.get("version")
    speaks = sorted({kind.VERSION for kind in kinds})
    if version not in speaks:
        spoken = ", ".join(str(v) for v in speaks)
        raise UnprocessableMessage(
            f"message version {version!r}, this consumer speaks {spoken}"
        )
    by_type: dict[Any, type[M]] = {kind.message_type(): kind for kind in kinds}
    kind = by_type.get(message.get("type"))
    if kind is None:
        raise UnprocessableMessage(f"unknown message type {message.get('type')!r}")
    if version != kind.VERSION:
        raise UnprocessableMessage(
            f"message version {version!r}, {kind.message_type()} is "
            f"version {kind.VERSION}"
        )
    if not isinstance(message.get("payload"), dict):
        raise UnprocessableMessage("message carries no payload object")
    try:
        return kind.model_validate(message)
    except ValidationError as exc:
        raise UnprocessableMessage(
            f"payload is not a valid {kind.message_type()}: {exc}"
        ) from exc


def _records(event: Any) -> list[dict[str, Any]]:
    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list):
        return []
    return [r for r in records if isinstance(r, dict)]


def _message_id(record: dict[str, Any]) -> str:
    return str(record.get("messageId") or "")


def all_failed(event: Any) -> dict[str, list[dict[str, str]]]:
    """Name every record in ``event`` for redelivery.

    For a handler that cannot run any of them: its settings did not load,
    say.
    """
    ids = dict.fromkeys(_message_id(r) for r in _records(event))
    return {"batchItemFailures": [{"itemIdentifier": i} for i in ids]}


def process_records(
    event: Any,
    handle: Callable[[dict[str, Any]], None],
    *,
    on_unprocessable: Callable[[UnprocessableMessage, dict[str, Any]], None]
    | None = None,
) -> dict[str, list[dict[str, str]]]:
    """Run ``handle`` on each record and name the ones that failed. Never raises.

    A record fails when ``handle`` raises :class:`UnprocessableMessage`,
    :class:`RunOutOfTime`, or any other ``Exception``. Each failing record
    is named once; a record that succeeds is not named, and is deleted.

    ``on_unprocessable`` is told about each unprocessable record — to
    report it, say. It is best-effort: an error it raises is logged, and
    the record is still named.
    """
    failed: dict[str, None] = {}
    for record in _records(event):
        message_id = _message_id(record)
        try:
            handle(record)
        except UnprocessableMessage as exc:
            failed[message_id] = None
            with suppress(Exception):
                log.error(
                    "lambda_worker: unprocessable message %s: %s", message_id, exc
                )
            if on_unprocessable is not None:
                try:
                    on_unprocessable(exc, record)
                except Exception:  # noqa: BLE001 — the report is not the job
                    with suppress(Exception):
                        log.exception(
                            "lambda_worker: could not report unprocessable message %s",
                            message_id,
                        )
        except RunOutOfTime as exc:
            failed[message_id] = None
            with suppress(Exception):
                log.error("lambda_worker: run %s out of time: %s", message_id, exc)
        except Exception:  # noqa: BLE001 — named for redelivery, never raised
            failed[message_id] = None
            with suppress(Exception):
                log.exception("lambda_worker: run %s failed", message_id)
    return {"batchItemFailures": [{"itemIdentifier": i} for i in failed]}
