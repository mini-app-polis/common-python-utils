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


# ── Grooming records ──────────────────────────────────────────────────────


def test_a_grooming_record_round_trips_and_its_write_refuses_extras() -> None:
    from mini_app_polis.api.contract import GroomingRecord, GroomingRecordWrite

    write = GroomingRecordWrite(
        task_gid="1219755103049631",
        task_id="MINI-151",
        fingerprint="a" * 64,
        outcome="not_ready",
        problems=["Effort is blank, not S, M or L"],
    )
    stored = GroomingRecord.model_validate(
        {**write.model_dump(mode="json"), "updated_at": "2026-10-21T19:38:16Z", "x": 1}
    )
    assert stored.returned_count == 0
    assert stored.moved_to_ready_at is None
    with pytest.raises(ValidationError):
        GroomingRecordWrite.model_validate({**write.model_dump(), "x": 1})
    with pytest.raises(ValidationError):
        GroomingRecordWrite.model_validate({**write.model_dump(), "fingerprint": "abc"})


# ── Grooming claims ───────────────────────────────────────────────────────

_GID = "1219848148842653"


def test_a_grooming_claim_request_refuses_extras_and_bad_keys() -> None:
    from mini_app_polis.api.contract import GroomingClaimRequest

    claim = GroomingClaimRequest(task_gid=_GID, fingerprint="b" * 64)
    assert claim.model_dump() == {"task_gid": _GID, "fingerprint": "b" * 64}
    with pytest.raises(ValidationError):
        GroomingClaimRequest.model_validate({**claim.model_dump(), "x": 1})
    with pytest.raises(ValidationError):
        GroomingClaimRequest(task_gid="MINI-292", fingerprint="b" * 64)
    with pytest.raises(ValidationError):
        GroomingClaimRequest(task_gid=_GID, fingerprint="B" * 64)
    with pytest.raises(ValidationError):
        GroomingClaimRequest(task_gid=_GID, fingerprint="b" * 63)


def test_a_claim_already_held_is_an_answer_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mini_app_polis.api.contract import GroomingClaimRequest, GroomingClaimResult

    answer = {"task_gid": _GID, "fingerprint": "b" * 64, "claimed": False}
    client, calls = _client(monkeypatch, {"data": answer, "meta": _META})

    out = client.claim_grooming(
        GroomingClaimRequest(task_gid=_GID, fingerprint="b" * 64)
    )

    assert calls == [
        (
            "POST",
            "/v1/tasks/groomings/claims",
            {"task_gid": _GID, "fingerprint": "b" * 64},
        )
    ]
    assert isinstance(out, GroomingClaimResult)
    assert out.claimed is False


def test_releasing_a_claim_posts_the_same_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mini_app_polis.api.contract import GroomingClaimReleased, GroomingClaimRequest

    answer = {"task_gid": _GID, "fingerprint": "b" * 64, "released": True}
    client, calls = _client(monkeypatch, {"data": answer, "meta": _META})

    out = client.release_grooming_claim(
        GroomingClaimRequest(task_gid=_GID, fingerprint="b" * 64)
    )

    assert calls == [
        (
            "POST",
            "/v1/tasks/groomings/claims/release",
            {"task_gid": _GID, "fingerprint": "b" * 64},
        )
    ]
    assert isinstance(out, GroomingClaimReleased)
    assert out.released is True


# ── Grooming budget stops ─────────────────────────────────────────────────


def test_a_budget_spent_report_refuses_extras_and_bad_keys() -> None:
    from mini_app_polis.api.contract import GroomingBudgetSpentRequest

    stop = GroomingBudgetSpentRequest(task_gid=_GID, fingerprint="c" * 64)
    assert stop.model_dump() == {"task_gid": _GID, "fingerprint": "c" * 64}
    with pytest.raises(ValidationError):
        GroomingBudgetSpentRequest.model_validate({**stop.model_dump(), "x": 1})
    with pytest.raises(ValidationError):
        GroomingBudgetSpentRequest(task_gid="MINI-292", fingerprint="c" * 64)
    with pytest.raises(ValidationError):
        GroomingBudgetSpentRequest(task_gid=_GID, fingerprint="C" * 64)
    with pytest.raises(ValidationError):
        GroomingBudgetSpentRequest(task_gid=_GID, fingerprint="c" * 63)


def test_marking_a_budget_stop_posts_the_task_and_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mini_app_polis.api.contract import (
        GroomingBudgetSpentMarked,
        GroomingBudgetSpentRequest,
    )

    answer = {"task_gid": _GID, "fingerprint": "c" * 64}
    client, calls = _client(monkeypatch, {"data": answer, "meta": _META})

    out = client.mark_grooming_budget_spent(
        GroomingBudgetSpentRequest(task_gid=_GID, fingerprint="c" * 64)
    )

    assert calls == [
        (
            "POST",
            "/v1/tasks/groomings/budget-spent",
            {"task_gid": _GID, "fingerprint": "c" * 64},
        )
    ]
    assert isinstance(out, GroomingBudgetSpentMarked)
    assert out.fingerprint == "c" * 64


