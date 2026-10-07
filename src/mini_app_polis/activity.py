"""The running list — what changed, and what broke — for a FastAPI service.

Why this exists. api-kaianolevine-com built a Discord feed of its own data
changes and faults; api-deejaytools wants the same feed, plus two things
the first service never needed. This is the shared version. Requires
SQLAlchemy 2 (the ``activity`` extra); importing ``mini_app_polis`` without
it is unaffected, and only importing this module needs it. FastAPI and
Starlette are not imported: the middleware reads ``request.method``,
``request.url.path``, ``request.state`` and ``response.status_code`` and
nothing else.

Four producers feed Discord from here, and they split by channel: changes
and announcements go to ``activity`` (or wherever an announcement says),
faults to ``errors`` alongside every other broken thing in the fleet. The
split is by what the reader is doing — ``activity`` is scrolled back
through after the fact, ``errors`` is watched.

**Data changes.** SQLAlchemy listeners tally every ORM insert, update and
delete that actually committed, and the middleware posts one message per
request summarising them. They are wired to the session rather than to the
routes on purpose: a route added later is covered without anyone
remembering to cover it, which is the only version of this that stays
true. A hand-maintained list of "routes that notify" decays, and its
cheapest failure mode is silence. A flush is not a commit: flushed work is
held until the transaction commits, so a rolled-back request reports
nothing rather than reporting what it tried.

**Faults.** The same middleware reports every 5xx and every unhandled
exception, plus any 4xx returned to a machine caller — when the service
says how to recognise one (``ActivityConfig.is_machine``). A human hitting
a guard is the guard working and is not news. A machine getting a 400 or a
403 is a broken contract between two things this ecosystem owns.

**Announcements** (``announce_on_commit``). A domain event worth its own
message — "song added", with the detail a table tally cannot carry — is
registered on the session and posted only when that session's transaction
commits. A rolled-back transaction announces nothing. It works the same
inside a request and in a background job with its own session, because it
hangs off the session, not the request.

**Background faults** (``report_fault``). Scheduler ticks and job runners
have no request and no middleware, so they post their own fault message,
in the same shape the middleware uses.

What this does not see, stated here rather than left to be discovered:
``session.execute(text(...))``, and anything writing through a connection
the service did not open through an ORM session. ORM-enabled Core
``insert()``, ``update()`` and ``delete()`` are seen, but their row counts
are not — the event that names the table fires before the statement runs —
so they are counted as statements and rendered as such. A request's tally
lives in a context variable the middleware sets, so a session used *outside*
a request (a background job) is not tallied at all; announce what matters
there instead. An exception raised after the response has started — a
lazily streamed body — never reaches this middleware either: Starlette
re-raises it after the middleware has already returned, so the middleware
takes its success branch and the caller sees a truncated 200. Sentry's ASGI
integration does catch it; Discord cannot without wrapping every streaming
response body. The middleware is a function middleware
(``app.middleware("http")``), which is what puts it on that side of
Starlette's machinery. A rolled-back savepoint discards the whole request's
pending tally, so a request that recovers from a savepoint under-reports
rather than over-reports; an announcement registered inside a rolled-back
savepoint still goes out when the outer transaction commits.

Delivery is fire-and-forget. Nothing is retried and nothing here claims a
success it did not get; ``mini_app_polis.discord`` owns what happens to a
failed delivery. Tasks are held in a set until they finish, because a
fire-and-forget task with no strong reference is a fire-and-maybe. Durable
delivery would mean an outbox row written inside the change's own
transaction and a drain to empty it — deferred, and the thing that changes
the decision is wanting to answer "what changed on Tuesday" from the record
rather than from the channel.

Nothing derived from row data reaches the channel from the middleware. A
fault message carries the exception's type and a Sentry id and nothing
else, because a DBAPI error's string representation contains the statement
and its bound parameters, and the channel is a chat room. An announcement
carries what its caller wrote, deliberately.

Usage::

    from mini_app_polis import activity, discord

    config = activity.ActivityConfig(
        service="api-deejaytools",
        suppressed_tables={"identity_audit_events"},
        excluded_paths=("/health", "/version"),
    )
    app.middleware("http")(activity.activity_middleware(config))

    # In a route or a background job, before the commit:
    activity.announce_on_commit(
        session, discord.CHANNEL_ACTIVITY, content="Song added: ..."
    )

    # In a scheduler tick's except block:
    await activity.report_fault("scheduler · drive_jobs", exc, service="api-deejaytools")
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session

from . import _sentry, discord
from .environment import current_environment
from .logger import LOG_WARNING, with_log_prefix

#: A child of the package logger, not ``get_logger()``'s instance, so
#: records name this module and a test that stubs the shared logger
#: module does not swap this one out.
logger = logging.getLogger(__name__)

#: Embed colours. Blue for a change, GitHub's red for a fault — the same
#: red GitHub's workflow embeds use, so severity reads the same way
#: whichever producer put the message in the channel.
_CHANGE_COLOR = 0x58A6FF
_FAULT_COLOR = 0xDA3633

CREATED = "created"
UPDATED = "updated"
DELETED = "deleted"
BULK = "bulk"

#: Render order and mark for each operation. Fixed order so two messages
#: about the same table are comparable at a glance.
_MARKS: tuple[tuple[str, str], ...] = (
    (CREATED, "+"),
    (UPDATED, "~"),
    (DELETED, "-"),
    (BULK, "*"),
)

_LEGEND = "+ created · ~ updated · - deleted · * bulk statement (rows not counted)"

#: Discord's code fence, built rather than written literally so this module's
#: own source can be pasted inside one.
_FENCE = "`" * 3

#: Longest exception detail placed in a fault message.
_DETAIL_LIMIT = 1500

#: Where ``announce_on_commit`` keeps a session's pending announcements.
_ANNOUNCEMENTS_KEY = "mini_app_polis.activity.announcements"

#: ``(channel, payload, context) -> accepted``. The default posts through
#: ``discord.send_payload`` with webhooks read from the environment; a
#: service that resolves webhooks from its own settings passes its own.
Sender = Callable[[str, dict[str, Any], str], Awaitable[bool]]


def _default_send(channel: str, payload: dict[str, Any], context: str) -> Any:
    return discord.send_payload(channel, payload, context=context)


@dataclass
class Recorder:
    """One request's tally of what its transaction actually changed.

    Two buckets, because a flush is not a commit. Work lands in ``pending``
    when the unit of work is flushed and moves to ``committed`` only when
    the transaction commits, so a rolled-back request reports nothing
    rather than reporting what it tried.
    """

    pending: dict[str, Counter[str]] = field(default_factory=dict)
    committed: dict[str, Counter[str]] = field(default_factory=dict)

    def record(self, table: str | None, op: str) -> None:
        """Tally one operation against one table."""
        if not table:
            return
        self.pending.setdefault(table, Counter())[op] += 1

    def promote(self) -> None:
        """Move flushed work into the committed tally."""
        for table, ops in self.pending.items():
            self.committed.setdefault(table, Counter()).update(ops)
        self.pending.clear()

    def discard(self) -> None:
        """Forget flushed work that was rolled back."""
        self.pending.clear()

    def summary(self, suppressed: Collection[str]) -> str:
        """Render the committed tally, or an empty string if nothing is left.

        An empty string is the signal not to send: a request that changed
        only suppressed tables is indistinguishable from one that changed
        nothing, and neither is worth a message.
        """
        lines: list[str] = []
        for table in sorted(self.committed):
            if table in suppressed:
                continue
            ops = self.committed[table]
            marks = " ".join(f"{mark}{ops[op]}" for op, mark in _MARKS if ops.get(op))
            if marks:
                lines.append(f"`{table}` {marks}")
        return "\n".join(lines)

    def has_bulk(self) -> bool:
        """Whether anything in the tally is a statement rather than a row."""
        return any(ops.get(BULK) for ops in self.committed.values())


def _labelled(title: str, label: bool) -> str:
    """``title``, prefixed with the environment outside production if ``label``."""
    return f"{discord.environment_prefix()}{title}" if label else title


def _default_caller(request: Any) -> str | None:
    """``request.state.caller``: the display name a service's auth stamps."""
    name = getattr(getattr(request, "state", None), "caller", None)
    return str(name) if name else None


