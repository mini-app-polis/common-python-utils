"""The secret-rotation contract: rotation events, rotation requests, and that
no model in it can carry a secret's value."""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from mini_app_polis.api import KaianoApiClient
from mini_app_polis.api.contract import (
    ENDPOINTS_BY_NAME,
    RotationEvent,
    RotationEventCreate,
    RotationRequest,
    RotationRequestAccepted,
    SecretChange,
    secret_rotations,
)

_META = {"count": 1, "total": 1, "version": "test"}
_MINTED = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
_EVENT_ID = "0b6f1c9e-5d1a-4c1e-9a43-2f7f3f1d8e21"


def _event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "DEEJAY_COG_API_KEY",
        "config": "prd",
        "kind": "machine-key",
        "mode": "routine",
        "actor": "rotator-cog",
        "minted": _MINTED.isoformat(),
        "verification": "ok",
    }
    base.update(overrides)
    return base


def _client(monkeypatch: pytest.MonkeyPatch, answer: Any) -> tuple[Any, list]:
    calls: list[tuple[str, str, Any]] = []

    def fake_post(self, path, payload):  # noqa: ARG001
        calls.append(("POST", path, payload))
        return answer

    def fake_get(self, path, params=None):  # noqa: ARG001
        calls.append(("GET", path, params))
        return answer

    monkeypatch.setattr(KaianoApiClient, "post", fake_post)
    monkeypatch.setattr(KaianoApiClient, "get", fake_get)
    return KaianoApiClient(base_url="https://example.com", api_key="k"), calls


# ── The catalog ───────────────────────────────────────────────────────────


def test_the_routes_and_their_scopes() -> None:
    routes = {
        name: (e.method, e.path, e.request, e.scope)
        for name in (
            "record_rotation_event",
            "list_rotation_events",
            "request_rotation",
            "list_secret_changes",
        )
        for e in [ENDPOINTS_BY_NAME[name]]
    }
    assert routes == {
        "record_rotation_event": (
            "POST",
            "/v1/secrets/rotation-events",
            RotationEventCreate,
            "secrets.rotation-events.write",
        ),
        "list_rotation_events": (
            "GET",
            "/v1/secrets/rotation-events",
            None,
            "secrets.rotation-events.read",
        ),
        "request_rotation": (
            "POST",
            "/v1/secrets/rotations",
            RotationRequest,
            "secrets.rotations.create",
        ),
        "list_secret_changes": (
            "GET",
            "/v1/secrets/changes",
            None,
            "secrets.changes.read",
        ),
    }


# ── Rotation events ───────────────────────────────────────────────────────


def test_an_event_carries_what_a_rotation_did() -> None:
    event = RotationEventCreate.model_validate(
        _event(
            kind="aws-access-key",
            name="AWS_SECRET_ACCESS_KEY",
            mode="compromised",
            actor="owner@example.com",
            retired=(_MINTED + timedelta(minutes=5)).isoformat(),
            aws_key_last4="7QXZ",
        )
    )
    assert event.mode == "compromised"
    assert event.retired == _MINTED + timedelta(minutes=5)
    assert event.aws_key_last4 == "7QXZ"


def test_a_failed_verification_says_why() -> None:
    event = RotationEventCreate.model_validate(
        _event(verification="failed", verification_reason="whoami answered 401")
    )
    assert event.verification == "failed"
    assert event.verification_reason == "whoami answered 401"


def test_a_verified_event_has_no_failure_reason() -> None:
    with pytest.raises(ValidationError, match="no failure reason"):
        RotationEventCreate.model_validate(_event(verification_reason="fine"))


def test_an_unchecked_rotation_is_recorded_without_a_reason() -> None:
    event = RotationEventCreate.model_validate(_event(verification="unchecked"))
    assert event.verification == "unchecked"
    assert event.verification_reason is None


def test_an_unchecked_rotation_has_no_failure_reason() -> None:
    with pytest.raises(ValidationError):
        RotationEventCreate.model_validate(
            _event(verification="unchecked", verification_reason="no check")
        )