def test_the_budget_spent_endpoint_is_a_grooming_write() -> None:
    from mini_app_polis.api.contract import (
        ENDPOINTS_BY_NAME,
        GroomingBudgetSpentMarked,
        GroomingBudgetSpentRequest,
    )

    endpoint = ENDPOINTS_BY_NAME["mark_grooming_budget_spent"]
    assert (endpoint.method, endpoint.path, endpoint.scope) == (
        "POST",
        "/v1/tasks/groomings/budget-spent",
        "tasks.groomings.write",
    )
    assert endpoint.request is GroomingBudgetSpentRequest
    assert endpoint.response is GroomingBudgetSpentMarked


def test_a_grooming_record_carries_a_budget_stop_only_when_read() -> None:
    from mini_app_polis.api.contract import GroomingRecord, GroomingRecordWrite

    stored = {
        "task_gid": _GID,
        "fingerprint": "a" * 64,
        "outcome": "not_ready",
        "updated_at": "2026-10-21T19:38:16Z",
    }
    assert GroomingRecord.model_validate(stored).budget_spent_fingerprint is None
    stopped = GroomingRecord.model_validate(
        {**stored, "budget_spent_fingerprint": "a" * 64}
    )
    assert stopped.budget_spent_fingerprint == "a" * 64
    with pytest.raises(ValidationError):
        GroomingRecord.model_validate({**stored, "budget_spent_fingerprint": "abc"})
    assert "budget_spent_fingerprint" not in GroomingRecordWrite.model_fields


_PROPOSAL = {
    "task_gid": "1219804023646894",
    "fingerprint": "a" * 64,
    "title": "common-python-utils: a grooming proposal can carry an executor tag",
    "model": "claude-sonnet-5-5",
    "cost_usd": 0.12,
}


@pytest.mark.parametrize("executor", ["cowork", "person"])
def test_a_grooming_proposal_can_name_its_executor(executor: str) -> None:
    from mini_app_polis.api.contract import GroomingProposal

    assert GroomingProposal(**_PROPOSAL, executor=executor).executor == executor


def test_a_grooming_proposal_without_an_executor_is_for_the_agent() -> None:
    from mini_app_polis.api.contract import GroomingProposal

    assert GroomingProposal(**_PROPOSAL).executor is None


def test_a_grooming_proposal_refuses_an_unknown_executor() -> None:
    from mini_app_polis.api.contract import GroomingProposal

    with pytest.raises(ValidationError):
        GroomingProposal(**_PROPOSAL, executor="agent")


def _child(**overrides: Any) -> dict[str, Any]:
    return {
        "title": "common-python-utils: one piece",
        "done_when": "It is released.",
        "where": "common-python-utils",
        "constraints": "None",
        "effort": "S",
        "body": "Part of the split.",
        **overrides,
    }


def test_a_grooming_proposal_can_split_its_task_into_children() -> None:
    from mini_app_polis.api.contract import GroomingProposal

    proposal = GroomingProposal(
        **_PROPOSAL,
        children=[
            _child(),
            _child(where="task-manager-cog", executor="cowork"),
            _child(blocked_by_siblings=[0, 1], blocked_by="MINI-21, 2026-11-02"),
        ],
    )
    assert [c.executor for c in proposal.children] == [None, "cowork", None]
    assert proposal.children[2].blocked_by_siblings == [0, 1]


def test_a_grooming_proposal_without_children_still_validates() -> None:
    from mini_app_polis.api.contract import GroomingProposal

    assert GroomingProposal(**_PROPOSAL).children == []


def test_a_grooming_proposal_over_the_children_cap_is_refused() -> None:
    from mini_app_polis.api.contract import GROOMING_MAX_CHILDREN, GroomingProposal

    GroomingProposal(**_PROPOSAL, children=[_child()] * GROOMING_MAX_CHILDREN)
    with pytest.raises(ValidationError):
        GroomingProposal(**_PROPOSAL, children=[_child()] * (GROOMING_MAX_CHILDREN + 1))


@pytest.mark.parametrize("sibling", [5, 1, -1])
def test_a_child_waiting_on_a_sibling_that_is_not_earlier_is_refused(
    sibling: int,
) -> None:
    from mini_app_polis.api.contract import GroomingProposal

    with pytest.raises(ValidationError):
        GroomingProposal(
            **_PROPOSAL,
            children=[_child(), _child(blocked_by_siblings=[sibling])],
        )


