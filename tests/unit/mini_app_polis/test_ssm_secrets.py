"""Loading a worker's secrets from Parameter Store."""

from __future__ import annotations

import json
from typing import Any

import pytest

from mini_app_polis import ssm_secrets
from mini_app_polis.ssm_secrets import MissingParameterError, load_secrets

PREFIX = "/mini-app-polis/prd/"


class FakeSSM:
    """Just enough of the SSM client: get_parameters over a dict."""

    def __init__(self, store: dict[str, str]) -> None:
        self.store = store
        self.calls: list[list[str]] = []

    def get_parameters(
        self, *, Names: list[str], WithDecryption: bool
    ) -> dict[str, Any]:
        assert WithDecryption is True
        assert len(Names) <= 10, "GetParameters accepts at most ten names"
        self.calls.append(Names)
        return {
            "Parameters": [
                {"Name": n, "Value": self.store[n]} for n in Names if n in self.store
            ],
            "InvalidParameters": [n for n in Names if n not in self.store],
        }


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts unloaded, with none of the variables set."""
    monkeypatch.setattr(ssm_secrets, "_loaded", False)
    for var in (
        ssm_secrets.PREFIX_VAR,
        ssm_secrets.REQUIRED_VAR,
        ssm_secrets.OPTIONAL_VAR,
    ):
        monkeypatch.delenv(var, raising=False)


def _declare(
    monkeypatch: pytest.MonkeyPatch,
    required: dict[str, str] | None = None,
    optional: dict[str, str] | None = None,
    prefix: str = PREFIX,
) -> None:
    monkeypatch.setenv(ssm_secrets.PREFIX_VAR, prefix)
    if required is not None:
        monkeypatch.setenv(ssm_secrets.REQUIRED_VAR, json.dumps(required))
    if optional is not None:
        monkeypatch.setenv(ssm_secrets.OPTIONAL_VAR, json.dumps(optional))


def test_nothing_declared_is_a_no_op() -> None:
    client = FakeSSM({})
    assert load_secrets(client=client) == []
    assert client.calls == []


def test_loads_required_into_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_KEY", raising=False)
    _declare(monkeypatch, required={"API_KEY": "DEEJAY_COG_API_KEY"})
    client = FakeSSM({f"{PREFIX}DEEJAY_COG_API_KEY": "s3cret"})

    assert load_secrets(client=client) == ["API_KEY"]
    assert ssm_secrets.os.environ["API_KEY"] == "s3cret"


def test_parameter_value_replaces_an_existing_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SENTRY_DSN", "stale")
    _declare(monkeypatch, required={"SENTRY_DSN": "SENTRY_DSN"})
    load_secrets(client=FakeSSM({f"{PREFIX}SENTRY_DSN": "fresh"}))
    assert ssm_secrets.os.environ["SENTRY_DSN"] == "fresh"


def test_one_parameter_can_feed_two_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("A", "B"):
        monkeypatch.delenv(name, raising=False)
    _declare(monkeypatch, required={"A": "SHARED", "B": "SHARED"})
    client = FakeSSM({f"{PREFIX}SHARED": "v"})

    assert load_secrets(client=client) == ["A", "B"]
    assert client.calls == [[f"{PREFIX}SHARED"]]


def test_fetches_in_batches_of_ten(monkeypatch: pytest.MonkeyPatch) -> None:
    names = {f"V{i:02d}": f"P{i:02d}" for i in range(23)}
    for env in names:
        monkeypatch.delenv(env, raising=False)
    _declare(monkeypatch, required=names)
    client = FakeSSM({f"{PREFIX}{p}": p.lower() for p in names.values()})

    assert len(load_secrets(client=client)) == 23
    assert [len(c) for c in client.calls] == [10, 10, 3]


def test_missing_required_fails_and_sets_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("PRESENT", "ABSENT"):
        monkeypatch.delenv(name, raising=False)
    _declare(monkeypatch, required={"PRESENT": "HERE", "ABSENT": "GONE"})

    with pytest.raises(
        MissingParameterError, match=r"ABSENT \(/mini-app-polis/prd/GONE\)"
    ):
        load_secrets(client=FakeSSM({f"{PREFIX}HERE": "v"}))
    assert "PRESENT" not in ssm_secrets.os.environ


def test_missing_optional_is_left_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("KEY", "LLM_MODEL"):
        monkeypatch.delenv(name, raising=False)
    _declare(
        monkeypatch,
        required={"KEY": "KEY"},
        optional={"LLM_MODEL": "LLM_MODEL"},
    )

    assert load_secrets(client=FakeSSM({f"{PREFIX}KEY": "v"})) == ["KEY"]
    assert "LLM_MODEL" not in ssm_secrets.os.environ


def test_second_call_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEY", raising=False)
    _declare(monkeypatch, required={"KEY": "KEY"})
    client = FakeSSM({f"{PREFIX}KEY": "v"})

    load_secrets(client=client)
    assert load_secrets(client=client) == []
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "prefix", ["", "/", "mini-app-polis/prd/", "/mini-app-polis/prd"]
)
def test_prefix_must_be_a_slashed_path(
    monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    _declare(monkeypatch, required={"KEY": "KEY"}, prefix=prefix)
    with pytest.raises(ValueError, match="SSM_PREFIX"):
        load_secrets(client=FakeSSM({}))


def test_names_are_relative_to_the_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    _declare(monkeypatch, required={"KEY": "/mini-app-polis/prd/KEY"})
    with pytest.raises(ValueError, match="without a leading slash"):
        load_secrets(client=FakeSSM({}))


@pytest.mark.parametrize("raw", ["not json", "[]", '{"KEY": ""}', '{"KEY": 1}'])
def test_malformed_map_is_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(ssm_secrets.PREFIX_VAR, PREFIX)
    monkeypatch.setenv(ssm_secrets.REQUIRED_VAR, raw)
    with pytest.raises(ValueError, match="SSM_PARAMETERS"):
        load_secrets(client=FakeSSM({}))


def test_a_name_cannot_be_both_required_and_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _declare(monkeypatch, required={"KEY": "A"}, optional={"KEY": "B"})
    with pytest.raises(ValueError, match="both"):
        load_secrets(client=FakeSSM({}))


def test_exposed_at_package_level() -> None:
    import mini_app_polis

    assert mini_app_polis.load_secrets is load_secrets
