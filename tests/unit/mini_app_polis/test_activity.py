"""The running list: what it tallies, what it suppresses, what it reports.

The tally arithmetic, fault policy and middleware cases are ported from
api-kaianolevine-com's tests of the module this was extracted from. That
service's integration suite still runs the wiring end to end against
Postgres — a real route, a real commit, the tally read back across
SQLAlchemy's greenlet. Here the session events are driven directly, with
stand-in sessions shaped like the real ones, because nothing else in this
library's suite runs a database.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from mini_app_polis import _sentry, activity, discord

ACTIVITY_URL = "https://discord.test/api/webhooks/3/activity"
ERRORS_URL = "https://discord.test/api/webhooks/2/errors"


# ── Stand-ins ───────────────────────────────────────────────────────────


class Track:
    __tablename__ = "tracks"


class AuditEvent:
    __tablename__ = "identity_audit_events"


class Unmapped:
    pass


class FakeSession:
    """What the listeners read off a ``Session``."""

    def __init__(self, *, new=(), dirty=(), deleted=(), modified=True) -> None:
        self.new = list(new)
        self.dirty = list(dirty)
        self.deleted = list(deleted)
        self.info: dict[str, Any] = {}
        self.nested = False
        self._modified = modified

    def is_modified(self, _obj: Any, include_collections: bool = True) -> bool:
        assert include_collections is False
        return self._modified

    def in_nested_transaction(self) -> bool:
        return self.nested

    def get_transaction(self) -> Any:
        return object()  # always inside one: the real thing is tested below

    # The sequence SQLAlchemy runs, event by event.
    def flush(self) -> None:
        activity._capture_flush(self, None)  # type: ignore[arg-type]

    def commit(self) -> None:
        activity._capture_commit(self)  # type: ignore[arg-type]
        if not self.nested:
            activity._forget_announcements(self, SimpleNamespace(parent=None))  # type: ignore[arg-type]

    def rollback(self) -> None:
        activity._capture_rollback(self, None)  # type: ignore[arg-type]
        activity._forget_announcements(self, SimpleNamespace(parent=None))  # type: ignore[arg-type]


def bulk(kind: str, table: str | None = "tracks") -> SimpleNamespace:
    """A ``do_orm_execute`` state for one statement."""
    statement = SimpleNamespace(table=SimpleNamespace(name=table) if table else None)
    return SimpleNamespace(
        is_insert=kind == "insert",
        is_update=kind == "update",
        is_delete=kind == "delete",
        statement=statement,
    )


class Sent:
    """A ``Sender`` that records instead of posting."""

    def __init__(self, ok: bool = True) -> None:
        self.messages: list[tuple[str, dict[str, Any], str]] = []
        self.ok = ok

    async def __call__(self, channel: str, payload: dict[str, Any], context: str):
        self.messages.append((channel, payload, context))
        return self.ok

    def embeds(self, channel: str | None = None) -> list[dict[str, Any]]:
        return [p["embeds"][0] for c, p, _ in self.messages if channel in (None, c)]

    def text(self) -> str:
        return json.dumps([p for _, p, _ in self.messages], ensure_ascii=False)


def make_request(path: str = "/v1/things", method: str = "POST", **state: Any):
    return SimpleNamespace(
        method=method, url=SimpleNamespace(path=path), state=SimpleNamespace(**state)
    )


def run_request(
    config: Any,
    handler: Any,
    request: Any = None,
) -> Any:
    """Run one request through the middleware, then let deliveries land."""
    request = request or make_request()
    middleware = activity.activity_middleware(config)

    async def scenario() -> Any:
        try:
            return await middleware(request, handler)
        finally:
            await activity.wait_for_deliveries()

    return asyncio.run(scenario())


def responds(status: int, *, before: Any = None):
    async def call_next(request: Any) -> Any:
        if before is not None:
            before(request)
        return SimpleNamespace(status_code=status)

    return call_next


@pytest.fixture
def sent() -> Sent:
    return Sent()


@pytest.fixture
def config(sent: Sent) -> activity.ActivityConfig:
    return activity.ActivityConfig(
        environment="production",
        suppressed_tables={"identity_audit_events"},
        excluded_paths=("/health", "/v1/notify"),
        is_machine=lambda r: getattr(r.state, "caller_kind", None) == "machine",
        capture=lambda _exc: "evt42",
        send=sent,
    )


# ── Tally arithmetic ────────────────────────────────────────────────────


def test_summary_renders_marks_per_table():
    rec = activity.Recorder()
    rec.record("tracks", activity.CREATED)
    rec.record("tracks", activity.CREATED)
    rec.record("tracks", activity.UPDATED)
    rec.record("sets", activity.DELETED)
    rec.record(None, activity.CREATED)
    rec.promote()

    assert rec.summary(set()) == "`sets` -1\n`tracks` +2 ~1"


def test_uncommitted_work_is_not_reported():
    rec = activity.Recorder()
    rec.record("tracks", activity.CREATED)
    assert rec.summary(set()) == ""


def test_rollback_discards_flushed_work():
    rec = activity.Recorder()
    rec.record("tracks", activity.CREATED)
    rec.discard()
    rec.promote()
    assert rec.summary(set()) == ""


def test_suppressed_table_alone_produces_no_message():
    rec = activity.Recorder()
    rec.record("identity_audit_events", activity.CREATED)
    rec.promote()
    assert rec.summary({"identity_audit_events"}) == ""


def test_bulk_statements_are_counted_as_statements():
    rec = activity.Recorder()
    rec.record("wcs_source_extractions", activity.BULK)
    rec.promote()
    assert rec.summary(set()) == "`wcs_source_extractions` *1"
    assert rec.has_bulk() is True


# ── Capture ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "listener"),
    [
        ("after_flush", activity._capture_flush),
        ("do_orm_execute", activity._capture_bulk),
        ("after_commit", activity._capture_commit),
        ("after_soft_rollback", activity._capture_rollback),
        ("after_transaction_end", activity._forget_announcements),
    ],
)
def test_listeners_are_registered_on_every_session(name, listener):
    """Importing the module is what wires it — no per-session setup."""
    assert event.contains(Session, name, listener)


def test_listeners_do_nothing_outside_a_request():
    session = FakeSession(new=[Track()])
    session.flush()
    activity._capture_bulk(bulk("update"))
    session.commit()
    session.rollback()  # nothing to assert beyond "did not raise"


def test_flush_then_commit_is_tallied():
    rec = activity.Recorder()
    token = activity._recorder.set(rec)
    try:
        FakeSession(
            new=[Track(), Track(), Unmapped()],
            dirty=[Track()],
            deleted=[Track()],
        ).flush()
        FakeSession(dirty=[Track()], modified=False).flush()  # no net change
        activity._capture_bulk(bulk("update"))
        activity._capture_bulk(bulk("delete", table=None))
        activity._capture_bulk(bulk("select"))
        FakeSession().commit()
    finally:
        activity._recorder.reset(token)

    assert rec.summary(set()) == "`tracks` +2 ~1 -1 *1"


def test_rollback_reports_nothing():
    rec = activity.Recorder()
    token = activity._recorder.set(rec)
    try:
        session = FakeSession(new=[Track()])
        session.flush()
        session.rollback()
        session.commit()
    finally:
        activity._recorder.reset(token)

    assert rec.summary(set()) == ""


# ── Fault policy ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "machine", "expected"),
    [
        (500, False, True),
        (502, False, True),
        (403, False, False),
        (404, False, False),
        (422, False, False),
        (403, True, True),
        (422, True, True),
        (200, True, False),
        (201, False, False),
    ],
)
def test_fault_policy(status, machine, expected):
    assert activity.is_notifiable_fault(status, machine=machine) is expected


def test_faults_can_be_turned_off():
    assert activity.is_notifiable_fault(500, machine=True, report_faults=False) is False


def test_excluded_paths_cover_children():
    excluded = ("/health", "/v1/webhooks/github")
    assert activity.is_excluded("/health", excluded) is True
    assert activity.is_excluded("/v1/webhooks/github/x", excluded) is True
    assert activity.is_excluded("/v1/flags", excluded) is False
    assert activity.is_excluded("/healthz", excluded) is False


def test_fault_detail_carries_no_row_data():
    """A DBAPI error's message must never reach the channel."""
    exc = IntegrityError(
        "INSERT INTO wcs_notes (title) VALUES (?)",
        ("Kristen Wallace — private lesson notes",),
        Exception("UNIQUE constraint failed"),
    )
    detail = activity.fault_detail(exc, capture=lambda _e: "abc")

    assert detail == "IntegrityError · sentry abc"
    assert "Kristen Wallace" not in detail
    assert "INSERT INTO" not in detail