def test_a_child_is_a_complete_leaf() -> None:
    from mini_app_polis.api.contract import GroomingProposal

    for missing in ("done_when", "where", "constraints", "effort", "body"):
        child = _child()
        del child[missing]
        with pytest.raises(ValidationError):
            GroomingProposal(**_PROPOSAL, children=[child])


def test_the_executor_names_are_defined_once() -> None:
    from mini_app_polis.api.contract import GroomingExecutor

    assert typing.get_args(GroomingExecutor) == ("cowork", "person")


# ── Agent runs ────────────────────────────────────────────────────────────

_RUN_ID = "0b6f7f5e-3c1a-4c8e-9a51-6f0c2f3d4e5a"


def test_a_run_that_succeeded_names_its_pull_request() -> None:
    from mini_app_polis.api.contract import AgentRunResult

    result = AgentRunResult(
        run_id=_RUN_ID,
        outcome="succeeded",
        cost_usd=0.84,
        pr_url="https://github.com/mini-app-polis/watcher-cog/pull/7",
    )
    assert result.pr_url is not None
    with pytest.raises(ValidationError):
        AgentRunResult(run_id=_RUN_ID, outcome="succeeded", cost_usd=0.84)


@pytest.mark.parametrize("outcome", ["failed", "capped"])
def test_a_run_that_did_not_succeed_opened_no_pull_request(outcome: str) -> None:
    from mini_app_polis.api.contract import AgentRunResult

    assert AgentRunResult(run_id=_RUN_ID, outcome=outcome, reason="r").pr_url is None
    with pytest.raises(ValidationError):
        AgentRunResult(
            run_id=_RUN_ID,
            outcome=outcome,
            pr_url="https://github.com/mini-app-polis/watcher-cog/pull/7",
        )


def test_a_run_that_needs_grooming_asks_its_questions() -> None:
    from mini_app_polis.api.contract import AgentRun, AgentRunResult

    result = AgentRunResult(
        run_id=_RUN_ID,
        outcome="needs_grooming",
        cost_usd=0.31,
        questions=["Which env var name should replace FOLDER_ID?"],
    )
    assert result.questions == ["Which env var name should replace FOLDER_ID?"]
    stored = AgentRun.model_validate(
        {
            "id": _RUN_ID,
            "task_gid": "1219804099689049",
            "repository": "watcher-cog",
            "model": "claude-sonnet-5-5",
            "status": "needs_grooming",
            "questions": result.questions,
            "dispatched_at": "2026-10-23T16:00:00Z",
        }
    )
    assert stored.questions == result.questions


@pytest.mark.parametrize("questions", [[], ["  "]])
def test_a_run_that_needs_grooming_without_questions_is_refused(
    questions: list[str],
) -> None:
    from mini_app_polis.api.contract import AgentRunResult

    with pytest.raises(ValidationError):
        AgentRunResult(run_id=_RUN_ID, outcome="needs_grooming", questions=questions)


@pytest.mark.parametrize("outcome", ["failed", "capped"])
def test_questions_on_another_outcome_are_refused(outcome: str) -> None:
    from mini_app_polis.api.contract import AgentRunResult

    with pytest.raises(ValidationError):
        AgentRunResult(run_id=_RUN_ID, outcome=outcome, questions=["Which one?"])
    with pytest.raises(ValidationError):
        AgentRunResult(
            run_id=_RUN_ID,
            outcome="succeeded",
            pr_url="https://github.com/mini-app-polis/watcher-cog/pull/7",
            questions=["Which one?"],
        )


def test_a_run_that_needs_grooming_opened_no_pull_request() -> None:
    from mini_app_polis.api.contract import AgentRunResult

    with pytest.raises(ValidationError):
        AgentRunResult(
            run_id=_RUN_ID,
            outcome="needs_grooming",
            questions=["Which one?"],
            pr_url="https://github.com/mini-app-polis/watcher-cog/pull/7",
        )


def test_an_agent_result_refuses_extras_and_bad_ids() -> None:
    from mini_app_polis.api.contract import AgentRunResult

    with pytest.raises(ValidationError):
        AgentRunResult.model_validate({"run_id": _RUN_ID, "outcome": "failed", "x": 1})
    with pytest.raises(ValidationError):
        AgentRunResult(run_id="MINI-12", outcome="failed")
    with pytest.raises(ValidationError):
        AgentRunResult(run_id=_RUN_ID, outcome="failed", cost_usd=-1)


def test_listing_agent_runs_sends_only_the_filters_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, calls = _client(monkeypatch, {"data": [], "meta": _META})
    assert client.list_agent_runs() == []
    client.list_agent_runs(status="dispatched", repository="watcher-cog")
    assert calls == [
        ("GET", "/v1/tasks/agent-runs", None),
        (
            "GET",
            "/v1/tasks/agent-runs",
            {"status": "dispatched", "repository": "watcher-cog"},
        ),
    ]