@dataclass(frozen=True)
class ActivityConfig:
    """What differs between two services using the same feed.

    ``service`` is shown in every message's footer, so two services sharing
    a channel can be told apart; None leaves it out. ``environment``
    defaults to ``current_environment()``. ``caller`` returns the request's
    display name (default: ``request.state.caller``, read after the
    response, so auth that ran during the request has stamped it).
    ``is_machine`` says whether the caller is a machine; None means the
    service has no machine callers and no 4xx is ever reported. ``capture``
    reports an exception and returns its Sentry event id. ``send`` posts a
    built message; the default reads webhooks from the environment.
    ``label`` prefixes change and fault titles with
    ``discord.environment_prefix()`` outside production, so a development
    fault in a channel production shares is not read as production's; off
    by default because the footer already carries the environment, and a
    service whose readers rely on that need not change.
    """

    service: str | None = None
    environment: str | None = None
    suppressed_tables: Collection[str] = frozenset()
    excluded_paths: Collection[str] = ()
    report_changes: bool = True
    report_faults: bool = True
    caller: Callable[[Any], str | None] = _default_caller
    is_machine: Callable[[Any], bool] | None = None
    capture: Callable[[BaseException], str | None] = _sentry.capture_exception
    send: Sender | None = None
    label: bool = False

    def _title(self, title: str) -> str:
        return _labelled(title, self.label)

    def _footer(self, *parts: str) -> str:
        env = self.environment or current_environment().value
        tail = [self.service] if self.service else []
        return " · ".join([*parts, *tail, env])


