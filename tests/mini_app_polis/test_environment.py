"""Environment resolution and effect gating."""

from __future__ import annotations

import pytest

from mini_app_polis.environment import (
    Effect,
    Environment,
    api_base_url,
    current_environment,
    effect_enabled,
    env_var,
    resolve,
    summary,
)

_ALL_SOURCES = ("ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ambient environment leaks into these tests."""
    for name in _ALL_SOURCES:
        monkeypatch.delenv(name, raising=False)
    for effect in Effect:
        monkeypatch.delenv(f"{effect.name}_ENABLED", raising=False)
    for name in ("KAIANO_API_BASE_URL", "KAIANO_API_BASE_URL_DEV"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("production", Environment.PRODUCTION),
        ("prod", Environment.PRODUCTION),
        ("PRD", Environment.PRODUCTION),
        ("dev", Environment.DEVELOPMENT),
        ("Development", Environment.DEVELOPMENT),
        ("local", Environment.LOCAL),
    ],
)
def test_known_names_normalise(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: Environment
) -> None:
    monkeypatch.setenv("ENVIRONMENT", raw)
    assert current_environment() is expected


def test_unknown_name_is_not_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed. An environment we cannot name must not act like production."""
    monkeypatch.setenv("ENVIRONMENT", "staging-experiment-3")
    assert current_environment() is Environment.DEVELOPMENT


def test_nothing_set_is_local(monkeypatch: pytest.MonkeyPatch) -> None:
    env, source = resolve()
    assert env is Environment.LOCAL
    assert source == "default"


def test_explicit_wins_over_railway(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "dev")
    env, source = resolve()
    assert env is Environment.PRODUCTION
    assert source == "ENVIRONMENT"


def test_legacy_railway_variable_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "dev")
    env, source = resolve()
    assert env is Environment.DEVELOPMENT
    assert source == "RAILWAY_ENVIRONMENT"


def test_blank_source_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "   ")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    env, source = resolve()
    assert env is Environment.PRODUCTION
    assert source == "RAILWAY_ENVIRONMENT_NAME"


@pytest.mark.parametrize("effect", list(Effect))
def test_effects_fire_in_production(
    monkeypatch: pytest.MonkeyPatch, effect: Effect
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert effect_enabled(effect) is True


@pytest.mark.parametrize("effect", list(Effect))
def test_effects_are_suppressed_outside_production(
    monkeypatch: pytest.MonkeyPatch, effect: Effect
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert effect_enabled(effect) is False


def test_explicit_flag_overrides_both_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("PREFECT_TRIGGER_ENABLED", "true")
    assert effect_enabled(Effect.PREFECT_TRIGGER) is True
    # ...and the other gate is untouched by it.
    assert effect_enabled(Effect.HEALTHCHECKS) is False

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("HEALTHCHECKS_ENABLED", "false")
    assert effect_enabled(Effect.HEALTHCHECKS) is False


def test_api_base_url_is_suffixed_outside_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    monkeypatch.setenv("KAIANO_API_BASE_URL_DEV", "https://dev-api.example")

    monkeypatch.setenv("ENVIRONMENT", "production")
    assert api_base_url() == "https://api.example"

    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert api_base_url() == "https://dev-api.example"


def test_missing_dev_value_does_not_fall_back_to_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point. A forgotten dev variable must not reach production."""
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("KAIANO_API_BASE_URL", "https://api.example")
    assert api_base_url() == ""


def test_env_var_is_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("SOME_TARGET_DEV", "dev-value")
    assert env_var("SOME_TARGET") == "dev-value"


def test_summary_names_environment_source_and_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "dev")
    monkeypatch.setenv("KAIANO_API_BASE_URL_DEV", "https://dev-api.example")
    line = summary()
    assert "environment=development" in line
    assert "from RAILWAY_ENVIRONMENT_NAME" in line
    assert "prefect_trigger=off" in line
    assert "healthchecks=off" in line
    assert "api_base_url=https://dev-api.example" in line


def test_summary_marks_an_unset_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "dev")
    assert "api_base_url=<unset>" in summary()