def test_a_stored_event_can_be_unchecked() -> None:
    stored = RotationEvent.model_validate(
        {
            **_event(verification="unchecked"),
            "id": _EVENT_ID,
            "recorded_at": _MINTED.isoformat(),
        }
    )
    assert stored.verification == "unchecked"


def test_only_an_aws_rotation_names_a_key_id() -> None:
    with pytest.raises(ValidationError, match="no AWS key id"):
        RotationEventCreate.model_validate(_event(aws_key_last4="7QXZ"))


@pytest.mark.parametrize("last4", ["7qxz", "7QX", "7QXZ1", "AKIA7QXZ"])
def test_the_aws_key_id_is_exactly_its_last_four(last4: str) -> None:
    with pytest.raises(ValidationError):
        RotationEventCreate.model_validate(
            _event(kind="aws-access-key", aws_key_last4=last4)
        )


def test_the_old_value_is_not_retired_before_the_new_one_is_minted() -> None:
    with pytest.raises(ValidationError, match="retired after"):
        RotationEventCreate.model_validate(
            _event(retired=(_MINTED - timedelta(seconds=1)).isoformat())
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "deejay_cog_api_key"),
        ("name", "DEEJAY COG KEY"),
        ("config", "production"),
        ("kind", "not-rotated"),
        ("mode", "urgent"),
        ("actor", "Secrets Rotator"),
        ("verification", "skipped"),
    ],
)
def test_an_event_refuses_what_is_not_its_shape(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        RotationEventCreate.model_validate(_event(**{field: value}))


def test_a_stored_event_tolerates_fields_a_newer_api_adds() -> None:
    stored = RotationEvent.model_validate(
        _event(id=_EVENT_ID, recorded_at=_MINTED.isoformat(), later="x")
    )
    assert stored.id == _EVENT_ID


def test_recording_an_event_posts_it_and_returns_the_stored_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _event(id=_EVENT_ID, recorded_at=_MINTED.isoformat())
    client, calls = _client(monkeypatch, {"data": stored, "meta": _META})

    out = client.record_rotation_event(RotationEventCreate.model_validate(_event()))

    assert calls == [
        (
            "POST",
            "/v1/secrets/rotation-events",
            {**_event(), "minted": "2026-10-25T09:00:00Z"},
        )
    ]
    assert isinstance(out, RotationEvent)
    assert out.verification == "ok"


def test_listing_events_sends_only_the_filters_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, calls = _client(monkeypatch, {"data": [], "meta": _META})
    assert client.list_rotation_events() == []
    client.list_rotation_events(latest=False)
    client.list_rotation_events(
        name="DEEJAY_COG_API_KEY", config="prd", latest=True, limit=10, offset=20
    )
    assert calls == [
        ("GET", "/v1/secrets/rotation-events", None),
        ("GET", "/v1/secrets/rotation-events", None),
        (
            "GET",
            "/v1/secrets/rotation-events",
            {
                "name": "DEEJAY_COG_API_KEY",
                "config": "prd",
                "latest": "true",
                "limit": 10,
                "offset": 20,
            },
        ),
    ]


# ── Rotation requests ─────────────────────────────────────────────────────


def test_requesting_a_rotation_posts_name_config_and_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accepted = {
        "accepted": True,
        "message_id": "m-1",
        "name": "TICK_SECRET",
        "config": "prd",
    }
    client, calls = _client(monkeypatch, {"data": accepted, "meta": _META})

    out = client.request_rotation(
        RotationRequest(name="TICK_SECRET", config="prd", mode="compromised")
    )

    assert calls == [
        (
            "POST",
            "/v1/secrets/rotations",
            {"name": "TICK_SECRET", "config": "prd", "mode": "compromised"},
        )
    ]
    assert isinstance(out, RotationRequestAccepted)
    assert out.message_id == "m-1"


# ── Secret changes ────────────────────────────────────────────────────────


def _change(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "OPENAI_API_KEY",
        "config": "dev",
        "change": "updated",
        "changed_at": _MINTED.isoformat(),
        "received_id": _EVENT_ID,
    }
    base.update(overrides)
    return base


def test_a_change_names_a_secret_and_when_doppler_reported_it() -> None:
    change = SecretChange.model_validate(_change())
    assert (change.name, change.config, change.change) == (
        "OPENAI_API_KEY",
        "dev",
        "updated",
    )
    assert change.changed_at == _MINTED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("change", "rotated"),
        ("config", "stg"),
        ("name", "openai key"),
        ("received_id", "delivery-1"),
    ],
)
def test_a_change_refuses_what_is_not_its_shape(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        SecretChange.model_validate(_change(**{field: value}))


def test_a_change_tolerates_fields_a_newer_api_adds() -> None:
    assert SecretChange.model_validate(_change(source="doppler")).name == (
        "OPENAI_API_KEY"
    )


def test_listing_changes_sends_only_the_filters_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, calls = _client(monkeypatch, {"data": [_change()], "meta": _META})
    changes = client.list_secret_changes(config="dev")
    client.list_secret_changes()
    client.list_secret_changes(
        config="prd", since=_MINTED, until=_MINTED + timedelta(days=1)
    )

    assert changes == [SecretChange.model_validate(_change())]
    assert calls == [
        ("GET", "/v1/secrets/changes", {"config": "dev"}),
        ("GET", "/v1/secrets/changes", None),
        (
            "GET",
            "/v1/secrets/changes",
            {
                "config": "prd",
                "since": _MINTED.isoformat(),
                "until": (_MINTED + timedelta(days=1)).isoformat(),
            },
        ),
    ]


def test_a_request_does_not_say_who_asked() -> None:
    # The person asking is the authenticated caller; a body cannot claim one.
    with pytest.raises(ValidationError):
        RotationRequest.model_validate(
            {"name": "TICK_SECRET", "config": "prd", "mode": "routine", "actor": "x"}
        )


# ── No model carries a secret's value ─────────────────────────────────────
#
# What this guarantees: no field is named for a value, every request model
# refuses an undeclared field (so ``{"value": ...}`` is a 422, not a stored
# secret), no field is a container or an untyped blob, and every string is a
# fixed choice, a timestamp, or bounded, and all but the listed free-text ones
# are held to a pattern that the shapes of the fleet's secrets do not fit.
#
# What it does not: the free-text fields (a failure reason, a queue message
# id) are bounded, not shaped, so a careless rotator could still write a value
# into one; a secret that happens to look like an environment variable name
# or a hyphenated machine name would pass those patterns; four characters of
# a value could pass as an AWS key id's last four; and response models must
# tolerate fields a newer API adds, so they are not checked for extras. The
# rotator not handling values at all is what keeps them out; this keeps the
# record's shape from inviting them.

_MODELS: list[type[BaseModel]] = [
    obj
    for _, obj in inspect.getmembers(secret_rotations, inspect.isclass)
    if issubclass(obj, BaseModel)
    and obj.__module__ == secret_rotations.__name__
    and not obj.__name__.startswith("_")
]

_REQUEST_MODELS = [RotationEventCreate, RotationRequest]

_VALUE_LIKE = re.compile(
    r"value|secret|token|key|password|passwd|credential|private|cipher", re.I
)

#: The one field named like a value, and why it is not one.
_NAMED_LIKE_A_VALUE = {"aws_key_last4"}  # four characters of a public key id

#: Strings bounded but not shaped, and their bound.
_FREE_TEXT = {"verification_reason": 500, "message_id": 100}

#: Strings whose pattern fixes their length.
_FIXED_LENGTH = {"aws_key_last4", "id"}

#: Shapes of real secrets in the fleet, none of which a patterned field accepts.
_SECRET_SHAPES = [
    "dev_Xk3vQ9mZ2pL8rT4wY6uB1nC5eF7gH0jK",
    "Xk3vQ9mZ2pL8rT4wY6uB1nC5eF7gH0jKaS9dF2gH4jK",
    "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    "postgresql://user:hunter2@db.internal:5432/app",
    "https://discord.com/api/webhooks/123/abcDEF-ghi_JKL",
    # Split so the secret scanner does not read a made-up value as a live key.
    "sk_" + "live_51HxAbCdEfGhIjKlMnOpQrStUv",
]


def _properties(model: type[BaseModel]) -> dict[str, dict[str, Any]]:
    """Each field's JSON schema, with ``X | None`` reduced to ``X``."""
    out: dict[str, dict[str, Any]] = {}
    for name, schema in model.model_json_schema()["properties"].items():
        options = [o for o in schema.get("anyOf", [schema]) if o != {"type": "null"}]
        assert len(options) == 1, f"{model.__name__}.{name} is a union"
        out[name] = options[0]
    return out


def test_the_models_checked_are_every_model_in_the_contract() -> None:
    assert set(_MODELS) == {
        RotationEvent,
        RotationEventCreate,
        RotationRequest,
        RotationRequestAccepted,
        SecretChange,
    }


@pytest.mark.parametrize("model", _MODELS, ids=lambda m: m.__name__)
def test_no_field_is_named_for_a_value(model: type[BaseModel]) -> None:
    for name, field in model.model_fields.items():
        for spelling in {name, field.alias or name}:
            if spelling in _NAMED_LIKE_A_VALUE:
                continue
            assert not _VALUE_LIKE.search(spelling), f"{model.__name__}.{spelling}"


@pytest.mark.parametrize("model", _REQUEST_MODELS, ids=lambda m: m.__name__)
def test_a_request_refuses_a_field_it_does_not_declare(
    model: type[BaseModel],
) -> None:
    assert model.model_config.get("extra") == "forbid"


@pytest.mark.parametrize("model", _MODELS, ids=lambda m: m.__name__)
def test_every_string_is_a_choice_a_time_or_bounded(model: type[BaseModel]) -> None:
    for name, schema in _properties(model).items():
        where = f"{model.__name__}.{name}"
        assert schema.get("type") in {"string", "boolean"} or "enum" in schema, (
            f"{where} is not a string, a choice or a flag: {schema}"
        )
        if schema.get("type") != "string" or "enum" in schema or "const" in schema:
            continue
        if schema.get("format") == "date-time":
            continue
        if name in _FREE_TEXT:
            assert schema.get("maxLength", 10**9) <= _FREE_TEXT[name], where
            continue
        assert "pattern" in schema, f"{where} is free text"
        if name not in _FIXED_LENGTH:
            assert "maxLength" in schema, f"{where} is unbounded"


@pytest.mark.parametrize("value", _SECRET_SHAPES)
def test_a_secret_shaped_string_fits_no_patterned_field(value: str) -> None:
    for field in ("name", "actor"):
        with pytest.raises(ValidationError):
            RotationEventCreate.model_validate(_event(**{field: value}))
    with pytest.raises(ValidationError):
        RotationEventCreate.model_validate(
            _event(kind="aws-access-key", aws_key_last4=value)
        )
    with pytest.raises(ValidationError):
        RotationRequest(name=value, config="prd", mode="routine")


@pytest.mark.parametrize("model", _REQUEST_MODELS, ids=lambda m: m.__name__)
def test_a_value_sent_under_any_undeclared_name_is_refused(
    model: type[BaseModel],
) -> None:
    body = (
        _event()
        if model is RotationEventCreate
        else {
            "name": "TICK_SECRET",
            "config": "prd",
            "mode": "routine",
        }
    )
    model.model_validate(body)
    for extra in ("value", "secret", "new_value", "previous"):
        with pytest.raises(ValidationError):
            model.model_validate({**body, extra: _SECRET_SHAPES[0]})