#: The request's recorder. Set by the middleware before the downstream app
#: runs, which is what makes it visible to the ORM listeners: Starlette
#: copies the context into the downstream task, and SQLAlchemy's asyncio
#: bridge copies it into the greenlet the listeners run in. Nothing writes
#: this variable below the middleware — the listeners mutate the Recorder
#: object, which is shared, rather than rebinding the variable, which is not.
_recorder: ContextVar[Recorder | None] = ContextVar("activity_recorder", default=None)

#: Live delivery tasks. Held so the event loop does not garbage-collect a
#: send that is still in flight.
_in_flight: set[asyncio.Task[Any]] = set()


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def _table_of(obj: Any) -> str | None:
    """The table an ORM instance belongs to, or None if it is not mapped."""
    table = getattr(type(obj), "__tablename__", None)
    return str(table) if table else None


@event.listens_for(Session, "after_flush")
def _capture_flush(session: Session, _flush_context: Any) -> None:
    """Tally the unit of work while the session still describes it.

    ``after_flush`` is the one moment where the statements have been
    emitted and ``session.new`` / ``dirty`` / ``deleted`` still hold what
    produced them. By the time the flush completes those collections are
    empty, and ``before_flush`` would tally work that a failing statement
    could still take back.
    """
    recorder = _recorder.get()
    if recorder is None:
        return
    for obj in session.new:
        recorder.record(_table_of(obj), CREATED)
    for obj in session.deleted:
        recorder.record(_table_of(obj), DELETED)
    for obj in session.dirty:
        if session.is_modified(obj, include_collections=False):
            recorder.record(_table_of(obj), UPDATED)


@event.listens_for(Session, "do_orm_execute")
def _capture_bulk(state: Any) -> None:
    """Tally ORM-enabled Core DML, which never passes through a flush.

    ``update(Model).where(...)`` and its siblings write rows without ever
    loading them, so the unit of work never sees them. This event does —
    but it fires before the statement runs, so the row count does not
    exist yet. One statement is counted as one statement, and the message
    says so rather than implying a row count it does not have.
    """
    if not (state.is_insert or state.is_update or state.is_delete):
        return
    recorder = _recorder.get()
    if recorder is None:
        return
    table = getattr(getattr(state.statement, "table", None), "name", None)
    recorder.record(str(table) if table else None, BULK)


