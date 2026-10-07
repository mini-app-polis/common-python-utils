"""Discord transport: where each channel goes, what is labeled, and the cooldown.

The rate-limit cases are ported from api-kaianolevine-com, where they were
written after production on 2026-09-23: Cloudflare answered every webhook
with error 1015, an HTML page applied to the service's IP, and the API kept
posting — 41 refused sends in four minutes, each reported to Sentry.
Posting through a 1015 is what extends it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from mini_app_polis import _sentry, discord

DEFAULT_URL = "https://discord.test/api/webhooks/1/token"
ERRORS_URL = "https://discord.test/api/webhooks/2/errors"
ACTIVITY_URL = "https://discord.test/api/webhooks/3/activity"
RUNS_URL = "https://discord.test/api/webhooks/4/runs"

CLOUDFLARE_1015 = (
    "<!doctype html><html><head><title>Access denied | discord.com used "
    "Cloudflare to restrict access</title></head><body>error code: 1015"
    "</body></html>"
)


class FakeDiscord:
    """A mock transport: answers per URL and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answers: dict[str, list[Any]] = {}

    def answer(self, url: str, *responses: Any) -> None:
        self.answers[url] = list(responses)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        queue = self.answers.get(str(request.url)) or [httpx.Response(204)]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def calls(self, url: str) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) == url]

    def bodies(self, url: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.calls(url)]


@pytest.fixture(autouse=True)
def _no_cooldown_between_tests():
    """Cooldowns are process state; one test's 429 must not mute the next."""
    discord.reset_cooldowns()
    yield
    discord.reset_cooldowns()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeDiscord:
    transport = FakeDiscord()

    def _client(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(transport.handler), timeout=timeout
        )

    monkeypatch.setattr(discord, "_client", _client)
    return transport


@pytest.fixture
def channels(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three channels split out; ``default`` left on the fallback."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DEFAULT_URL)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_ERRORS", ERRORS_URL)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_ACTIVITY", ACTIVITY_URL)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_RUNS", RUNS_URL)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL_DEFAULT", raising=False)