def test_fault_detail_with_a_known_event_id_does_not_capture_again():
    captured: list[BaseException] = []

    def capture(exc: BaseException) -> str:
        captured.append(exc)
        return "new"

    detail = activity.fault_detail(ValueError("x"), event_id="old", capture=capture)
    assert detail == "ValueError · sentry old"
    assert captured == []
    assert activity.fault_detail(ValueError("x"), capture=None) == "ValueError"


def test_fault_detail_uses_sentry_when_installed(monkeypatch):
    monkeypatch.setattr(
        _sentry,
        "sentry_sdk",
        SimpleNamespace(capture_exception=lambda _exc: "sdk-id"),
    )
    assert activity.fault_detail(KeyError("k")) == "KeyError · sentry sdk-id"


def test_record_fault_detail_on_a_request_and_on_a_scope():
    request = make_request()
    activity.record_fault_detail(request, "brevo: unauthorized")
    assert request.state.fault_detail == "brevo: unauthorized"

    scope: dict[str, Any] = {"type": "http"}
    activity.record_fault_detail(scope, "RuntimeError · sentry 1")
    assert scope["state"]["fault_detail"] == "RuntimeError · sentry 1"

    activity.record_fault_detail(object(), "ignored")  # neither: a no-op


# ── Middleware ──────────────────────────────────────────────────────────