@event.listens_for(Session, "after_commit")
def _capture_commit(session: Session) -> None:
    """Promote flushed work, and deliver announcements, once durable.

    ``after_commit`` also fires when a savepoint is released. The tally
    promotes on either, as it always has; announcements wait for the
    outermost commit, since a released savepoint is not yet durable.
    """
    recorder = _recorder.get()
    if recorder is not None:
        recorder.promote()
    if session.in_nested_transaction():
        return
    pending = session.info.pop(_ANNOUNCEMENTS_KEY, None)
    for channel, payload, context, sender in pending or ():
        dispatch(_send(sender, channel, payload, context))


@event.listens_for(Session, "after_soft_rollback")
def _capture_rollback(_session: Session, _previous_transaction: Any) -> None:
    """Drop flushed work the transaction took back."""
    recorder = _recorder.get()
    if recorder is not None:
        recorder.discard()


@event.listens_for(Session, "after_transaction_end")
def _forget_announcements(session: Session, transaction: Any) -> None:
    """Drop announcements when the outermost transaction ends uncommitted.

    A commit has already taken them (``after_commit`` fires first). Any
    other end — rollback, ``close()``, an exception out of ``async with``
    — means the thing they announce did not happen, and they must not ride
    along on the session's next, unrelated commit.
    """
    if getattr(transaction, "parent", None) is None:
        session.info.pop(_ANNOUNCEMENTS_KEY, None)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def dispatch(coro: Any) -> None:
    """Run ``coro`` without making the caller wait, and without losing the task.

    Needs a running event loop. Without one — a sync script — the coroutine
    is closed and the drop is logged: there is nothing to send from.
    """
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:  # no running loop
        coro.close()
        logger.warning(
            with_log_prefix(LOG_WARNING, "activity notification dropped: no event loop")
        )
        return
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)


async def wait_for_deliveries() -> None:
    """Wait until every dispatched delivery has finished.

    For tests, and for a shutdown that wants its last messages out. Loops
    because a delivery can dispatch another.
    """
    while _in_flight:
        await asyncio.gather(*list(_in_flight), return_exceptions=True)
    await asyncio.sleep(0)


async def _send(
    send: Sender | None, channel: str, payload: dict[str, Any], context: str
) -> bool:
    """Post through ``send``, never raising: a feed message is not the work."""
    sender = send or _default_send
    try:
        return bool(await sender(channel, payload, context))
    except Exception:
        logger.exception("activity: delivery failed (%s)", context)
        return False


async def emit_change(
    config: ActivityConfig,
    *,
    method: str,
    path: str,
    actor: str,
    summary: str,
    legend: bool,
) -> bool:
    """Post one message summarising what a request committed."""
    footer = config._footer(f"{method} {path}", actor)
    if legend:
        footer = f"{footer}\n{_LEGEND}"
    return await _send(
        config.send,
        discord.CHANNEL_ACTIVITY,
        {
            "embeds": [
                {
                    "title": config._title("data changed"),
                    "color": _CHANGE_COLOR,
                    "description": summary,
                    "footer": {"text": footer},
                }
            ]
        },
        "activity/change",
    )


def _fault_payload(title: str, where: str, detail: str | None, footer: str) -> dict:
    description = f"`{where}`"
    if detail:
        description = f"{description}\n{_FENCE}{detail[:_DETAIL_LIMIT]}{_FENCE}"
    return {
        "embeds": [
            {
                "title": title,
                "color": _FAULT_COLOR,
                "description": description,
                "footer": {"text": footer},
            }
        ]
    }


async def emit_fault(
    config: ActivityConfig,
    *,
    method: str,
    path: str,
    actor: str,
    status_code: int,
    detail: str | None,
) -> bool:
    """Post one message about a request that failed on this side of the line."""
    return await _send(
        config.send,
        discord.CHANNEL_ERRORS,
        _fault_payload(
            config._title(f"fault · {status_code}"),
            f"{method} {path}",
            detail,
            config._footer(actor),
        ),
        "activity/fault",
    )


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def is_notifiable_fault(
    status_code: int, *, machine: bool = False, report_faults: bool = True
) -> bool:
    """Whether this response is a fault worth reporting.

    A 5xx is always the service's problem. A 4xx is the guard working —
    unless the caller is a machine, in which case two things this
    ecosystem owns disagree about the contract between them, and that is
    exactly the failure that otherwise sits undetected for a day.
    """
    if not report_faults:
        return False
    if status_code >= 500:
        return True
    if 400 <= status_code < 500:
        return machine
    return False