@pytest.fixture
def sentry(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """A stand-in sentry_sdk, so what is reported can be asserted."""
    reported: dict[str, list[Any]] = {"messages": [], "exceptions": []}

    class FakeSentry:
        @staticmethod
        def capture_message(message: str, **_: Any) -> None:
            reported["messages"].append(message)

        @staticmethod
        def capture_exception(exc: BaseException) -> str:
            reported["exceptions"].append(exc)
            return "evt123"

    monkeypatch.setattr(_sentry, "sentry_sdk", FakeSentry)
    return reported


def run(coro_factory: Callable[[], Any]) -> Any:
    return asyncio.run(coro_factory())


async def _send(channel: str) -> bool:
    return await discord.send_payload(channel, {"content": "x"})


# ── Resolution ──────────────────────────────────────────────────────────


def test_set_channel_resolves_to_its_own_webhook(channels) -> None:
    assert discord.webhook_url(discord.CHANNEL_ERRORS) == ERRORS_URL
    assert discord.webhook_url(discord.CHANNEL_ACTIVITY) == ACTIVITY_URL
    assert discord.webhook_url(discord.CHANNEL_RUNS) == RUNS_URL


def test_unset_and_unknown_channels_fall_back(channels) -> None:
    """No variable means the original webhook, not a dropped message."""
    assert discord.webhook_url(discord.CHANNEL_DEFAULT) == DEFAULT_URL
    assert discord.webhook_url("erorrs") == DEFAULT_URL


def test_blank_channel_variable_falls_back(
    channels, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_ERRORS", "  ")
    assert discord.webhook_url(discord.CHANNEL_ERRORS) == DEFAULT_URL


def test_github_suffix_and_trailing_slash_are_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The paste-the-docs-URL affordance survives the per-channel split."""
    monkeypatch.setenv("DISCORD_WEBHOOK_URL_ERRORS", f"{ERRORS_URL}/github/")
    assert discord.webhook_url(discord.CHANNEL_ERRORS) == ERRORS_URL


def test_nothing_configured_resolves_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL_ERRORS", raising=False)
    assert discord.webhook_url(discord.CHANNEL_ERRORS) is None


def test_source_replaces_the_environment(channels) -> None:
    source = {"DISCORD_WEBHOOK_URL": "https://elsewhere.test/hook"}
    assert discord.webhook_url(discord.CHANNEL_ERRORS, source=source) == (
        "https://elsewhere.test/hook"
    )
    assert discord.webhook_url(discord.CHANNEL_ERRORS, source={}) is None


# ── Labeling ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("environment", "prefix"),
    [
        ("production", ""),
        ("development", "[DEVELOPMENT] "),
        ("local", "[LOCAL] "),
    ],
)
def test_environment_prefix(
    monkeypatch: pytest.MonkeyPatch, environment: str, prefix: str
) -> None:
    monkeypatch.setenv("ENVIRONMENT", environment)
    assert discord.environment_prefix() == prefix


def test_send_message_labels_content_and_titles_outside_production(
    channels, fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    embed = {"title": "Song added", "description": "d"}
    sent = run(
        lambda: discord.send_message(
            discord.CHANNEL_ACTIVITY,
            content="hello",
            embeds=[embed, {"description": "untitled"}],
            username="deejaytools",
        )
    )

    assert sent is True
    (body,) = fake.bodies(ACTIVITY_URL)
    assert body["content"] == "[DEVELOPMENT] hello"
    assert body["embeds"][0]["title"] == "[DEVELOPMENT] Song added"
    assert "title" not in body["embeds"][1]
    assert body["username"] == "deejaytools"
    assert embed["title"] == "Song added", "the caller's embed is not mutated"


def test_production_and_label_false_are_unmarked(
    channels, fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run(lambda: discord.send_message(content="prod")) is True
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert run(lambda: discord.send_message(content="raw", label=False)) is True
    assert [b["content"] for b in fake.bodies(DEFAULT_URL)] == ["prod", "raw"]


def test_send_payload_posts_exactly_what_it_is_given(
    channels, fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "development")
    payload = {"embeds": [{"title": "data changed"}]}
    assert run(lambda: discord.send_payload(discord.CHANNEL_RUNS, payload)) is True
    assert fake.bodies(RUNS_URL) == [payload]


def test_mentions_are_off_by_default(channels, fake) -> None:
    """A routine name typed as "@everyone" must stay text."""
    run(lambda: discord.send_message(content="@everyone added 'R'"))
    (body,) = fake.bodies(DEFAULT_URL)
    assert body["allowed_mentions"] == {"parse": []}


def test_allowed_mentions_can_be_given_or_left_out() -> None:
    users = {"parse": ["users"]}
    assert discord.build_message(content="x", allowed_mentions=users)[
        "allowed_mentions"
    ] == {"parse": ["users"]}
    assert "allowed_mentions" not in discord.build_message(
        content="x", allowed_mentions=None
    )
    built = discord.build_message(content="x")
    built["allowed_mentions"]["parse"].append("everyone")
    assert discord.NO_MENTIONS == {"parse": []}, "the default is not shared"


def test_a_message_that_cannot_be_built_is_dropped_not_raised(
    channels, fake, sentry
) -> None:
    bad: Any = [None]  # not a mapping: dict(None) raises
    assert run(lambda: discord.send_message(content="x", embeds=bad)) is False
    assert fake.requests == []
    assert len(sentry["exceptions"]) == 1


def test_empty_message_is_not_sent(channels, fake) -> None:
    assert run(lambda: discord.send_message(discord.CHANNEL_ERRORS)) is False
    assert fake.requests == []


# ── Never raising ───────────────────────────────────────────────────────


def test_no_webhook_drops_without_posting(fake, monkeypatch) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL_RUNS", raising=False)
    assert run(lambda: _send(discord.CHANNEL_RUNS)) is False
    assert fake.requests == []


def test_a_broken_source_is_not_the_callers_failure(fake) -> None:
    class Broken(dict):
        def get(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("settings exploded")

    sent = run(
        lambda: discord.send_payload("errors", {"content": "x"}, source=Broken())
    )
    assert sent is False
    assert fake.requests == []


def test_transport_error_is_reported_not_raised(channels, fake, sentry) -> None:
    fake.answer(RUNS_URL, httpx.ConnectError("refused"))
    assert run(lambda: _send(discord.CHANNEL_RUNS)) is False
    assert len(sentry["exceptions"]) == 1


def test_any_exception_from_the_client_is_swallowed(
    channels, monkeypatch: pytest.MonkeyPatch, sentry
) -> None:
    def _broken(timeout: float) -> httpx.AsyncClient:
        raise ValueError("bad client")

    monkeypatch.setattr(discord, "_client", _broken)
    assert run(lambda: _send(discord.CHANNEL_RUNS)) is False
    assert len(sentry["exceptions"]) == 1


def test_rejection_is_reported_with_its_status(channels, fake, sentry) -> None:
    fake.answer(RUNS_URL, httpx.Response(400, json={"message": "Invalid Form Body"}))
    assert run(lambda: _send(discord.CHANNEL_RUNS)) is False
    assert sentry["messages"] == [
        "Discord rejected notification (notify) channel=runs: 400"
    ]


def test_no_sentry_installed_is_silent(channels, fake, monkeypatch) -> None:
    monkeypatch.setattr(_sentry, "sentry_sdk", None)
    fake.answer(RUNS_URL, httpx.Response(500))
    assert run(lambda: _send(discord.CHANNEL_RUNS)) is False


# ── Rate limits ─────────────────────────────────────────────────────────


def test_bucket_429_holds_only_that_webhook(channels, fake) -> None:
    fake.answer(RUNS_URL, httpx.Response(429, json={"retry_after": 5, "global": False}))

    async def scenario() -> list[bool]:
        return [
            await _send(discord.CHANNEL_RUNS),
            await _send(discord.CHANNEL_RUNS),
            await _send(discord.CHANNEL_ACTIVITY),
        ]

    assert run(scenario) == [False, False, True]
    assert len(fake.calls(RUNS_URL)) == 1
    assert len(fake.calls(ACTIVITY_URL)) == 1


def test_global_429_holds_every_webhook(channels, fake) -> None:
    fake.answer(RUNS_URL, httpx.Response(429, json={"retry_after": 5, "global": True}))

    async def scenario() -> list[bool]:
        return [await _send(discord.CHANNEL_RUNS), await _send("activity")]

    assert run(scenario) == [False, False]
    assert len(fake.calls(RUNS_URL)) == 1
    assert fake.calls(ACTIVITY_URL) == []


def test_cloudflare_1015_holds_every_webhook_for_the_default(channels, fake) -> None:
    fake.answer(RUNS_URL, httpx.Response(429, text=CLOUDFLARE_1015))

    async def scenario() -> list[bool]:
        return [await _send(discord.CHANNEL_RUNS), await _send("activity")]

    assert run(scenario) == [False, False]
    assert fake.calls(ACTIVITY_URL) == []
    remaining = discord._cooldown_remaining(ACTIVITY_URL)
    assert 50 < remaining <= discord._DEFAULT_COOLDOWN_SECS


def test_retry_after_header_sets_the_cooldown(channels, fake) -> None:
    fake.answer(
        RUNS_URL,
        httpx.Response(429, text="slow down", headers={"Retry-After": "120"}),
    )
    run(lambda: _send(discord.CHANNEL_RUNS))
    assert 110 < discord._cooldown_remaining(RUNS_URL) <= 120


def test_absurd_retry_after_is_capped(channels, fake) -> None:
    fake.answer(
        RUNS_URL, httpx.Response(429, json={"retry_after": 10**9, "global": True})
    )
    run(lambda: _send(discord.CHANNEL_RUNS))
    assert discord._cooldown_remaining(RUNS_URL) <= discord._MAX_COOLDOWN_SECS


def test_sends_resume_after_the_cooldown(
    channels, fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.answer(
        RUNS_URL,
        httpx.Response(429, json={"retry_after": 5, "global": False}),
        httpx.Response(204),
    )
    now = discord.time.monotonic()
    monkeypatch.setattr(discord.time, "monotonic", lambda: now)
    assert run(lambda: _send(discord.CHANNEL_RUNS)) is False

    monkeypatch.setattr(discord.time, "monotonic", lambda: now + 6)
    assert run(lambda: _send(discord.CHANNEL_RUNS)) is True
    assert len(fake.calls(RUNS_URL)) == 2


def test_rate_limit_reported_to_sentry_once(channels, fake, sentry) -> None:
    fake.answer(RUNS_URL, httpx.Response(429, text=CLOUDFLARE_1015))

    async def scenario() -> None:
        for _ in range(5):
            await _send(discord.CHANNEL_RUNS)

    run(scenario)
    assert len(sentry["messages"]) == 1
    assert "rate limit" in sentry["messages"][0]


def test_reset_cooldowns_unmutes(channels, fake) -> None:
    fake.answer(
        RUNS_URL, httpx.Response(429, json={"retry_after": 5}), httpx.Response(204)
    )
    assert run(lambda: _send(discord.CHANNEL_RUNS)) is False
    discord.reset_cooldowns()
    assert run(lambda: _send(discord.CHANNEL_RUNS)) is True


def test_post_webhook_forwards_raw_content_and_headers(channels, fake) -> None:
    """The lower level api-kaianolevine-com forwards GitHub events through."""
    url = f"{DEFAULT_URL}{discord.GITHUB_SUFFIX}"
    sent = run(
        lambda: discord.post_webhook(
            url,
            content=b'{"raw": true}',
            headers={"X-GitHub-Event": "push"},
            channel="default",
            context="github/push",
        )
    )
    assert sent is True
    (request,) = fake.calls(url)
    assert request.content == b'{"raw": true}'
    assert request.headers["X-GitHub-Event"] == "push"


def test_real_client_factory_honours_the_timeout() -> None:
    client = discord._client(3.0)
    try:
        assert client.timeout.connect == 3.0
    finally:
        asyncio.run(client.aclose())


# ── The Sentry shim ─────────────────────────────────────────────────────


def test_sentry_shim_without_sentry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_sentry, "sentry_sdk", None)
    assert _sentry.capture_exception(RuntimeError("x")) is None
    _sentry.capture_message("nothing happens")


def test_sentry_shim_survives_a_broken_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        @staticmethod
        def capture_exception(_exc: BaseException) -> str:
            raise RuntimeError("sdk broke")

        @staticmethod
        def capture_message(_message: str, **_: Any) -> None:
            raise RuntimeError("sdk broke")

    monkeypatch.setattr(_sentry, "sentry_sdk", Broken)
    assert _sentry.capture_exception(ValueError("x")) is None
    _sentry.capture_message("still fine")


def test_sentry_shim_returns_no_id_for_an_uninitialised_sdk(monkeypatch) -> None:
    class Uninitialised:
        @staticmethod
        def capture_exception(_exc: BaseException) -> None:
            return None

    monkeypatch.setattr(_sentry, "sentry_sdk", Uninitialised)
    assert _sentry.capture_exception(ValueError("x")) is None


def test_sentry_shim_loads_nothing_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "sentry_sdk", None)  # import raises
    assert _sentry._load() is None


def test_sentry_shim_loads_the_installed_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    fake = types.ModuleType("sentry_sdk")
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake)
    assert _sentry._load() is fake


def test_importable_without_sqlalchemy() -> None:
    """A cog with no SQLAlchemy can import the package and the transport.

    Only ``mini_app_polis.activity`` needs it, and says so with an ImportError.
    A subprocess, because SQLAlchemy is already imported in this one.
    """
    import subprocess
    import sys

    code = (
        "import sys\n"
        "sys.modules['sqlalchemy'] = None\n"
        "import mini_app_polis\n"
        "from mini_app_polis import discord\n"
        "assert discord.CHANNEL_ERRORS == 'errors'\n"
        "try:\n"
        "    import mini_app_polis.activity\n"
        "except ImportError:\n"
        "    print('activity needs sqlalchemy')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "activity needs sqlalchemy"