def _writes(*objs: Any, commit: bool = True):
    """A handler body that flushes ``objs`` and commits or rolls back."""

    def before(_request: Any) -> None:
        session = FakeSession(new=objs)
        session.flush()
        if commit:
            session.commit()
        else:
            session.rollback()

    return before


def test_committed_change_is_reported_and_the_audit_row_is_silent(config, sent):
    response = run_request(
        config,
        responds(201, before=_writes(Track(), AuditEvent())),
        make_request(caller="deejay-cog"),
    )

    assert response.status_code == 201
    assert [c for c, _, _ in sent.messages] == [discord.CHANNEL_ACTIVITY]
    (embed,) = sent.embeds()
    assert embed["title"] == "data changed"
    assert embed["description"] == "`tracks` +1"
    assert embed["footer"]["text"] == "POST /v1/things · deejay-cog · production"
    assert "identity_audit_events" not in sent.text()


def test_rolled_back_request_reports_nothing(config, sent):
    run_request(config, responds(200, before=_writes(Track(), commit=False)))
    assert sent.messages == []


def test_read_only_request_says_nothing(config, sent):
    run_request(config, responds(200), make_request(method="GET"))
    assert sent.messages == []


def test_suppressed_tables_alone_say_nothing(config, sent):
    run_request(config, responds(200, before=_writes(AuditEvent())))
    assert sent.messages == []


def test_bulk_statement_adds_the_legend(config, sent):
    def before(_request: Any) -> None:
        activity._capture_bulk(bulk("update"))
        FakeSession().commit()

    run_request(config, responds(200, before=before))
    (embed,) = sent.embeds()
    assert embed["description"] == "`tracks` *1"
    assert "bulk statement (rows not counted)" in embed["footer"]["text"]