def is_excluded(path: str, excluded_paths: Collection[str]) -> bool:
    """Whether ``path`` is one of ``excluded_paths`` or below one."""
    return any(
        path == prefix or path.startswith(f"{prefix}/") for prefix in excluded_paths
    )


def fault_detail(
    exc: BaseException,
    *,
    event_id: str | None = None,
    capture: Callable[[BaseException], str | None] | None = _sentry.capture_exception,
) -> str:
    """The exception's identity, and deliberately not its message.

    A DBAPI error's ``str()`` carries the failing statement and its bound
    parameters — ``hide_parameters`` defaults to False — so formatting the
    exception into a message puts row data into a chat channel.

    The type name is what a person reads to decide whether to go and look.
    The Sentry id is how they find the rest, behind auth. Pass ``event_id``
    when the exception was already reported, so it is not reported twice;
    otherwise ``capture`` reports it.
    """
    if event_id is None and capture is not None:
        event_id = capture(exc)
    name = type(exc).__name__
    return f"{name} · sentry {event_id}" if event_id else name


def record_fault_detail(target: Any, detail: str) -> None:
    """Attach operator-facing context to this request's fault notification.

    ``target`` is the request, or the raw ASGI scope for a pure ASGI
    middleware (an error-rendering middleware that turns an exception into
    a 500 itself, so this middleware sees a response rather than a raise:
    it records ``fault_detail(exc, event_id=...)`` here and the alert still
    names the exception).

    The response body belongs to the caller; this belongs to whoever is on
    call. What is recorded is a string chosen deliberately — never an
    exception's ``str()``, and never anything derived from row data.
    """
    state = getattr(target, "state", None)
    if state is not None:
        state.fault_detail = str(detail)
    elif isinstance(target, dict):
        target.setdefault("state", {})["fault_detail"] = str(detail)


def _recorded_fault_detail(request: Any) -> str | None:
    detail = getattr(getattr(request, "state", None), "fault_detail", None)
    return str(detail) if detail else None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


def _report_changes(
    config: ActivityConfig, recorder: Recorder, method: str, path: str, actor: str
) -> None:
    """Post the change summary, if the request committed anything worth saying."""
    if not config.report_changes:
        return
    summary = recorder.summary(set(config.suppressed_tables))
    if not summary:
        return
    dispatch(
        emit_change(
            config,
            method=method,
            path=path,
            actor=actor,
            summary=summary,
            legend=recorder.has_bulk(),
        )
    )


def _actor(config: ActivityConfig, request: Any) -> str:
    try:
        return config.caller(request) or "anonymous"
    except Exception:
        return "anonymous"


def _machine(config: ActivityConfig, request: Any) -> bool:
    if config.is_machine is None:
        return False
    try:
        return bool(config.is_machine(request))
    except Exception:
        return False


