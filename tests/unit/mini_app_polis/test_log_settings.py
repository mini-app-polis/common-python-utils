"""log_settings: values only for allowlisted names."""

from __future__ import annotations

import logging

import pytest

from mini_app_polis.environment import log_settings

_SECRET = "hunter2-do-not-log"


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SETTINGS_ALLOWLIST", "PORT", "API_KEY", "REGION"):
        monkeypatch.delenv(name, raising=False)


def _lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records]


def test_allowlisted_name_set_in_environment(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("PORT", "9000")
    with caplog.at_level(logging.INFO):
        log_settings({"PORT": 9000}, ["PORT"])
    assert _lines(caplog) == ["PORT=9000 (set)"]


def test_allowlisted_name_took_default(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        log_settings({"PORT": 8000}, "PORT")
    assert _lines(caplog) == ["PORT=8000 (default)"]


def test_empty_environment_value_counts_as_unset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("PORT", "")
    with caplog.at_level(logging.INFO):
        log_settings({"PORT": 8000}, ["PORT"])
    assert _lines(caplog) == ["PORT=8000 (default)"]


def test_name_off_allowlist_never_shows_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("API_KEY", _SECRET)
    with caplog.at_level(logging.DEBUG):
        log_settings({"API_KEY": _SECRET, "REGION": _SECRET, "PORT": 8000}, ["PORT"])
    assert _lines(caplog) == [
        "API_KEY (set)",
        "REGION (unset)",
        "PORT=8000 (default)",
    ]
    assert _SECRET not in caplog.text


@pytest.mark.parametrize("env", [None, ""])
def test_missing_or_empty_allowlist_shows_no_value(
    env: str | None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    if env is not None:
        monkeypatch.setenv("SETTINGS_ALLOWLIST", env)
    with caplog.at_level(logging.DEBUG):
        log_settings({"PORT": _SECRET})
    assert _lines(caplog) == ["PORT (unset)"]
    assert _SECRET not in caplog.text


def test_allowlist_read_from_environment(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SETTINGS_ALLOWLIST", " PORT , ,REGION")
    with caplog.at_level(logging.INFO):
        log_settings({"PORT": 1, "API_KEY": _SECRET})
    assert _lines(caplog) == ["PORT=1 (default)", "API_KEY (unset)"]


def test_accepts_pydantic_settings(caplog: pytest.LogCaptureFixture) -> None:
    pydantic_settings = pytest.importorskip("pydantic_settings")

    class Settings(pydantic_settings.BaseSettings):
        port: int = 8000
        api_key: str = _SECRET

    with caplog.at_level(logging.INFO):
        log_settings(Settings(), ["PORT"])
    assert _lines(caplog) == ["PORT=8000 (default)", "API_KEY (unset)"]
    assert _SECRET not in caplog.text