def test_excluded_path_is_not_recorded_at_all(config, sent):
    seen: list[Any] = []

    def before(_request: Any) -> None:
        seen.append(activity._recorder.get())
        _writes(Track())(_request)

    run_request(config, responds(500, before=before), make_request("/v1/notify"))
    assert seen == [None]
    assert sent.messages == []


def test_changes_can_be_turned_off(config, sent):
    from dataclasses import replace

    run_request(
        replace(config, report_changes=False), responds(200, before=_writes(Track()))
    )
    assert sent.messages == []


def test_denied_human_is_not_reported(config, sent):
    request = make_request(caller="someone", caller_kind="human")
    run_request(config, responds(403), request)
    assert sent.messages == []


def test_machine_4xx_is_reported(config, sent):
    request = make_request(caller="an-unregistered-cog", caller_kind="machine")
    run_request(config, responds(403), request)

    assert [c for c, _, _ in sent.messages] == [discord.CHANNEL_ERRORS]
    (embed,) = sent.embeds()
    assert embed["title"] == "fault · 403"
    assert embed["footer"]["text"] == "an-unregistered-cog · production"


def test_no_machine_callers_means_no_4xx_reports(config, sent):
    from dataclasses import replace

    request = make_request(caller_kind="machine")
    run_request(replace(config, is_machine=None), responds(422), request)
    assert sent.messages == []


def test_a_broken_is_machine_counts_as_human(config, sent):
    from dataclasses import replace

    def broken(_request: Any) -> bool:
        raise RuntimeError("no state")

    run_request(replace(config, is_machine=broken), responds(403))
    assert sent.messages == []


def test_returned_fault_carries_the_handlers_reason(config, sent):
    """A 5xx the handler *returned* reaches the channel with its reason."""

    def before(request: Any) -> None:
        activity.record_fault_detail(request, "brevo: unauthorized")

    response = run_request(config, responds(502, before=before))
    assert response.status_code == 502
    (embed,) = sent.embeds(discord.CHANNEL_ERRORS)
    assert embed["title"] == "fault · 502"
    assert "brevo: unauthorized" in embed["description"]
    assert embed["footer"]["text"] == "anonymous · production"


def test_returned_5xx_without_a_reason_names_the_route(config, sent):
    run_request(config, responds(500))
    (embed,) = sent.embeds()
    assert embed["description"] == "`POST /v1/things`"


def test_unhandled_exception_is_reported_and_re_raised(config, sent):
    """The middleware sees the raise, and the message never carries its text."""

    async def boom(_request: Any) -> Any:
        _writes(Track())(_request)
        raise RuntimeError("the thing broke")

    with pytest.raises(RuntimeError, match="the thing broke"):
        run_request(config, boom)

    channels = sorted(c for c, _, _ in sent.messages)
    assert channels == [discord.CHANNEL_ACTIVITY, discord.CHANNEL_ERRORS]
    (fault,) = sent.embeds(discord.CHANNEL_ERRORS)
    assert fault["title"] == "fault · 500"
    assert "RuntimeError · sentry evt42" in fault["description"]
    assert "the thing broke" not in sent.text()


def test_unhandled_exception_with_faults_off_is_still_re_raised(config, sent):
    from dataclasses import replace

    async def boom(_request: Any) -> Any:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        run_request(replace(config, report_faults=False), boom)
    assert sent.messages == []


def test_config_is_read_per_request_and_service_shows_in_footers(sent):
    built: list[int] = []

    def config() -> activity.ActivityConfig:
        built.append(1)
        return activity.ActivityConfig(
            service="api-deejaytools", environment="development", send=sent
        )

    run_request(config, responds(500, before=_writes(Track())))
    run_request(config, responds(200))

    assert len(built) == 2
    footers = sorted(e["footer"]["text"] for e in sent.embeds())
    assert footers == [
        "POST /v1/things · anonymous · api-deejaytools · development",
        "anonymous · api-deejaytools · development",
    ]


