from __future__ import annotations

import datetime as dt
import types
import typing
import uuid
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from mini_app_polis.api import KaianoApiClient
from mini_app_polis.api.contract import (
    ENDPOINTS,
    ENDPOINTS_BY_NAME,
    IngestSet,
    IngestTrack,
    NotificationResult,
    NotifyRequest,
    PipelineEvaluationItem,
    SetListItem,
    TranscriptionRunRequest,
    WcsExtractionRawOutput,
    WcsSourceCreate,
)

_META = {"count": 1, "total": 1, "version": "test"}


def _nested_models(annotation: Any) -> set[type[BaseModel]]:
    """Every BaseModel reachable from one annotation, itself included."""
    found: set[type[BaseModel]] = set()
    stack = [annotation]
    while stack:
        current = stack.pop()
        if isinstance(current, type) and issubclass(current, BaseModel):
            if current in found:
                continue
            found.add(current)
            stack.extend(f.annotation for f in current.model_fields.values())
        else:
            stack.extend(typing.get_args(current))
    return found


def _client(monkeypatch: pytest.MonkeyPatch, answer: Any) -> tuple[Any, list]:
    """A client whose get/post return ``answer`` and record what they were sent."""
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


def test_catalog_has_one_entry_per_method_and_path() -> None:
    routes = [(e.method, e.path) for e in ENDPOINTS]
    assert len(routes) == len(set(routes))
    assert len(ENDPOINTS_BY_NAME) == len(ENDPOINTS)


def test_every_catalog_endpoint_has_a_typed_client_method() -> None:
    for endpoint in ENDPOINTS:
        assert callable(getattr(KaianoApiClient, endpoint.name, None)), endpoint.name


def test_only_get_endpoints_have_no_request_model() -> None:
    for endpoint in ENDPOINTS:
        assert (endpoint.request is None) == (endpoint.method == "GET"), endpoint.name


def test_response_models_tolerate_fields_they_do_not_know() -> None:
    """An API that adds a response field must not break an older cog.

    The cogs pin this library to a major, so a cog can run an older contract
    than the API it calls. That is safe only while no response model, at any
    depth, forbids extra fields. Request models may forbid them, and most do.
    """
    for endpoint in ENDPOINTS:
        for model in _nested_models(endpoint.response):
            assert model.model_config.get("extra") != "forbid", (
                f"{endpoint.name}: {model.__name__} forbids extra fields"
            )


def test_the_response_annotation_helper_sees_through_containers() -> None:
    assert PipelineEvaluationItem in _nested_models(list[PipelineEvaluationItem])
    assert IngestTrack in _nested_models(IngestSet)
    assert _nested_models(types.NoneType) == set()


# ── Typed client methods ──────────────────────────────────────────────────


def test_typed_post_sends_only_the_fields_set_and_returns_the_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, calls = _client(
        monkeypatch,
        {
            "data": {"forwarded": True, "event": "notify", "reason": "sent"},
            "meta": _META,
        },
    )

    out = client.send_notification(NotifyRequest(content="deploy finished"))

    assert calls == [("POST", "/v1/notify", {"content": "deploy finished"})]
    assert isinstance(out, NotificationResult)
    assert out.forwarded is True


def test_typed_post_serialises_dates_as_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_id = uuid.uuid4()
    client, calls = _client(
        monkeypatch,
        {
            "data": {
                "set_id": str(set_id),
                "tracks_created": 1,
                "catalog_new": 1,
                "catalog_updated": 0,
                "catalog_unchanged": 0,
            },
            "meta": _META,
        },
    )

    out = client.ingest(
        IngestSet(
            set_date=dt.date(2026, 9, 1),
            venue="Studio",
            source_file="2026-09-01.csv",
            tracks=[IngestTrack(title="Song", artist="Artist")],
        )
    )

    assert calls[0][:2] == ("POST", "/v1/ingest")
    assert calls[0][2] == {
        "set_date": "2026-09-01",
        "venue": "Studio",
        "source_file": "2026-09-01.csv",
        "tracks": [{"title": "Song", "artist": "Artist"}],
    }
    assert out.set_id == set_id