def activity_middleware(
    config: ActivityConfig | Callable[[], ActivityConfig],
) -> Callable[[Any, Callable[[Any], Awaitable[Any]]], Awaitable[Any]]:
    """Build the function middleware: ``app.middleware("http")(...)``.

    ``config`` may be a callable, read once per request, for a service
    whose settings can change under it (tests that patch them, chiefly).

    Register it after any middleware whose effect on the status it should
    see, so it wraps them, and inside Starlette's server-error middleware,
    so an unhandled exception reaches it as a raise: it is caught,
    reported and re-raised unchanged. Committed changes are reported on
    both paths — a request that wrote and then failed rendering its
    response still changed the data.
    """

    async def middleware(request: Any, call_next: Any) -> Any:
        cfg = config() if callable(config) else config
        path = request.url.path
        if is_excluded(path, cfg.excluded_paths):
            return await call_next(request)

        method = request.method
        recorder = Recorder()
        token = _recorder.set(recorder)
        try:
            try:
                response = await call_next(request)
            except Exception as exc:
                actor = _actor(cfg, request)
                _report_changes(cfg, recorder, method, path, actor)
                if cfg.report_faults:
                    dispatch(
                        emit_fault(
                            cfg,
                            method=method,
                            path=path,
                            actor=actor,
                            status_code=500,
                            detail=fault_detail(exc, capture=cfg.capture),
                        )
                    )
                raise

            actor = _actor(cfg, request)
            _report_changes(cfg, recorder, method, path, actor)
            if is_notifiable_fault(
                response.status_code,
                machine=_machine(cfg, request),
                report_faults=cfg.report_faults,
            ):
                dispatch(
                    emit_fault(
                        cfg,
                        method=method,
                        path=path,
                        actor=actor,
                        status_code=response.status_code,
                        detail=_recorded_fault_detail(request),
                    )
                )
            return response
        finally:
            _recorder.reset(token)

    return middleware


# ---------------------------------------------------------------------------
# Announcements and background faults
# ---------------------------------------------------------------------------


def announce_on_commit(
    session: Any,
    channel: str,
    *,
    content: str | None = None,
    embeds: Sequence[Mapping[str, Any]] | None = None,
    username: str | None = None,
    context: str = "announce",
    label: bool = True,
    send: Sender | None = None,
) -> None:
    """Post this message when ``session``'s transaction commits, and only then.

    ``session`` is a ``Session`` or an ``AsyncSession``. Call it before the
    commit, in a request or a background job alike: the announcement rides
    on the session's outermost transaction, goes out (fire-and-forget, on
    the running loop) when it commits, and is dropped when it rolls back or
    the session closes without committing. ``label`` prefixes the
    environment outside production, and mentions are off, as
    ``discord.send_message`` has them.

    The announcement belongs to the transaction it was registered in. With
    none open yet, this begins one — no IO, the connection is acquired
    lazily — exactly as the session's next query would have: otherwise an
    announcement registered before any query has no transaction to end with,
    survives a ``close()``, and rides on whatever the session commits next.
    So, as after any query, a later explicit ``session.begin()`` on the same
    session raises; register inside it instead.

    The message is built now, so write it from values that will not change
    before the commit — a generated id is known after ``flush()``.
    """
    payload = discord.build_message(
        content=content, embeds=embeds, username=username, label=label
    )
    sync_session = getattr(session, "sync_session", session)
    if sync_session.get_transaction() is None:
        sync_session.begin()
    sync_session.info.setdefault(_ANNOUNCEMENTS_KEY, []).append(
        (channel, payload, context, send or _default_send)
    )


async def report_fault(
    where: str,
    exc: BaseException | None = None,
    *,
    detail: str | None = None,
    event_id: str | None = None,
    service: str | None = None,
    environment: str | None = None,
    capture: Callable[[BaseException], str | None] | None = _sentry.capture_exception,
    send: Sender | None = None,
    label: bool = False,
) -> bool:
    """Post a fault from code with no request — a scheduler tick, a job runner.

    Same shape as the middleware's: ``fault · background``, where it
    happened, and the exception's type and Sentry id — never its message.
    Pass ``event_id`` when the exception was already reported to Sentry
    (with its own tags), so it is not reported twice. ``detail`` replaces
    the exception's identity with a string the caller chose. ``label`` is
    ``ActivityConfig.label``'s: the environment prefix on the title. Never raises;
    returns whether Discord accepted it. To avoid waiting on it, wrap it in
    ``dispatch``.
    """
    try:
        text = detail
        if text is None and exc is not None:
            text = fault_detail(exc, event_id=event_id, capture=capture)
        footer = ActivityConfig(service=service, environment=environment)._footer()
        payload = _fault_payload(
            _labelled("fault · background", label), where, text, footer
        )
    except Exception:
        logger.exception("activity: could not build fault report (%s)", where)
        return False
    return await _send(send, discord.CHANNEL_ERRORS, payload, "activity/background")