def test_environment_defaults_to_the_resolver(sent, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "dev")
    config = activity.ActivityConfig(send=sent)
    run_request(config, responds(500))
    assert sent.embeds()[0]["footer"]["text"] == "anonymous · development"


def test_titles_are_labelled_only_when_asked(sent, monkeypatch):
    """Unlabelled by default (api-kaianolevine-com's messages, unchanged);
    with ``label`` a development fault cannot pass for production's."""
    monkeypatch.setenv("ENVIRONMENT", "development")
    plain = activity.ActivityConfig(send=sent)
    labelled = activity.ActivityConfig(send=sent, label=True)
    run_request(plain, responds(500, before=_writes(Track())))
    run_request(labelled, responds(500, before=_writes(Track())))

    titles = [e["title"] for e in sent.embeds()]
    assert sorted(titles) == [
        "[DEVELOPMENT] data changed",
        "[DEVELOPMENT] fault · 500",
        "data changed",
        "fault · 500",
    ]


def test_labelled_titles_are_unmarked_in_production(sent, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    run_request(
        activity.ActivityConfig(send=sent, label=True),
        responds(500, before=_writes(Track())),
    )
    assert sorted(e["title"] for e in sent.embeds()) == ["data changed", "fault · 500"]


def test_a_broken_caller_is_anonymous(config, sent):
    from dataclasses import replace

    def broken(_request: Any) -> str:
        raise RuntimeError("no state")

    run_request(replace(config, caller=broken), responds(500))
    assert sent.embeds()[0]["footer"]["text"] == "anonymous · production"


def test_a_failing_sender_never_reaches_the_request(config):
    from dataclasses import replace

    async def explode(*_: Any) -> bool:
        raise RuntimeError("discord is down")

    response = run_request(replace(config, send=explode), responds(500))
    assert response.status_code == 500


def test_default_sender_routes_by_channel(monkeypatch):
    """Changes reach the activity webhook, faults the errors webhook."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    monkeypatch.setattr(
        discord,
        "_client",
        lambda _timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/fallback")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_ACTIVITY", ACTIVITY_URL)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_ERRORS", ERRORS_URL)
    discord.reset_cooldowns()

    run_request(activity.ActivityConfig(), responds(500, before=_writes(Track())))

    assert sorted(str(r.url) for r in requests) == [ERRORS_URL, ACTIVITY_URL]


# ── Delivery ────────────────────────────────────────────────────────────


def test_dispatch_without_a_loop_drops_and_closes(sent):
    coro = sent("errors", {}, "ctx")
    activity.dispatch(coro)
    assert coro.cr_frame is None  # closed, so no "never awaited" warning
    assert sent.messages == []


def test_dispatched_tasks_are_held_until_done(sent):
    async def scenario() -> None:
        activity.dispatch(sent("errors", {}, "ctx"))
        assert len(activity._in_flight) == 1
        await activity.wait_for_deliveries()
        assert activity._in_flight == set()

    asyncio.run(scenario())
    assert len(sent.messages) == 1


# ── Announcements ───────────────────────────────────────────────────────


def test_announcement_goes_out_on_commit_outside_a_request(sent):
    """A background job: its own session, no middleware, no recorder."""

    async def job() -> None:
        session = FakeSession()
        activity.announce_on_commit(
            session, discord.CHANNEL_ACTIVITY, content="Song added", send=sent
        )
        assert sent.messages == []
        session.commit()
        await activity.wait_for_deliveries()

    asyncio.run(job())
    assert sent.messages == [
        (
            discord.CHANNEL_ACTIVITY,
            {"content": "Song added", "allowed_mentions": {"parse": []}},
            "announce",
        )
    ]


def test_announcement_inside_a_request_rides_the_same_commit(config, sent):
    def before(_request: Any) -> None:
        session = FakeSession(new=[Track()])
        session.flush()
        activity.announce_on_commit(
            session,
            discord.CHANNEL_ACTIVITY,
            embeds=[{"title": "Song added"}],
            context="songs/added",
            send=sent,
        )
        session.commit()

    run_request(config, responds(201, before=before))

    contexts = sorted(ctx for _, _, ctx in sent.messages)
    assert contexts == ["activity/change", "songs/added"]


def test_rolled_back_transaction_announces_nothing(sent):
    async def job() -> None:
        session = FakeSession()
        activity.announce_on_commit(session, "activity", content="no", send=sent)
        session.rollback()
        session.commit()  # the next, unrelated transaction
        await activity.wait_for_deliveries()

    asyncio.run(job())
    assert sent.messages == []


def test_released_savepoint_waits_for_the_outer_commit(sent):
    async def job() -> None:
        session = FakeSession()
        activity.announce_on_commit(session, "activity", content="yes", send=sent)
        session.nested = True
        session.commit()  # RELEASE SAVEPOINT
        await activity.wait_for_deliveries()
        assert sent.messages == []
        session.nested = False
        session.commit()
        await activity.wait_for_deliveries()

    asyncio.run(job())
    assert len(sent.messages) == 1


def test_nested_transaction_end_keeps_announcements(sent):
    session = FakeSession()
    activity.announce_on_commit(session, "activity", content="yes", send=sent)
    activity._forget_announcements(session, SimpleNamespace(parent=object()))  # type: ignore[arg-type]
    assert session.info[activity._ANNOUNCEMENTS_KEY]


def test_async_session_announcements_live_on_its_sync_session(sent, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    sync = FakeSession()
    async_session = SimpleNamespace(sync_session=sync)

    async def job() -> None:
        activity.announce_on_commit(async_session, "runs", content="built", send=sent)
        sync.commit()
        await activity.wait_for_deliveries()

    asyncio.run(job())
    assert sent.messages[0][1]["content"] == "[DEVELOPMENT] built"


def test_announcement_default_sender_posts_through_discord(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    monkeypatch.setattr(
        discord,
        "_client",
        lambda _timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_ACTIVITY", ACTIVITY_URL)
    discord.reset_cooldowns()

    async def job() -> None:
        session = FakeSession()
        activity.announce_on_commit(session, "activity", content="Song added")
        session.commit()
        await activity.wait_for_deliveries()

    asyncio.run(job())
    assert [json.loads(r.content) for r in requests] == [
        {"content": "Song added", "allowed_mentions": {"parse": []}}
    ]


# ── Announcements on a real Session ─────────────────────────────────────
#
# The stand-in above calls the listeners in the order SQLAlchemy is believed
# to; these let SQLAlchemy decide. In-memory SQLite, no tables: the
# transactions are the thing under test. (AsyncSession shares the listeners
# through its sync_session; api-deejaytools' suite runs one on Postgres.)


@pytest.fixture
def engine():
    from sqlalchemy import create_engine

    eng = create_engine("sqlite://")
    yield eng
    eng.dispose()


def _announce_job(scenario: Callable[[], None]) -> None:
    """Run ``scenario`` on a running loop (deliveries need one), then drain."""

    async def job() -> None:
        scenario()
        await activity.wait_for_deliveries()

    asyncio.run(job())


def test_real_savepoint_release_waits_for_the_outer_commit(engine, sent):
    from sqlalchemy import text

    seen: list[int] = []

    def scenario() -> None:
        with Session(engine) as session:
            session.execute(text("SELECT 1"))
            activity.announce_on_commit(session, "activity", content="yes", send=sent)
            with session.begin_nested():  # RELEASE SAVEPOINT on exit
                session.execute(text("SELECT 1"))
            seen.append(len(activity._in_flight))
            session.commit()

    _announce_job(scenario)
    assert seen == [0], "nothing dispatched on the savepoint release"
    assert len(sent.messages) == 1


def test_real_rollback_announces_nothing(engine, sent):
    from sqlalchemy import text

    def scenario() -> None:
        with Session(engine) as session:
            session.execute(text("SELECT 1"))
            activity.announce_on_commit(session, "activity", content="no", send=sent)
            session.rollback()
            session.execute(text("SELECT 1"))
            session.commit()  # the next, unrelated transaction

    _announce_job(scenario)
    assert sent.messages == []


def test_real_announcement_before_any_query_does_not_outlive_a_close(engine, sent):
    """Registered with no transaction open, then the session closed: it must
    not ride on the session's next commit (the leak this binding fixes)."""
    from sqlalchemy import text

    def scenario() -> None:
        session = Session(engine)
        activity.announce_on_commit(session, "activity", content="no", send=sent)
        session.close()
        session.execute(text("SELECT 1"))
        session.commit()
        session.close()

    _announce_job(scenario)
    assert sent.messages == []


def test_real_announcement_before_any_query_goes_out_on_commit(engine, sent):
    from sqlalchemy import text

    def scenario() -> None:
        with Session(engine) as session:
            activity.announce_on_commit(session, "activity", content="yes", send=sent)
            session.execute(text("SELECT 1"))
            session.commit()

    _announce_job(scenario)
    assert [p["content"] for _, p, _ in sent.messages] == ["yes"]


# ── Background faults ───────────────────────────────────────────────────


def test_background_fault_has_the_middleware_shape(sent):
    exc = IntegrityError("INSERT INTO songs (title) VALUES (?)", ("secret",), None)
    ok = asyncio.run(
        activity.report_fault(
            "scheduler · song_builds",
            exc,
            service="api-deejaytools",
            environment="production",
            capture=lambda _e: "evt9",
            send=sent,
        )
    )

    assert ok is True
    ((channel, payload, context),) = sent.messages
    assert (channel, context) == (discord.CHANNEL_ERRORS, "activity/background")
    embed = payload["embeds"][0]
    assert embed["title"] == "fault · background"
    assert embed["description"].startswith("`scheduler · song_builds`\n")
    assert "IntegrityError · sentry evt9" in embed["description"]
    assert embed["footer"]["text"] == "api-deejaytools · production"
    assert "secret" not in sent.text()
    assert "INSERT" not in sent.text()


def test_background_fault_reuses_a_known_event_id(sent):
    captured: list[BaseException] = []

    async def scenario() -> bool:
        return await activity.report_fault(
            "drive_jobs",
            RuntimeError("x"),
            event_id="already",
            environment="production",
            capture=lambda e: captured.append(e) or "again",
            send=sent,
        )

    assert asyncio.run(scenario()) is True
    assert captured == []
    assert "RuntimeError · sentry already" in sent.text()
    assert sent.embeds()[0]["footer"]["text"] == "production"


def test_background_fault_with_a_chosen_detail_and_no_exception(sent):
    asyncio.run(
        activity.report_fault(
            "drive_jobs",
            detail="queue not draining",
            environment="production",
            send=sent,
        )
    )
    assert "queue not draining" in sent.embeds()[0]["description"]


def test_background_fault_is_labelled_only_when_asked(sent, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")

    async def scenario() -> None:
        await activity.report_fault("a", detail="d", send=sent)
        await activity.report_fault("b", detail="d", send=sent, label=True)

    asyncio.run(scenario())
    assert [e["title"] for e in sent.embeds()] == [
        "fault · background",
        "[DEVELOPMENT] fault · background",
    ]


def test_background_fault_never_raises(sent):
    assert asyncio.run(activity.report_fault("x", send=Sent(ok=False))) is False

    def broken_capture(_exc: BaseException) -> str:
        raise RuntimeError("sentry broke")

    assert (
        asyncio.run(
            activity.report_fault(
                "x", ValueError("v"), capture=broken_capture, send=sent
            )
        )
        is False
    )
    assert sent.messages == []
