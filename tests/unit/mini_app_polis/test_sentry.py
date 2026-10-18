"""init_sentry: on with a DSN, an ERROR without one in production (CD-038)."""

from __future__ import annotations

import types
from typing import Any

import pytest

from mini_app_polis import _sentry
from mini_app_polis import sentry as sentry_module
from mini_app_polis.sentry import DEFAULT_DSN_VAR, init_sentry


class _FakeScope:
    def __init__(self) -> None:
        self.tags: dict[str, str] = {}

    def set_tag(self, key: str, value: str) -> None:
        self.tags[key] = value


class _FakeSdk:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.scope = _FakeScope()
        self._fail = fail

    def init(self, **options: Any) -> None:
        if self._fail:
            raise RuntimeError("bad dsn")
        self.calls.append(options)

    def get_global_scope(self) -> _FakeScope:
        return self.scope


class _RecordingLogger:
    """Records ERRORs. Patched in rather than read through caplog: another
    suite's conftest stubs ``mini_app_polis.logger`` in ``sys.modules``, so
    the real logger is not guaranteed under every test ordering."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def error(self, message: str, *args: Any, **_: Any) -> None:
        self.errors.append(message % args)


@pytest.fixture(autouse=True)
def log(monkeypatch: pytest.MonkeyPatch) -> _RecordingLogger:
    recorder = _RecordingLogger()
    stub = types.SimpleNamespace(get_logger=lambda: recorder)
    monkeypatch.setattr(sentry_module, "logger", stub)
    return recorder


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> _FakeSdk:
    fake = _FakeSdk()
    monkeypatch.setattr(_sentry, "sentry_sdk", fake)
    monkeypatch.delenv(DEFAULT_DSN_VAR, raising=False)
    monkeypatch.delenv("SENTRY_DSN_APIS", raising=False)
    return fake


def test_starts_with_a_dsn_and_tags_the_service(
    sdk: _FakeSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEFAULT_DSN_VAR, " https://key@sentry.example/1 ")

    assert init_sentry("watcher-cog") is True

    assert sdk.calls == [
        {
            "dsn": "https://key@sentry.example/1",
            "environment": "production",
            "traces_sample_rate": 0.0,
        }
    ]
    assert sdk.scope.tags == {"service": "watcher-cog"}


def test_environment_label_follows_the_shared_resolution(
    sdk: _FakeSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv(DEFAULT_DSN_VAR, "https://key@sentry.example/1")

    init_sentry("watcher-cog")

    assert sdk.calls[0]["environment"] == "development"


def test_passes_integrations_rate_and_release(
    sdk: _FakeSdk, monkeypatch: pytest.MonkeyPatch
) -> None:
    integration = object()

    init_sentry(
        "api-deejaytools",
        dsn_var="SENTRY_DSN_APIS",
        dsn="https://key@sentry.example/2",
        integrations=[integration],
        traces_sample_rate=1.0,
        release="abc123",
    )

    assert sdk.calls[0]["integrations"] == [integration]
    assert sdk.calls[0]["traces_sample_rate"] == 1.0
    assert sdk.calls[0]["release"] == "abc123"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_dsn_in_production_logs_an_error_naming_the_variable(
    sdk: _FakeSdk,
    monkeypatch: pytest.MonkeyPatch,
    log: _RecordingLogger,
    value: str | None,
) -> None:
    if value is not None:
        monkeypatch.setenv("SENTRY_DSN_APIS", value)

    assert init_sentry("api-kaianolevine-com", dsn_var="SENTRY_DSN_APIS") is False

    assert sdk.calls == []
    [message] = log.errors
    assert "api-kaianolevine-com" in message
    assert "SENTRY_DSN_APIS" in message


def test_explicit_empty_dsn_wins_over_the_environment(
    sdk: _FakeSdk,
    monkeypatch: pytest.MonkeyPatch,
    log: _RecordingLogger,
) -> None:
    monkeypatch.setenv(DEFAULT_DSN_VAR, "https://key@sentry.example/1")

    assert init_sentry("db-ops", dsn="") is False

    assert sdk.calls == []
    assert len(log.errors) == 1


@pytest.mark.parametrize("environment", ["dev", "local"])
def test_empty_dsn_outside_production_is_quiet(
    sdk: _FakeSdk,
    monkeypatch: pytest.MonkeyPatch,
    log: _RecordingLogger,
    environment: str,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", environment)

    assert init_sentry("db-ops") is False

    assert sdk.calls == []
    assert log.errors == []


def test_missing_sdk_in_production_logs_an_error(
    monkeypatch: pytest.MonkeyPatch, log: _RecordingLogger
) -> None:
    monkeypatch.setattr(_sentry, "sentry_sdk", None)
    monkeypatch.setenv(DEFAULT_DSN_VAR, "https://key@sentry.example/1")

    assert init_sentry("deejay-cog") is False

    [message] = log.errors
    assert "sentry_sdk is not installed" in message


def test_a_failing_init_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, log: _RecordingLogger
) -> None:
    monkeypatch.setattr(_sentry, "sentry_sdk", _FakeSdk(fail=True))
    monkeypatch.setenv(DEFAULT_DSN_VAR, "https://key@sentry.example/1")

    assert init_sentry("evaluator-cog") is False

    assert len(log.errors) == 1