def test_typed_post_sends_fields_under_their_wire_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``from_`` goes out as ``"from"``, which is what the API reads.

    Sent by field name, the relation arrives without ``from`` and the whole
    source is a 422. The body must validate as the request model, the way
    the API will validate it.
    """
    client, calls = _client(monkeypatch, {"data": {}, "meta": _META})
    payload = WcsSourceCreate(
        transcript_id=uuid.uuid4(),
        extractor_version="1",
        extractor_model="m",
        extractor_provider="p",
        prompt_version="v",
        raw_output=WcsExtractionRawOutput.model_validate(
            {"entity_relations": [{"from": "a", "to": "b", "relation_kind": "r"}]}
        ),
    )

    with pytest.raises(ValidationError):  # the fake answer is not a source
        client.create_wcs_source(payload)

    sent = calls[0][2]
    assert sent["raw_output"]["entity_relations"][0]["from"] == "a"
    assert WcsSourceCreate.model_validate(sent) == payload


def test_list_evaluations_sends_only_the_filters_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = {
        "id": str(uuid.uuid4()),
        "run_id": "r1",
        "violation_id": None,
        "repo": "deejay-cog",
        "dimension": "testing",
        "severity": "WARN",
        "finding": "f",
        "suggestion": None,
        "standards_version": None,
        "evaluator_version": None,
        "evaluated_at": "2026-09-01T00:00:00Z",
    }
    client, calls = _client(monkeypatch, {"data": [item], "meta": _META})

    out = client.list_evaluations(run_id="r1", limit=500)

    assert calls == [("GET", "/v1/evaluations", {"run_id": "r1", "limit": 500})]
    assert [type(i) for i in out] == [PipelineEvaluationItem]
    assert out[0].repo == "deejay-cog"


def test_list_sets_sends_dates_as_iso_and_only_the_filters_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = {
        "id": str(uuid.uuid4()),
        "set_date": "2026-10-02",
        "year": 2026,
        "venue": "TC Rebels",
        "source_file": "2026-10-02 TC Rebels",
        "track_count": 48,
    }
    client, calls = _client(monkeypatch, {"data": [item], "meta": _META})

    out = client.list_sets(date_from=dt.date(2026, 4, 5), limit=200)

    assert calls == [("GET", "/v1/sets", {"date_from": "2026-04-05", "limit": 200})]
    assert [type(i) for i in out] == [SetListItem]
    assert out[0].source_file == "2026-10-02 TC Rebels"


def test_a_response_outside_the_contract_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(monkeypatch, {"data": {"forwarded": True}, "meta": _META})

    with pytest.raises(ValidationError):
        client.send_notification(NotifyRequest(content="x"))


def test_a_response_with_a_field_added_by_the_api_still_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = _client(
        monkeypatch,
        {
            "data": {
                "forwarded": True,
                "event": "notify",
                "reason": "sent",
                "added_later": 1,
            },
            "meta": {**_META, "added_later": 1},
        },
    )

    assert client.send_notification(NotifyRequest(content="x")).forwarded is True


def test_request_validators_run_before_anything_is_sent() -> None:
    with pytest.raises(ValidationError):
        TranscriptionRunRequest(mode="voicenotes")
    with pytest.raises(ValidationError):
        TranscriptionRunRequest(mode="voicenotes-cleanup", drive_file_id="f")
    with pytest.raises(ValidationError):
        NotifyRequest()


def test_a_post_endpoint_without_a_body_is_refused() -> None:
    client = KaianoApiClient(base_url="https://example.com", api_key="k")

    with pytest.raises(TypeError):
        client._call("send_notification")


@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=lambda e: e.name)
def test_each_typed_method_calls_its_own_catalog_entry(
    monkeypatch: pytest.MonkeyPatch, endpoint: Any
) -> None:
    seen: list[str] = []
    sentinel = object()

    def fake_call(self, endpoint_name, body=None, params=None):  # noqa: ARG001
        seen.append(endpoint_name)
        if body is not None:
            assert body is sentinel
        return sentinel

    monkeypatch.setattr(KaianoApiClient, "_call", fake_call)
    client = KaianoApiClient(base_url="https://example.com", api_key="k")
    method = getattr(client, endpoint.name)

    out = method(sentinel) if endpoint.request is not None else method()

    assert seen == [endpoint.name]
    assert out is sentinel


def test_notify_request_to_discord_omits_anything_unset() -> None:
    assert NotifyRequest(content="deploy finished").to_discord() == {
        "content": "deploy finished"
    }
    embed = {"title": "Run failed", "color": 0xFF0000}
    assert NotifyRequest(embeds=[embed]).to_discord() == {"embeds": [embed]}
    assert NotifyRequest(
        content="hi", embeds=[embed], username="evaluator-cog"
    ).to_discord() == {"content": "hi", "embeds": [embed], "username": "evaluator-cog"}


def test_notify_request_to_discord_drops_empty_values() -> None:
    """An empty string or list is not sent; Discord would reject or ignore it."""
    out = NotifyRequest(content="x", embeds=[], username="").to_discord()

    assert out == {"content": "x"}


def test_notify_request_with_only_empty_bodies_is_refused() -> None:
    with pytest.raises(ValidationError, match="one of content or embeds"):
        NotifyRequest(content="", embeds=[])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content": "x" * 2001},
        {"embeds": [{}] * 11},
        {"content": "x", "username": "u" * 81},
    ],
    ids=["content", "embeds", "username"],
)
def test_notify_request_enforces_discords_limits(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        NotifyRequest(**kwargs)


def test_notify_request_accepts_values_at_discords_limits() -> None:
    req = NotifyRequest(content="x" * 2000, embeds=[{}] * 10, username="u" * 80)

    assert len(req.to_discord()["content"]) == 2000
